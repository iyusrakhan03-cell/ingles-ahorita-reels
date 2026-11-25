"""Decide the edit: which take of each script line to keep, word by word.

An LLM reads the numbered script lines and the timestamped utterances and returns,
per script line, the words to keep. Code then turns that into exact cut times.

Rules the editor follows (learned from the reference reel):
  * keep only the LAST complete, clean take of each line (retakes, stumbles dropped)
  * drop setup chatter, "okay"s, and off-camera talk
  * obey off-camera instructions such as "puedes quitar esa parte" / "no hace falta
    poner esa parte": the line(s) they refer to are removed from the edit
  * a line can span two utterances; a take can be part of an utterance
"""
from __future__ import annotations

import json
from pathlib import Path

MODEL = "gpt-5.5"

SYSTEM = """You are the editor of short Spanish/English language-learning reels.
The presenter reads a teleprompter script. You get the numbered SCRIPT lines and the
TRANSCRIPT of the raw recording as utterances; each word has an index like [3]word.
Transcription is imperfect (accents, numbers like 20 vs veinte, small mishearings):
match on meaning and wording, not exact spelling.

For EVERY script line decide what to keep:
- If the line was said, pick the LAST complete clean take. Earlier attempts, stumbles,
  restarts and repeated fragments are not kept.
- A take may start/end inside an utterance (give word indices) and may continue into
  the next utterance (give several parts, in order). Never include words that belong
  to another script line, filler ("okay", "eh"), or off-camera talk.
- If the line was never said, keep=false, reason="not recorded".
- Someone off camera sometimes gives editing instructions, e.g. "puedes quitar esa
  parte", "no hace falta poner esa parte", "quita el con remaining...". Work out which
  script line(s) they refer to and set keep=false, reason="removed by instruction: <quote>".
  An instruction usually refers to the line(s) just said or quoted in it, and it can
  cover a whole example pair (a Spanish line plus its English line).
  "ese par" / "esa parte" after a lead-in such as "Y para responder:" removes the
  lead-in AND the example pair that follows it (Spanish + English), not just the
  lead-in. When an instruction removes an explanation, also remove its intro line
  (e.g. "Fíjate en esto.").
- The presenter may paraphrase a line slightly; still keep it if it is the same sentence.
- For every kept line also give "said": the caption text = the script line rewritten to
  what the presenter ACTUALLY said in the chosen take (e.g. he says "esenciales" where the
  script has "importantes" -> use "esenciales"). Keep the script's spelling, accents,
  quotes and punctuation everywhere else. Do NOT copy transcription errors: if a word only
  sounds like a mishearing ("formado" for "formal", "20" for "veinte"), keep the script word.
  Each utterance was transcribed twice (forced Spanish, forced English); the forced pass
  often TRANSLATES instead of transcribing. The presenter never translates a line: every
  word stays in the language the script uses. Never change a line's language in "said";
  only swap individual words that clearly differ in the same language.

Return JSON only:
{"lines":[{"line":<n>,"keep":true|false,"reason":"...","said":"...","parts":[{"u":<utterance id>,"from":<first word index>,"to":<last word index>}]}],
 "instructions_found":["..."], "warnings":["..."]}"""


def _fmt_utts(utts: list[dict]) -> str:
    out = []
    for u in utts:
        ws = " ".join(f"[{i}]{w['w'].strip()}" for i, w in enumerate(u["words"]))
        alt = {k: v["text"] for k, v in u.get("versions", {}).items() if k != u["lang"]}
        alt_s = f'   (other hearing: {next(iter(alt.values()))})' if alt else ""
        out.append(f'u{u["id"]} {u["s"]:.2f}-{u["e"]:.2f}s: {ws}{alt_s}')
    return "\n".join(out)


def decide(script_lines: list[str], utts: list[dict], cache: Path | None = None) -> dict:
    if cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    from openai import OpenAI

    user = "SCRIPT:\n" + "\n".join(f"{i}: {l}" for i, l in enumerate(script_lines)) + \
           "\n\nTRANSCRIPT:\n" + _fmt_utts(utts)
    r = OpenAI().chat.completions.create(
        model=MODEL, response_format={"type": "json_object"}, reasoning_effort="high",
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
    d = json.loads(r.choices[0].message.content)
    if cache:
        cache.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


SCRIPTLESS = """A presenter recorded a talking-to-camera video WITHOUT a script in our doc.
From the TRANSCRIPT utterances, reconstruct the script they meant to deliver: the final,
clean version of each sentence, in order, one sentence per line.
- Leave out setup chatter, "okay"/filler, false starts, repeated attempts (keep only the
  wording of the last complete attempt), and anything said by someone off camera.
- Keep the presenter's own words and language (do not translate, do not improve wording);
  fix only obvious transcription spelling.
Return JSON only: {"lines": ["...", "..."]}"""


def script_from_transcript(utts: list[dict], cache: Path | None = None) -> list[str]:
    """Fallback when the recording matches no script in the doc (e.g. a test video)."""
    if cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))["lines"]
    from openai import OpenAI

    text = "\n".join(f'u{u["id"]} {u["s"]:.2f}s: {u["text"]}   (other hearing: '
                     f'{next((v["text"] for k, v in u.get("versions", {}).items() if k != u["lang"]), "-")})'
                     for u in utts)
    r = OpenAI().chat.completions.create(
        model=MODEL, response_format={"type": "json_object"}, reasoning_effort="medium",
        messages=[{"role": "system", "content": SCRIPTLESS}, {"role": "user", "content": "TRANSCRIPT:\n" + text}])
    d = json.loads(r.choices[0].message.content)
    if cache:
        cache.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d["lines"]


def cut_list(decision: dict, utts: list[dict], script_lines: list[str],
             pad_in: float = 0.06, pad_out: float = 0.10) -> list[dict]:
    """Turn word picks into clips: [{line, text, s, e, words:[...]}] in script order."""
    U = {u["id"]: u for u in utts}
    clips = []
    for L in sorted(decision["lines"], key=lambda x: x["line"]):
        if not L.get("keep") or not L.get("parts"):
            continue
        for k, p in enumerate(L["parts"]):
            u = U.get(p["u"])
            if not u:
                continue
            a, b = max(0, int(p["from"])), min(len(u["words"]) - 1, int(p["to"]))
            if b < a:
                continue
            ws = u["words"][a:b + 1]
            # whole utterance -> use the silence-detected bounds (more precise than whisper)
            s = u["s"] if a == 0 else ws[0]["s"]
            e = u["e"] if b == len(u["words"]) - 1 else ws[-1]["e"]
            s, e = max(u["s"] - 0.12, s - pad_in), min(u["e"] + 0.12, e + pad_out)
            clips.append({"line": L["line"], "part": k, "text": script_lines[L["line"]],
                          "s": round(s, 3), "e": round(e, 3), "words": ws})
    # never let two clips overlap in source time order within a line
    return clips
