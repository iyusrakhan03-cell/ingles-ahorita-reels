"""Create/update the 'Inglés Ahorita – Reel Editor' workflow on n8n via the Public API.

Usage:  python -I n8n/build_workflow.py [--tunnel https://xxx.trycloudflare.com]
Reads N8N_BASE, N8N_API_KEY from ../.env. Writes the workflow id to n8n/workflow_id.txt
so re-running updates the same workflow instead of creating a new one.
"""
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l)
BASE, KEY = env["N8N_BASE"].rstrip("/"), env["N8N_API_KEY"]
H = {"X-N8N-API-KEY": KEY, "Content-Type": "application/json"}
ID_FILE = Path(__file__).with_name("workflow_id.txt")

tunnel = sys.argv[sys.argv.index("--tunnel") + 1] if "--tunnel" in sys.argv else "https://REPLACE-ME.trycloudflare.com"
RENDER_CRED = {"httpHeaderAuth": {"id": "kKQxEe6zJosrFYBd", "name": "Reel render service key"}}
DONE_PATH = "ingles-reel-done"

nodes = [
    # ---------- Path 1: team submits a raw video ----------
    {
        "id": "n-form", "name": "Upload raw video", "type": "n8n-nodes-base.formTrigger", "typeVersion": 2.2,
        "position": [0, 0], "webhookId": "ingles-reel-form",
        "parameters": {
            "formTitle": "Inglés Ahorita – new reel",
            "formDescription": "Paste the Google Drive link to the raw recording (shared as 'Anyone with the link'). The matching script is found automatically. You'll get an email when the draft is ready.",
            "formFields": {"values": [
                {"fieldLabel": "Drive link to raw video", "requiredField": True,
                 "placeholder": "https://drive.google.com/file/d/.../view"},
                {"fieldLabel": "Script Google Doc link", "requiredField": False,
                 "placeholder": "Leave empty to use the default script doc"},
                {"fieldLabel": "Email for the draft", "fieldType": "email", "requiredField": True},
            ]},
            "responseMode": "onReceived",
            "options": {"path": "ingles-reel", "respondWithOptions": {"values": {
                "formSubmittedText": "Got it. The draft is being edited – you'll get an email with the link when it's ready."}}},
        },
    },
    {
        "id": "n-settings", "name": "Settings", "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [220, 0],
        "parameters": {
            "mode": "manual",
            "assignments": {"assignments": [
                {"id": "a1", "name": "render_url", "type": "string", "value": tunnel},
                {"id": "a2", "name": "callback_url", "type": "string", "value": f"{BASE}/webhook/{DONE_PATH}"},
                {"id": "a3", "name": "default_script_doc", "type": "string",
                 "value": "1bSfR828BJsyQfi1yhfx1pYQ5YZjmwZEgRLgSb3F1xws"},
            ]},
            "includeOtherFields": True, "options": {},
        },
    },
    {
        "id": "n-parse", "name": "Read the links", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [440, 0],
        "parameters": {"jsCode": r"""
const it = $input.first().json;
const idFrom = (s) => {
  if (!s) return null;
  const m = String(s).match(/\/d\/([A-Za-z0-9_-]{10,})/) || String(s).match(/[?&]id=([A-Za-z0-9_-]{10,})/);
  return m ? m[1] : (/^[A-Za-z0-9_-]{20,}$/.test(String(s).trim()) ? String(s).trim() : null);
};
const fileId = idFrom(it['Drive link to raw video']);
if (!fileId) throw new Error('Could not find a Google Drive file id in: ' + it['Drive link to raw video']);
return [{ json: {
  render_url: it.render_url.replace(/\/$/, ''),
  body: {
    drive_file_id: fileId,
    script_doc_id: idFrom(it['Script Google Doc link']) || it.default_script_doc,
    script_section: 'auto',  // render service detects the script from the transcript
    callback_url: it.callback_url,
    meta: { notify_email: it['Email for the draft'], submitted_at: it.submittedAt || new Date().toISOString() },
  },
}}];
"""},
    },
    {
        "id": "n-start", "name": "Start render job", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [660, 0], "credentials": RENDER_CRED,
        "parameters": {
            "method": "POST", "url": "={{ $json.render_url }}/jobs",
            "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
            "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.body) }}",
            "options": {"timeout": 30000},
        },
    },
    # ---------- Path 2: render service reports back ----------
    {
        "id": "n-done", "name": "Draft finished", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": "ingles-reel-done", "credentials": RENDER_CRED,
        "parameters": {"httpMethod": "POST", "path": DONE_PATH, "authentication": "headerAuth",
                       "responseMode": "onReceived", "options": {}},
    },
    {
        "id": "n-if", "name": "Did it work?", "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [220, 300],
        "parameters": {"conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
            "combinator": "and",
            "conditions": [{"id": "c1", "leftValue": "={{ $json.body.status }}", "rightValue": "done",
                            "operator": {"type": "string", "operation": "equals"}}]},
            "options": {}},
    },
    {
        "id": "n-mail-ok", "name": "Email: draft ready", "type": "n8n-nodes-base.gmail", "typeVersion": 2.1,
        "position": [460, 200],
        "parameters": {
            "sendTo": "={{ $json.body.meta.notify_email }}",
            "subject": "=Reel draft ready – {{ $json.body.script_title || $json.body.job_id }}",
            "emailType": "text",
            "message": "=Your draft reel is ready ({{ $json.body.duration }}s).\n\nWatch / download: {{ $json.body.draft_url }}\nInstagram cover: {{ $json.body.cover_url }}\nWhat was cut and why: {{ $json.body.report_url }}\nRaw video transcript: {{ $json.body.transcript_raw_url }}\nReel transcript: {{ $json.body.transcript_reel_url }}\nSubtitles (.srt): {{ $json.body.srt_url }}\n\nSaved on the render PC: {{ $json.body.local_path }}\nJob: {{ $json.body.job_id }}\n(Links work while the render PC is on.)",
            "options": {"appendAttribution": False},
        },
    },
    {
        "id": "n-mail-fail", "name": "Email: draft failed", "type": "n8n-nodes-base.gmail", "typeVersion": 2.1,
        "position": [460, 400],
        "parameters": {
            "sendTo": "={{ $json.body.meta.notify_email }}",
            "subject": "=Reel draft FAILED – {{ $json.body.script_title || $json.body.job_id }}",
            "emailType": "text",
            "message": "=The reel could not be edited.\n\nReason: {{ $json.body.error }}\n\nJob: {{ $json.body.job_id }}",
            "options": {"appendAttribution": False},
        },
    },
]

