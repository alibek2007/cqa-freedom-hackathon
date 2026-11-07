# CQA Hackathon — Development Plan (Freedom Broker)

Goal: a working **Conversation Quality Analytics** prototype in 24 h that covers every point of the task
(sections 01–06), survives the two control checks (new transcripts at hour 23, criterion replacement),
and fits a 5‑minute demo.

---

## 0. What the documents actually require (checklist for the jury)

| # | Requirement (source) | Where we satisfy it |
|---|---|---|
| R1 | Edit checklist: add/change criteria, wording, weight, answer type, N/A allowed, scale for numeric; save config (Guide p.2 §01) | Checklist editor + versioned JSON config |
| R2 | New criterion applies **via settings**, no code change (Guide p.2 §01, p.1 "Контроль") | Prompt is built dynamically from checklist JSON |
| R3 | Upload/choose transcript, choose checklist, run evaluation; RU / KZ / EN (§02) | Dialog list + "Evaluate" button, language‑agnostic prompt |
| R4 | Bot and system messages = context only; we judge the **operator** (§02) | Prompt rule + criterion 01 uses *first operator message*, not bot greeting |
| R5 | Per criterion: answer, points, weight, explanation, evidence message (§03) | Result card per criterion |
| R6 | Missed action → show the **checked range** of the conversation; N/A → explain reason (§03, Checklist p.3) | `checked_range` and `na_reason` fields in LLM output |
| R7 | Total + section results (§03) | 3 sections from checklist: Начало / Безопасность и решение / Ведение и завершение |
| R8 | Scale → points by weight; N/A weight redistributed; gross error flagged separately; round to 2 decimals; no applicable → no total (§03 box, Checklist p.2) | Deterministic scoring engine **in code, not in LLM** |
| R9 | From list → transcript + result; evidence link jumps to the message / call fragment; audio playback (§04) | Anchors `#turn-N`, audio player seek by offset |
| R10 | Appeal: pick criterion, comment, save; reviewer sees disputed score, comment, fragment (§05) | Appeals module |
| R11 | Reviewer confirms or changes score with reason; total recalculated; card shows original, decision, final (§06) | Decision module + audit history |
| R12 | UI in HTML/CSS/JS, adaptable as **Dynamics 365 HTML component** (p.1) | Vanilla JS, no build step, isolated CSS, configurable API base |
| R13 | No login; appeal and review shown as separate actions (FAQ) | Role switch "Operator / Reviewer" in header |
| R14 | Name the AI model + approx. cost of one evaluation incl. all calls (FAQ) | Token counter → cost shown in UI + README |
| R15 | CORS → fetch via backend / adapter (FAQ) | Backend proxy `/api/datasets/{group_id}` |
| R16 | Hour 23: process control transcripts in advance; hour 24: stop‑code, public Git repo, README with run steps, link sent early (p.4) | Batch evaluation by `group_id`, release checklist |

**Note on the "Data" PDF:** it is only the Freedom Holding intro deck (ecosystem, 874k brokerage accounts,
NASDAQ: FRHC). No data there — but it tells us the pitch angle: *one brand, one standard* → one checklist,
consistently applied to every chat in RU/KZ/EN, auditable and integrated into their CRM (Dynamics 365).

---

## 1. What the DEV data looks like (already fetched → `data/dev.json`)

- 65 records: 60 chats (30 RU, 15 KZ, 15 EN) + 5 voice calls (RU, `p4-ru-001..005`).
- Speakers: client 505, operator 339, **bot 315**, system 109 → bot is very present; it greets the client
  ("Здравствуйте! Я AIM…"). **Criterion 01 must not count the bot greeting** — only the operator's first reply.
- Chats: timestamps `MM:SS`. Calls: ISO‑8601 with timezone → convert to offset seconds from first turn for audio seek.
- Calls are 45–115 turns, ASR is noisy ("Салими цбе Фридом брокер за кошки…"), IVR menus and language
  switches appear as bot/system → the prompt must tolerate recognition errors.
