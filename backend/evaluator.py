"""Turns a dialog + checklist into per-criterion answers with evidence.

The LLM only classifies and cites; all points/totals come from scoring.py.
Providers:
  anthropic  - Claude via the official SDK, structured JSON output (real evaluation)
  heuristic  - offline keyword rules for the seed checklist (dev/demo without an API key)
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from . import scoring

# USD per 1M tokens (input, output) — Anthropic list prices
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}

SPEAKER_RU = {"client": "клиент", "operator": "оператор", "bot": "бот", "system": "система"}


# ---------------------------------------------------------------- transcript
def _parse_ts(ts: str):
    if re.fullmatch(r"\d{1,3}:\d{2}", ts or ""):
        m, s = ts.split(":")
        return int(m) * 60 + int(s), None
    try:
        t = ts.replace("Z", "+00:00")
        # trim fractional seconds to 6 digits for fromisoformat
        t = re.sub(r"(\.\d{6})\d+", r"\1", t)
        return None, datetime.fromisoformat(t)
    except Exception:
        return None, None


def prepare_transcript(dialog: dict) -> list[dict]:
    """Adds 1-based turn numbers and offset_s (seconds from start; used for audio seek)."""
    turns = []
    t0 = None
    for i, t in enumerate(dialog.get("transcript", []), start=1):
        sec, dt = _parse_ts(t.get("timestamp", ""))
        if dt is not None:
            t0 = t0 or dt
            sec = (dt - t0).total_seconds()
        turns.append({**t, "n": i, "offset_s": round(sec, 1) if sec is not None else None})
    return turns


def render_transcript(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        ts = t["timestamp"] if len(t["timestamp"]) <= 8 else f"+{int(t['offset_s'] or 0)}s"
        lines.append(f"[#{t['n']} | {ts} | {t['speaker']}] {t['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- prompt
SYSTEM = """You are a senior quality-assurance analyst at Freedom Broker (brokerage, Kazakhstan).
You grade how the human OPERATOR handled a customer conversation, strictly by the checklist you are given.

Rules:
- Grade only the operator. Bot and system messages are context (e.g. what the client already said to the bot, when the chat was handed over, when it was closed). A greeting by the bot does NOT count as the operator's greeting; "first response" means the operator's first message.
- Conversations are in Russian, Kazakh or English. Expect the operator to use the client's language. Voice-call transcripts come from speech recognition and may contain distorted words — judge meaning, not spelling.
- For each criterion choose exactly one allowed answer:
  binary: "yes" or "no"; flag: "yes" (violation found) or "no"; scale: one of the listed step values as a string, e.g. "15"; "na" only when the criterion allows N/A AND its N/A condition is met.
