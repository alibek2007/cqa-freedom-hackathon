import json
from pathlib import Path

from backend import scoring
from backend.evaluator import evaluate_heuristic, normalize_items, prepare_transcript

CL = json.loads((Path(__file__).parent.parent / "backend" / "seed_checklist.json").read_text(encoding="utf-8"))
for i, c in enumerate(CL["criteria"]):
    c.setdefault("id", f"c{i}")

ALL_MAX = {"c01": "yes", "c02": "yes", "c03": "yes", "c04": 20, "c05": "yes", "c06": 10, "c07": "yes", "c08": "no"}


def test_all_max_is_100():
    r = scoring.compute(CL, ALL_MAX)
    assert r["total"] == 100.0
    assert r["gross_error"] is False


def test_all_zero():
    a = {"c01": "no", "c02": "no", "c03": "no", "c04": 0, "c05": "no", "c06": 0, "c07": "no", "c08": "no"}
    assert scoring.compute(CL, a)["total"] == 0.0


def test_scale_proportional_to_weight():
    a = dict(ALL_MAX, c04=10)  # half of weight 20 -> lose 10 points
    assert scoring.compute(CL, a)["total"] == 90.0
    a = dict(ALL_MAX, c06=7)  # 7/10 of 15 -> lose 4.5
    assert scoring.compute(CL, a)["total"] == 95.5


def test_na_redistributes_weight():
    # identification N/A (weight 20) -> remaining 80 scaled to 100
    a = dict(ALL_MAX, c03="na", c01="no")  # lose 10 of 80 -> 87.5
    r = scoring.compute(CL, a)
    assert r["total"] == 87.5
    assert r["items"]["c01"]["max_points"] == 12.5
    assert r["items"]["c03"]["points"] is None


def test_rounding_two_decimals():
    a = dict(ALL_MAX, c02="na", c03="na", c06=7)  # applicable 65; earned 65-4.5=60.5 -> 93.0769..
    assert scoring.compute(CL, a)["total"] == 93.08


def test_no_applicable_criteria_gives_none():
    cl = {"sections": [], "criteria": [
        {"id": "a", "type": "binary", "weight": 10, "allow_na": True, "scale": []},
        {"id": "g", "type": "flag", "weight": 0, "allow_na": False, "scale": []}]}
    r = scoring.compute(cl, {"a": "na", "g": "yes"})
    assert r["total"] is None
    assert r["gross_error"] is True


def test_gross_error_does_not_change_total():
    r = scoring.compute(CL, dict(ALL_MAX, c08="yes"))
    assert r["total"] == 100.0 and r["gross_error"] and r["gross_criteria"] == ["c08"]


def test_sections():
    r = scoring.compute(CL, dict(ALL_MAX, c01="no"))
    start = next(s for s in r["sections"] if s["id"] == "start")
    assert start["percent"] == 60.0 and start["points"] == 15.0 and start["max_points"] == 25.0
    assert all(s["id"] != "gross" for s in r["sections"])


def test_validate_answer():
    c01, c04 = CL["criteria"][0], CL["criteria"][3]
    assert scoring.validate_answer(c01, "na")  # N/A not allowed for contact
    assert scoring.validate_answer(c04, 12)    # not a step
    assert scoring.validate_answer(c04, 15) is None
    assert scoring.validate_answer(c04, "na") is None


def test_normalize_items_rejects_bad_and_clamps_turns():
    raw = [{"criterion_id": "c04", "answer": "15", "explanation": "x", "na_reason": "",
            "evidence_turns": [3, 99], "checked_from": 0, "checked_to": 0},
           {"criterion_id": "c01", "answer": "na", "explanation": "x", "na_reason": "",
            "evidence_turns": [], "checked_from": 0, "checked_to": 0}]
    items, errors = normalize_items(raw, CL, n_turns=10)
    assert items["c04"]["answer"] == 15 and items["c04"]["evidence_turns"] == [3]
    assert "c01" not in items and any(e.startswith("c01") for e in errors)


def test_call_offsets_from_iso_timestamps():
    d = {"transcript": [{"timestamp": "2026-11-03T13:10:00.6388245+00:00", "speaker": "system", "text": "a"},
                        {"timestamp": "2026-11-03T13:10:30.6388245+00:00", "speaker": "bot", "text": "b"}]}
    t = prepare_transcript(d)
    assert t[0]["offset_s"] == 0 and t[1]["offset_s"] == 30.0 and t[1]["n"] == 2


def test_heuristic_ignores_bot_greeting():
    d = {"conversation_id": "x", "transcript": [
        {"timestamp": "00:01", "speaker": "bot", "text": "Здравствуйте! Я AIM"},
        {"timestamp": "00:30", "speaker": "operator", "text": "Добрый день. Тариф в разделе Кабинет."}]}
    r = evaluate_heuristic(d, CL)
    assert r["items"]["c01"]["answer"] == "no"


def test_llm_path_retries_invalid_answers(monkeypatch):
    from backend import evaluator
    d = {"conversation_id": "x", "channel": "agent chat", "language": "RU", "transcript": [
        {"timestamp": "00:01", "speaker": "client", "text": "Где тариф?"},
        {"timestamp": "00:30", "speaker": "operator", "text": "Здравствуйте, меня зовут Алина. В Кабинете."}]}
    good = {c["id"]: ("yes" if c["type"] != "scale" else str(max(s["value"] for s in c["scale"]))) for c in CL["criteria"]}
    good["c08"] = "no"
    mk = lambda ans: {"summary": "s", "items": [{"criterion_id": k, "answer": v, "explanation": "e", "na_reason": "",
                                               "evidence_turns": [2], "checked_from": 0, "checked_to": 0} for k, v in ans.items()]}
    calls = []

    def fake(system, messages, schema, model, effort):
        calls.append(messages)
        return (mk(dict(good, c01="na")) if len(calls) == 1 else mk(good)), 1000, 200, model
    monkeypatch.setattr(evaluator, "_anthropic_call", fake)
    r = evaluator.evaluate_llm(d, CL, "claude-opus-5-5", "medium")
    assert len(calls) == 2 and "c01" in calls[1][-1]["content"]
    assert r["items"]["c01"]["answer"] == "yes"
    assert r["cost_usd"] == round(2000 / 1e6 * 4 + 400 / 1e6 * 20, 5)
