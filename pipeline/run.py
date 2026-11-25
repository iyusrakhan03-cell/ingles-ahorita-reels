"""Whole pipeline for one job: raw video in Drive -> draft.mp4 (+ cover, cut list, report).

Everything a person may want to inspect is written to the job folder:
  transcript.json   what was heard, per utterance, with word times
  decision.json     the editor's choice per script line (kept / dropped + why)
  cutlist.json      exact source times of every clip in the draft
  captions.ass      caption file (open in Aegisub to tweak)
  draft.mp4         the edit
  cover.jpg         Instagram cover with the yellow hook title
  report.txt        plain-language summary of what was cut and why
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import requests

from . import captions as cap
from . import align_words, edit, render, script_parse, transcribe


def log(job_dir: Path, msg: str) -> None:
    with open(job_dir / "progress.log", "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def download_drive(file_id: str, cache_dir: Path) -> Path:
    dst = cache_dir / f"{file_id}.mp4"
    if dst.exists() and dst.stat().st_size > 1_000_000:
        return dst
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    part = dst.with_suffix(".part")
    for _ in range(6):  # Drive drops long downloads; resume where it stopped
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=120) as r:
                if r.status_code == 416:
                    break
                r.raise_for_status()
                if "text/html" in r.headers.get("content-type", ""):
                    raise RuntimeError("Drive file is not shared as 'Anyone with the link' (got a sign-in page)")
                with open(part, "ab" if have and r.status_code == 206 else "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            break
        except requests.RequestException:
            continue
    part.rename(dst)
    return dst


def _clock(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_transcripts(job_dir: Path, utts: list[dict], decision: dict, clips: list[dict],
                      events: list[dict], said: dict, lines: list[str]) -> None:
    """transcript_raw.txt  = everything said in the raw video, each line marked KEPT or CUT
       transcript_reel.txt = only what is in the reel, timed to the draft
       reel.srt            = subtitles (spoken line + translation) for uploading to IG/YouTube"""
    used = {p["u"] for L in decision["lines"] if L.get("keep") for p in L.get("parts") or []}
    raw = ["RAW VIDEO TRANSCRIPT  (time = position in the raw recording)", ""]
    for u in utts:
        raw.append(f"[{_clock(u['s'])}]  {'KEPT' if u['id'] in used else 'CUT '}  {u['text']}")
    (job_dir / "transcript_raw.txt").write_text("\n".join(raw) + "\n", encoding="utf-8")

    reel, seen = ["REEL TRANSCRIPT  (time = position in the draft)", ""], set()
    for c in clips:
        if c["line"] in seen:
            continue
        seen.add(c["line"])
        reel.append(f"[{_clock(c['os'])}]  {cap.clean(said.get(c['line'], lines[c['line']]))}")
    (job_dir / "transcript_reel.txt").write_text("\n".join(reel) + "\n", encoding="utf-8")

    srt = []
    for n, ev in enumerate(events, 1):
        text = ev["top"] + (f"\n{ev['bottom']}" if ev.get("bottom") else "")
        srt.append(f"{n}\n{_srt_time(ev['s'])} --> {_srt_time(ev['e'])}\n{text}\n")
    (job_dir / "reel.srt").write_text("\n".join(srt), encoding="utf-8")


def process(job: dict, jobs_dir: Path) -> dict:
    job_dir = jobs_dir / job["job_id"]
    cache = jobs_dir / "cache"
    cache.mkdir(exist_ok=True)
    job_dir.mkdir(exist_ok=True)

    log(job_dir, "downloading raw video")
    src = download_drive(job["drive_file_id"], cache)

    log(job_dir, "transcribing")
    tr = transcribe.transcribe(src, cache / f"{job['drive_file_id']}.utts.json")
    utts = tr["utterances"]

    log(job_dir, "finding the script")
    doc = script_parse.fetch_doc(job["script_doc_id"], cache / f"{job['script_doc_id']}.txt")
    sections = [script_parse.clean_section(s) for s in script_parse.split_sections(doc)]
    all_texts = [v["text"] for u in utts for v in u.get("versions", {}).values()]
    if job.get("script_section") == "none":  # force "no script" mode (testing)
        best = None
    elif job.get("script_section") and job["script_section"] != "auto":
        cands = [i for i, s in enumerate(sections) if s["label"] == job["script_section"]]
        best = cands[0] if cands else script_parse.detect(sections, all_texts)[0]
    else:
        best, scores = script_parse.detect(sections, all_texts)
        if scores[best] < 0.4:
            best = None  # no script recorded this -> build one from the recording itself
    if best is None:
        log(job_dir, "no matching script in the doc: building one from the recording")
        built = edit.script_from_transcript(utts, job_dir / "built_script.json")
        sec = {"label": "none (built from the recording)", "lines": built, "hook_words": [], "notes": []}
    else:
        sec = sections[best]
    lines = sec["lines"]
    job["script_label"] = sec["label"]
    job["script_title"] = cap.clean(lines[0])[:60]
    script_parse.pick_versions(utts, lines)
    (job_dir / "transcript.json").write_text(json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")

    log(job_dir, "deciding the edit")
    decision = edit.decide(lines, utts, job_dir / "decision.json")
    clips = edit.cut_list(decision, utts, lines)
    if not clips:
        raise RuntimeError("The editor kept nothing — check decision.json")
    log(job_dir, "exact word timing for captions")
    said_now = {L["line"]: L.get("said") or lines[L["line"]] for L in decision["lines"] if L.get("keep")}
    for c in clips:
        c["caption_text"] = said_now.get(c["line"], c["text"])
    align_words.refine(src, clips, job_dir / "aligned_words.json")
    segments = render.merge_segments(clips)
    render.snap_edges(src, segments)
    render.seg_out_times(segments)

    log(job_dir, "captions + translation")
    by_line: dict[int, list[dict]] = {}
    for c in clips:
        by_line.setdefault(c["line"], []).append(c)
    kept = sorted(by_line)
    # script example pairs: fully quoted lines come in runs of Spanish, English, Spanish, English…
    quoted = lambda s: s.strip().startswith("“") and s.strip().rstrip(".").endswith("”")
    said = {L["line"]: L.get("said") or lines[L["line"]] for L in decision["lines"] if L.get("keep")}
    cap_lines = [said.get(i, l) for i, l in enumerate(lines)]  # caption = what was actually said
    pair_of, example_role, i = {}, {}, 0
    while i < len(lines):
        if quoted(lines[i]):
            j = i
            while j < len(lines) and quoted(lines[j]):
                j += 1
            for a in range(i, j - 1, 2):
                pair_of[a], pair_of[a + 1] = cap.clean(cap_lines[a + 1]), cap.clean(cap_lines[a])
                example_role[a], example_role[a + 1] = "example_es", "example_en"
            i = j
        else:
            i += 1
    chunks = {i: cap.chunk_line(cap_lines[i]) for i in kept}
    for i in kept:  # a Spanish example stays whole so it pairs with its English line as one unit
        if example_role.get(i) == "example_es" and len(cap.clean(cap_lines[i])) <= 48:
            chunks[i] = [cap.clean(cap_lines[i])]
    tr_in = [{"id": i, "text": cap.clean(cap_lines[i]), "chunks": chunks[i], "pair": pair_of.get(i)} for i in kept]
    trans = cap.translate(tr_in, job_dir / "translations.json") if cap.STYLE.get("bilingual") else {}
    events, es_shown = [], {}
    for i in kept:
        segs = [(c["os"], c["oe"], c["words"]) for c in by_line[i]]
        times = cap.time_chunks(chunks[i], segs)
        wt = cap.word_times(chunks[i], segs, times)
        tr_i = trans.get(str(i), [])
        role = example_role.get(i, "narration")
        # the Spanish example already shows this English line underneath: keep that pair on
        # screen while he says the English, instead of repeating it as a big white caption
        if role == "example_en" and i - 1 in es_shown:
            first, last = es_shown[i - 1]
            last["e"] = first["bottom_e"] = times[-1][1]
            first["bottom_s"] = times[0][0]  # English appears when he starts saying it
            continue
        line_evs = []
        for k, (txt, (s, e)) in enumerate(zip(chunks[i], times)):
            ev = {"s": s, "e": e, "top": txt, "words": wt[k], "role": role,
                  "bottom": tr_i[k] if k < len(tr_i) else ""}
            if role == "example_es" and k == 0 and pair_of.get(i):
                ev["bottom"], ev["bottom_e"] = pair_of[i], times[-1][1]
            line_evs.append(ev)
        if role == "example_es" and line_evs and line_evs[0].get("bottom_e"):
            es_shown[i] = (line_evs[0], line_evs[-1])
        events += line_evs
    # hold each caption through the natural pause before the next one (no blank flicker between phrases)
    events.sort(key=lambda ev: ev["s"])
    for a, b in zip(events, events[1:]):
        if 0 < b["s"] - a["e"] < 1.5:
            if a.get("bottom_e") == a["e"]:
                a["bottom_e"] = b["s"]
            a["e"] = b["s"]
        elif a["e"] > b["s"]:  # overlapping takes: never show two phrases at once
            a["e"] = b["s"]
            if a.get("bottom_e", 0) > b["s"]:
                a["bottom_e"] = b["s"]
    ass = job_dir / "captions.ass"
    cap.write_ass(events, ass)
    # same captions as data, for the HyperFrames caption renderer (pipeline/hf_captions.py)
    (job_dir / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=1), encoding="utf-8")

    log(job_dir, "rendering")
    draft, face_xy = render.render(src, clips, segments, ass, job_dir, reuse_clips=bool(job.get("reuse_clips")))
    hook = lines[0]
    title = cap.clean(hook[hook.index("“"):] if "“" in hook else hook)
    render.cover(src, clips[0], title, job_dir, face_xy=face_xy)
    for c in clips:
        c["zoom"] = round(render.zoom_at((c["os"] + c["oe"]) / 2), 3)

    (job_dir / "cutlist.json").write_text(json.dumps(
        [{k: c.get(k) for k in ("line", "part", "text", "s", "e", "os", "oe", "zoom", "crop")} for c in clips],
        ensure_ascii=False, indent=1), encoding="utf-8")
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(draft)], capture_output=True, text=True).stdout.strip())
    dropped = [L for L in decision["lines"] if not L.get("keep")]
    rep = [f"Script detected: #{sec['label']} — {job['script_title']}",
           f"Draft length: {dur:.1f}s from a {float(utts[-1]['e']):.0f}s recording; {len(clips)} clips",
           "", "Lines left out:"]
    rep += [f"  - {cap.clean(lines[L['line']])}  ({L.get('reason', '')})" for L in dropped] or ["  (none)"]
    changed = [(lines[i], said[i]) for i in sorted(said) if cap.clean(said[i]) != cap.clean(lines[i])]
    if changed:
        rep += ["", "Captions follow what was said where it differs from the script:"]
        rep += [f"  - script: {cap.clean(a)}  ->  said: {cap.clean(b)}" for a, b in changed]
    rep += ["", "Off-camera instructions followed:"] + [f"  - {x}" for x in decision.get("instructions_found", [])]
    if decision.get("warnings"):
        rep += ["", "Editor warnings (check these spots):"] + [f"  - {x}" for x in decision["warnings"]]
    if sec["hook_words"]:
        rep += ["", "Script asks for words behind the presenter in the hook (not done automatically yet): "
                + ", ".join(sec["hook_words"])]
    (job_dir / "report.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")
    write_transcripts(job_dir, utts, decision, clips, events, said, lines)
    job["duration"] = round(dur, 1)
    job["report"] = "\n".join(rep)
    log(job_dir, "done")
    return job