- "no" means the action was needed but the operator skipped it or did it wrong. "na" means the criterion does not apply by its condition. If unsure between "no" and "na", re-read the N/A condition; if it is not clearly met, answer "no".
- evidence_turns: the turn numbers (#) that prove your answer. For "yes" cite the operator turn(s). For a missed action leave evidence empty if nothing proves it and instead give checked_from/checked_to = the range of turns where the action was expected. For "na" cite the turn(s) showing why it does not apply, if any. Otherwise set checked_from/checked_to to 0.
- explanation: 1–2 sentences IN RUSSIAN, concrete, in the style: «Завершение — Нет. После ответа оператор закрыл чат без прощания. Основание — #14 и системное сообщение #15.»
- na_reason: in Russian, only for "na" (otherwise empty string).
- Return one item for every criterion id in the checklist, no extra ids."""


def checklist_for_prompt(checklist: dict) -> str:
    sec = {s["id"]: s["title"] for s in checklist.get("sections", [])}
    out = []
    for c in checklist["criteria"]:
        line = [f"- id: {c['id']}  ({c.get('code', '')} {c['title']}; section: {sec.get(c.get('section'), '-')})",
                f"  type: {c['type']}; weight: {c.get('weight', 0)}; N/A allowed: {'yes' if c.get('allow_na') else 'no'}"]
        if c.get("allow_na") and c.get("na_condition"):
            line.append(f"  N/A condition: {c['na_condition']}")
        line.append(f"  rule: {c['description']}")
        if c["type"] == "scale":
            for s in sorted(c["scale"], key=lambda s: -s["value"]):
                line.append(f"    {s['value']}: {s['label']}")
        out.append("\n".join(line))
    return "\n".join(out)


def output_schema(checklist: dict) -> dict:
    ids = [c["id"] for c in checklist["criteria"]]
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion_id": {"type": "string", "enum": ids},
                        "answer": {"type": "string"},
                        "explanation": {"type": "string"},
                        "na_reason": {"type": "string"},
                        "evidence_turns": {"type": "array", "items": {"type": "integer"}},
                        "checked_from": {"type": "integer"},
                        "checked_to": {"type": "integer"},
                    },
                    "required": ["criterion_id", "answer", "explanation", "na_reason",
                                 "evidence_turns", "checked_from", "checked_to"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "items"],
        "additionalProperties": False,
    }


def user_message(dialog: dict, turns: list[dict], checklist: dict) -> str:
    return (f"CHECKLIST\n{checklist_for_prompt(checklist)}\n\n"
            f"CONVERSATION {dialog['conversation_id']} (channel: {dialog.get('channel')}, "
            f"language: {dialog.get('language')}, {len(turns)} turns)\n{render_transcript(turns)}")


# ---------------------------------------------------------------- validation
def normalize_items(raw_items: list[dict], checklist: dict, n_turns: int) -> tuple[dict, list[str]]:
    """Returns ({criterion_id: item}, errors)."""
    byid = {c["id"]: c for c in checklist["criteria"]}
    items, errors = {}, []
    for it in raw_items:
        cid = it.get("criterion_id")
        c = byid.get(cid)
        if not c:
            continue
        a = str(it.get("answer", "")).strip().lower()
        a = {"n/a": "na", "да": "yes", "нет": "no"}.get(a, a)
        if c["type"] == "scale" and a != "na":
            try:
                a = int(float(a))
            except ValueError:
                pass
        err = scoring.validate_answer(c, a)
        if err:
            errors.append(f"{cid}: {err}")
            continue
        ev = [n for n in it.get("evidence_turns", []) if isinstance(n, int) and 1 <= n <= n_turns]
        cf, ct = it.get("checked_from") or 0, it.get("checked_to") or 0
        rng = [max(1, cf), min(n_turns, ct)] if cf and ct and cf <= ct else None
        items[cid] = {
            "criterion_id": cid, "answer": a,
            "explanation": it.get("explanation", "").strip(),
            "na_reason": it.get("na_reason", "").strip() if a == "na" else "",
            "evidence_turns": ev, "checked_range": rng,
        }
    for cid in byid:
        if cid not in items and not any(e.startswith(cid + ":") for e in errors):
            errors.append(f"{cid}: missing")
    return items, errors


# ---------------------------------------------------------------- providers
class EvaluationError(RuntimeError):
    pass


def _anthropic_call(system: str, messages: list, schema: dict, model: str, effort: str):
    import anthropic
    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=messages,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            # if a safety classifier declines, the server retries on a fallback model
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise EvaluationError("Anthropic API key is missing or invalid (set ANTHROPIC_API_KEY)") from e
    except anthropic.RateLimitError as e:
        raise EvaluationError("Rate limited by the API, retry later") from e
    except anthropic.APIStatusError as e:
        raise EvaluationError(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise EvaluationError(f"Cannot reach the API: {e}") from e
    if resp.stop_reason == "refusal":
        raise EvaluationError("The model declined to evaluate this conversation")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise EvaluationError(f"Model returned invalid JSON (stop_reason={resp.stop_reason})") from e
    return data, resp.usage.input_tokens, resp.usage.output_tokens, resp.model


def evaluate_llm(dialog: dict, checklist: dict, model: str, effort: str) -> dict:
    turns = prepare_transcript(dialog)
    schema = output_schema(checklist)
    messages = [{"role": "user", "content": user_message(dialog, turns, checklist)}]
    tin = tout = 0
    items, errors, summary, used_model = {}, [], "", model
    for attempt in range(2):
        data, i, o, used_model = _anthropic_call(SYSTEM, messages, schema, model, effort)
        tin, tout = tin + i, tout + o
        new_items, errors = normalize_items(data.get("items", []), checklist, len(turns))
        items.update(new_items)
        summary = data.get("summary", summary)
        errors = [e for e in errors if e.split(":")[0] not in items]
        if not errors:
            break
        messages += [{"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                     {"role": "user", "content": "Fix these problems and return the full JSON again:\n- "
                      + "\n- ".join(errors)}]
    if errors:
        raise EvaluationError("Invalid answers after retry: " + "; ".join(errors))
    pin, pout = PRICES.get(model, (0, 0))
    return {"items": items, "summary": summary, "model": used_model, "provider": "anthropic",
            "tokens_in": tin, "tokens_out": tout,
            "cost_usd": round(tin / 1e6 * pin + tout / 1e6 * pout, 5)}


# --- offline heuristic: keyword rules for the seed checklist only -----------------
GREET = r"здравствуй|добрый (день|вечер|утро)|сәлем|қайырлы|hello|hi\b|good (morning|afternoon|evening)"
INTRO = r"меня зовут|менің атым|my name is|this is \w+|на связи|с вами"
BYE = r"до свидания|всего доброго|хорошего дня|сау болыңыз|goodbye|have a (nice|great) day|bye"
MORE = r"чем[- ]?(то|нибудь)? ещё|могу (ли )?помочь|тағы|anything else|other questions|ещё вопрос"
IDENT = r"идентифик|верифик|кодов(ое|ое слово)|подтвердите (личность|номер|данные)|сәйкестендір|verify|verification|security question"
EMPATHY = r"извин|сожале|понимаю|кешіріңіз|түсінемін|sorry|apolog|understand"
WAIT = r"минут|подождите|ожида|күте тұрыңыз|bear with|one moment|please wait"
GROSS = r"переведите (мне|на мою)|лично мне|мой (личный )?номер|дура|идиот|stupid|my personal (card|account|number)"


def _first(turns, speaker, pattern=None):
    for t in turns:
        if t["speaker"] == speaker and (pattern is None or re.search(pattern, t["text"], re.I)):
            return t
    return None


def evaluate_heuristic(dialog: dict, checklist: dict) -> dict:
    turns = prepare_transcript(dialog)
    ops = [t for t in turns if t["speaker"] == "operator"]
    n = len(turns)
    rng = [ops[0]["n"], ops[-1]["n"]] if ops else [1, n]
    out = {}

    def put(cid, answer, expl, ev=(), checked=None, na=""):
        out[cid] = {"criterion_id": cid, "answer": answer, "explanation": "[эвристика] " + expl,
                    "na_reason": na, "evidence_turns": list(ev), "checked_range": checked}

    for c in checklist["criteria"]:
        cid, code = c["id"], c.get("code")
        if not ops:
            if c.get("allow_na"):
                put(cid, "na", "Оператор не участвовал в диалоге.", na="Оператор не подключился.")
            elif c["type"] == "scale":
                put(cid, min(s["value"] for s in c["scale"]), "Оператор не участвовал в диалоге.", checked=[1, n])
            else:
                put(cid, "no", "Оператор не участвовал в диалоге.", checked=[1, n])
            continue
        f = ops[0]
        if code == "01":
            ok = re.search(GREET, f["text"], re.I) and re.search(INTRO, f["text"], re.I)
            put(cid, "yes" if ok else "no",
                "Первая реплика оператора содержит приветствие и имя." if ok else
                "В первой реплике оператора нет приветствия и/или представления.", [f["n"]], None if ok else [f["n"], f["n"]])
        elif code == "02":
            q = next((t for t in ops if "?" in t["text"]), None)
            if q:
                put(cid, "yes", "Оператор задал уточняющий вопрос.", [q["n"]])
            else:
                put(cid, "na", "Уточняющих вопросов не было; запрос был понятен.", na="Данных для ответа хватало с начала.")
        elif code == "03":
            t = _first(turns, "operator", IDENT)
            if t:
                put(cid, "yes", "Оператор провёл проверку личности.", [t["n"]])
            else:
                put(cid, "na", "Признаков защищённого действия не найдено.", na="Запрос не требовал идентификации.")
        elif code == "04":
            longest = max(ops, key=lambda t: len(t["text"]))
            v = 15 if len(longest["text"]) > 60 else 10
            put(cid, v, "Оценка по наличию развёрнутого ответа оператора.", [longest["n"]])
        elif code == "05":
            t = _first(turns, "operator", EMPATHY)
            put(cid, "yes", "Оператор вежлив" + (" и отреагировал на проблему." if t else "."), [t["n"] if t else f["n"]])
        elif code == "06":
            put(cid, 10 if len(ops) <= 6 else 7, "Оценка по количеству и связности реплик оператора.", [o["n"] for o in ops[:3]])
        elif code == "07":
            last = ops[-1]
            ok = re.search(BYE, last["text"], re.I) or re.search(MORE, last["text"], re.I)
            put(cid, "yes" if ok else "no",
                "Оператор попрощался/предложил помощь." if ok else "Последняя реплика оператора без прощания.",
                [last["n"]], None if ok else [last["n"], n])
        elif code == "08":
            t = _first(turns, "operator", GROSS)
            put(cid, "yes" if t else "no",
                "Найдена реплика с признаком грубого нарушения." if t else "Признаков грубых нарушений не найдено.",
                [t["n"]] if t else [], None if t else rng)
        else:
            if c.get("allow_na"):
                put(cid, "na", "Эвристический режим не умеет оценивать новый критерий — нужен LLM.",
                    na="Нет LLM: критерий не оценён.")
            elif c["type"] == "scale":
                put(cid, max(s["value"] for s in c["scale"]), "Эвристический режим: требуется LLM.", checked=rng)
            else:
                put(cid, "no", "Эвристический режим не умеет оценивать новый критерий — нужен LLM.", checked=rng)
    return {"items": out, "summary": "Оценка выполнена офлайн-эвристикой (без LLM), только для отладки.",
            "model": "heuristic", "provider": "heuristic", "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}


def evaluate(dialog: dict, checklist: dict) -> dict:
    provider = os.getenv("CQA_PROVIDER", "anthropic").lower()
    if provider == "heuristic":
        res = evaluate_heuristic(dialog, checklist)
    else:
        res = evaluate_llm(dialog, checklist, os.getenv("CQA_MODEL", "claude-opus-5-5"),
                           os.getenv("CQA_EFFORT", "medium"))
    answers = {k: v["answer"] for k, v in res["items"].items()}
    res["score"] = scoring.compute(checklist, answers)
    return res
