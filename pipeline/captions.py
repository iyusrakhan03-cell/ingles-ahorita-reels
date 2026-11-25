"""Captions in the client's reference style (screenshot 2026-11-12):
big yellow Nunito Black, thick black outline + drop shadow, two centred lines per phrase,
lower-middle of the frame, spoken language only (no translation line).
Words pop in as they are spoken (animate=True). Text comes from the script (correct
spelling); timing from the transcript.
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from pathlib import Path

STYLE = {
    # 2026-11-17 reference match: text ~15% smaller than before (150) -> 5-6 words per phrase
    "top": {"font": "Nunito Black", "size": 128, "bold": 0, "y": 1190},  # y = centre of the 2-line block
    "max_width": 960,          # px per line; a longer line is narrowed to fit
    "max_chars": 34,           # per phrase (quote marks not counted), shown as up to two balanced lines
    "min_break": 20,           # break a phrase at , . : only once it has this many chars
    "colour": "&H0033E5FF&",   # yellow #FFE533 (ASS colours are BGR)
    "outline": 9,
    "shadow": 4,
    "bilingual": False,        # True = add a small translation line underneath
    # English example line under the Spanish one: big white, almost the size of the Spanish,
    # top edge `gap` px below the Spanish block, appears only when the English is spoken
    "bottom": {"font": "Nunito Black", "size": 118, "bold": 0, "gap": 40, "outline": 8, "shadow": 4},
    # animate = each word pops in when spoken and the phrase bounces in; False = static phrases
    "animate": False,  # client 2026-11-13: no pop/bounce — whole phrase appears, like the reference
}
TRANSLATE_MODEL = "gpt-5.5"


def clean(text: str) -> str:
    # the reference keeps the outer "?" of the hook: “¿Cuánto te queda?”?
    t = text.replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", " ", t).strip()


def _norm(w: str) -> str:
    w = unicodedata.normalize("NFKD", w.lower())
    w = "".join(c for c in w if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", w)


def chunk_line(text: str, max_chars: int = STYLE["max_chars"]) -> list[str]:
    """Split at punctuation first, then balance long pieces by length."""
    words = clean(text).split(" ")
    pieces, cur = [], []
    for w in words:
        # a new "¿" / "¡" question inside a line starts a new phrase
        if cur and w.lstrip("“\"")[:1] in "¿¡" and len(" ".join(cur)) > 8:
            pieces.append(cur); cur = []
        cur.append(w)
        if re.search(r"[,.:;!?][”\"]?$", w) and len(" ".join(cur)) >= STYLE["min_break"]:
            pieces.append(cur); cur = []
    if cur:
        pieces.append(cur)
    out = []
    for p in pieces:
        s = unquote(" ".join(p))  # quotes are mostly not drawn: don't let them force a split
        if len(s) <= max_chars:
            out.append(p); continue
        n = -(-len(s) // max_chars)  # ceil
        target = len(s) / n
        acc, buf = 0, []
        for w in p:
            if buf and acc + len(w) > target + 2 and acc >= target * 0.6:
                out.append(buf); buf, acc = [], 0
            buf.append(w); acc += len(w) + 1
        if buf:
            out.append(buf)
    # merge a dangling 1-word chunk into its neighbour
    merged = []
    for c in out:
        if merged and len(c) == 1 and len(" ".join(merged[-1] + c)) <= max_chars + 3:
            merged[-1] += c
        else:
            merged.append(c)
    return [" ".join(c) for c in merged]


def time_chunks(chunks: list[str], segs: list[tuple[float, float, list[dict]]]) -> list[tuple[float, float]]:
    """segs = [(out_start, out_end, words_with_out_times)] for one script line.
    Maps script words to transcript words (difflib) to find each chunk's start."""
    t_words = [w for _, _, ws in segs for w in ws]
    line_s, line_e = segs[0][0], segs[-1][1]
    s_words = [w for c in chunks for w in c.split(" ")]
    sm = difflib.SequenceMatcher(a=[_norm(w) for w in s_words], b=[_norm(w["w"]) for w in t_words], autojunk=False)
    start_of = {}
    for a, b, n in sm.get_matching_blocks():
        for k in range(n):
            start_of[a + k] = t_words[b + k]["os"]
    starts, idx = [], 0
    total = max(1, sum(len(c) for c in chunks))
    acc = 0
    for c in chunks:
        guess = line_s + (line_e - line_s) * acc / total
        starts.append(start_of.get(idx, guess))
        idx += len(c.split(" ")); acc += len(c)
    starts[0] = line_s
    for i in range(1, len(starts)):  # keep monotonic
        starts[i] = min(max(starts[i], starts[i - 1] + 0.35), line_e - 0.2)
    return [(starts[i], starts[i + 1] if i + 1 < len(starts) else line_e) for i in range(len(starts))]


