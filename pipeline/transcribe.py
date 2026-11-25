"""Speech-to-text with per-word timestamps, split at pauses.

Whisper drops whole sentences on long files (it skipped most English lines here),
so the audio is cut at silences into utterances and each one is transcribed on
its own. Each utterance gets its own language detection, which also keeps the
English lines in English.

Returns {"utterances": [{"id", "s", "e", "text", "lang", "words": [{"w","s","e"}]}]}
(times are absolute seconds in the source video).
Provider is swappable (ElevenLabs Scribe can go behind the same function later).
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HALLUCINATIONS = re.compile(
    r"(amara\.org|subt[ií]tulos|suscr[ií]bete|thanks? for watching|gracias por ver|www\.|\.com)", re.I)


def find_speech(audio: Path, noise_db: int = -38, min_sil: float = 0.35) -> list[tuple[float, float]]:
    """Speech intervals = the gaps between silences (ffmpeg silencedetect)."""
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)],
        capture_output=True, text=True).stdout.strip())
    log = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(audio), "-af", f"silencedetect=n={noise_db}dB:d={min_sil}", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    sil = list(zip(starts, ends + [dur] * (len(starts) - len(ends))))
    speech, cur = [], 0.0
    for a, b in sil:
        if a - cur > 0.05:
            speech.append([cur, a])
        cur = b
    if dur - cur > 0.05:
        speech.append([cur, dur])
    # merge tiny blips into the neighbour so whisper never gets a 0.2 s clip
    merged: list[list[float]] = []
    for s, e in speech:
        if merged and (e - s < 0.7 or s - merged[-1][1] < 0.2) and s - merged[-1][1] < 0.8:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    # split anything very long (whisper is happier < 25 s)
    out = []
    for s, e in merged:
        while e - s > 25:
            out.append((s, s + 20))
            s += 20
        out.append((s, e))
    return out


def _whisper(client, path: Path, language: str) -> dict:
    with open(path, "rb") as f:
        r = client.audio.transcriptions.create(
            model="whisper-1", file=f, response_format="verbose_json", language=language,
            timestamp_granularities=["word"], temperature=0)
    return r.model_dump() if hasattr(r, "model_dump") else dict(r)


def transcribe(media: Path, cache_path: Path | None = None, pad: float = 0.12) -> dict:
    if cache_path and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    from openai import OpenAI

    client = OpenAI()
    tmp = Path(tempfile.mkdtemp(prefix="utt_"))
    wav = tmp / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
    spans = find_speech(wav)

    def job(i_span):
        i, (s, e) = i_span
        s0, e0 = max(0.0, s - pad), e + pad
        clip = tmp / f"u{i:03d}.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{s0:.3f}", "-to", f"{e0:.3f}", "-i", str(wav),
                        "-b:a", "64k", str(clip)], check=True)
        versions = {}
        for lang in ("es", "en"):  # only Spanish and English are ever spoken
            for attempt in range(3):
                try:
                    d = _whisper(client, clip, lang)
                    break
                except Exception:
                    if attempt == 2:
                        raise
            text = (d.get("text") or "").strip()
            words = [{"w": w["word"], "s": round(s0 + w["start"], 3), "e": round(s0 + w["end"], 3)}
                     for w in d.get("words") or []]
            if words and not HALLUCINATIONS.search(text):
                versions[lang] = {"text": text, "words": words}
        if not versions:
            return None
        # default pick = Spanish; align.py re-picks per utterance using the script
        pick = "es" if "es" in versions else "en"
        return {"id": i, "s": round(s, 3), "e": round(e, 3), "lang": pick, "text": versions[pick]["text"],
                "words": versions[pick]["words"], "versions": versions}

    with ThreadPoolExecutor(max_workers=8) as ex:
        utts = [u for u in ex.map(job, enumerate(spans)) if u]
    for k, u in enumerate(utts):  # renumber densely
        u["id"] = k
    out = {"utterances": utts}
    if cache_path:
        cache_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    import sys

    m = Path(sys.argv[1])
    t = transcribe(m, m.with_suffix(".utts.json"))
    for u in t["utterances"]:
        v = u["versions"]
        print(f'u{u["id"]:03d} {u["s"]:7.2f}-{u["e"]:7.2f} ES: {v.get("es", {}).get("text", "-")}  ||  EN: {v.get("en", {}).get("text", "-")}')
