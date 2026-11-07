"""Evaluate selected DEV chats with ONE model and a given checklist, without touching the database.
Used to A/B-test prompt / rule changes against eval/dev_reference.json.

  python -m eval.experiment --model gemma-4-26b-a4b-it --checklist backend/seed_checklist.json \
      --ids p1-ru-003 p1-ru-005 --out eval/runs/new.json
  python -m eval.compare eval/runs/new.json --subset
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import app as _app  # noqa: F401  (loads .env)
from backend import evaluator, export

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--checklist", default=str(ROOT / "backend" / "seed_checklist.json"))
    ap.add_argument("--ids", nargs="*", help="conversation ids (default: all 60 reference chats)")
    ap.add_argument("--ids-from", help="file with one id per line")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--per-call", type=int, default=5, help="dialogs per model request")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    os.environ["CQA_FALLBACK_MODELS"] = ""          # one model only: no silent switching
    cl = json.loads(Path(a.checklist).read_text(encoding="utf-8"))
    for i, c in enumerate(cl["criteria"]):
        c.setdefault("id", f"c{i}")
    ref = json.loads((ROOT / "eval" / "dev_reference.json").read_text(encoding="utf-8"))
    ids = a.ids or (Path(a.ids_from).read_text().split() if a.ids_from else list(ref["dialogs"]))
    dev = {r["conversation_id"]: r for r in
           json.loads((ROOT / "data" / "dev.json").read_text(encoding="utf-8-sig"))["data"]["records"]}

    def chunk(cids):
        t = time.time()
        res = evaluator.evaluate_many([dev[c] for c in cids], cl, a.model, os.getenv("CQA_EFFORT", "low"),
                                      call=evaluator._openai_call)
        recs = []
        for cid in cids:
            r = res[cid]
            if isinstance(r, Exception):
                print(f"  FAIL {cid} {str(r)[:150]}", flush=True)
                recs.append(export.result_record({"conversation_id": cid}, cl, None, str(r)[:300]))
            else:
                ev = {"model": r["model"], "final": {"items": r["items"], "score": r["score"]}}
                print(f"  ok   {cid} {r['score']['total']} ({int(time.time() - t)}s, batched={r.get('batched', 1)})", flush=True)
                recs.append(export.result_record({"conversation_id": cid}, cl, ev))
        return recs

    chunks = [ids[i:i + a.per_call] for i in range(0, len(ids), a.per_call)]
    with ThreadPoolExecutor(a.workers) as ex:
        results = [r for part in ex.map(chunk, chunks) for r in part]
    doc = {"schema_version": "1.0", "team_name": "experiment", "model": {"mode": "external_api", "name": a.model},
           "checklist": a.checklist, "results": results}
    export.write(doc, a.out)
    print(f"written {a.out}")


if __name__ == "__main__":
    main()