- `audio_url` points to a Power Automate endpoint → likely needs our backend to proxy the audio (CORS / content type).

---

## 2. Architecture

```
┌──────────────── Frontend (vanilla HTML/CSS/JS, ES modules) ───────────────┐
│  Dialog list │ Dialog card (transcript + result) │ Checklist editor │ Appeals │
│  role switch: Operator / Reviewer      config: window.CQA_API_BASE          │
└───────────────────────────────┬────────────────────────────────────────────┘
                                │ REST JSON
┌───────────────────────────────▼────────────────────────────────────────────┐
│ Backend (Python FastAPI  — or Node/Express, team choice)                    │
│  /datasets/{group_id}  proxy+cache organizers' API                          │
│  /checklists           CRUD, versioned                                      │
│  /evaluate             single + batch (async, concurrency 5–10)             │
│  /evaluations/{id}     result, history                                      │
│  /appeals, /appeals/{id}/decision                                           │
│  /audio/{conversation_id}  proxy stream                                     │
│  Scoring engine (pure function, unit-tested)                                │
│  LLM adapter (provider‑agnostic, JSON mode, retry, validation)              │
└───────────────────────────────┬────────────────────────────────────────────┘
                         SQLite (one file, zero setup)
```

Why this split: the LLM only **classifies and cites**; all arithmetic (weights, N/A redistribution,
rounding, gross error) is deterministic code → reproducible, explainable, and instantly recalculated after
an appeal without calling the LLM again.

### 2.1 Data model

```text
Dialog       { conversation_id, group_id, channel, language, transcript[] (turn_idx, timestamp, offset_s, speaker, text), audio_url }
Checklist    { id, version, name, sections[], criteria[], created_at }   -- immutable versions
Criterion    { id, section, title, description, type: binary|scale|flag,
               weight, allow_na, na_condition, scale_steps:[{value, description}] }
Evaluation   { id, conversation_id, checklist_id+version, model, tokens_in/out, cost,
               items[]: { criterion_id, answer (yes|no|na|<scale value>), na_reason,
                          explanation, evidence_turns[], checked_range [from,to], points, effective_weight },
               section_scores, total (nullable), gross_error (bool + which), status }
Appeal       { id, evaluation_id, criterion_id, comment, created_by:"operator", status: open|resolved }
Decision     { appeal_id, action: confirm|change, new_answer, reason, reviewer, decided_at }
```
Final result = original evaluation items with decisions applied → re‑run scoring engine.
Card shows **original total → decision → final total**.

### 2.2 Initial checklist (from PDF) as JSON seed

| id | Section | Type | Weight | N/A |
|---|---|---|---|---|
| 01 Установление контакта | Начало и понимание | yes/no | 10 | no |
| 02 Выявление потребности | Начало и понимание | yes/no | 15 | yes — data sufficient from start |
| 03 Идентификация | Безопасность и решение | yes/no | 20 | yes — no identification needed |
| 04 Консультация | Безопасность и решение | scale 0/5/10/15/20 | 20 | yes — chat ended before request |
| 05 Эмпатия | Ведение и завершение | yes/no | 15 | no |
| 06 Управление диалогом | Ведение и завершение | scale 0/4/7/10 | 15 | no |
| 07 Завершение | Ведение и завершение | yes/no | 5 | yes — chat cut off |
| 08 Грубые ошибки | Грубые нарушения | flag yes/no | 0 (no points) | no |

Copy the full descriptions, scale step texts and page‑3 clarifications (No vs N/A, "service bypassing company",
example justification) into the criterion descriptions — they become the LLM instructions.

### 2.3 Scoring engine (exact rules from Checklist p.2)