def word_times(chunks: list[str], segs: list[tuple[float, float, list[dict]]],
               chunk_times: list[tuple[float, float]]) -> list[list[tuple[str, float, float]]]:
    """Per caption word: (word, start, end) on the output timeline.
    Matched words take the transcript's timing; the rest are spread between neighbours."""
    t_words = [w for _, _, ws in segs for w in ws]
    s_words = [w for c in chunks for w in c.split(" ")]
    sm = difflib.SequenceMatcher(a=[_norm(w) for w in s_words], b=[_norm(w["w"]) for w in t_words], autojunk=False)
    known: dict[int, tuple[float, float]] = {}
    for a, b, n in sm.get_matching_blocks():
        for k in range(n):
            tw = t_words[b + k]
            known[a + k] = (tw["os"], tw.get("oe", tw["os"] + 0.3))
    out, idx = [], 0
    for c, (cs, ce) in zip(chunks, chunk_times):
        ws = c.split(" ")
        ids = list(range(idx, idx + len(ws)))
        idx += len(ws)
        times: list[list[float] | None] = [list(known[i]) if i in known else None for i in ids]
        # fill gaps proportionally to word length between the surrounding known times
        k = 0
        while k < len(ws):
            if times[k] is not None:
                k += 1; continue
            j = k
            while j < len(ws) and times[j] is None:
                j += 1
            left = times[k - 1][1] if k > 0 else cs
            right = times[j][0] if j < len(ws) else ce
            lens = [len(w) + 1 for w in ws[k:j]]
            tot, acc = sum(lens), 0
            for m, L in zip(range(k, j), lens):
                a = left + (right - left) * acc / tot
                acc += L
                times[m] = [a, left + (right - left) * acc / tot]
            k = j
        prev = cs
        res = []
        for w, (a, b) in zip(ws, times):
            a = min(max(a, prev, cs), ce - 0.05)
            b = min(max(b, a + 0.12), ce)
            res.append((w, a, b))
            prev = a
        if res:
            res[0] = (res[0][0], cs, max(res[0][2], cs + 0.12))  # never start a phrase blank
        out.append(res)
    return out


def translate(lines: list[dict], cache: Path | None = None) -> dict:
    """lines = [{"id", "text", "chunks", "pair"}] -> {id: [translation per chunk]}"""
    if cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    from openai import OpenAI

    sys_p = ("You write the small translation line under bilingual captions for a Spanish-speaking audience "
             "learning English. For each caption chunk give its translation: Spanish -> English, English -> "
             "Spanish; for mixed lines translate into the other language of the main sentence (usually English). "
             "Keep it short, natural, same punctuation style, no quotes. If a 'pair' (the script's own "
             "translation of the line) is given, use its wording, split to match the chunks. "
             'Return JSON {"lines":[{"id":..,"tr":["..", ".."]}]} with exactly one entry per chunk.')
    r = OpenAI().chat.completions.create(
        model=TRANSLATE_MODEL, response_format={"type": "json_object"}, reasoning_effort="low",
        messages=[{"role": "system", "content": sys_p},
                  {"role": "user", "content": json.dumps({"lines": lines}, ensure_ascii=False)}])
    d = {str(x["id"]): x["tr"] for x in json.loads(r.choices[0].message.content)["lines"]}
    if cache:
        cache.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


_FONT_CACHE: dict[int, object] = {}


def _width(text: str, size: int) -> float:
    try:
        from PIL import ImageFont
        if size not in _FONT_CACHE:
            f = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Nunito-900.ttf"
            # libass sizes a font by its full line height (ascent+descent); Nunito's is 1.364 em,
            # so an ASS size of N draws glyphs at N/1.364 px. Measure at that real size.
            _FONT_CACHE[size] = ImageFont.truetype(str(f), max(1, round(size / 1.364)))
        return _FONT_CACHE[size].getlength(text)
    except Exception:
        return len(text) * size * 0.62


def _fit(lines: list[str], size: int) -> int:
    """Horizontal scale (%) so the widest line fits STYLE['max_width'], measured with the real font."""
    w = max(_width(line, size) for line in lines)
    return min(100, int(STYLE["max_width"] / w * 100)) if w else 100


def split_two(words: list[str], size: int | None = None) -> int:
    """Index of the first word of line 2 (0 = one line). Balanced by rendered width.
    The one-line limit scales with the font, so a smaller font does not turn 2-line phrases
    into wide single lines (the reference almost always shows two lines)."""
    size = size or STYLE["top"]["size"]
    if len(words) < 2 or _width(" ".join(words), size) <= STYLE["max_width"] * 0.62 * size / 150:
        return 0
    return min(range(1, len(words)),
               key=lambda k: abs(_width(" ".join(words[:k]), size) - _width(" ".join(words[k:]), size)))


