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
| `CQA_PROVIDER` | `gemini` (free tier, default), `anthropic` (Claude, paid), `openai` (any OpenAI-compatible API: Groq, OpenRouter, local Ollama) or `heuristic` (offline keyword rules, UI development only) |
| `GEMINI_API_KEY` | free key from https://aistudio.google.com/apikey |
| `ANTHROPIC_API_KEY` | key for `anthropic` |
| `CQA_BASE_URL`, `CQA_API_KEY` | endpoint and key for `openai`, e.g. `http://localhost:11434/v1` for Ollama |
| `CQA_MODEL` | empty = provider default (`gemini-3.8-flash` / `claude-opus-5-5`) |
| `CQA_EFFORT` | reasoning effort `low` / `medium` / `high` |
| `CQA_CONCURRENCY` | parallel evaluations in batch mode (default 2 for Gemini free tier, 6 otherwise) |

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

With Gemini, 5 dialogs are scored in one request (`CQA_DIALOGS_PER_CALL`); with Claude, one dialog per request.
The prompt is built from the checklist JSON, so new criteria work without code changes.
The transcript is sent with numbered turns; the model returns, via structured JSON output, one item per
criterion: answer, explanation (Russian), evidence turn numbers, checked range and N/A reason.
Answers are validated (allowed values, N/A permission, existing turn numbers); invalid ones are sent back
to the model once for correction. Points are then computed in code.

Models:
- **Used for the submitted results: Google Gemini Flash on the free tier — $0 per evaluation.**
  `results/prod`: `gemini-3.6-flash` and `gemini-3.5-flash`; `results/dev`: `gemini-3-flash-preview` and
  `gemini-3.5-flash-lite`. The primary model is `CQA_MODEL`; the app falls back automatically to other Flash
  models when one is overloaded, too slow or out of its daily free quota (`CQA_FALLBACK_MODELS`). Batch runs use 2 parallel calls. Note: on the free
  tier Google may use the submitted content to improve its products; for production use a paid tier
  (content not used for training) or a self-hosted model.
- **Alternative: Claude Opus 5.5** (`claude-opus-5-5`, $4 / $20 per 1M tokens), roughly $0.06–0.10 per chat.
- The exact tokens and cost are recorded on every evaluation card and summed for batch runs.

For OpenAI-compatible APIs the backend first asks for schema-constrained JSON, falls back to plain JSON
mode if the server rejects it, and remembers what worked.
Prompt rules handle the data quirks: the bot's greeting is not the operator's, the greeting must be in the
client's language, and call transcripts come from speech recognition.

Recordings: `audio_url` points to a player page holding a signed MP3 link that expires after about an hour. The
backend (`/api/audio/{id}`) extracts and refreshes that link and streams the file with byte-range support,
so the in-app player can seek to any turn.

## Results files for the organizers (JSON 1.0)

`results/dev/results.json` (DEV, 65 dialogs) and `results/prod/results.json` (control set, 10 chats) follow
the appendix format: criterion ids `contact … critical`, values `true/false/null` or scale steps,
`evidence_turns` numbered over the full original transcript, `final_score` with N/A weight redistribution,
`critical_flag`, and `status: "error"` records for dialogs that could not be evaluated.

One command does everything — fetch, evaluate (failed dialogs get 2 more attempts), export, validate:

```bash
python -m backend.run_set --group <control group_id> --out results/prod/results.json --expect 10 --push
```

Check that it prints `[validate] OK`, then commit and push (or add `--push` to do it automatically).
`--export-only` rewrites the file from stored evaluations (e.g. after appeals). The UI has the same export
as **Скачать results.json** on the dialog list.

**Gemini free-tier quota:** about 20 requests per day per model (resets at midnight Pacific time). The app
spreads requests over several Flash models and skips a model whose daily quota is used up, so the model
name in `results.json` can list more than one model. Don't spend the quota on test runs on the control day.

## Submitted results

| File | Set | Checklist | Requests | Time | Status |
|---|---|---|---|---|---|
| `results/prod/results.json` | FBroker, 10 chats | v3 (clarified rules) | 2 | 19 s | 10 ok, validation OK |
| `results/dev/results.json` | DEV, 60 chats + 5 calls | v3 (clarified rules) | 13 | 150 s | 65 ok, validation OK |

DEV against the hand-made reference (`eval/dev_reference.json`, 65 dialogs): **91.2%** of clear-cut labels
(first run with the original rules and mixed models: 88.4%); identification 100%, consultation 98.3%,
gross errors 100%.

**Known limitation:** closing is still the weakest criterion (52%). The organizers clarified that a goodbye
alone is not enough — a summary or an offer of further help is also required — and the models still often
treat "Пожалуйста. Хорошего дня!" as an offer of help. A sharper rule or a stronger model is the next step.

## Measuring accuracy

`eval/gold_dev.txt` holds reference answers for the 60 DEV chats, labelled by hand against the checklist
(`a|b` marks cases where the checklist wording allows two readings). Compare any results file with it:

```bash
python eval/accuracy.py results/dev/results.json --details
```

To test a rule or model change cheaply on selected dialogs (no database writes):

```bash
python -m eval.try_model --model gemini-3.8-flash --ids p1-ru-003 p1-ru-005 p1-kz-005
```

The checklist descriptions include clarifications derived from the disagreements (what counts as a
greeting, when identification is required, how to score unanswered questions and closing). They are
part of the checklist data, so they can be edited in the UI like any other wording.

## Accuracy check

`eval/dev_reference.json` holds hand-made labels for the 60 DEV chats (8 criteria each). Labels that the
checklist wording allows to read either way are marked `uncertain`. Compare any results file with:

```bash
python -m eval.compare results/dev/results.json --details
python -m eval.experiment --model gemini-3.8-flash --out eval/runs/run.json   # re-score with one model
```

Same model (`gemini-3.1-flash-lite`), original vs clarified checklist rules:

| | Original rules | Clarified rules |
|---|---|---|
| Clear-cut labels | 87.9% | **93.3%** |
| Need discovery | 56.9% | 77.6% |
| Identification | 94.8% | 100% |
| Consultation | 89.5% | 94.7% |

The clarifications are part of the checklist text (editable in the UI), not code. Gross errors were found in
all 5 DEV chats that contain them, in RU, KZ and EN.

**Efficiency:** with Gemini, 5 chats are scored in one request (`CQA_DIALOGS_PER_CALL`), so DEV needs ~12
requests and the 10 control chats ~2; a chat with an invalid answer is re-scored alone.

## Architecture

```
frontend/   vanilla HTML/CSS/JS (ES modules, no build). Single root #cqa-app, all CSS scoped under .cqa,
            API base from window.CQA_CONFIG.apiBase → can be hosted as a Dynamics 365 HTML web resource.
backend/    FastAPI: dataset proxy, checklist versions, evaluation (single + batch jobs), appeals, audio proxy
            scoring.py   – deterministic scoring engine
            evaluator.py – prompt, structured output, validation, offline heuristic fallback
            db.py        – SQLite (data/cqa.db), created automatically
backend/export.py, run_set.py – results.json export/validation and the one-command control-set run
tests/      scoring rules, results format, validation, LLM retry path (mocked)
```

Main API: `GET /api/dialogs`, `GET /api/dialogs/{id}`, `POST /api/evaluate`, `POST /api/evaluate/batch`,
`GET /api/jobs/{id}`, `GET/POST /api/checklists`, `POST /api/appeals`, `POST /api/appeals/{id}/decision`,
`POST /api/datasets/{group_id}/load`, `GET /api/audio/{id}`.

No login: the header has an Operator / Reviewer switch so appeal and review are separate actions.
