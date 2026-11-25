"""Tonal balance (speech frames, relative to 1-2.5 kHz), background/speech level, loudness + LRA."""
import re, subprocess, sys
import numpy as np
BANDS = [(60, 150), (150, 400), (400, 1000), (1000, 2500), (2500, 5000), (5000, 9000)]
def load(f, sr=48000):
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", f, "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
for f in sys.argv[1:]:
    a = load(f)[:48000 * 70]
    fr = 4096; n = len(a) // fr
    X = np.abs(np.fft.rfft(a[:n * fr].reshape(n, fr) * np.hanning(fr), axis=1)) ** 2
    P = X[X.sum(1) > np.percentile(X.sum(1), 60)].mean(0); fq = np.fft.rfftfreq(fr, 1 / 48000)
    e = np.array([10 * np.log10(P[(fq >= lo) & (fq < hi)].sum()) for lo, hi in BANDS]); e -= e[3]
    w = a[: len(a) // 2400 * 2400].reshape(-1, 2400)
    db = 20 * np.log10(np.sqrt((w ** 2).mean(1)) + 1e-9)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", f, "-vn", "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    s = r[r.rfind("Summary"):]
    print(f"{f[-28:]:>28} | " + " ".join(f"{v:+5.1f}" for v in e)
          + f" | bg {np.percentile(db, 10):6.1f} sp {np.percentile(db, 90):6.1f}"
          + f" | I {re.search(r'I:\s+(-?[\d.]+)', s).group(1)} LRA {re.search(r'LRA:\s+([\d.]+)', s).group(1)}")