```
for each criterion with weight > 0:
    if answer == NA: skip (must have allow_na, else validation error → re‑ask LLM)
    raw = 1 if yes, 0 if no, scale_value / max(scale_steps) for scale
    points_raw_i = raw * weight_i
W = Σ weight_i over applicable
if W == 0: total = null ("not calculated")
else: total = round(Σ points_raw_i * 100 / W, 2)        # redistribution keeps max = 100
section_score = same formula inside a section (show as % and as points)
gross_error = criterion of type flag answered "yes"  → red banner, NOT subtracted from total
```
Display per criterion: weight (original) and effective weight (`weight*100/W`), points earned.
Write ~10 unit tests (all N/A, one N/A, all max, scale midpoints, rounding 2 decimals).

### 2.4 LLM evaluation

- **One call per dialog** with all criteria (cheap, consistent); fallback: per‑criterion re‑ask only for items
  that fail validation.
- Input: numbered transcript `[#12 | 01:15 | operator] text`, dialog language, checklist JSON, rules:
  - evaluate only the operator; bot/system = context;
  - "first response" = first operator message;
  - greeting must be in the client's language (KZ client → KZ greeting);
  - N/A only if allowed and condition met, otherwise "No";
  - always cite turn numbers; for a missed action give the checked range; for N/A give the reason;
  - write explanations in the UI language (RU) regardless of dialog language;
  - ASR text in calls may be distorted — judge meaning, not spelling.
- Output: strict JSON schema (structured output / JSON mode). Validate: answer ∈ allowed set, scale value ∈ steps,
  turn ids exist, operator turns cited for "Yes". Temperature 0.
- Model: a strong multilingual model (Kazakh quality matters!) — e.g. Claude Sonnet / GPT‑4‑class; test a cheaper
  one on DEV and pick by accuracy. Log tokens → cost per evaluation for the README/FAQ answer.
- Cache key = hash(transcript + checklist version + model) → re‑opening is instant; changing the checklist
  creates a new version and therefore new evaluations, old ones remain for comparison.
- Optional bonus: audio‑based diction/intonation (FAQ hint) — only if time remains; mark as experimental.

### 2.5 Frontend screens

1. **Dialogs list** — filters (language, channel, status, has appeal, gross error), score badges, `group_id` loader
   field (DEV / control id), "Evaluate all" with progress bar.
2. **Dialog card** — left: transcript (speaker colors, bot/system greyed, timestamps, audio player for calls);
   right: total, section scores, gross‑error banner, criteria table. Clicking evidence → scroll + highlight
   `#turn-N`; for calls also `audio.currentTime = offset_s`. Missed action → highlight the checked range.
   N/A badge with reason. "Appeal" button per criterion (operator role).
3. **Checklist editor** — list of criteria, add/edit/delete, section, type (yes/no, scale, flag), weight,
   allow N/A + condition, scale steps editor; live check "weights sum = 100"; Save → new version;
   "Apply to dataset" button → batch re‑evaluation.
4. **Appeals queue** (reviewer role) — disputed criterion, original answer + explanation, operator comment,
   transcript fragment ±3 turns; actions Confirm / Change (new value + mandatory reason) → instant recalculation.

**Dynamics 365 readiness:** no framework, no build; one root `<div id="cqa-app">`, all CSS prefixed `.cqa-`
(or Shadow DOM), API base URL from config, no `localStorage` dependency, relative asset paths. Say this
explicitly in README and in the pitch.

---

## 3. 24‑hour timeline (team of 3–4: **BE** backend, **AI** prompts/quality, **FE** frontend, **PM** data+demo)

