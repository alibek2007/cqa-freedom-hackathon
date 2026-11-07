"""Turns a dialog + checklist into per-criterion answers with evidence.

The LLM only classifies and cites; all points/totals come from scoring.py.
Providers:
  anthropic  - Claude via the official SDK, structured JSON output (real evaluation)
  heuristic  - offline keyword rules for the seed checklist (dev/demo without an API key)
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

from . import scoring

log = logging.getLogger("cqa")

# USD per 1M tokens (input, output) — Anthropic list prices
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}

def price(model: str) -> tuple[float, float]:
    """USD per 1M tokens; CQA_PRICE_IN / CQA_PRICE_OUT override (free tiers = 0)."""
    if os.getenv("CQA_PRICE_IN") or os.getenv("CQA_PRICE_OUT"):
        return float(os.getenv("CQA_PRICE_IN") or 0), float(os.getenv("CQA_PRICE_OUT") or 0)
    return PRICES.get(model, (0.0, 0.0))


SPEAKER_RU ={"client": "клиент", "operator": "оператор", "bot": "бот", "system": "система"}


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
- The explanation must justify exactly the chosen answer. Example: if you conclude that the client gave enough data from the start, the answer for "need identification" is "na", not "yes".
- Be strict and calibrated like a real QA reviewer: the top scale step only when there is no notable flaw; any unclear, incoherent or contradictory operator phrase is a flaw for dialogue management.
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


# --- OpenAI-compatible chat API: Gemini (default), Groq, OpenRouter, Ollama --------
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
# response_format modes tried in order; the first one the server accepts is remembered
_JSON_MODES = ["json_schema", "json_object", "none"]
_json_mode_ok: dict[str, str] = {}


def _parse_json(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"<thought>.*?</thought>", "", t, flags=re.S).strip()   # Gemma prints its reasoning first
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _openai_settings() -> tuple[str, str]:
    provider = os.getenv("CQA_PROVIDER", "").lower()
    if provider == "gemini":
        return os.getenv("CQA_BASE_URL") or GEMINI_BASE, os.getenv("GEMINI_API_KEY") or os.getenv("CQA_API_KEY", "")
    base = os.getenv("CQA_BASE_URL")
    if not base:
        raise EvaluationError("Set CQA_BASE_URL for the openai-compatible provider")
    return base, os.getenv("CQA_API_KEY", "")


class _Overloaded(Exception):
    """The model can't serve now (overloaded, too slow or its daily quota is used up)."""
    def __init__(self, model: str, cooldown: float = 300, reason: str = "overloaded"):
        super().__init__(model)
        self.cooldown, self.reason = cooldown, reason


_cooldown: dict[str, float] = {}   # model -> time until which it is skipped
_no_effort: set[str] = set()       # models that reject reasoning_effort


def _retry_delay(text: str) -> float | None:
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text)
    return float(m.group(1)) if m else None


def _fallback_models(model: str) -> list[str]:
    env = os.getenv("CQA_FALLBACK_MODELS")
    if env is None and os.getenv("CQA_PROVIDER", "").lower() == "gemini":
        # free tier: ~20 requests/day per model, so spread the load over several Flash models
        env = ("gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash-preview,"
               "gemini-3.5-flash-lite,gemini-3.1-flash-lite")
    return [model] + [m.strip() for m in (env or "").split(",") if m.strip() and m.strip() != model]


def _openai_call(system: str, messages: list, schema: dict, model: str, effort: str):
    """Tries the configured model, then fallbacks when a model is overloaded or out of daily quota."""
    import time
    models = _fallback_models(model)
    ready = [m for m in models if _cooldown.get(m, 0) < time.time()]
    if not ready:
        raise EvaluationError("Daily free quota of all models is used up (or all are overloaded); "
                              "try again after the quota resets")
    for i, m in enumerate(ready):
        try:
            return _openai_call_one(system, messages, schema, m, effort, can_fallback=i < len(ready) - 1)
        except _Overloaded as e:
            _cooldown[m] = time.time() + e.cooldown
            log.warning("model %s %s, switching to %s", m, e.reason, ready[i + 1] if i + 1 < len(ready) else "-")
    raise EvaluationError("Daily free quota of all models is used up (or all are overloaded); "
                          "try again after the quota resets")


