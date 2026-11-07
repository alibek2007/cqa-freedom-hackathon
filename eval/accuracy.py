"""Compare a results.json with the reference labels in eval/gold_dev.txt.

  python eval/accuracy.py results/dev/results.json [--details]

Labels: Y/N/- (- = N/A) or a scale step; "a|b" means a is preferred and b is also acceptable
(the checklist wording is ambiguous for that case). "strict" counts only the preferred value.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

IDS = ["contact", "discovery", "identification", "consultation", "empathy", "dialogue", "closure", "critical"]


def parse(tok):
    out = []
    for t in tok.split("|"):
        out.append({"Y": True, "N": False, "-": None}[t] if t in ("Y", "N", "-") else int(t))
    return out


def load_gold(path=Path(__file__).with_name("gold_dev.txt")):
    gold = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cid, rest = [x.strip() for x in line.split("|", 1)]
        vals = rest.split()
        assert len(vals) == 8, line
        gold[cid] = {k: parse(v) for k, v in zip(IDS, vals)}
    return gold


def show(v):
    return {True: "Y", False: "N", None: "N/A"}.get(v, v) if not isinstance(v, int) or isinstance(v, bool) else str(v)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    res = {r["conversation_id"]: r for r in json.load(open(sys.argv[1], encoding="utf-8"))["results"]}
    gold = load_gold()
    if "--only-present" in sys.argv:
        gold = {k: v for k, v in gold.items() if k in res}
    strict, lenient, n = defaultdict(int), defaultdict(int), defaultdict(int)
    by_lang = defaultdict(lambda: [0, 0])
    misses = []
    for cid, g in gold.items():
        r = res.get(cid)
        if not r or r["status"] != "ok":
            misses.append(f"{cid}: no result")
            continue
        vals = {c["criterion_id"]: c["value"] for c in r["criteria"]}
        lang = cid.split("-")[1].upper()
        for k in IDS:
            v, acc = vals.get(k, "missing"), g[k]
            n[k] += 1
            s, l = (v == acc[0] and type(v) is type(acc[0])), any(v == a and type(v) is type(a) for a in acc)
            strict[k] += s
            lenient[k] += l
            by_lang[lang][0] += l
            by_lang[lang][1] += 1
            if not l:
                rat = next((c["rationale"] for c in r["criteria"] if c["criterion_id"] == k), "")
                misses.append(f"{cid:10} {k:14} model={show(v):4} ref={'/'.join(show(a) for a in acc):6} | {rat[:150]}")
    tot_s, tot_l, tot = sum(strict.values()), sum(lenient.values()), sum(n.values())
    print(f"Dialogs compared: {len(gold)}   overall agreement: {tot_l / tot:.1%} (strict {tot_s / tot:.1%})\n")
    print(f"{'criterion':15}{'agree':>8}{'strict':>8}")
    for k in IDS:
        print(f"{k:15}{lenient[k] / n[k]:>8.0%}{strict[k] / n[k]:>8.0%}")
    print("\nby language: " + ", ".join(f"{l} {a / b:.0%}" for l, (a, b) in sorted(by_lang.items())))
    if "--details" in sys.argv:
        print(f"\nDisagreements ({len(misses)}):")
        print("\n".join(misses))


if __name__ == "__main__":
    main()
