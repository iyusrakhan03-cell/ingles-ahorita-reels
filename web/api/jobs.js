import { checkPassword, callBackend, withCurrentLinks } from "./_lib.js";

function driveId(link) {
  const s = String(link || "").trim();
  const m = s.match(/\/d\/([A-Za-z0-9_-]{20,})/) || s.match(/[?&]id=([A-Za-z0-9_-]{20,})/) || s.match(/^([A-Za-z0-9_-]{20,})$/);
  return m ? m[1] : null;
}

export default async function handler(req, res) {
  if (!checkPassword(req, res)) return;

  if (req.method === "POST") {
    const video = driveId(req.body?.video);
    if (!video) return res.status(400).json({ error: "That doesn't look like a Google Drive file link." });
    const scriptLink = String(req.body?.script || "").trim();
    const doc = scriptLink ? driveId(scriptLink) : process.env.DEFAULT_SCRIPT_DOC_ID;
    if (!doc) return res.status(400).json({ error: "That doesn't look like a Google Doc link." });
    const section = String(req.body?.section || "").trim();
    const r = await callBackend("/jobs", {
      method: "POST",
      body: JSON.stringify({ drive_file_id: video, script_doc_id: doc, script_section: section || "auto",
                             meta: { source: "web" } }),
    });
    if (r.offline) return res.status(503).json({ error: "The editing computer is offline." });
    return res.status(r.status).json(r.body);
  }

  const id = req.query.id ? `/${encodeURIComponent(req.query.id)}` : "";
  const r = await callBackend(`/jobs${id}`);
  if (r.offline) return res.status(503).json({ error: "The editing computer is offline." });
  if (r.status !== 200) return res.status(r.status).json(r.body);
  const stale = (j) => (j.status === "failed" && Date.now() / 1000 - (j.created_at || 0) > 2 * 86400)
                    || (j.status === "done" && !j.draft_url);
  const body = Array.isArray(r.body) ? r.body.filter((j) => !stale(j)).map((j) => withCurrentLinks(j, r.base))
                                     : withCurrentLinks(r.body, r.base);
  res.setHeader("cache-control", "no-store");
  res.json(body);
}
