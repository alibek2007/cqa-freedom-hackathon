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


class _Resp:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status_code, self._p, self.headers = status, payload, headers or {}
        self.text = text or json.dumps(payload or {})

    def json(self):
        return self._p


def test_openai_compatible_falls_back_and_retries(monkeypatch):
    import time

    import httpx
    from backend import evaluator
    monkeypatch.setenv("CQA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.delenv("CQA_BASE_URL", raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    evaluator._json_mode_ok.clear()
    ok = {"choices": [{"message": {"content": '```json\n{"summary": "s", "items": []}\n```'}}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": "gemini-3.8-flash"}
    seq = [_Resp(400, text="json_schema not supported"), _Resp(429, text="quota"), _Resp(200, ok)]
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return seq.pop(0)
    monkeypatch.setattr(httpx, "post", fake_post)
    data, tin, tout, model = evaluator._openai_call("sys", [{"role": "user", "content": "x"}], {}, "gemini-3.8-flash", "low")
    assert data == {"summary": "s", "items": []} and (tin, tout) == (10, 5)
    assert sent[0]["response_format"]["type"] == "json_schema"
    assert sent[1]["response_format"]["type"] == "json_object" and sent[2] == sent[1]
    assert evaluator._json_mode_ok[evaluator.GEMINI_BASE] == "json_object"
    assert sent[0]["reasoning_effort"] == "low"


def test_export_record_matches_appendix_format():
    from backend import export
    cl = {**CL, "criteria": [dict(c) for c in CL["criteria"]]}
    items = {"c01": "yes", "c02": "na", "c03": "na", "c04": 15, "c05": "yes", "c06": 7, "c07": "yes", "c08": "no"}
    full = {k: {"answer": v, "explanation": "e", "na_reason": "n" if v == "na" else "",
                "evidence_turns": [2], "checked_range": None} for k, v in items.items()}
    score = scoring.compute(cl, items)
    ev = {"model": "m", "final": {"items": full, "score": score}}
    rec = export.result_record({"conversation_id": "x-1"}, cl, ev)
    vals = {c["criterion_id"]: c["value"] for c in rec["criteria"]}
    assert vals == {"contact": True, "discovery": None, "identification": None, "consultation": 15,
                    "empathy": True, "dialogue": 7, "closure": True, "critical": False}
    assert [c["criterion_id"] for c in rec["criteria"]] == export.EXPORT_IDS
    assert rec["score"] == {"final_score": score["total"], "critical_flag": False}
    err = export.result_record({"conversation_id": "x-2"}, cl, None, "timeout")
    doc = {"schema_version": "1.0", "team_name": "T", "model": {"mode": "external_api", "name": "m"},
           "results": [rec, err]}
    assert export.validate(doc, ["x-1", "x-2"], 2) == []
    bad = json.loads(json.dumps(doc))
    bad["results"][0]["criteria"][5]["value"] = 5          # dialogue has no step 5
    bad["results"][0]["criteria"][0]["value"] = 1          # contact must be boolean
    bad["results"][0]["score"]["critical_flag"] = True
    probs = export.validate(bad, ["x-1", "x-2", "x-3"], 3)
    assert any("dialogue" in p for p in probs) and any("contact" in p for p in probs)
    assert any("critical_flag" in p for p in probs) and any("missing" in p for p in probs)


def test_daily_quota_switches_to_next_model(monkeypatch):
    import time

    import httpx
    from backend import evaluator
    monkeypatch.setenv("CQA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("CQA_FALLBACK_MODELS", "m2")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    evaluator._json_mode_ok.clear(); evaluator._cooldown.clear()
    quota = ('[{"error": {"code": 429, "details": [{"violations": [{"quotaId": '
             '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}, {"retryDelay": "3600s"}]}}]')
    ok = {"choices": [{"message": {"content": '{"summary": "", "items": []}'}}], "usage": {}}
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append(json["model"])
        return _Resp(429, text=quota) if json["model"] == "m1" else _Resp(200, ok)
    monkeypatch.setattr(httpx, "post", fake_post)
    evaluator._openai_call("s", [], {}, "m1", "")
    assert seen == ["m1", "m2"] and evaluator._cooldown["m1"] > time.time() + 3000
    seen.clear()
    evaluator._openai_call("s", [], {}, "m1", "")   # m1 is skipped while its quota is used up
    assert seen == ["m2"]


def test_evaluate_many_one_request_and_fallback(monkeypatch):
    from backend import evaluator
    monkeypatch.setenv("CQA_PROVIDER", "gemini")
    mk = lambda ans: [{"criterion_id": k, "answer": v, "explanation": "e", "na_reason": "",
                       "evidence_turns": [1], "checked_from": 0, "checked_to": 0} for k, v in ans.items()]
    good = {c["id"]: ("yes" if c["type"] != "scale" else str(max(s["value"] for s in c["scale"]))) for c in CL["criteria"]}
    good["c08"] = "no"
    dlg = lambda cid: {"conversation_id": cid, "transcript": [{"timestamp": "00:01", "speaker": "operator", "text": "Hi"}]}
    calls = []

    def fake(system, messages, schema, model, effort):
        calls.append(schema)
        if "dialogs" in schema["properties"]:   # batch: second dialog has an invalid answer
            return {"dialogs": [{"conversation_id": "a", "summary": "", "items": mk(good)},
                                {"conversation_id": "b", "summary": "", "items": mk(dict(good, c01="na"))}]}, 3000, 900, model
        return {"summary": "", "items": mk(good)}, 1000, 300, model
    out = evaluator.evaluate_many([dlg("a"), dlg("b")], CL, "m", "low", call=fake)
    assert len(calls) == 2 and "dialogs" in calls[0]["properties"]       # 1 batch + 1 single retry
    assert out["a"]["batched"] == 2 and out["a"]["score"]["total"] == 100.0
    assert "batched" not in out["b"] and out["b"]["items"]["c01"]["answer"] == "yes"
