"""CQA backend: REST API + static frontend.

Run:  python -m uvicorn backend.app:app --port 8000
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent


def _load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

from . import db, evaluator, scoring  # noqa: E402

app = FastAPI(title="CQA — Conversation Quality Analytics")
# the UI may be hosted elsewhere (e.g. as a Dynamics 365 web resource)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _seed():
    if not db.list_checklists():
        seed = json.loads((ROOT / "backend" / "seed_checklist.json").read_text(encoding="utf-8"))
        db.save_checklist(seed, activate=True)
    if not db.list_dialogs():
        local = ROOT / "data" / "dev.json"
        if local.exists():
            _ingest(json.loads(local.read_text(encoding="utf-8-sig")), "DEV")


# ------------------------------------------------------------------ helpers
def _ingest(payload, group_id: str) -> int:
    if isinstance(payload, dict) and "data" in payload:
        if payload.get("isSuccess") is False:
            raise HTTPException(502, f"Dataset API error: {payload.get('error')}")
        group_id = payload["data"].get("dataset_group_id") or group_id
        records = payload["data"].get("records", [])
    elif isinstance(payload, dict) and "records" in payload:
        records = payload["records"]
    elif isinstance(payload, dict) and "transcript" in payload:
        records = [payload]
    elif isinstance(payload, list):
        records = payload
    else:
        raise HTTPException(400, "Unrecognized format: expected API response, {records:[...]}, a record or a list")
    n = 0
    for r in records:
        if not r.get("conversation_id") or not isinstance(r.get("transcript"), list):
            continue
        db.upsert_dialog(r, group_id)
        n += 1
    return n


def _checklist(cid: int | None) -> dict:
    cl = db.get_checklist(cid) if cid else db.active_checklist()
    if not cl:
        raise HTTPException(404, "Checklist not found")
    return cl


def _summary(ev: dict | None) -> dict | None:
    if not ev:
        return None
    return {"id": ev["id"], "total": ev["final"]["score"]["total"],
            "original_total": ev["original"]["score"]["total"],
            "gross_error": ev["final"]["score"]["gross_error"], "status": ev.get("status"),
            "appealed": bool(ev.get("decisions")) or bool(db.list_appeals(evaluation_id=ev["id"]))}


def run_evaluation(conversation_id: str, checklist: dict, force: bool = False) -> dict:
    if not force:
        prev = db.latest_evaluation(conversation_id, checklist["id"])
        if prev:
            return prev
    dialog = db.get_dialog(conversation_id)
    if not dialog:
        raise HTTPException(404, f"Dialog {conversation_id} not found")
    res = evaluator.evaluate(dialog, checklist)
    body = {
        "conversation_id": conversation_id, "checklist_id": checklist["id"],
        "checklist_name": checklist.get("name"),
        "model": res["model"], "provider": res["provider"],
        "tokens_in": res["tokens_in"], "tokens_out": res["tokens_out"], "cost_usd": res["cost_usd"],
        "summary": res["summary"],
        "original": {"items": res["items"], "score": res["score"]},
        "final": {"items": res["items"], "score": res["score"]},
        "decisions": [], "status": "evaluated",
    }
    body["id"] = db.save_evaluation(body)
    return db.get_evaluation(body["id"])


# ------------------------------------------------------------------ config & datasets
@app.get("/api/config")
def config():
    return {"provider": os.getenv("CQA_PROVIDER", "anthropic"),
            "model": os.getenv("CQA_MODEL", "claude-opus-5-5"),
            "effort": os.getenv("CQA_EFFORT", "medium"),
            "groups": db.list_groups(),
            "dataset_api": bool(os.getenv("CQA_DATASET_URL"))}


@app.post("/api/datasets/{group_id}/load")
def load_dataset(group_id: str):
    tpl = os.getenv("CQA_DATASET_URL")
    if not tpl:
        raise HTTPException(400, "CQA_DATASET_URL is not set in .env")
    url = tpl.replace("{group_id}", group_id)
    try:
        r = httpx.get(url, timeout=120)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch dataset: {e}")
    cache = ROOT / "data" / f"{group_id}.json"
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {"group_id": group_id, "loaded": _ingest(payload, group_id)}


@app.post("/api/dialogs/upload")
async def upload_dialogs(file: UploadFile, group_id: str = "UPLOAD"):
    try:
        payload = json.loads((await file.read()).decode("utf-8-sig"))
    except Exception:
        raise HTTPException(400, "File must be JSON")
    return {"group_id": group_id, "loaded": _ingest(payload, group_id)}


@app.get("/api/dialogs")
def dialogs(group_id: str | None = None, checklist_id: int | None = None):
    cl = _checklist(checklist_id)
    evs = db.latest_evaluations_map(cl["id"])
    out = []
    for d in db.list_dialogs(group_id):
        t = d["transcript"]
        out.append({"conversation_id": d["conversation_id"], "group_id": d["group_id"],
                    "channel": d.get("channel"), "language": d.get("language"),
                    "turns": len(t), "has_audio": bool(d.get("audio_url")),
                    "preview": next((x["text"] for x in t if x["speaker"] == "client"), "")[:120],
                    "evaluation": _summary(evs.get(d["conversation_id"]))})
    return {"checklist_id": cl["id"], "dialogs": out}


@app.get("/api/dialogs/{cid}")
def dialog(cid: str, checklist_id: int | None = None):
    d = db.get_dialog(cid)
    if not d:
        raise HTTPException(404, "Dialog not found")
    cl = _checklist(checklist_id)
    d["turns"] = evaluator.prepare_transcript(d)
    d.pop("transcript", None)
    ev = db.latest_evaluation(cid, cl["id"])
    return {"dialog": d, "checklist": cl, "evaluation": ev,
            "appeals": db.list_appeals(evaluation_id=ev["id"]) if ev else []}


_AUDIO_SRC: dict[str, tuple[float, str]] = {}  # conversation_id -> (fetched_at, direct media url)


def _media_url(cid: str, page_url: str, refresh: bool = False) -> str:
    """audio_url is a small player page; the real file is a short-lived signed link in <source src>."""
    import html
    import re
    import time
    hit = _AUDIO_SRC.get(cid)
    if hit and not refresh and time.time() - hit[0] < 30 * 60:
        return hit[1]
    r = httpx.get(page_url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    if r.headers.get("content-type", "").startswith("audio"):
        url = page_url
    else:
        m = re.search(r'<source[^>]+src="([^"]+)"', r.text) or re.search(r'<audio[^>]+src="([^"]+)"', r.text)
        if not m:
            raise HTTPException(502, "No audio link found in the recording page")
        url = html.unescape(m.group(1))
    _AUDIO_SRC[cid] = (time.time(), url)
    return url


@app.get("/api/audio/{cid}")
def audio(cid: str, request: Request):
    """Streams the call recording through the backend (avoids CORS, refreshes expiring links, supports seeking)."""
    d = db.get_dialog(cid)
    if not d or not d.get("audio_url"):
        raise HTTPException(404, "No recording for this dialog")
    fwd = {"Range": request.headers["range"]} if "range" in request.headers else {}
    client = httpx.Client(timeout=120, follow_redirects=True)
    r = None
    for refresh in (False, True):
        try:
            url = _media_url(cid, d["audio_url"], refresh)
        except httpx.HTTPError as e:
            client.close()
            raise HTTPException(502, f"Recording page unavailable: {e}")
        r = client.send(client.build_request("GET", url, headers=fwd), stream=True)
        if r.status_code < 400:
            break
        r.close()  # signed link probably expired -> refetch the page once
    if r.status_code >= 400:
        client.close()
        raise HTTPException(502, f"Recording source returned {r.status_code}")
    headers = {k: v for k, v in r.headers.items()
               if k.lower() in ("content-length", "content-range", "accept-ranges")}
    headers.setdefault("Accept-Ranges", "bytes")

    def gen():
        try:
            yield from r.iter_bytes(64 * 1024)
        finally:
            r.close(); client.close()
    return StreamingResponse(gen(), status_code=r.status_code, headers=headers,
                             media_type=r.headers.get("content-type", "audio/mpeg"))


# ------------------------------------------------------------------ checklists
def _validate_checklist(body: dict) -> dict:
    crit = body.get("criteria") or []
    if not crit:
        raise HTTPException(400, "Checklist must have at least one criterion")
    ids = set()
    for c in crit:
        if not c.get("id"):
            c["id"] = "c_" + uuid.uuid4().hex[:6]
        if c["id"] in ids:
            raise HTTPException(400, f"Duplicate criterion id {c['id']}")
        ids.add(c["id"])
        if c.get("type") not in ("binary", "scale", "flag"):
            raise HTTPException(400, f"{c.get('title')}: type must be binary, scale or flag")
        if not c.get("title") or not c.get("description"):
            raise HTTPException(400, "Every criterion needs a title and a description")
        c["weight"] = 0 if c["type"] == "flag" else float(c.get("weight") or 0)
        if c["type"] != "flag" and c["weight"] <= 0:
            raise HTTPException(400, f"{c['title']}: weight must be > 0")
        if c["type"] == "scale":
            steps = c.get("scale") or []
            if len(steps) < 2 or len({int(s["value"]) for s in steps}) != len(steps):
                raise HTTPException(400, f"{c['title']}: scale needs at least 2 distinct steps")
            c["scale"] = sorted([{"value": int(s["value"]), "label": s.get("label", "")} for s in steps],
                                key=lambda s: -s["value"])
        else:
            c["scale"] = []
        if c["type"] == "flag":
            c["allow_na"] = False
        c.setdefault("na_condition", "")
    secs = body.get("sections") or []
    sec_ids = {s["id"] for s in secs}
    for c in crit:
        if c.get("section") and c["section"] not in sec_ids:
            secs.append({"id": c["section"], "title": c["section"]}); sec_ids.add(c["section"])
    body["sections"] = secs
    for k in ("id", "created_at", "active"):
        body.pop(k, None)
    return body


@app.get("/api/checklists")
def checklists():
    return db.list_checklists()


@app.get("/api/checklists/{cid}")
def checklist(cid: int):
    return _checklist(cid)


@app.post("/api/checklists")
def save_checklist(body: dict = Body(...)):
    """Saves a NEW version (checklists are immutable so old evaluations stay reproducible)."""
    body = _validate_checklist(body)
    cid = db.save_checklist(body, activate=True)
    return db.get_checklist(cid)


@app.post("/api/checklists/{cid}/activate")
def activate(cid: int):
    _checklist(cid)
    db.activate_checklist(cid)
    return {"ok": True}


# ------------------------------------------------------------------ evaluation
@app.post("/api/evaluate")
def evaluate(body: dict = Body(...)):
    cl = _checklist(body.get("checklist_id"))
    try:
        return run_evaluation(body["conversation_id"], cl, force=bool(body.get("force")))
    except evaluator.EvaluationError as e:
        raise HTTPException(502, str(e))


JOBS: dict[str, dict] = {}
_pool = ThreadPoolExecutor(max_workers=int(os.getenv("CQA_CONCURRENCY", "6")))


@app.post("/api/evaluate/batch")
def evaluate_batch(body: dict = Body(...)):
    cl = _checklist(body.get("checklist_id"))
    ids = body.get("conversation_ids") or [d["conversation_id"] for d in db.list_dialogs(body.get("group_id"))]
    force = bool(body.get("force"))
    job = {"id": uuid.uuid4().hex[:8], "total": len(ids), "done": 0, "failed": [], "running": True,
           "checklist_id": cl["id"], "cost_usd": 0.0}
    JOBS[job["id"]] = job
    lock = threading.Lock()

    def one(cid):
        try:
            ev = run_evaluation(cid, cl, force)
            with lock:
                job["cost_usd"] = round(job["cost_usd"] + (ev.get("cost_usd") or 0), 4)
        except Exception as e:  # keep going; failures are listed and can be retried
            with lock:
                job["failed"].append({"conversation_id": cid, "error": getattr(e, "detail", str(e))})
        finally:
            with lock:
                job["done"] += 1
                job["running"] = job["done"] < job["total"]

    if not ids:
        job["running"] = False
    for cid in ids:
        _pool.submit(one, cid)
    return job


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    if jid not in JOBS:
        raise HTTPException(404, "Job not found")
    return JOBS[jid]


# ------------------------------------------------------------------ appeals
@app.post("/api/appeals")
def create_appeal(body: dict = Body(...)):
    ev = db.get_evaluation(int(body["evaluation_id"]))
    if not ev:
        raise HTTPException(404, "Evaluation not found")
    if body.get("criterion_id") not in ev["final"]["items"]:
        raise HTTPException(400, "Unknown criterion")
    if not (body.get("comment") or "").strip():
        raise HTTPException(400, "Comment is required")
    if any(a["criterion_id"] == body["criterion_id"] and a["status"] == "open"
           for a in db.list_appeals(evaluation_id=ev["id"])):
        raise HTTPException(409, "There is already an open appeal for this criterion")
    aid = db.create_appeal(ev["id"], body["criterion_id"], body["comment"].strip())
    ev["status"] = "appealed"
    db.update_evaluation(ev["id"], ev)
    return db.get_appeal(aid)


@app.get("/api/appeals")
def appeals(status: str | None = None):
    out = []
    for a in db.list_appeals(status=status):
        ev = db.get_evaluation(a["evaluation_id"])
        cl = db.get_checklist(ev["checklist_id"]) if ev else None
        crit = next((c for c in (cl or {}).get("criteria", []) if c["id"] == a["criterion_id"]), None)
        out.append({**a, "conversation_id": ev and ev["conversation_id"],
                    "checklist_id": ev and ev["checklist_id"],
                    "criterion_title": crit and f"{crit.get('code', '')} {crit['title']}".strip(),
                    "item": ev and ev["final"]["items"].get(a["criterion_id"]),
                    "total": ev and ev["final"]["score"]["total"]})
    return out


@app.post("/api/appeals/{aid}/decision")
def decide(aid: int, body: dict = Body(...)):
    ap = db.get_appeal(aid)
    if not ap:
        raise HTTPException(404, "Appeal not found")
    if ap["status"] != "open":
        raise HTTPException(409, "Appeal already resolved")
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "Reason is required")
    ev = db.get_evaluation(ap["evaluation_id"])
    cl = db.get_checklist(ev["checklist_id"])
    crit = next(c for c in cl["criteria"] if c["id"] == ap["criterion_id"])
    item = dict(ev["final"]["items"][ap["criterion_id"]])
    old = item["answer"]
    action = body.get("action")
    if action == "change":
        new = body.get("new_answer")
        if crit["type"] == "scale" and new != "na":
            new = int(new)
        err = scoring.validate_answer(crit, new)
        if err:
            raise HTTPException(400, err)
        item.update(answer=new, reviewed=True,
                    explanation=f"Изменено проверяющим: {reason}",
                    na_reason=reason if new == "na" else "")
    elif action == "confirm":
        new = old
        item["reviewed"] = True
    else:
        raise HTTPException(400, "action must be confirm or change")

    before = ev["final"]["score"]["total"]
    ev["final"]["items"][ap["criterion_id"]] = item
    answers = {k: v["answer"] for k, v in ev["final"]["items"].items()}
    ev["final"]["score"] = scoring.compute(cl, answers)
    decision = {"action": action, "old_answer": old, "new_answer": new, "reason": reason,
                "reviewer": body.get("reviewer") or "Проверяющий",
                "total_before": before, "total_after": ev["final"]["score"]["total"]}
    ev["decisions"].append({**decision, "appeal_id": aid, "criterion_id": ap["criterion_id"], "at": db.now()})
    db.decide_appeal(aid, decision)
    ev["status"] = "appealed" if db.list_appeals(status="open", evaluation_id=ev["id"]) else "reviewed"
    db.update_evaluation(ev["id"], ev)
    return {"appeal": db.get_appeal(aid), "evaluation": db.get_evaluation(ev["id"])}


# ------------------------------------------------------------------ static UI
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="ui")
