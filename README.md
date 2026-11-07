# CQA — Conversation Quality Analytics (Freedom Broker hackathon)

Prototype that scores operator chats and calls against a configurable checklist, shows evidence for every
score, and supports appeals with reviewer decisions and automatic recalculation.

## Quick start

Requirements: Python 3.10+.

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in CQA_DATASET_URL and ANTHROPIC_API_KEY
python -m uvicorn backend.app:app --port 8000
```

Open http://localhost:8000. On first start the app seeds the checklist from the PDF and, if
`data/dev.json` exists, imports the DEV dataset. Otherwise load it from the UI: type `DEV` in the
`group_id` field and click **Загрузить из API**.

Tests: `python -m pytest -q tests`

### Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `CQA_DATASET_URL` | Organizers' GET link with `{group_id}` in place of the group value |
| `CQA_PROVIDER` | `anthropic` (real evaluation) or `heuristic` (offline keyword rules, for UI dev only) |
| `ANTHROPIC_API_KEY` | API key for Claude |
| `CQA_MODEL` | default `claude-opus-5-5` |
| `CQA_EFFORT` | `low` / `medium` / `high` |
| `CQA_CONCURRENCY` | parallel evaluations in batch mode (default 6) |

## What it does

| Task requirement | Where |
|---|---|
| 01 Edit checklist: criteria, wording, weight, type (yes/no, scale, flag), N/A + condition, scale steps | **Чек-лист** tab. Every save is a new immutable version, so old evaluations stay reproducible |
| 02 Evaluate a dialog with a chosen checklist, RU / KZ / EN, bot & system as context | Dialog card → **Оценить**; list → **Оценить все**; JSON upload or API load by `group_id` |
| 03 Answer, points, weight, explanation, evidence; checked range for missed actions; N/A reason; total and sections | Dialog card, right column |
| Weights & total | `backend/scoring.py` — deterministic code, not the LLM (see below) |
| 04 Evidence link jumps to the message / call fragment, audio playback | Click `#N` → transcript scrolls and highlights; for calls the player seeks to that moment |
| 05 Appeal a criterion with a comment | Role **Оператор** → **Оспорить оценку** |
| 06 Reviewer confirms/changes with a reason; total recalculated; original, decision and final visible | Role **Проверяющий** → in the card or the **Апелляции** queue (with the dialog fragment) |
| Control: new transcripts | Enter the new `group_id` → **Загрузить из API** → **Оценить все** |
| Control: replace a criterion | **Чек-лист** → edit → **Сохранить новую версию** → **Применить к диалогам** |

### Scoring rules (from the checklist PDF)

- Yes = full weight, No = 0; scale = step / top step × weight.
- N/A (only where allowed) removes the criterion; weights of the rest are scaled up so the maximum stays 100.
- Total rounded to 2 decimals; if no weighted criterion applies, the total is not calculated.
- Gross errors (flag type) are shown separately and do not change the total.

### How the LLM is used

One call per dialog. The prompt is built from the checklist JSON, so new criteria work without code changes.
The transcript is sent with numbered turns; the model returns, via structured JSON output, one item per
criterion: answer, explanation (Russian), evidence turn numbers, checked range and N/A reason.
Answers are validated (allowed values, N/A permission, existing turn numbers); invalid ones are sent back
to the model once for correction. Points are then computed in code.

Model: **Claude Opus 5.5** (`claude-opus-5-5`, configurable), $4 / $20 per 1M input / output tokens.
Rough estimate before measurement: ~4k input + ~2–4k output (including thinking) tokens per chat ≈ **$0.06–0.10
per evaluation**; long calls cost more. The exact tokens and cost are recorded on every evaluation card and
summed for batch runs — replace this estimate with the measured DEV average.
Prompt rules handle the data quirks: the bot's greeting is not the operator's, the greeting must be in the
client's language, and call transcripts come from speech recognition.

Recordings: `audio_url` points to a player page holding a signed MP3 link that expires after about an hour. The
backend (`/api/audio/{id}`) extracts and refreshes that link and streams the file with byte-range support,
so the in-app player can seek to any turn.

## Architecture

```
frontend/   vanilla HTML/CSS/JS (ES modules, no build). Single root #cqa-app, all CSS scoped under .cqa,
            API base from window.CQA_CONFIG.apiBase → can be hosted as a Dynamics 365 HTML web resource.
backend/    FastAPI: dataset proxy, checklist versions, evaluation (single + batch jobs), appeals, audio proxy
            scoring.py   – deterministic scoring engine
            evaluator.py – prompt, structured output, validation, offline heuristic fallback
            db.py        – SQLite (data/cqa.db), created automatically
tests/      scoring rules, validation, LLM retry path (mocked)
```

Main API: `GET /api/dialogs`, `GET /api/dialogs/{id}`, `POST /api/evaluate`, `POST /api/evaluate/batch`,
`GET /api/jobs/{id}`, `GET/POST /api/checklists`, `POST /api/appeals`, `POST /api/appeals/{id}/decision`,
`POST /api/datasets/{group_id}/load`, `GET /api/audio/{id}`.

No login: the header has an Operator / Reviewer switch so appeal and review are separate actions.