connections = {
    "Upload raw video": {"main": [[{"node": "Settings", "type": "main", "index": 0}]]},
    "Settings": {"main": [[{"node": "Read the links", "type": "main", "index": 0}]]},
    "Read the links": {"main": [[{"node": "Start render job", "type": "main", "index": 0}]]},
    "Draft finished": {"main": [[{"node": "Did it work?", "type": "main", "index": 0}]]},
    "Did it work?": {"main": [[{"node": "Email: draft ready", "type": "main", "index": 0}],
                               [{"node": "Email: draft failed", "type": "main", "index": 0}]]},
}

wf = {"name": "Inglés Ahorita – Reel Editor", "nodes": nodes, "connections": connections,
      "settings": {"executionOrder": "v1"}}

if ID_FILE.exists():
    wid = ID_FILE.read_text().strip()
    live = requests.get(f"{BASE}/api/v1/workflows/{wid}", headers=H, timeout=60).json()
    by_name = {n["name"]: n for n in live.get("nodes", [])}
    for n in nodes:
        old = by_name.get(n["name"], {})
        if old.get("credentials"):
            n["credentials"] = {**n.get("credentials", {}), **old["credentials"]}
        if "disabled" in old:
            n["disabled"] = old["disabled"]
        if n["name"] == "Settings" and "--tunnel" not in sys.argv:  # keep the live tunnel URL
            n["parameters"] = old.get("parameters", n["parameters"])
    r = requests.put(f"{BASE}/api/v1/workflows/{wid}", headers=H, data=json.dumps(wf), timeout=60)
else:
    r = requests.post(f"{BASE}/api/v1/workflows", headers=H, data=json.dumps(wf), timeout=60)
print(r.status_code, r.text[:600])
r.raise_for_status()
wid = r.json()["id"]
ID_FILE.write_text(wid)
if live_active := requests.get(f"{BASE}/api/v1/workflows/{wid}", headers=H, timeout=60).json().get("active"):
    a = requests.post(f"{BASE}/api/v1/workflows/{wid}/activate", headers=H, timeout=60)
    print("republish:", a.status_code, a.text[:200] if a.status_code != 200 else "ok")
print("workflow:", f"{BASE}/workflow/{wid}")
