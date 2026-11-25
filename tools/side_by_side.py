"""Reference (left) vs our draft (right), same height, labelled, for a manual check before sending.

usage: python tools/side_by_side.py REF.mp4 DRAFT.mp4 OUT.mp4 [--audio ref|ours]
"""
import subprocess
import sys

ref, ours, out = sys.argv[1:4]
audio = sys.argv[sys.argv.index("--audio") + 1] if "--audio" in sys.argv else "ours"
H = 960
lab = "drawtext=text='{t}':fontcolor=white:fontsize=34:box=1:boxcolor=black@0.6:x=16:y=16"
vf = (f"[0:v]fps=30,scale=-2:{H},setsar=1,{lab.format(t='REFERENCE')}[a];"
      f"[1:v]fps=30,scale=-2:{H},setsar=1,{lab.format(t='OURS')}[b];"
      f"[a][b]hstack=inputs=2:shortest=0[v]")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", ref, "-i", ours, "-filter_complex", vf,
                "-map", "[v]", "-map", "0:a" if audio == "ref" else "1:a",
                "-c:v", "libx264", "-crf", "22", "-preset", "veryfast", "-c:a", "aac", "-b:a", "160k",
                out], check=True)
print(out)
