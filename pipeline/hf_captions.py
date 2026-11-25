"""Second caption renderer: HyperFrames (HTML + GSAP rendered to video by headless Chrome).

Same edit, same audio, same words and timings as the ffmpeg/libass version — only the
caption layer differs, so the two can be compared side by side. Same look as the client's
reference (big yellow Nunito Black, thick black outline, two centred lines, no translation),
plus what libass can't do well:
  * each word springs in on its own (scale + rise, elastic "back" ease)
  * the word being spoken grows a little while it is said, without the line shifting

Usage:  python -X utf8 -m pipeline.hf_captions <job_dir>
Needs: job_dir/events.json, job_dir/clips/concat.mkv, job_dir/draft.mp4 (for the cleaned audio).
Then:  cd <job_dir>/hyperframes && npx hyperframes render --output ../draft-hyperframes.mp4
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "assets" / "fonts" / "Nunito-900.ttf"

W, H, FPS = 1080, 1920, 30
CENTER_Y = 1190            # centre of the caption block (same as the libass version)
BASE, MIN = 110, 64        # px (em size); matches libass size 150 for Nunito
MAX_W = 960                # px per line
YELLOW = "#FFE533"


def _tidy(s: str) -> str:
    return re.sub(r"([?!.,])\1+", r"\1", s)


_F: dict[int, object] = {}


def _w(text: str, size: int) -> float:
    from PIL import ImageFont
    if size not in _F:
        _F[size] = ImageFont.truetype(str(FONT), size)
    return _F[size].getlength(text)


def _split(words: list[str]) -> int:
    if len(words) < 2 or _w(" ".join(words), BASE) <= MAX_W * 0.62:
        return 0
    return min(range(1, len(words)), key=lambda k: abs(_w(" ".join(words[:k]), BASE) - _w(" ".join(words[k:]), BASE)))


def build_project(job_dir: Path, proj: Path) -> float:
    events = json.loads((job_dir / "events.json").read_text(encoding="utf-8"))
    # overwrite in place (Windows refuses to delete a folder a preview/check process still has open)
    (proj / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    (proj / "compositions").mkdir(exist_ok=True)

    base = proj / "assets" / "base.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(job_dir / "clips" / "concat.mkv"),
                    "-i", str(job_dir / "draft.mp4"), "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart", str(base)], check=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(base)], capture_output=True, text=True).stdout.strip())
    shutil.copy(FONT, proj / "assets" / "fonts" / "Nunito-900.ttf")

    groups_html, tweens = [], []
    for g, ev in enumerate(events):
        s, e = float(ev["s"]), float(ev["e"])
        words = [(_tidy(w), float(a), float(b)) for w, a, b in ev.get("words") or []] or \
                [(w, s, e) for w in _tidy(ev["top"]).split(" ")]
        texts = [w for w, _, _ in words]
        cut = _split(texts)
        lines = [texts[:cut], texts[cut:]] if cut else [texts]
        size = BASE
        while size > MIN and max(_w(" ".join(l), size) for l in lines) > MAX_W:
            size -= 2
        spans, k = [], 0
        for li, line in enumerate(lines):
            row = "".join(f'<span class="w" id="g{g}w{k + j}">{html.escape(t)}</span>' for j, t in enumerate(line))
            spans.append(f'<div class="ln">{row}</div>')
            k += len(line)
        groups_html.append(f'<div class="grp" id="g{g}" style="font-size:{size}px">{"".join(spans)}</div>')
        tweens.append(f'tl.set("#g{g}",{{opacity:1,visibility:"visible"}},{s:.3f});')
        tweens.append(f'tl.set("#g{g}",{{opacity:0,visibility:"hidden"}},{e:.3f});')
        for k, (w, a, b) in enumerate(words):
            a = max(s, a)
            b = min(max(b, a + 0.14), e - 0.03)
            sel = f"#g{g}w{k}"
            tweens.append(f'tl.fromTo("{sel}",{{opacity:0,scale:0.5,y:40}},'
                          f'{{opacity:1,scale:1,y:0,duration:0.24,ease:"back.out(2.8)"}},{a:.3f});')
            # grows while being said, then settles (scale on an inline-block: neighbours don't move)
            if b - a > 0.42:  # only words held long enough; never overlaps the spring-in
                tweens.append(f'tl.to("{sel}",{{scale:1.13,duration:0.10,ease:"power2.out"}},{a + 0.26:.3f});')
                tweens.append(f'tl.to("{sel}",{{scale:1,duration:0.14,ease:"power2.inOut"}},{max(b, a + 0.38):.3f});')

    captions = f"""<template>
  <style>
    @font-face {{ font-family: "NunitoBlack"; font-weight: 900; src: url('assets/fonts/Nunito-900.ttf'); }}
    #root {{ position: absolute; inset: 0; overflow: visible; }}
    .grp {{ position: absolute; left: 0; width: {W}px; top: {CENTER_Y}px; transform: translateY(-50%);
            text-align: center; opacity: 0; visibility: hidden; overflow: visible;
            font-family: "NunitoBlack", "Nunito", sans-serif; font-weight: 900; line-height: 1.12; }}
    .ln {{ white-space: nowrap; }}
    .w {{ display: inline-block; color: {YELLOW}; margin: 0 0.2em; transform-origin: 50% 70%;
          -webkit-text-stroke: 0.2em #000; paint-order: stroke fill;
          text-shadow: 0 0.07em 0 rgba(0,0,0,0.55), 0 0.12em 0.18em rgba(0,0,0,0.35); }}
  </style>
  <div id="root" data-composition-id="captions" data-width="{W}" data-height="{H}" data-layout-allow-caption-zone>
    {''.join(groups_html)}
  </div>
  <script>
    const tl = gsap.timeline({{ paused: true }});
{chr(10).join('    ' + t for t in tweens)}
    window.__timelines["captions"] = tl;
  </script>
</template>
"""
    (proj / "compositions" / "captions.html").write_text(captions, encoding="utf-8")

    index = f"""<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Ingles Ahorita reel – HyperFrames captions</title>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  <style> html, body {{ margin: 0; background: #000; }} </style></head>
  <body>
    <div id="main" data-composition-id="main" data-start="0" data-duration="{dur:.3f}"
         data-width="{W}" data-height="{H}" data-fps="{FPS}">
      <video id="base" class="clip" src="assets/base.mp4" data-start="0" data-duration="{dur:.3f}"
             data-track-index="0" playsinline data-has-audio="true"
             style="position:absolute;left:0;top:0;width:{W}px;height:{H}px;object-fit:cover;z-index:1"></video>
      <div id="captions" data-composition-id="captions" data-composition-src="compositions/captions.html"
           data-start="0" data-duration="{dur:.3f}" data-track-index="1" data-track-kind="captions"
           data-width="{W}" data-height="{H}" style="position:absolute;inset:0;z-index:2"></div>
    </div>
    <script>
      window.__timelines["main"] = gsap.timeline({{ paused: true }});
    </script>
  </body>
</html>
"""
    (proj / "index.html").write_text(index, encoding="utf-8")
    return dur


if __name__ == "__main__":
    job = Path(sys.argv[1])
    d = build_project(job, job / "hyperframes")
    print(f"project ready: {job / 'hyperframes'} ({d:.1f}s)")