| Hours | BE | AI | FE | PM / QA |
|---|---|---|---|---|
| 0–1 | Repo, skeleton, SQLite, dataset proxy | Read checklist, draft prompt | UI skeleton & layout | Hand‑label 15 DEV dialogs (5 RU, 5 KZ, 5 EN incl. 1 call) as gold set |
| 1–4 | Checklist CRUD + seed JSON; scoring engine + tests | Prompt v1, JSON schema, validator | Dialog list + card with transcript | Continue labeling; collect tricky cases (N/A, ident, gross error) |
| 4–8 | /evaluate single + batch, cache, cost logging | Run on gold set, measure per‑criterion agreement | Result panel, evidence jump, audio player | Compare LLM vs gold, report errors |
| 8–12 | Appeals + decisions API, history | Prompt v2 (fix KZ, bot‑greeting, N/A vs No) | Checklist editor | Write README draft |
| 12–16 | Audio proxy, error handling, retries | Evaluate full DEV (65), spot‑check | Appeals queue, role switch, recalculation view | First full demo rehearsal |
| 16–20 | Hardening, timeouts, concurrency | Rehearse criterion replacement end‑to‑end | Polish, filters, responsive, D365 notes | **Push to public repo, send link to organizers** |
| 20–23 | Freeze features, bug fixes only | Final prompt; make sure new criterion works from UI only | Bug fixes | Demo script, timing ≤ 5 min |
| **23** | Load control `group_id`, run batch immediately | Monitor failures, re‑run invalid | — | Check results of a few dialogs |
| **24** | **Stop‑code: final commit** | | | After stop: criterion change + apply only via UI |

Golden rule: by **hour 12** the full loop (load → evaluate → card → appeal → decision → new total) must work
end‑to‑end, even if ugly. Everything after is quality and polish.

---

## 4. Control checks — how to not fail them

- **New transcripts (h23):** UI field "group_id" → Load → Evaluate all. Batch with concurrency + retries; for 60+
  dialogs run time must be < 10 min → test with DEV at hour ~16. Keep the old DEV results.
- **Replace a criterion:** only via the editor: edit/delete old, add new (type, weight, N/A, scale), weights = 100,
  Save → new version → "Apply to issued dialogs". Rehearse with an invented criterion
  (e.g. "Operator warned about waiting time", yes/no/N/A, weight 10) at least twice before hour 23.
- Nothing about criteria may be hard‑coded in the prompt or UI (except the generic rules). Grep the code for
  "Установление", "Идентификация" before stop‑code.

---

## 5. Quality bar (what convinces a bank)

- **Accuracy report:** agreement with our gold labels per criterion and per language (e.g. "92 % on 15 hand‑labeled
  dialogs, KZ 87 %"). Even a small honest table beats claims.
- **Explainability:** every score has a quote + clickable turn; missed actions have a range; N/A has a reason.
- **Determinism & audit:** same input → same output (temp 0 + cache); full history original → appeal → decision.
- **Compliance value:** gross errors (insult, off‑company paid service, confidential data leak) surfaced as red flags
  and a filter — this is what risk/compliance at a broker cares about most.
- **Cost:** model name + cost per evaluation + cost for 1 000 chats/day.
- **Data privacy (pitch only):** option for PII masking before sending to LLM / on‑prem model for production.
- **Integration:** ready to embed as Dynamics 365 HTML web resource; backend API can be called from Power Automate
  (their data API is already Power Automate).

---

## 6. 5‑minute demo script (matches Guide p.4)

1. **0:00–1:00** — list of dialogs (show RU/KZ/EN + call), open one KZ chat.
2. **1:00–3:00** — evaluation: criteria, evidence click jumps to message; open a call → evidence jumps audio;
   show an **N/A** with reason and redistributed weights; total + sections; gross‑error example.
3. **3:00–4:00** — operator appeals criterion → switch to reviewer → change score with reason → new total, history visible.
4. **4:00–5:00** — checklist editor: replace a criterion → save → apply to dataset → new scores.
   Close with: model, cost per evaluation, accuracy table, D365 readiness.

Prepare dialog IDs in advance (one with N/A, one with gross error, one call). Have a screen recording as backup.

---

## 7. Release checklist (before stop‑code)

- [ ] Public Git repo, link sent to organizers early
- [ ] README: requirements, `.env.example` (API key, model, dataset URL), 3–5 command start, screenshots, model + cost
- [ ] Seed checklist loads on first run; DB file created automatically
- [ ] Scoring unit tests pass
- [ ] Control dataset evaluated and stored before the presentation
- [ ] No secrets committed (API keys, the signed organizers' URL → put into `.env`)
