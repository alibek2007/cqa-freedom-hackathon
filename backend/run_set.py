"""One command for the control set: fetch -> evaluate -> retry failures -> export -> validate.

  python -m backend.run_set --group <group_id> --out results/prod/results.json --expect 10
  python -m backend.run_set --group DEV --out results/dev/results.json            (DEV)
  python -m backend.run_set --group DEV --out results/dev/results.json --export-only

Works without the web server (uses the same database). Add --push to commit and push results.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import app as cqa  # loads .env, defines ingest/evaluation helpers
from . import db, evaluator, export


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, help="dataset group_id (DEV or the control id)")
    ap.add_argument("--out", required=True, help="path of results.json")
    ap.add_argument("--checklist", type=int, help="checklist version id (default: active/standard 8 criteria)")
    ap.add_argument("--expect", type=int, help="expected number of dialogs (10 for prod)")
    ap.add_argument("--no-fetch", action="store_true", help="use dialogs already in the database")
    ap.add_argument("--force", action="store_true", help="re-evaluate dialogs that already have results")
    ap.add_argument("--export-only", action="store_true", help="only write results.json from stored evaluations")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--per-call", type=int, default=None, help="dialogs per model request (default CQA_DIALOGS_PER_CALL)")
    ap.add_argument("--team", help="team_name for the file (or CQA_TEAM_NAME in .env)")
    ap.add_argument("--push", action="store_true", help="git add, commit and push the results file")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time()
    cqa._seed()

    if not (a.no_fetch or a.export_only):
        print(f"[fetch] group {a.group} ...", flush=True)
        r = cqa.load_dataset(a.group)
        print(f"[fetch] {r['loaded']} dialogs", flush=True)
    dialogs = db.list_dialogs(a.group)
    if not dialogs:
        print("No dialogs for this group", file=sys.stderr)
        return 2
    cl = export.pick_checklist(a.checklist)
    print(f"[checklist] v{cl['id']} {cl['name']}", flush=True)

    errors: dict[str, str] = {}
    if not a.export_only:
        todo = [d["conversation_id"] for d in dialogs]
        workers = a.workers or cqa._pool._max_workers
        per_call = a.per_call or evaluator.dialogs_per_call()
        for rnd in (1, 2, 3):   # failed dialogs get two more attempts
            if not todo:
                break
            chunks = [todo[i:i + per_call] for i in range(0, len(todo), per_call)]
            print(f"[evaluate] round {rnd}: {len(todo)} dialogs in {len(chunks)} requests "
                  f"({per_call} per request), {workers} parallel", flush=True)
            failed = []
            with ThreadPoolExecutor(workers) as ex:
                futs = {ex.submit(cqa.run_evaluations, ch, cl, a.force and rnd == 1): ch for ch in chunks}
                for f in as_completed(futs):
                    try:
                        results = f.result()
                    except Exception as e:
                        results = {cid: e for cid in futs[f]}
                    for cid in futs[f]:
                        r = results.get(cid, RuntimeError("no result"))
                        if isinstance(r, Exception):
                            msg = getattr(r, "detail", None) or str(r)
                            errors[cid] = f"Не удалось получить ответ модели: {msg}"[:300]
                            failed.append(cid)
                            print(f"  FAIL {cid}  {msg[:120]}", flush=True)
                        else:
                            errors.pop(cid, None)
                            print(f"  ok   {cid}  {r['final']['score']['total']}  ({r['model']}, {int(time.time()-t0)}s)", flush=True)
            todo = failed

    doc = export.build(a.group, cl["id"], a.team, errors)
    problems = export.validate(doc, [d["conversation_id"] for d in dialogs], a.expect)
    path = export.write(doc, a.out)
    ok = sum(r["status"] == "ok" for r in doc["results"])
    print(f"[export] {path}: {len(doc['results'])} records, {ok} ok, {len(doc['results']) - ok} error; "
          f"model: {doc['model']['name']}; {int(time.time()-t0)}s total", flush=True)
    if problems:
        print("[validate] PROBLEMS:\n  - " + "\n  - ".join(problems), flush=True)
    else:
        print("[validate] OK", flush=True)

    if a.push:
        subprocess.run(["git", "add", str(path)], check=True)
        subprocess.run(["git", "commit", "-m", f"Results for {a.group}: {ok}/{len(doc['results'])} evaluated"], check=False)
        subprocess.run(["git", "push"], check=True)
        print("[git] pushed", flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
