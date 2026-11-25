"""Build a synthetic raw recording to test the pipeline end to end.

Script #7 ("No hagas caso") read by a generic TTS voice, with planted problems:
setup chatter, stumbles + retakes, "okay" filler, a long pause, an off-camera
instruction to remove a part, and both CTA endings. The picture is a plain card
with a running clock of the ORIGINAL recording time, so every cut is visible.

Usage: python -I tools/make_test_video.py <out.mp4>
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
P, OFF = "presenter", "offcamera"

# (who, text, pause_after_seconds)
TAKES = [
    (P, "Ok, ahora voy a grabar el video número siete. Ok.", 2.0),                       # chatter -> cut
    (P, "¿Sabes cómo se dice en inglés \"No hagas caso\"?", 0.6),
    (P, "Hay tres alternativas, y cada una tiene su momento.", 0.6),
    (P, "Después de este video, te va a quedar muy claro.", 0.6),
    (P, "Empecemos con \"don't listen to\".", 0.6),
    (P, "La usas cuando alguien dice algo que no... perdón.", 0.8),                      # stumble -> cut
    (P, "Okay, okay.", 0.8),                                                             # filler -> cut
    (P, "La usas cuando alguien dice algo que no vale la pena.", 0.6),                   # retake -> keep
    (P, "No le hagas caso.", 0.5), (P, "Don't listen to him.", 0.6),
    (P, "No les hagas caso a tus amigos.", 0.5), (P, "Don't listen to your friends.", 0.6),
    (P, "No hagas caso a lo que dicen.", 0.5), (P, "Don't listen to what they say.", 0.6),
    (P, "Fíjate: \"listen\" siempre va con \"to\".", 0.5),                               # removed by instruction
    (P, "No es \"don't listen him\".", 0.5),                                             # removed by instruction
    (P, "Es \"don't listen to him\".", 1.0),                                             # removed by instruction
    (OFF, "Puedes quitar esa parte, ¿ok? La del listen to.", 1.2),                      # instruction -> cut
    (P, "Ahora, \"ignore\".", 0.6),
    (P, "La usas para cosas, como un mensaje, un comentario o un ruido.", 4.0),          # long pause -> shortened
    (P, "No hagas caso a ese mensaje.", 0.5), (P, "Ignore that message.", 0.6),
    (P, "No hagas caso al ruido.", 0.5),
    (P, "Ignore the... the... uh.", 0.8),                                                # stumble -> cut
    (P, "Ignore the noise.", 0.6),                                                       # retake -> keep
    (P, "Ignóralo, no es importante.", 0.5), (P, "Ignore it, it's not important.", 0.6),
    (P, "Pero cuidado.", 0.5),
    (P, "\"Ignore\" suena fuerte con personas.", 0.5),
    (P, "\"Ignore him\" es más como \"no le hables\".", 0.5),
    (P, "Por eso, con personas, mejor usa \"don't listen to\".", 0.6),
    (P, "Y por último, \"never mind\".", 0.5),
    (P, "Esta es diferente.", 0.5),
    (P, "La usas cuando tú dijiste algo, y quieres que la otra persona lo olvide.", 0.6),
    (P, "No me hagas caso.", 0.5), (P, "Never mind.", 0.6),
    (P, "No me hagas caso, estoy cansado.", 0.5), (P, "Never mind, I'm just tired.", 0.6),
    (P, "No me hagas caso, ya lo encontré.", 0.5), (P, "Never mind, I found it.", 0.6),
    (P, "Es como decir \"olvídalo\".", 0.5),
    (P, "Y los nativos la usan todo el tiempo.", 0.6),
    (P, "Entonces recuerda:", 0.5),
    (P, "\"Don't listen to\" cuando alguien dice algo que no vale la pena.", 0.5),
    (P, "\"Ignore\" para mensajes, comentarios o cosas.", 0.5),
    (P, "\"Never mind\" cuando quieres que olviden lo que tú dijiste.", 0.7),
    (P, "Comenta \"triunfar\" y te mando las 100 frases que necesitas para hablar como un nativo.", 0.8),
    (P, "Puedes encontrar las 100 frases más comunes en el enlace de mi perfil.", 1.0),  # TT/YT ending -> cut
    (P, "Gracias.", 1.0),                                                                # chatter -> cut
]

VOICE = {
    P: ("ash", "You are a friendly, energetic Spanish teacher recording a short video for Latin American "
               "learners. Speak Latin American Spanish; pronounce English words and sentences with a clear "
               "American accent. Natural pace."),
    OFF: ("coral", "You are a producer talking quietly from behind the camera, casual and brief, in Spanish."),
}


def tts(i_take, tmp: Path, client: OpenAI) -> Path:
    i, (who, text, _) = i_take
    voice, instr = VOICE[who]
    out = tmp / f"t{i:03d}.wav"
    r = client.audio.speech.create(model="gpt-4o-mini-tts", voice=voice, input=text,
                                   instructions=instr, response_format="wav")
    out.write_bytes(r.read() if hasattr(r, "read") else r.content)
    if who == OFF:  # sound like it's off-mic: quieter, duller
        dull = tmp / f"t{i:03d}_off.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out), "-af", "lowpass=f=2500,volume=0.45",
                        str(dull)], check=True)
        return dull
    return out


def main(dst: Path) -> None:
    client = OpenAI()
    tmp = Path(tempfile.mkdtemp(prefix="testvid_"))
    with ThreadPoolExecutor(max_workers=6) as ex:
        wavs = list(ex.map(lambda it: tts(it, tmp, client), enumerate(TAKES)))

    # normalise every take to 48 kHz mono, add its pause, then join
    parts = []
    for i, (w, (_, _, pause)) in enumerate(zip(wavs, TAKES)):
        p = tmp / f"n{i:03d}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(w), "-af", f"aresample=48000,apad=pad_dur={pause}",
                        "-ac", "1", str(p)], check=True)
        parts.append(p)
    lead = tmp / "lead.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "1.5",
                    str(lead)], check=True)
    (tmp / "list.txt").write_text("".join(f"file '{p.name}'\n" for p in [lead, *parts]), encoding="utf-8")
    voice = tmp / "voice.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy",
                    "voice.wav"], cwd=tmp, check=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(voice)], capture_output=True, text=True).stdout.strip())

    # picture: plain card + running clock of the original time; light room hiss under the voice
    fonts = tmp / "fonts"
    fonts.mkdir()
    for f in ("Montserrat-700.ttf", "Montserrat-400.ttf"):
        (fonts / f).write_bytes((ROOT / "assets" / "fonts" / f).read_bytes())
    vf = ("drawtext=fontfile=fonts/Montserrat-700.ttf:text='TEST RECORDING':fontcolor=white:fontsize=84:"
          "x=(w-tw)/2:y=360,"
          "drawtext=fontfile=fonts/Montserrat-400.ttf:text='Script 7 · No hagas caso · synthetic voice':"
          "fontcolor=0xCFD8E3:fontsize=40:x=(w-tw)/2:y=480,"
          "drawtext=fontfile=fonts/Montserrat-700.ttf:text='raw %{pts\\:hms}':fontcolor=0xFFF000:fontsize=96:"
          "x=(w-tw)/2:y=760")
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"gradients=s=1080x1920:c0=0x1d3557:c1=0x274c77:d={dur:.2f}:speed=0.01:r=30",
        "-i", "voice.wav",
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.006:r=48000,atrim=duration={dur}",
        "-filter_complex", f"[0:v]{vf}[v];[1:a][2:a]amix=inputs=2:duration=first:normalize=0[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", "out.mp4"], cwd=tmp, check=True)
    dst.write_bytes((tmp / "out.mp4").read_bytes())
    print(f"{dst}  ({dur:.0f}s, {dst.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