def _esc(s: str) -> str:
    # nested script quotes (“¿Cuánto te queda?”?) leave doubled marks once quotes are stripped
    s = re.sub(r"([?!.,])\1+", r"\1", s)
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


ROLE_COLOUR = {
    "narration": "&H0033E5FF&",   # yellow #FFE533
    "example_es": "&H00D8A594&",  # blue #94A5D8
    "example_en": "&H00FFFFFF&",  # white
}


def unquote(s: str) -> str:
    return s.replace("“", "").replace("”", "").replace('"', "")


def _esc_lines(lines: list[str], role: str, col: str) -> str:
    """Escape for ASS, styled like the reference:
    - narration: quoted ENGLISH keywords -> white italic, no quotes (Empecemos con *left*);
      quoted Spanish keeps its quotes (Es “queda”, the hook)
    - example lines: no quotes at all (¿Cuánto te queda? / How much do you have left?)"""
    text = "\n".join(lines)
    if role == "narration":
        from .align_words import line_language
        parts = re.split(r"(“[^”]*”)", text)
        text = "".join("{\\i1\\1c&H00FFFFFF&}" + _esc(unquote(p)) + "{\\i0\\1c" + col + "}"
                       if p.startswith("“") and line_language(p) == "en" else _esc(p) for p in parts)
    else:
        text = _esc(unquote(text))
    return text.replace("\n", "\\N")


def write_ass(events: list[dict], path: Path) -> None:
    """events = [{"s","e","top","bottom"}]"""
    T, B = STYLE["top"], STYLE["bottom"]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Top,{T['font']},{T['size']},{STYLE['colour']},{STYLE['colour']},&H00000000,&H64000000,{T['bold']},0,0,0,100,100,0,0,1,{STYLE['outline']},{STYLE['shadow']},5,40,40,0,1
Style: Bottom,{B['font']},{B['size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,{B['bold']},0,0,0,100,100,0,0,1,{B['outline']},{B['shadow']},8,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    for ev in events:
        role = ev.get("role", "narration")
        col = ROLE_COLOUR.get(role, STYLE["colour"])
        words = [w for w, _, _ in ev.get("words") or []] or ev["top"].split(" ")
        if role != "narration":  # examples are shown without quotes: measure them that way
            words = [unquote(w) for w in words if unquote(w)]
        cut = split_two(words)
        lines = [" ".join(words[:cut]), " ".join(words[cut:])] if cut else [" ".join(words)]
        fx = _fit(lines, T["size"])
        if STYLE["animate"] and ev.get("words"):
            # phrase bounces in; each word fades in when spoken. Hidden words keep their
            # space, so the centred lines never shift while words appear.
            def ms(t: float) -> int:
                return max(0, int(round((t - ev["s"]) * 1000)))
            parts = [f"{{\\pos(540,{T['y']})\\blur0.6\\fscx{fx * 86 // 100}\\fscy86"
                     f"\\t(0,90,\\fscx{fx * 107 // 100}\\fscy107)\\t(90,190,\\fscx{fx}\\fscy100)}}"]
            n = len(ev["words"])
            for k, (w, a, b) in enumerate(ev["words"]):
                A = ms(a)
                sep = "" if k == n - 1 else ("\\N" if k + 1 == cut else " ")
                parts.append(f"{{\\1a&HFF&\\3a&HFF&\\4a&HFF&\\1c{col}"
                             f"\\t({A},{A + 80},\\1a&H00&\\3a&H00&\\4a&H64&)}}{_esc(w)}{sep}")
            rows.append(f"Dialogue: 0,{_ts(ev['s'])},{_ts(ev['e'])},Top,,0,0,0,,{''.join(parts)}")
        else:
            rows.append(f"Dialogue: 0,{_ts(ev['s'])},{_ts(ev['e'])},Top,,0,0,0,,"
                        f"{{\\pos(540,{T['y']})\\blur0.6\\fscx{fx}\\1c{col}}}"
                        + _esc_lines(lines, role, col))
        if ev.get("bottom"):
            bw = unquote(ev["bottom"]).split(" ")
            bc = split_two(bw, B["size"])
            blines = [" ".join(bw[:bc]), " ".join(bw[bc:])] if bc else [" ".join(bw)]
            top_y = T["y"] + len(lines) * T["size"] / 2 + B["gap"]  # just under the Spanish block
            bs = max(ev["s"], ev.get("bottom_s", ev["s"]))
            rows.append(f"Dialogue: 0,{_ts(bs)},{_ts(ev.get('bottom_e', ev['e']))},Bottom,,0,0,0,,"
                        f"{{\\pos(540,{round(top_y)})\\blur0.6\\fscx{_fit(blines, B['size'])}}}"
                        + _esc("\n".join(blines)).replace("\n", "\\N"))
    path.write_text(head + "\n".join(rows) + "\n", encoding="utf-8-sig")
