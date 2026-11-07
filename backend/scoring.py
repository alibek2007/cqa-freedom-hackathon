"""Deterministic scoring engine (rules from the checklist PDF, page 2).

1. N/A only where allowed.
2. Yes = full weight, No = 0; scale -> one of the listed steps.
3. Scale points are proportional to weight; the top step gives the full weight.
4. N/A criteria are excluded and their weight is redistributed proportionally,
   so the maximum stays 100.
5. Total is rounded to 2 decimals; with no applicable weighted criteria it is None.
   Gross errors (type "flag") are reported separately and never change the total.
"""
from __future__ import annotations

NA = "na"


def raw_ratio(criterion: dict, answer) -> float | None:
    """Share of the criterion's weight earned (0..1), or None for N/A."""
    if answer == NA:
        return None
    ctype = criterion["type"]
    if ctype == "binary":
        return 1.0 if answer == "yes" else 0.0
    if ctype == "scale":
        top = max(s["value"] for s in criterion["scale"])
        return float(answer) / top if top else 0.0
    return 0.0  # flag: no points


def validate_answer(criterion: dict, answer) -> str | None:
    """Return an error message if the answer is not allowed for this criterion."""
    if answer == NA:
        return None if criterion.get("allow_na") else "N/A is not allowed for this criterion"
    ctype = criterion["type"]
    if ctype in ("binary", "flag"):
        return None if answer in ("yes", "no") else f"expected yes/no, got {answer!r}"
    if ctype == "scale":
        steps = {s["value"] for s in criterion["scale"]}
        try:
            v = int(answer)
        except (TypeError, ValueError):
            return f"expected one of {sorted(steps)}, got {answer!r}"
        return None if v in steps else f"expected one of {sorted(steps)}, got {answer!r}"
    return f"unknown criterion type {ctype!r}"


def _aggregate(criteria: list[dict], answers: dict) -> tuple[float | None, float, float]:
    applicable_w = 0.0
    earned = 0.0
    for c in criteria:
        if c["type"] == "flag" or not c.get("weight"):
            continue
        r = raw_ratio(c, answers.get(c["id"], NA))
        if r is None:
            continue
        applicable_w += c["weight"]
        earned += r * c["weight"]
    if applicable_w == 0:
        return None, 0.0, 0.0
    return round(earned * 100.0 / applicable_w, 2), earned, applicable_w


def compute(checklist: dict, answers: dict) -> dict:
    """answers: {criterion_id: "yes"|"no"|"na"|int}. Returns per-item points and totals."""
    criteria = checklist["criteria"]
    total, _, applicable_w = _aggregate(criteria, answers)

    items = {}
    for c in criteria:
        a = answers.get(c["id"], NA)
        r = raw_ratio(c, a)
        if c["type"] == "flag" or not c.get("weight"):
            items[c["id"]] = {"points": None, "max_points": None, "effective_weight": None}
            continue
        if r is None or not applicable_w:
            items[c["id"]] = {"points": None, "max_points": 0.0, "effective_weight": 0.0}
            continue
        eff = c["weight"] * 100.0 / applicable_w
        items[c["id"]] = {
            "points": round(r * eff, 2),
            "max_points": round(eff, 2),
            "effective_weight": round(eff, 2),
        }

    sections = []
    for s in checklist.get("sections", []):
        sc = [c for c in criteria if c.get("section") == s["id"]]
        if not sc or all(c["type"] == "flag" for c in sc):
            continue
        pct, _, w = _aggregate(sc, answers)
        pts = sum((items[c["id"]]["points"] or 0) for c in sc)
        mx = sum((items[c["id"]]["max_points"] or 0) for c in sc)
        sections.append({
            "id": s["id"], "title": s["title"],
            "percent": pct,                       # section score on its own 0..100 scale
            "points": round(pts, 2), "max_points": round(mx, 2),  # contribution to total
        })

    gross = [c["id"] for c in criteria if c["type"] == "flag" and answers.get(c["id"]) == "yes"]
    return {"total": total, "items": items, "sections": sections,
            "gross_error": bool(gross), "gross_criteria": gross}
