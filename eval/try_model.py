"""Score selected DEV dialogs with the seed checklist and a chosen model, without touching the database.

  python -m eval.try_model --model gemma-4-26b-a4b-it --ids p1-ru-003 p1-ru-005 ...  [--out eval/runs/x.json]

Writes a results.json-style file and prints agreement with eval/gold_dev.txt for those dialogs.
Use it to test prompt/rule changes on the cases that went wrong before re-scoring everything.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend import app as _app  # noqa: E402,F401  (loads .env)
from backend import evaluator, export  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--ids", nargs="+", required=True)
ap.add_argument("--out", default=None)
ap.add_argument("--workers", type=int, default=2)
a = ap.parse_args()
sys.stdout.reconfigure(encoding="utf-8")
os.environ["CQA_MODEL"] = a.model
os.environ["CQA_FALLBACK_MODELS"] = ""          # measure this model only
cl = json.loads((ROOT / "backend" / "seed_checklist.json").read_text(encoding="utf-8"))
dev = {r["conversation_id"]: r for r in
       json.loads((ROOT / "data" / "dev.json").read_text(encoding="utf-8-sig"))["data"]["records"]}


def one(cid):
    t = time.time()
    try:
        res = evaluator.evaluate(dev[cid], cl)
        ev = {"final": {"items": res["items"], "score": res["score"]}}
        rec = export.result_record({"conversation_id": cid}, cl, ev)
        print(f"  ok   {cid} {res['score']['total']} ({res['model']}, {time.time() - t:.0f}s, "
              f"{res['tokens_in']}+{res['tokens_out']} tok)", flush=True)
        return rec
    except Exception as e:
        print(f"  FAIL {cid}: {str(e)[:150]}", flush=True)
        return export.result_record({"conversation_id": cid}, cl, None, str(e)[:200])


with ThreadPoolExecutor(a.workers) as ex:
    recs = list(ex.map(one, a.ids))
out = Path(a.out or ROOT / "eval" / "runs" / f"{a.model}.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"schema_version": "1.0", "team_name": "eval", "model": {"mode": "external_api", "name": a.model},
                           "results": recs}, ensure_ascii=False, indent=2), encoding="utf-8")
import subprocess  # noqa: E402
subprocess.run([sys.executable, "-I", str(ROOT / "eval" / "accuracy.py"), str(out), "--details", "--only-present"])
