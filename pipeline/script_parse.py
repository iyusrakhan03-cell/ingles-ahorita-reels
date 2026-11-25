"""Read the teleprompter Google Doc, split it into scripts, find the one that was recorded.

The doc numbers scripts inconsistently (two "9"s, two "10"s, some titled by name),
so we never trust the numbers: we score every section against the transcript.
"""
from __future__ import annotations

import re
from pathlib import Path

import requests
from rapidfuzz import fuzz

SEP = re.compile(r"^_{5,}\s*$")
HEADER = re.compile(r"^\s*(\d{1,2})(?:\s+|$)(.*)$")
URL = re.compile(r"https?://\S+")
HOOK_WORDS = re.compile(r"put these words in the background in the hook\s*:\s*(.+)", re.I)
CTA = re.compile(r"^\s*(FB\+IG|TT\+YT)\s*:\s*(.+)$", re.I)


def fetch_doc(doc_id: str, cache: Path | None = None) -> str:
    if cache and cache.exists():
        return cache.read_text(encoding="utf-8")
    r = requests.get(f"https://docs.google.com/document/d/{doc_id}/export?format=txt", timeout=60)
    r.raise_for_status()
    txt = r.content.decode("utf-8-sig")
    if "<html" in txt[:500].lower():
        raise RuntimeError("Script doc is not shared publicly (Anyone with the link → Viewer)")
    if cache:
        cache.write_text(txt, encoding="utf-8")
    return txt


def split_sections(doc: str) -> list[dict]:
    chunks, cur = [], []
    for raw in doc.replace("﻿", "").splitlines():
        line = raw.strip()
        if SEP.match(line):
            chunks.append(cur); cur = []
            continue
        if HEADER.match(line) and cur and any(l for l in cur if l):
            chunks.append(cur); cur = []
        cur.append(line)
    chunks.append(cur)
    sections = []
    for ch in chunks:
        lines = [l for l in ch if l]
        if len(lines) < 3:
            continue
        label = None
        m = HEADER.match(lines[0])
        if m:
            label = m.group(1)
            rest = m.group(2).strip()
            lines = ([rest] if rest else []) + lines[1:]
        elif len(lines[0]) < 25 and not lines[0].endswith((".", "?", "!", "”", '"')):
            label, lines = lines[0], lines[1:]
        sections.append({"label": label, "lines": lines})
    return sections


def clean_section(sec: dict, cta: str = "FB+IG") -> dict:
    """Spoken lines only. Pulls out editor notes (hook words) and picks one CTA variant."""
    spoken, hook_words, notes = [], [], []
    for l in sec["lines"]:
        if URL.fullmatch(l.strip()):
            continue
        m = HOOK_WORDS.search(l)
        if m:
            hook_words = [w.strip() for w in m.group(1).split(",") if w.strip()]
            continue
        c = CTA.match(l)
        if c:
            if c.group(1).upper() == cta.upper():
                spoken.append(c.group(2).strip())
            else:
                notes.append(f"dropped {c.group(1)} ending")
            continue
        spoken.append(URL.sub("", l).strip())
    return {"label": sec["label"], "lines": [s for s in spoken if s], "hook_words": hook_words, "notes": notes}


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", s)


def detect(sections: list[dict], utt_texts: list[str]) -> tuple[int, list[float]]:
    """Score = how many script lines appear somewhere in the transcript."""
    U = [_norm(t) for t in utt_texts if t]
    scores = []
    for sec in sections:
        lines = [_norm(l) for l in sec["lines"] if len(l) > 8]
        if not lines:
            scores.append(0.0); continue
        hits = [max((fuzz.partial_ratio(l, u) if len(u) >= 6 else 0) for u in U) for l in lines]
        scores.append(sum(h >= 80 for h in hits) / len(lines) + sum(hits) / (100 * len(lines)) * 0.1)
    best = max(range(len(scores)), key=scores.__getitem__)
    return best, scores


def pick_versions(utts: list[dict], script_lines: list[str]) -> None:
    """For each utterance keep the language version (es/en) that matches the script best."""
    S = [_norm(l) for l in script_lines]
    for u in utts:
        best_lang, best = u["lang"], -1
        for lang, v in u.get("versions", {}).items():
            t = _norm(v["text"])
            sc = max((fuzz.partial_ratio(t, s) if len(t) > 3 else 0) for s in S) if S else 0
            if sc > best:
                best_lang, best = lang, sc
        v = u["versions"][best_lang]
        u.update(lang=best_lang, text=v["text"], words=v["words"], script_match=best)
