"""Export evaluations in the organizers' results format (JSON 1.0, appendix to the student guide).

results/dev/results.json  - DEV set
results/prod/results.json - control set
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import db

# checklist code -> criterion_id required by the results format
CODE_TO_EXPORT = {"01": "contact", "02": "discovery", "03": "identification", "04": "consultation",
                  "05": "empathy", "06": "dialogue", "07": "closure", "08": "critical"}
EXPORT_IDS = list(CODE_TO_EXPORT.values())
# allowed values per criterion (null = N/A)
ALLOWED = {
    "contact": {True, False}, "discovery": {True, False, None}, "identification": {True, False, None},
    "consultation": {0, 5, 10, 15, 20, None}, "empathy": {True, False}, "dialogue": {0, 4, 7, 10},
    "closure": {True, False, None}, "critical": {True, False},
}


def export_id(criterion: dict) -> str | None:
    return criterion.get("export_id") or CODE_TO_EXPORT.get(str(criterion.get("code", "")).zfill(2))


def covers_format(checklist: dict) -> bool:
    return sorted(filter(None, (export_id(c) for c in checklist["criteria"]))) == sorted(EXPORT_IDS)


def pick_checklist(checklist_id: int | None = None) -> dict:
    """The requested checklist, else the active one if it has all 8 format criteria,
    else the newest checklist that does."""
    if checklist_id:
        cl = db.get_checklist(checklist_id)
        if not cl:
            raise ValueError(f"Checklist {checklist_id} not found")
        return cl
    active = db.active_checklist()
    if active and covers_format(active):
        return active
    for cl in db.list_checklists():
        if covers_format(cl):
            return cl
    raise ValueError("No checklist contains all 8 criteria of the results format")


def _value(criterion: dict, answer):
    if answer == "na":
        return None
    if criterion["type"] == "scale":
        return int(answer)
    return answer == "yes"


def result_record(dialog: dict, checklist: dict, ev: dict | None, error: str | None = None) -> dict:
    cid = dialog["conversation_id"]
    if not ev:
        return {"conversation_id": cid, "status": "error", "criteria": [], "score": None,
                "error": error or "Диалог не был оценён."}
    items = ev["final"]["items"]
    criteria = []
    for c in checklist["criteria"]:
        xid = export_id(c)
        if not xid or c["id"] not in items:
            continue
        it = items[c["id"]]
        rationale = it.get("explanation") or ""
        if it["answer"] == "na" and it.get("na_reason"):
            rationale = f"{rationale} N/A: {it['na_reason']}".strip()
        ev_turns = list(it.get("evidence_turns") or [])
        if not ev_turns and it.get("checked_range"):  # missed action: point to the checked range
            a, b = it["checked_range"]
            ev_turns = list(range(a, b + 1))[:20]
        criteria.append({"criterion_id": xid, "value": _value(c, it["answer"]),
                         "rationale": rationale, "evidence_turns": ev_turns})
    order = {x: i for i, x in enumerate(EXPORT_IDS)}
    criteria.sort(key=lambda x: order[x["criterion_id"]])
    score = ev["final"]["score"]
    return {"conversation_id": cid, "status": "ok", "criteria": criteria,
            "score": {"final_score": score["total"], "critical_flag": bool(score["gross_error"])}}


def build(group_id: str | None, checklist_id: int | None = None, team_name: str | None = None,
          errors: dict[str, str] | None = None) -> dict:
    cl = pick_checklist(checklist_id)
    evs = db.latest_evaluations_map(cl["id"])
    dialogs = db.list_dialogs(group_id)
    results, models = [], []
    for d in dialogs:
        ev = evs.get(d["conversation_id"])
        if ev and ev.get("model") and ev["model"] not in models:
            models.append(ev["model"])
        results.append(result_record(d, cl, ev, (errors or {}).get(d["conversation_id"])))
    provider = os.getenv("CQA_PROVIDER", "anthropic").lower()
    mode = "local" if provider == "heuristic" or "localhost" in os.getenv("CQA_BASE_URL", "") else "external_api"
    return {
        "schema_version": "1.0",
        "team_name": team_name or os.getenv("CQA_TEAM_NAME") or "CQA Team",
        "model": {"mode": mode, "name": ", ".join(models) or os.getenv("CQA_MODEL") or provider},
        "results": results,
    }


def validate(doc: dict, expected_ids: list[str] | None = None, expected_count: int | None = None) -> list[str]:
    """Checks the file against the appendix rules. Returns a list of problems (empty = OK)."""
    p = []
    if doc.get("schema_version") != "1.0":
        p.append("schema_version must be '1.0'")
    if not doc.get("team_name"):
        p.append("team_name is empty")
    m = doc.get("model") or {}
    if m.get("mode") not in ("local", "external_api", "hybrid") or not m.get("name"):
        p.append("model.mode / model.name invalid")
    res = doc.get("results")
    if not isinstance(res, list):
        return p + ["results must be a list"]
    ids = [r.get("conversation_id") for r in res]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        p.append(f"duplicate conversation_id: {sorted(dup)}")
    if expected_ids is not None:
        missing = set(expected_ids) - set(ids)
        extra = set(ids) - set(expected_ids)
        if missing:
            p.append(f"missing dialogs: {sorted(missing)}")
        if extra:
            p.append(f"unexpected dialogs: {sorted(extra)}")
    if expected_count is not None and len(res) != expected_count:
        p.append(f"expected {expected_count} records, got {len(res)}")
    for r in res:
        cid = r.get("conversation_id")
        if r.get("status") == "error":
            if r.get("criteria") != [] or r.get("score") is not None or not r.get("error"):
                p.append(f"{cid}: error record needs criteria=[], score=null and an error message")
            continue
        if r.get("status") != "ok":
            p.append(f"{cid}: status must be ok or error")
            continue
        crit = r.get("criteria") or []
        got = [c.get("criterion_id") for c in crit]
        if sorted(got) != sorted(EXPORT_IDS):
            p.append(f"{cid}: criteria must be exactly {EXPORT_IDS}, got {got}")
        for c in crit:
            v = c.get("value")
            xid = c.get("criterion_id")
            allowed = ALLOWED.get(xid, set())
            numeric = xid in ("consultation", "dialogue")
            if v is None:
                ok = None in allowed
            elif numeric:
                ok = isinstance(v, int) and not isinstance(v, bool) and v in allowed
            else:
                ok = isinstance(v, bool)
            if not ok:
                p.append(f"{cid}/{c.get('criterion_id')}: invalid value {v!r}")
            if not isinstance(c.get("rationale"), str) or not c.get("rationale"):
                p.append(f"{cid}/{c.get('criterion_id')}: rationale is empty")
            if not isinstance(c.get("evidence_turns"), list) or not all(
                    isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in c["evidence_turns"]):
                p.append(f"{cid}/{c.get('criterion_id')}: evidence_turns must be a list of turn numbers >= 1")
        s = r.get("score") or {}
        fs = s.get("final_score")
        if fs is not None and not (isinstance(fs, (int, float)) and 0 <= fs <= 100 and round(fs, 2) == fs):
            p.append(f"{cid}: final_score must be 0..100 with 2 decimals")
        crit_v = next((c.get("value") for c in crit if c.get("criterion_id") == "critical"), None)
        if s.get("critical_flag") is not crit_v:
            p.append(f"{cid}: critical_flag must equal the 'critical' value")
    return p


def write(doc: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
