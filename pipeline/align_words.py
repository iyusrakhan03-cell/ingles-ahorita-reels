"""Exact caption timing: re-transcribe every KEPT clip with the sentence we expect.

The first transcription pass sometimes hears the wrong words (e.g. "minutes and don't
forget" for "Entonces recuerda"), and then caption words can't be matched to real
timings, so a caption falls back to a guess and appears early. Here each kept clip is
sent to Whisper again with:
  * language forced to the line's language (Spanish or English)
  * the expected sentence as the prompt, so Whisper writes exactly those words
which gives word timings that line up with the caption text.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EN = {"the", "is", "are", "you", "do", "how", "much", "have", "i", "it", "what", "there", "more", "many", "any",
      "need", "time", "of", "to", "and", "a", "left", "longer", "remaining", "minutes", "my", "your", "this",
      "that", "don't", "never", "mind", "listen", "ignore", "him", "her", "they", "we", "can", "will", "would"}
ES = {"el", "la", "los", "las", "de", "que", "es", "en", "y", "te", "se", "un", "una", "para", "con", "no", "por",
      "cuánto", "cuántos", "me", "lo", "le", "queda", "quedan", "esta", "este", "muy", "más", "pero", "si", "como",
      "cómo", "sabes", "dice", "inglés", "ahora", "entonces", "recuerda", "vas", "todos", "días", "algo"}


def line_language(text: str) -> str:
    words = re.findall(r"[a-záéíóúñü']+", text.lower())
    en = sum(w in EN for w in words)
    es = sum(w in ES for w in words) + 2 * len(re.findall(r"[¿¡ñáéíóú]", text.lower()))
    return "en" if en > es else "es"


def refine(src: Path, clips: list[dict], cache: Path | None = None) -> None:
    """Replace each clip's `words` with timings from a prompted, language-forced pass."""
    if cache and cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        for c, ws in zip(clips, saved):
            if ws:
                c["words"] = ws
        return
    from openai import OpenAI

    client = OpenAI()
    tmp = Path(tempfile.mkdtemp(prefix="align_"))

    def one(i_c):
        i, c = i_c
        clip = tmp / f"a{i:03d}.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{c['s']:.3f}", "-to", f"{c['e']:.3f}", "-i", str(src),
                        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(clip)], check=True)
        text = re.sub(r"[“”\"]", "", c.get("caption_text") or c["text"])
        try:
            with open(clip, "rb") as f:
                r = client.audio.transcriptions.create(
                    model="whisper-1", file=f, response_format="verbose_json", timestamp_granularities=["word"],
                    language=line_language(text), prompt=text, temperature=0)
            d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
        except Exception:
            return None
        ws = [{"w": w["word"], "s": round(c["s"] + w["start"], 3), "e": round(c["s"] + w["end"], 3)}
              for w in d.get("words") or []]
        return ws or None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(one, enumerate(clips)))
    for c, ws in zip(clips, results):
        if ws:
            c["words"] = ws
    if cache:
        cache.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
