"""Compare a results.json against the hand-made reference labels.

  python -m eval.compare results/dev/results.json            # summary
  python -m eval.compare results/dev/results.json --details  # list every disagreement
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REF = Path(__file__).with_name("dev_reference.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--ref", default=str(REF))
    ap.add_argument("--subset", action="store_true", help="only dialogs present in the results file")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    ref = json.loads(Path(a.ref).read_text(encoding="utf-8"))
    res = {r["conversation_id"]: r for r in json.loads(Path(a.results).read_text(encoding="utf-8"))["results"]}
    crits = ref["criteria"]
    hit = defaultdict(int); tot = defaultdict(int)          # per criterion, all labels
    hit_s = defaultdict(int); tot_s = defaultdict(int)      # per criterion, only confident labels
    lang_hit = defaultdict(int); lang_tot = defaultdict(int)
    exact = 0; n = 0; diffs = []
    for cid, d in ref["dialogs"].items():
        if a.subset and cid not in res:
            continue
        r = res.get(cid)
        if not r or r.get("status") != "ok":
            diffs.append((cid, "ALL", "missing/error", "", ""))
            continue
        got = {c["criterion_id"]: c["value"] for c in r["criteria"]}
        rat = {c["criterion_id"]: c.get("rationale", "") for c in r["criteria"]}
        n += 1
        all_ok = True
        lang = cid.split("-")[1].upper()
        for k in crits:
            exp, g = d["labels"][k], got.get(k, "MISSING")
            ok = exp == g and type(exp) is type(g)
            unsure = k in d["uncertain"] or (k == "closure" and d["ambiguous_closure"])
            tot[k] += 1; hit[k] += ok
            lang_tot[lang] += 1; lang_hit[lang] += ok
            if not unsure:
                tot_s[k] += 1; hit_s[k] += ok
            if not ok:
                all_ok = False
                diffs.append((cid, k, exp, g, ("(uncertain) " if unsure else "") + rat.get(k, "")[:160]))
        exact += all_ok
    print(f"Dialogs compared: {n}/{len(ref['dialogs'])}; all 8 criteria correct: {exact}")
    print(f"{'criterion':15} {'all labels':>12} {'confident only':>16}")
    T = H = TS = HS = 0
    for k in crits:
        T += tot[k]; H += hit[k]; TS += tot_s[k]; HS += hit_s[k]
        print(f"{k:15} {hit[k]:>4}/{tot[k]:<3} {100*hit[k]/max(1,tot[k]):5.1f}%  "
              f"{hit_s[k]:>4}/{tot_s[k]:<3} {100*hit_s[k]/max(1,tot_s[k]):5.1f}%")
    print(f"{'TOTAL':15} {H:>4}/{T:<3} {100*H/max(1,T):5.1f}%  {HS:>4}/{TS:<3} {100*HS/max(1,TS):5.1f}%")
    print("by language: " + ", ".join(f"{l} {100*lang_hit[l]/lang_tot[l]:.1f}%" for l in sorted(lang_tot)))
    if a.details:
        print("\nDisagreements (reference -> got):")
        for cid, k, exp, g, why in diffs:
            print(f"  {cid:10} {k:14} {exp!s:>5} -> {g!s:<5} {why}")


if __name__ == "__main__":
    main()
