"""One click: start the render server + Cloudflare quick tunnel, then point n8n and the Vercel UI at it.

Run:  python start_editor.py      (or double-click "Start Reel Editor.bat")
Needs in .env: REEL_API_KEY, N8N_API_KEY, N8N_BASE, VERCEL_TOKEN, VERCEL_SCOPE, APP_PASSWORD
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
PROJECT = "ingles-reel-editor"
DEFAULT_DOC = "1bSfR828BJsyQfi1yhfx1pYQ5YZjmwZEgRLgSb3F1xws"
# CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: hidden console the children (ffmpeg) inherit, so renders
# open no windows; DETACHED_PROCESS made Windows pop a new console for every ffmpeg call.
HIDDEN = 0x08000000 | 0x00000200


def env() -> dict:
    out = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def set_env(key: str, value: str) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines()
    lines = [l for l in lines if not l.startswith(key + "=")] + [f"{key}={value}"]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ok(url: str) -> bool:
    try:
        return requests.get(url + "/health", timeout=8).status_code == 200
    except requests.RequestException:
        return False


def start_server(jobs_dir: Path) -> None:
    if ok("http://127.0.0.1:8787"):
        print("server: already running")
        return
    log = open(jobs_dir / "server.log", "a", encoding="utf-8")
    subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8787"],
                     cwd=ROOT, stdout=log, stderr=log, creationflags=HIDDEN)
    for _ in range(60):
        if ok("http://127.0.0.1:8787"):
            print("server: started")
            return
        time.sleep(1)
    sys.exit("server did not start - see " + str(jobs_dir / "server.log"))


def start_tunnel(jobs_dir: Path) -> str:
    saved = jobs_dir / "tunnel_url.txt"
    if saved.exists() and ok(saved.read_text().strip()):
        print("tunnel: already running")
        return saved.read_text().strip()
    subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe"], capture_output=True)
    logf = jobs_dir / "tunnel.log"
    logf.write_text("")
    log = open(logf, "a", encoding="utf-8")
    subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8787"],
                     stdout=log, stderr=log, creationflags=HIDDEN)
    url = None
    for _ in range(60):
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", logf.read_text(errors="ignore"))
        if m:
            url = m.group(0)
            break
        time.sleep(1)
    if not url:
        sys.exit("tunnel did not give a URL - see " + str(logf))
    for _ in range(60):  # DNS for a fresh quick tunnel takes a few seconds
        if ok(url):
            break
        time.sleep(2)
    else:
        sys.exit("tunnel URL never answered: " + url)
    saved.write_text(url)
    print("tunnel:", url)
    return url


def update_n8n(e: dict, url: str) -> None:
    """Change only the Settings node's render_url (a full re-push breaks the edited workflow)."""
    base, h = e["N8N_BASE"].rstrip("/"), {"X-N8N-API-KEY": e["N8N_API_KEY"], "content-type": "application/json"}
    wid = (ROOT / "n8n" / "workflow_id.txt").read_text().strip()
    wf = requests.get(f"{base}/api/v1/workflows/{wid}", headers=h, timeout=60).json()
    changed = False
    for n in wf["nodes"]:
        if n["name"] == "Settings":
            for a in n["parameters"].get("assignments", {}).get("assignments", []):
                if a.get("name") == "render_url" and a.get("value") != url:
                    a["value"], changed = url, True
    if not changed:
        print("n8n: already up to date")
        return
    body = {"name": wf["name"], "nodes": wf["nodes"], "connections": wf["connections"],
            "settings": wf.get("settings", {})}
    r = requests.put(f"{base}/api/v1/workflows/{wid}", headers=h, data=json.dumps(body), timeout=60)
    if r.status_code == 400:  # older n8n rejects settings keys it doesn't know
        body["settings"] = {"executionOrder": wf.get("settings", {}).get("executionOrder", "v1")}
        r = requests.put(f"{base}/api/v1/workflows/{wid}", headers=h, data=json.dumps(body), timeout=60)
    r.raise_for_status()
    if wf.get("active"):
        requests.post(f"{base}/api/v1/workflows/{wid}/activate", headers=h, timeout=60)
    print("n8n: form now uses", url)


def update_vercel(e: dict, url: str) -> None:
    token, scope = e.get("VERCEL_TOKEN"), e.get("VERCEL_SCOPE")
    if not token:
        print("vercel: skipped (no VERCEL_TOKEN in .env)")
        return
    last = Path(e.get("JOBS_DIR", ROOT / "jobs")) / "vercel_backend.txt"
    if last.exists() and last.read_text().strip() == url and "--deploy" not in sys.argv:
        print(f"vercel: already up to date (https://{PROJECT}.vercel.app)")
        return
    h = {"Authorization": f"Bearer {token}"}
    q = {"slug": scope} if scope else {}
    proj = requests.get(f"https://api.vercel.com/v9/projects/{PROJECT}", headers=h, params=q, timeout=30)
    if proj.status_code == 404:
        proj = requests.post("https://api.vercel.com/v11/projects", headers=h, params=q, timeout=30,
                             json={"name": PROJECT, "framework": None})
        print("vercel: project created")
    proj.raise_for_status()
    proj = proj.json()
    if proj.get("ssoProtection"):  # Vercel login wall off: the page has its own password
        requests.patch(f"https://api.vercel.com/v9/projects/{PROJECT}", headers=h, params=q, timeout=30,
                       json={"ssoProtection": None}).raise_for_status()
    link = ROOT / "web" / ".vercel"
    link.mkdir(exist_ok=True)
    (link / "project.json").write_text(json.dumps({"projectId": proj["id"], "orgId": proj["accountId"],
                                                   "projectName": PROJECT}))
    envs = [("BACKEND_URL", url, "plain"), ("REEL_API_KEY", e["REEL_API_KEY"], "sensitive"),
            ("APP_PASSWORD", e["APP_PASSWORD"], "sensitive"), ("DEFAULT_SCRIPT_DOC_ID", DEFAULT_DOC, "plain")]
    r = requests.post(f"https://api.vercel.com/v10/projects/{PROJECT}/env", headers=h, timeout=30,
                      params={**q, "upsert": "true"},
                      json=[{"key": k, "value": v, "type": t, "target": ["production"]} for k, v, t in envs])
    r.raise_for_status()
    cmd = ["npx", "--yes", "vercel@latest", "deploy", "--prod", "--yes", "--token", token]
    if scope:
        cmd += ["--scope", scope]
    out = subprocess.run(cmd, cwd=ROOT / "web", capture_output=True, text=True, shell=True)
    if out.returncode != 0:
        sys.exit("vercel deploy failed:\n" + out.stderr[-1500:])
    p = requests.get(f"https://api.vercel.com/v9/projects/{PROJECT}", headers=h, params=q, timeout=30).json()
    aliases = (p.get("targets", {}).get("production", {}) or {}).get("alias") or []
    print("vercel: live at", "https://" + (min(aliases, key=len) if aliases else f"{PROJECT}.vercel.app"))
    last.write_text(url)


def main() -> None:
    e = env()
    if not e.get("APP_PASSWORD"):
        set_env("APP_PASSWORD", secrets.token_urlsafe(9))
        e = env()
        print("made a UI password (in .env as APP_PASSWORD)")
    jobs_dir = Path(e.get("JOBS_DIR", ROOT / "jobs"))
    jobs_dir.mkdir(parents=True, exist_ok=True)
    start_server(jobs_dir)
    url = start_tunnel(jobs_dir)
    update_n8n(e, url)
    update_vercel(e, url)
    print("\nAll set. Keep this laptop on and awake while reels are being made.")


if __name__ == "__main__":
    main()
