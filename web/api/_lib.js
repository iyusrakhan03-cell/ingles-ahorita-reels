import crypto from "node:crypto";

function same(a, b) {
  const x = Buffer.from(String(a || "")), y = Buffer.from(String(b || ""));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}

export function checkPassword(req, res) {
  if (process.env.REQUIRE_PASSWORD !== "true") return true;  // open by default; set REQUIRE_PASSWORD=true to lock it
  if (!process.env.APP_PASSWORD || !same(req.headers["x-app-password"], process.env.APP_PASSWORD)) {
    res.status(401).json({ error: "Wrong password" });
    return false;
  }
  return true;
}

// BACKEND_URL is rewritten by start_editor.py each time the laptop's tunnel starts (the URL changes).
export async function backendUrl() {
  return (process.env.BACKEND_URL || "").replace(/\/+$/, "") || null;
}

export async function callBackend(path, init = {}) {
  const base = await backendUrl();
  if (!base) return { offline: true };
  try {
    const r = await fetch(base + path, {
      ...init,
      headers: { "content-type": "application/json", "x-api-key": process.env.REEL_API_KEY, ...(init.headers || {}) },
      signal: AbortSignal.timeout(15000),
    });
    const text = await r.text();
    let body; try { body = JSON.parse(text); } catch { body = { error: text.slice(0, 300) }; }
    return { base, status: r.status, body };
  } catch {
    return { offline: true, base };
  }
}

// Output links point at the tunnel the job was made on; rewrite them to the current tunnel.
export function withCurrentLinks(job, base) {
  const out = { ...job };
  for (const k of Object.keys(out)) {
    if (k.endsWith("_url") && typeof out[k] === "string" && out[k].includes("/d/") && k !== "callback_url") {
      out[k] = base + out[k].slice(out[k].indexOf("/d/"));
    }
  }
  delete out.public_base; delete out.callback_url;
  return out;
}

