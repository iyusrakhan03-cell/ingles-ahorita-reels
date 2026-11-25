import { callBackend } from "./_lib.js";

export default async function handler(req, res) {
  const r = await callBackend("/health", { headers: {} });
  res.json({ online: !r.offline && r.status === 200 });
}