def _openai_call_one(system: str, messages: list, schema: dict, model: str, effort: str, can_fallback=False):
    import time

    import httpx
    base, key = _openai_settings()
    if not key and "localhost" not in base and "127.0.0.1" not in base:
        raise EvaluationError("API key is missing (set GEMINI_API_KEY or CQA_API_KEY in .env)")
    url = base.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    hint = ("\n\nReturn ONLY a JSON object matching this JSON Schema, no prose, no code fences:\n"
            + json.dumps(schema, ensure_ascii=False))
    modes = [_json_mode_ok[base]] if base in _json_mode_ok else _JSON_MODES
    last_err = ""
    for mode in modes:
        body = {"model": model, "messages": [{"role": "system", "content": system + hint}, *messages]}
        if mode == "json_schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "evaluation", "schema": schema, "strict": True}}
        elif mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        if effort and model not in _no_effort and os.getenv("CQA_SEND_EFFORT", "1") != "0":
            body["reasoning_effort"] = effort
        # retry on rate limits / transient errors (free tiers have low per-minute quotas)
        for attempt in range(5):
            try:
                r = httpx.post(url, json=body, headers=headers, timeout=float(os.getenv("CQA_TIMEOUT", "120")))
            except httpx.TimeoutException as e:
                if can_fallback:          # an overloaded model answers very slowly -> use the next one
                    raise _Overloaded(model) from e
                last_err = f"Timed out waiting for {model}"
                log.warning("%s: timeout, retry %s", model, attempt + 1)
                continue
            except httpx.HTTPError as e:
                last_err = f"Cannot reach the API: {e}"
                time.sleep(min(60, 2 ** attempt)); continue
            if r.status_code == 429 and "PerDay" in r.text:
                raise _Overloaded(model, _retry_delay(r.text) or 6 * 3600, "daily quota used up")
            if r.status_code == 503 and can_fallback and attempt >= 1:
                raise _Overloaded(model)
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"API error {r.status_code}: {r.text[:200]}"
                log.warning("%s: HTTP %s, retry %s", model, r.status_code, attempt + 1)
                wait = r.headers.get("retry-after")
                wait = float(wait) if wait and wait.replace(".", "").isdigit() else _retry_delay(r.text)
                time.sleep(min(60, wait) if wait else min(60, 5 * 2 ** attempt))
                continue
            break
        else:
            raise EvaluationError(last_err or "API unavailable")
        if r.status_code in (401, 403):
            raise EvaluationError(f"API key rejected ({r.status_code}): {r.text[:200]}")
        if r.status_code == 400 and "reasoning_effort" in body and (
                "reasoning" in r.text.lower() or "thinking" in r.text.lower()):
            _no_effort.add(model)   # this model does not support it; retry without
            return _openai_call_one(system, messages, schema, model, effort, can_fallback)
        if r.status_code == 400 and mode != "none" and base not in _json_mode_ok:
            last_err = f"API error 400 ({mode}): {r.text[:300]}"
            continue  # try a simpler response_format
        if r.status_code >= 400:
            raise EvaluationError(f"API error {r.status_code}: {r.text[:300]}")
        _json_mode_ok[base] = mode
        data = r.json()
        msg = (data.get("choices") or [{}])[0].get("message", {})
        try:
            parsed = _parse_json(msg.get("content") or "")
        except (json.JSONDecodeError, TypeError) as e:
            raise EvaluationError("Model returned invalid JSON") from e
        u = data.get("usage") or {}
        return parsed, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), data.get("model", model)
    raise EvaluationError(last_err or "No response format accepted by the API")


