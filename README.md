# Inglés Ahorita – Reel Editor

Turns a raw talking-head recording into a finished Instagram reel that matches the client's reference reel.

## How it works

```
Website (Vercel)  ─┐
                   ├─►  Cloudflare tunnel  ─►  Backend on the laptop (Python)  ─►  finished reel
n8n form          ─┘
```

1. **Website** (`web/`, hosted on Vercel): paste the Google Drive link of the raw video and click *Make the reel*. It shows progress and the finished reel.
2. **n8n**: a second way in. It sends the same request with a form.
3. **Backend** (`server.py` + `pipeline/`): does the editing.
   - downloads the video from Drive
   - transcribes the speech (OpenAI Whisper)
   - finds the matching script in the teleprompter Google Doc
   - picks the best take of each line and follows spoken instructions such as "puedes quitar esa parte"
   - cuts only where needed and snaps every cut to real silence, so no word is clipped
   - adds a slow zoom, cleans the audio to Instagram loudness, and burns captions (yellow Spanish, blue Spanish examples, white English)
   - saves `draft.mp4`, `cover.jpg`, `report.txt` (what was cut and why) and subtitles
4. **Cloudflare tunnel**: gives the laptop a public HTTPS link so the website and n8n can reach it.

A reel takes about 5–10 minutes.

## Run it

Needs Python 3.12, FFmpeg and cloudflared on the PATH, and Node (only for deploying the website).

```
pip install -r requirements.txt
copy .env.example .env      # then fill it in
python start_editor.py      # or double-click "Start Reel Editor.bat"
```

`start_editor.py` starts the backend and the tunnel, then points the website and the n8n form at the new tunnel link. Run it again after every restart.

## Folders

| Path | What it is |
|---|---|
| `server.py` | API the website and n8n call (`POST /jobs`, `GET /jobs`, `GET /jobs/{id}`) |
| `pipeline/` | the editing steps: transcribe, script matching, edit decisions, captions, render |
| `web/` | the Vercel website (static page + small API routes that forward to the backend) |
| `n8n/` | builder for the n8n workflow |
| `tools/` | checks used to compare our reels with the reference (audio, zoom, side-by-side) |
| `assets/` | caption fonts and the face-detection model |

## Next step

The backend runs on a laptop for now, so the laptop has to be on. Moving it to a cloud server will make it work 24/7 with a fixed address.
