"""Speech level (90th pct) and background level (10th pct) of 50 ms windows, in dBFS."""
import subprocess, sys
import numpy as np
for f in sys.argv[1:]:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", f, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    n = len(a) // 800
    rms = np.sqrt((a[:n * 800].reshape(n, 800) ** 2).mean(1) + 1e-12)
    db = 20 * np.log10(rms)
    print(f"{f[-40:]:>40}  speech {np.percentile(db, 90):6.1f} dB   background {np.percentile(db, 10):6.1f} dB")
