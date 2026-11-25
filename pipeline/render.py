"""Cut, reframe to 9:16, smooth zoom, clean audio, burn captions.

Clips from continuous takes are merged into segments (no visible cut between
sentences from the same take).  Zoom drifts smoothly between Z_LO and Z_HI
(measured from the reference reel) using a time-varying ffmpeg crop expression.
Audio: denoise, EQ (less boom/mud), de-ess, gentle compression, a -6 dB gate and
-14 LUFS: measured to match the reference's tone, dynamics and ~-69 dB background.
Hard cuts are snapped to real silence (snap_edges) so no word is clipped.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2

Z_LO, Z_HI = 1.11, 1.22
ZOOM_PERIOD = 12          # seconds for one full oscillation
ZOOM_SHARPNESS = 0.15     # smaller = snappier transition (~0.5 s at 12 s period)
OUT_W, OUT_H = 1080, 1920
FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_CASCADE = cv2.CascadeClassifier(
    str(Path(__file__).resolve().parent.parent / "assets" / "models"
        / "haarcascade_frontalface_default.xml"))


# ---------------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------------

def _probe(src: Path) -> tuple[int, int]:
    """Displayed (w, h): phones store portrait as landscape + rotation flag."""
    import json as _j
    d = _j.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:stream_side_data=rotation:stream_tags=rotate",
         "-of", "json", str(src)], capture_output=True, text=True).stdout
    )["streams"][0]
    w, h = int(d["width"]), int(d["height"])
    rot = int(float(d.get("tags", {}).get("rotate", 0) or 0))
    for sd in d.get("side_data_list", []) or []:
        rot = int(float(sd.get("rotation", rot)))
    return (h, w) if abs(rot) % 180 == 90 else (w, h)


def face_center(src: Path, s: float, e: float,
                fallback: tuple[float, float]) -> tuple[float, float]:
    cap = cv2.VideoCapture(str(src))
    pts = []
    for t in (s + (e - s) * f for f in (0.2, 0.5, 0.8)):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = _CASCADE.detectMultiScale(g, 1.15, 6, minSize=(120, 120))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            pts.append((x + w / 2, y + h / 2))
    cap.release()
    return pts[len(pts) // 2] if pts else fallback


def _global_face_center(src: Path, clips: list[dict],
                        sw: int, sh: int) -> tuple[float, float]:
    """Stable face centre averaged across the whole video."""
    pts = []
    for c in clips[::max(1, len(clips) // 8)]:
        cap = cv2.VideoCapture(str(src))
        for t in (c["s"] + (c["e"] - c["s"]) * f for f in (0.3, 0.7)):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = _CASCADE.detectMultiScale(g, 1.15, 6, minSize=(120, 120))
            if len(faces):
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                pts.append((x + w / 2, y + h / 2))
        cap.release()
    if not pts:
        return sw / 2, sh * 0.3
    pts.sort()
    return pts[len(pts) // 2]


def crop_box(cx: float, cy: float, zoom: float,
             sw: int, sh: int) -> tuple[int, int, int, int]:
    """Static 9:16 crop at a given zoom, face at ~30 % from the top."""
    h = min(sh, sw * OUT_H / OUT_W) / zoom
    w = h * OUT_W / OUT_H
    x = min(max(cx - w / 2, 0), sw - w)
    y = min(max(cy - 0.30 * h, 0), sh - h)
    ev = lambda v: int(v) // 2 * 2
    return ev(w), ev(h), ev(x), ev(y)


def zoom_at(t: float) -> float:
    """Zoom factor at output time *t* (matches the ffmpeg expression)."""
    flat = max(-1.0, min(1.0,
               math.sin(2 * math.pi * t / ZOOM_PERIOD) / ZOOM_SHARPNESS
               )) * 0.5 + 0.5
    return Z_LO + (Z_HI - Z_LO) * flat


# ---------------------------------------------------------------------------
#  segment merging & output-timeline
# ---------------------------------------------------------------------------

def merge_segments(clips: list[dict], gap: float = 1.0) -> list[list[dict]]:
    """Group consecutive clips whose source times are nearly continuous.

    Two clips are merged when the gap in the raw recording is < *gap* seconds (a natural pause)
    — the presenter was still talking, so there is no visible cut.
    """
    if not clips:
        return []
    segs: list[list[dict]] = [[clips[0]]]
    for c in clips[1:]:
        if c["s"] - segs[-1][-1]["e"] < gap:
            segs[-1].append(c)
        else:
            segs.append([c])
    return segs


def seg_out_times(segments: list[list[dict]]) -> None:
    """Assign output-timeline positions accounting for merged segments.

    Within a segment the source audio plays continuously, so output time =
    segment_output_start + (source_time − segment_source_start).
    """
    t = 0.0
    for seg in segments:
        seg_s = seg[0]["s"]
        seg_e = seg[-1]["e"]
        for c in seg:
            c["os"] = round(t + (c["s"] - seg_s), 3)
            c["oe"] = round(t + (c["e"] - seg_s), 3)
            for w in c["words"]:
                w["os"] = round(t + max(0.0, w["s"] - seg_s), 3)
                w["oe"] = round(t + min(seg_e - seg_s, max(0.0, w["e"] - seg_s)), 3)
        t = round(t + seg_e - seg_s, 3)


def _loudness_track(src: Path) -> list[float]:
    """dB per 10 ms of the source audio."""
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    n = len(a) // 160
    return (20 * np.log10(np.sqrt((a[:n * 160].reshape(n, 160) ** 2).mean(1)) + 1e-9)).tolist()


def snap_edges(src: Path, segments: list[list[dict]]) -> None:
    """Move every hard cut onto real silence.

    Whisper's word ends land early on soft endings ("lef|t", "esperand|o") and its starts can
    miss the first syllable, so a cut placed on word timings clips the word. Ends are pushed
    forward until 150 ms of quiet; starts are pulled back to the onset if sound is already
    going at the cut point.
    """
    import numpy as np
    db = np.array(_loudness_track(src))
    noise = float(np.percentile(db, 10))
    tail_thr = noise + 10    # soft endings ("f", "t" release, "s") sit 10-25 dB over the floor
    speech_thr = noise + 40  # a voiced vowel: the next sentence has started
    n = len(db)
    for seg in segments:
        first, last = seg[0], seg[-1]
        i = int(last["e"] * 100)
        last_loud, quiet, onset, dipped = i, 0, None, False
        for j in range(i, min(n, i + 60)):
            if dipped and db[j] > speech_thr:
                onset = j
                break
            if db[j] > tail_thr:
                last_loud, quiet = j, 0
            else:
                quiet += 1
                dipped = True
                if quiet >= 15:
                    break
        if onset is not None:   # next words follow closely (maybe a removed line): cut in the dip between
            end = (i + int(np.argmin(db[i:onset]))) / 100
        else:
            end = (last_loud + 1) / 100 + 0.08
        last["e"] = round(max(last["e"], end), 3)
        i = int(first["s"] * 100)
        if 30 <= i < n and db[i] > tail_thr:  # cut lands on sound: start at the quiet dip just before it
            k = i - 30 + int(np.argmin(db[i - 30:i + 1]))
            first["s"] = round(min(first["s"], k / 100), 3)


# ---------------------------------------------------------------------------
#  smooth-zoom crop expression
# ---------------------------------------------------------------------------

def _zoom_crop_vf(fcx: float, fcy: float, sw: int, sh: int,
                  seg_os: float) -> str:
    """ffmpeg -vf chain: 9:16 base crop -> 2x upscale -> per-frame zoompan (smooth zoom).

    crop can't vary its size over time, so the zoom is done by zoompan, on a 2x
    upscaled frame so its whole-pixel rounding doesn't show as jitter.
    """
    base_h = min(sh, sw * OUT_H / OUT_W)
    base_w = base_h * OUT_W / OUT_H
    bx = min(max(fcx - base_w / 2, 0), sw - base_w)
    by = min(max(fcy - 0.30 * base_h, 0), sh - base_h)
    up = 2
    W, H = OUT_W * up, OUT_H * up
    k = W / base_w
    fx, fy = (fcx - bx) * k, (fcy - by) * k
    fps = "(30000/1001)"
    t = f"(on/{fps}+{seg_os:.3f})"
    z = (f"{Z_LO}+{Z_HI - Z_LO:.4f}*(clip(sin(2*PI*{t}/{ZOOM_PERIOD})/{ZOOM_SHARPNESS},-1,1)*0.5+0.5)")
    x = f"clip({fx:.1f}-iw/zoom/2,0,iw-iw/zoom)"
    y = f"clip({fy:.1f}-0.30*ih/zoom,0,ih-ih/zoom)"
    return (f"crop={int(base_w)//2*2}:{int(base_h)//2*2}:{int(bx)}:{int(by)},"
            f"fps=30000/1001,scale={W}:{H}:flags=lanczos,"
            f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={OUT_W}x{OUT_H}:fps=30000/1001,setsar=1")


# ---------------------------------------------------------------------------
#  render
# ---------------------------------------------------------------------------

def render(src: Path, clips: list[dict], segments: list[list[dict]],
           ass: Path, out_dir: Path, *,
           workers: int = 4, reuse_clips: bool = False
           ) -> tuple[Path, tuple[float, float]]:
    """Encode segments → concat → audio cleanup + captions → draft.mp4.

    Returns (draft_path, (face_cx, face_cy)).
    """
    tmp = out_dir / "clips"
    tmp.mkdir(parents=True, exist_ok=True)
    sw, sh = _probe(src)
    fcx, fcy = _global_face_center(src, clips, sw, sh)

    if not (reuse_clips and (tmp / "concat.mkv").exists()):
        # precompute output start time for each segment
        seg_starts: list[float] = []
        t = 0.0
        for seg in segments:
            seg_starts.append(t)
            t += seg[-1]["e"] - seg[0]["s"]

        def encode(args):
            si, seg, seg_os = args
            seg_s = seg[0]["s"]
            d = seg[-1]["e"] - seg_s
            p = tmp / f"s{si:03d}.mkv"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y",
                "-ss", f"{seg_s:.3f}", "-i", str(src), "-t", f"{d:.3f}",
                "-vf", _zoom_crop_vf(fcx, fcy, sw, sh, seg_os),
                "-af", (f"aresample=48000,"
                        f"afade=t=in:d=0.015,"
                        f"afade=t=out:st={max(0, d - 0.025):.3f}:d=0.025"),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "15",
                "-pix_fmt", "yuv420p",
                "-c:a", "pcm_s16le", "-ac", "1", str(p)], check=True)
            return p

        with ThreadPoolExecutor(max_workers=workers) as ex:
            parts = list(ex.map(
                encode,
                [(si, seg, seg_starts[si]) for si, seg in enumerate(segments)]))

        (tmp / "list.txt").write_text(
            "".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat",
                        "-safe", "0", "-i", "list.txt", "-c", "copy",
                        "concat.mkv"],
                       cwd=tmp, check=True)

    # --- audio cleanup + captions ----------------------------------------
    fonts_local = out_dir / "fonts"
    shutil.copytree(FONTS, fonts_local, dirs_exist_ok=True)
    draft = out_dir / "draft.mp4"

    pre = ("highpass=f=100,afftdn=nr=18:nf=-30:tn=1,"
           "equalizer=f=130:t=q:w=1:g=-4,equalizer=f=320:t=q:w=1.2:g=-3,equalizer=f=650:t=q:w=1.2:g=-1.5,"
           "deesser=i=0.5,"
           "acompressor=threshold=-24dB:ratio=3:attack=10:release=160:makeup=1,"
           "agate=threshold=0.015:range=0.5:ratio=2:attack=5:release=120")

    import json as _j, re as _re
    meas = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", "clips/concat.mkv", "-vn",
         "-af", f"{pre},loudnorm=I=-14:TP=-1.5:LRA=7:print_format=json",
         "-f", "null", "-"],
        cwd=out_dir, capture_output=True, text=True).stderr
    m = _j.loads(_re.findall(r"\{[^{}]+\}", meas)[-1])
    ln = (f"loudnorm=I=-14:TP=-1.5:LRA=7:"
          f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
          f"offset={m['target_offset']}:linear=true")

    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", "clips/concat.mkv",
        "-vf", f"subtitles={ass.name}:fontsdir=fonts",
        "-af", f"{pre},{ln},aresample=48000",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", draft.name],
        cwd=out_dir, check=True)

    return draft, (fcx, fcy)


# ---------------------------------------------------------------------------
#  cover (Instagram cover image)
# ---------------------------------------------------------------------------

def cover(src: Path, clip: dict, title: str, out_dir: Path,
          face_xy: tuple[float, float] | None = None) -> Path:
    sw, sh = _probe(src)
    if face_xy:
        cx, cy = face_xy
    else:
        cx, cy = face_center(src, clip["s"], clip["e"], (sw / 2, sh * 0.3))
    w, h, x, y = crop_box(cx, cy, 1.0, sw, sh)
    ass_p = out_dir / "cover.ass"
    ass_p.write_text(f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Nunito Black,165,&H0033E5FF,&H0033E5FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,9,4,5,110,110,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,Hook,,0,0,0,,{{\\pos(540,1060)}}{title}
""", encoding="utf-8-sig")
    fonts_local = out_dir / "fonts"
    shutil.copytree(FONTS, fonts_local, dirs_exist_ok=True)
    t = clip["s"] + min(0.5, (clip["e"] - clip["s"]) / 2)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src),
        "-frames:v", "1",
        "-vf", (f"crop={w}:{h}:{x}:{y},"
                f"scale={OUT_W}:{OUT_H}:flags=lanczos,"
                f"subtitles=cover.ass:fontsdir=fonts"),
        "-q:v", "2", "cover.jpg"], cwd=out_dir, check=True)
    return out_dir / "cover.jpg"
