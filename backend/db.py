"""SQLite storage. Documents are stored as JSON blobs; one file, zero setup."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cqa.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS dialogs (
  conversation_id TEXT PRIMARY KEY, group_id TEXT, channel TEXT, language TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS checklists (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, body TEXT, created_at TEXT, active INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, checklist_id INTEGER,
  body TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_eval_conv ON evaluations(conversation_id, checklist_id);
CREATE TABLE IF NOT EXISTS appeals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id INTEGER, criterion_id TEXT,
  comment TEXT, status TEXT, decision TEXT, created_at TEXT, decided_at TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_conn = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
        _conn.executescript(SCHEMA)
    return _conn


def execute(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur


def query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


# ---------- dialogs ----------
def upsert_dialog(d: dict, group_id: str) -> None:
    execute("INSERT OR REPLACE INTO dialogs VALUES (?,?,?,?,?)",
            (d["conversation_id"], group_id, d.get("channel"), d.get("language"),
             json.dumps(d, ensure_ascii=False)))


def get_dialog(cid: str) -> dict | None:
    r = query("SELECT body, group_id FROM dialogs WHERE conversation_id=?", (cid,))
    if not r:
        return None
    d = json.loads(r[0]["body"])
    d["group_id"] = r[0]["group_id"]
    return d


def list_dialogs(group_id: str | None = None) -> list[dict]:
    sql = "SELECT body, group_id FROM dialogs"
    rows = query(sql + " WHERE group_id=?", (group_id,)) if group_id else query(sql)
    out = []
    for r in rows:
        d = json.loads(r["body"])
        d["group_id"] = r["group_id"]
        out.append(d)
    return sorted(out, key=lambda d: d["conversation_id"])


def list_groups() -> list[str]:
    return [r[0] for r in query("SELECT DISTINCT group_id FROM dialogs ORDER BY group_id")]


# ---------- checklists ----------
def save_checklist(body: dict, activate: bool = True) -> int:
    cur = execute("INSERT INTO checklists(name, body, created_at, active) VALUES (?,?,?,0)",
                  (body.get("name", "Checklist"), json.dumps(body, ensure_ascii=False), now()))
    cid = cur.lastrowid
    if activate:
        execute("UPDATE checklists SET active = (id = ?)", (cid,))
    return cid


def _cl(r) -> dict:
    b = json.loads(r["body"])
    b.update(id=r["id"], created_at=r["created_at"], active=bool(r["active"]))
    return b


def get_checklist(cid: int) -> dict | None:
    r = query("SELECT * FROM checklists WHERE id=?", (cid,))
    return _cl(r[0]) if r else None


def active_checklist() -> dict | None:
    r = query("SELECT * FROM checklists WHERE active=1 ORDER BY id DESC LIMIT 1")
    return _cl(r[0]) if r else None


def list_checklists() -> list[dict]:
    return [_cl(r) for r in query("SELECT * FROM checklists ORDER BY id DESC")]


def activate_checklist(cid: int) -> None:
    execute("UPDATE checklists SET active = (id = ?)", (cid,))


# ---------- evaluations ----------
def save_evaluation(body: dict) -> int:
    cur = execute("INSERT INTO evaluations(conversation_id, checklist_id, body, created_at) VALUES (?,?,?,?)",
                  (body["conversation_id"], body["checklist_id"], json.dumps(body, ensure_ascii=False), now()))
    return cur.lastrowid


def update_evaluation(eid: int, body: dict) -> None:
    execute("UPDATE evaluations SET body=? WHERE id=?", (json.dumps(body, ensure_ascii=False), eid))


def _ev(r) -> dict:
    b = json.loads(r["body"])
    b.update(id=r["id"], created_at=r["created_at"])
    return b


def get_evaluation(eid: int) -> dict | None:
    r = query("SELECT * FROM evaluations WHERE id=?", (eid,))
    return _ev(r[0]) if r else None


def latest_evaluation(cid: str, checklist_id: int | None = None) -> dict | None:
    if checklist_id:
        r = query("SELECT * FROM evaluations WHERE conversation_id=? AND checklist_id=? ORDER BY id DESC LIMIT 1",
                  (cid, checklist_id))
    else:
        r = query("SELECT * FROM evaluations WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (cid,))
    return _ev(r[0]) if r else None


def latest_evaluations_map(checklist_id: int) -> dict[str, dict]:
    rows = query("""SELECT e.* FROM evaluations e JOIN (
                      SELECT conversation_id, MAX(id) mid FROM evaluations WHERE checklist_id=? GROUP BY conversation_id
                    ) m ON e.id = m.mid""", (checklist_id,))
    return {r["conversation_id"]: _ev(r) for r in rows}


# ---------- appeals ----------
def _ap(r) -> dict:
    return {"id": r["id"], "evaluation_id": r["evaluation_id"], "criterion_id": r["criterion_id"],
            "comment": r["comment"], "status": r["status"],
            "decision": json.loads(r["decision"]) if r["decision"] else None,
            "created_at": r["created_at"], "decided_at": r["decided_at"]}


def create_appeal(eid: int, criterion_id: str, comment: str) -> int:
    cur = execute("INSERT INTO appeals(evaluation_id, criterion_id, comment, status, created_at) VALUES (?,?,?,?,?)",
                  (eid, criterion_id, comment, "open", now()))
    return cur.lastrowid


def get_appeal(aid: int) -> dict | None:
    r = query("SELECT * FROM appeals WHERE id=?", (aid,))
    return _ap(r[0]) if r else None


def list_appeals(status: str | None = None, evaluation_id: int | None = None) -> list[dict]:
    sql, args = "SELECT * FROM appeals WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    if evaluation_id:
        sql += " AND evaluation_id=?"; args.append(evaluation_id)
    return [_ap(r) for r in query(sql + " ORDER BY id DESC", tuple(args))]


def decide_appeal(aid: int, decision: dict) -> None:
    execute("UPDATE appeals SET status='resolved', decision=?, decided_at=? WHERE id=?",
            (json.dumps(decision, ensure_ascii=False), now(), aid))
