"""Inglés Ahorita reel render service.

n8n Cloud calls this over a Cloudflare Tunnel. Every request except /health
must send the header  X-API-Key: <REEL_API_KEY from .env>.

Endpoints
  GET  /health          -> {"ok": true}            (no key, used to test the tunnel)
  POST /jobs            -> start a render job      (returns job_id right away)
  GET  /jobs/{job_id}   -> job status + output link

Rendering runs in the background; when it finishes the service POSTs the result
to the job's callback_url (an n8n Webhook node), so n8n never has to wait on a
long HTTP request.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent


def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
API_KEY = os.environ["REEL_API_KEY"]
JOBS_DIR = Path(os.environ.get("JOBS_DIR", ROOT / "jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Inglés Ahorita reel service")
_RENDER_LOCK = threading.Lock()


def require_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key header")


class JobIn(BaseModel):
    drive_file_id: str               # raw video in Google Drive
    script_doc_id: Optional[str] = None
    script_section: Optional[str] = None  # e.g. "4" -> which script in the doc
    callback_url: Optional[str] = None    # n8n Webhook URL to POST the result to
    meta: dict = Field(default_factory=dict)  # echoed back untouched (e.g. notify_email)


def _job_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "job.json"


def _save(job: dict) -> None:
    p = _job_path(job["job_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")


def _run(job: dict) -> None:
    """Background worker: one job at a time (rendering uses every CPU core)."""
    try:
        job["status"] = "waiting" if _RENDER_LOCK.locked() else "running"
        _save(job)
        with _RENDER_LOCK:
            job["status"] = "running"
            _save(job)
            from pipeline import run
            run.process(job, JOBS_DIR)
        draft = JOBS_DIR / job["job_id"] / "draft.mp4"
        if not draft.exists():
            raise RuntimeError("Pipeline finished but no draft.mp4 was produced")
        job["local_path"] = str(draft)
        job["status"] = "done"
    except Exception as e:  # report failures to n8n instead of dying silently
        job["status"] = "failed"
        job["error"] = f"{type(e).__name__}: {e}"
        job["trace"] = traceback.format_exc()[-2000:]
    finally:
        job["finished_at"] = time.time()
        _save(job)
        if job["status"] == "done":
            base = f'{job["public_base"]}/d/{job["job_id"]}/{job["share_token"]}'
            job["draft_url"], job["cover_url"], job["report_url"] = f"{base}/draft.mp4", f"{base}/cover.jpg", f"{base}/report.txt"
            job["transcript_raw_url"], job["transcript_reel_url"], job["srt_url"] = (
                f"{base}/transcript_raw.txt", f"{base}/transcript_reel.txt", f"{base}/reel.srt")
            _save(job)
        if job.get("callback_url"):
            try:
                payload = {k: v for k, v in job.items() if k not in ("trace", "share_token")}
                httpx.post(job["callback_url"], json=payload, headers={"X-API-Key": API_KEY}, timeout=30)
            except Exception as e:
                job["callback_error"] = str(e)
                _save(job)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "ingles-ahorita-reels"}


@app.post("/jobs", dependencies=[Depends(require_key)])
def create_job(body: JobIn, request: Request) -> dict:
    job_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    job = {"job_id": job_id, "status": "queued", "created_at": time.time(),
           "public_base": f"https://{host}", "share_token": secrets.token_urlsafe(16), **body.model_dump()}
    _save(job)
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return {"job_id": job_id, "status": "queued"}


def _public(job: dict) -> dict:
    job = {k: v for k, v in job.items() if k not in ("trace", "share_token", "local_path")}
    log = JOBS_DIR / job["job_id"] / "progress.log"
    if log.exists():
        lines = [x for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
        job["progress"] = lines[-1] if lines else ""
    return job


@app.get("/jobs", dependencies=[Depends(require_key)])
def list_jobs(limit: int = 20) -> list[dict]:
    out = []
    for f in sorted(JOBS_DIR.glob("*/job.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if job.get("job_id"):
            out.append(_public(job))
        if len(out) >= limit:
            break
    return out


@app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
def get_job(job_id: str) -> dict:
    p = _job_path(job_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="No job with that id")
    return _public(json.loads(p.read_text(encoding="utf-8")))


_SHAREABLE = {"draft.mp4": "video/mp4", "cover.jpg": "image/jpeg", "report.txt": "text/plain; charset=utf-8",
              "transcript_raw.txt": "text/plain; charset=utf-8", "transcript_reel.txt": "text/plain; charset=utf-8",
              "reel.srt": "application/x-subrip; charset=utf-8"}


@app.get("/d/{job_id}/{token}/{name}")
def download_draft(job_id: str, token: str, name: str) -> FileResponse:
    """Unguessable per-job link so n8n (and the team) can fetch the outputs without the API key."""
    if name not in _SHAREABLE:
        raise HTTPException(status_code=404, detail="Not found")
    p = _job_path(job_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="No job with that id")
    job = json.loads(p.read_text(encoding="utf-8"))
    if not secrets.compare_digest(token, job.get("share_token", "")):
        raise HTTPException(status_code=404, detail="No job with that id")
    f = JOBS_DIR / job_id / name
    if not f.exists():
        raise HTTPException(status_code=404, detail="Not rendered yet")
    return FileResponse(f, media_type=_SHAREABLE[name], filename=f"{job_id}-{name}")