def evaluate_llm(dialog: dict, checklist: dict, model: str, effort: str, call=None) -> dict:
    call = call or _anthropic_call
    turns = prepare_transcript(dialog)
    schema = output_schema(checklist)
    messages = [{"role": "user", "content": user_message(dialog, turns, checklist)}]
    tin = tout = 0
    items, errors, summary, used_model = {}, [], "", model
    for attempt in range(2):
        data, i, o, used_model = call(SYSTEM, messages, schema, model, effort)
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
    pin, pout = price(model)
    return {"items": items, "summary": summary, "model": used_model,
            "provider": "anthropic" if call is _anthropic_call else os.getenv("CQA_PROVIDER", "openai"),
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


# --- several dialogs per request (free tiers limit requests per day, not tokens) ------------
def multi_schema(checklist: dict, ids: list[str]) -> dict:
    one = output_schema(checklist)
    dialog = {"type": "object",
              "properties": {"conversation_id": {"type": "string", "enum": ids}, **one["properties"]},
              "required": ["conversation_id", *one["required"]], "additionalProperties": False}
    return {"type": "object", "properties": {"dialogs": {"type": "array", "items": dialog}},
            "required": ["dialogs"], "additionalProperties": False}


def multi_message(dialogs: list[dict], checklist: dict) -> str:
    parts = [f"CHECKLIST\n{checklist_for_prompt(checklist)}\n",
             f"Evaluate EACH of the {len(dialogs)} conversations below independently. Turn numbers (#) restart "
             f"in every conversation; cite numbers of that conversation only. Return one entry in \"dialogs\" "
             f"per conversation_id."]
    for d in dialogs:
        turns = prepare_transcript(d)
        parts.append(f"\n===== CONVERSATION {d['conversation_id']} (channel: {d.get('channel')}, "
                     f"language: {d.get('language')}, {len(turns)} turns)\n{render_transcript(turns)}")
    return "\n".join(parts)


def _llm_settings() -> tuple:
    provider = os.getenv("CQA_PROVIDER", "anthropic").lower()
    if provider in ("gemini", "openai"):
        default = "gemini-3.8-flash" if provider == "gemini" else ""
        return os.getenv("CQA_MODEL") or default, os.getenv("CQA_EFFORT", "low"), _openai_call
    return os.getenv("CQA_MODEL") or "claude-opus-5-5", os.getenv("CQA_EFFORT", "medium"), _anthropic_call


def dialogs_per_call() -> int:
    v = os.getenv("CQA_DIALOGS_PER_CALL")
    if v:
        return max(1, int(v))
    return 5 if os.getenv("CQA_PROVIDER", "anthropic").lower() == "gemini" else 1


def evaluate_many(dialogs: list[dict], checklist: dict, model=None, effort=None, call=None) -> dict:
    """Evaluates several dialogs in ONE request. Returns {conversation_id: result | Exception}.
    Dialogs whose answers are missing or invalid are re-evaluated one by one."""
    if os.getenv("CQA_PROVIDER", "anthropic").lower() == "heuristic":
        return {d["conversation_id"]: evaluate(d, checklist) for d in dialogs}
    m, e, c = _llm_settings()
    model, effort, call = model or m, effort or e, call or c
    out: dict = {}
    if len(dialogs) > 1:
        ids = [d["conversation_id"] for d in dialogs]
        try:
            data, tin, tout, used = call(SYSTEM, [{"role": "user", "content": multi_message(dialogs, checklist)}],
                                         multi_schema(checklist, ids), model, effort)
            pin, pout = price(model)
            share_in, share_out = tin / len(dialogs), tout / len(dialogs)
            got = {x.get("conversation_id"): x for x in data.get("dialogs", []) if isinstance(x, dict)}
            for d in dialogs:
                x = got.get(d["conversation_id"])
                if not x:
                    continue
                items, errors = normalize_items(x.get("items", []), checklist, len(d["transcript"]))
                if errors:
                    log.warning("%s: %s; re-evaluating alone", d["conversation_id"], "; ".join(errors)[:200])
                    continue
                res = {"items": items, "summary": x.get("summary", ""), "model": used,
                       "provider": os.getenv("CQA_PROVIDER", "anthropic"),
                       "tokens_in": round(share_in), "tokens_out": round(share_out),
                       "cost_usd": round(share_in / 1e6 * pin + share_out / 1e6 * pout, 5),
                       "batched": len(dialogs)}
                res["score"] = scoring.compute(checklist, {k: v["answer"] for k, v in items.items()})
                out[d["conversation_id"]] = res
        except EvaluationError as err:
            log.warning("batch of %s failed (%s); evaluating one by one", len(dialogs), str(err)[:150])
    for d in dialogs:
        if d["conversation_id"] in out:
            continue
        try:
            res = evaluate_llm(d, checklist, model, effort, call=call)
            res["score"] = scoring.compute(checklist, {k: v["answer"] for k, v in res["items"].items()})
            out[d["conversation_id"]] = res
        except Exception as err:  # reported per dialog
            out[d["conversation_id"]] = err
    return out


def evaluate(dialog: dict, checklist: dict) -> dict:
    provider = os.getenv("CQA_PROVIDER", "anthropic").lower()
    if provider == "heuristic":
        res = evaluate_heuristic(dialog, checklist)
    elif provider in ("gemini", "openai"):
        default = "gemini-3.8-flash" if provider == "gemini" else ""
        res = evaluate_llm(dialog, checklist, os.getenv("CQA_MODEL") or default,
                           os.getenv("CQA_EFFORT", "low"), call=_openai_call)
    else:
        res = evaluate_llm(dialog, checklist, os.getenv("CQA_MODEL") or "claude-opus-5-5",
                           os.getenv("CQA_EFFORT", "medium"))
    answers = {k: v["answer"] for k, v in res["items"].items()}
    res["score"] = scoring.compute(checklist, answers)
    return res
