// Thin REST client. apiBase is configurable so the UI can run inside Dynamics 365.
const base = () => (window.CQA_CONFIG && window.CQA_CONFIG.apiBase) || "";

async function req(method, path, body, isForm) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) opts.body = body;
    else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  }
  const r = await fetch(base() + path, opts);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
  return data;
}

export const api = {
  config: () => req("GET", "/api/config"),
  loadGroup: (g) => req("POST", `/api/datasets/${encodeURIComponent(g)}/load`),
  upload: (file, group) => { const f = new FormData(); f.append("file", file); return req("POST", `/api/dialogs/upload?group_id=${encodeURIComponent(group)}`, f, true); },
  dialogs: (group, cl) => req("GET", `/api/dialogs?${new URLSearchParams({ ...(group ? { group_id: group } : {}), ...(cl ? { checklist_id: cl } : {}) })}`),
  dialog: (id, cl) => req("GET", `/api/dialogs/${encodeURIComponent(id)}${cl ? `?checklist_id=${cl}` : ""}`),
  audioUrl: (id) => `${base()}/api/audio/${encodeURIComponent(id)}`,
  checklists: () => req("GET", "/api/checklists"),
  saveChecklist: (b) => req("POST", "/api/checklists", b),
  activate: (id) => req("POST", `/api/checklists/${id}/activate`),
  evaluate: (conversation_id, checklist_id, force) => req("POST", "/api/evaluate", { conversation_id, checklist_id, force }),
  batch: (body) => req("POST", "/api/evaluate/batch", body),
  job: (id) => req("GET", `/api/jobs/${id}`),
  appeals: (status) => req("GET", `/api/appeals${status ? `?status=${status}` : ""}`),
  appeal: (evaluation_id, criterion_id, comment) => req("POST", "/api/appeals", { evaluation_id, criterion_id, comment }),
  decide: (id, body) => req("POST", `/api/appeals/${id}/decision`, body),
};
