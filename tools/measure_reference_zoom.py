"""Measure the reference reel's zoom/framing over time by aligning its static background
(bookshelf) to the raw footage. Output: per time t -> Z (zoom vs. full raw frame) and the
raw-pixel centre of the reel's crop. Usage: python -I tools/measure_reference_zoom.py <ref.mp4> <raw.mp4> <out.json>"""
import json, sys
import cv2, numpy as np

ref, raw, out = sys.argv[1], sys.argv[2], sys.argv[3]
D = 384 / 1080                      # phone screen px per reel px (reel shown at full width)
cap = cv2.VideoCapture(raw); cap.set(cv2.CAP_PROP_POS_MSEC, 60_000); ok, rawf = cap.read(); cap.release()
rh, rw = rawf.shape[:2]
orb = cv2.ORB_create(4000)
g_raw = cv2.cvtColor(rawf, cv2.COLOR_BGR2GRAY)
# background only: top 45% of the raw frame (bookshelf, plaque, trophies) — the person moves below/centre
mask_raw = np.zeros_like(g_raw); mask_raw[: int(rh * 0.42), :] = 255
k_raw, d_raw = orb.detectAndCompute(g_raw, mask_raw)
bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

cap = cv2.VideoCapture(ref); fps = cap.get(5); n = int(cap.get(7)); res = []
t = 0.0
while t * fps < n - 1:
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000); ok, f = cap.read()
    if not ok: break
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    m = np.zeros_like(g); m[60:300, 0:330] = 255          # picture's upper part, avoid status bar + right-side buttons
    k, d = orb.detectAndCompute(g, m)
    row = {"t": round(t, 2), "Z": None}
    if d is not None and len(k) > 30:
        ms = sorted(bf.match(d, d_raw), key=lambda x: x.distance)[:400]
        if len(ms) >= 20:
            src = np.float32([k_raw[x.trainIdx].pt for x in ms]); dst = np.float32([k[x.queryIdx].pt for x in ms])
            M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3)
            if M is not None and inl.sum() >= 15:
                s = float(np.hypot(M[0, 0], M[1, 0]))            # screen px per raw px
                Z = s / D * (rw / 1080)                           # reel zoom relative to the full raw frame
                inv = cv2.invertAffineTransform(M)
                cx, cy = inv @ np.array([192, 683 / 2, 1.0])       # screen centre of the reel -> raw px
                row = {"t": round(t, 2), "Z": round(Z, 3), "cx": round(float(cx)), "cy": round(float(cy)), "inliers": int(inl.sum())}
    res.append(row); t += 0.5
json.dump(res, open(out, "w"), indent=0)
zs = [r["Z"] for r in res if r["Z"]]
print(f"frames {len(res)} measured {len(zs)}  Z min {min(zs):.2f} median {sorted(zs)[len(zs)//2]:.2f} max {max(zs):.2f}")
print(" ".join(f'{r["t"]:.0f}:{r["Z"]:.2f}' for r in res if r["Z"] and r["t"] % 1 == 0))
