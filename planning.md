# Provenance Guard - planning.md

Provenance Guard is a Flask API for a creative sharing platform. It accepts submitted text, runs two attribution signals, combines them into a confidence score, returns the label a reader would see, and records the decision for audit or appeal.

The system does not pretend AI detection is certain. Scores near the middle return `uncertain`, and creators can appeal classifications they believe are wrong.

---

## Architecture

```text
  POST /submit   { text, creator_id, content_id? }
                │
                ▼
  ┌───────────────────────────┐
  │ Validation + rate limit   │  ──▶ invalid request or 429 (rate limit)
  └─────────────┬─────────────┘
                │ validated text + creator_id + content_id
                ▼
  ┌───────────────────────────┐
  │ Detection pipeline        │
  └─────────────┬─────────────┘
                │ text
                ├──────────▶ llm_judgement     → llm_score + llm_rationale
                │
                ├──────────▶ pattern_analysis  → pattern_score + pattern_markers
                │
                ▼ llm_score + pattern_score
  ┌───────────────────────────┐
  │ Confidence scoring        │
  │ (llm + pattern) / 2       │
  └─────────────┬─────────────┘
                │ combined confidence + attribution
                ▼
  ┌───────────────────────────┐
  │ Label selector            │
  └─────────────┬─────────────┘
                │ transparency label text
                ▼
  ┌───────────────────────────┐
  │ SQLite audit log          │
  │ status = classified       │
  └─────────────┬─────────────┘
                │ classification row
                ▼
          JSON response
```

```text
  POST /appeal   { content_id, creator_reasoning }
                │
                ▼
  ┌───────────────────────────┐
  │ Validate request + find   │  ──▶ unknown id or already under_review
  │ the submission record     │
  └─────────────┬─────────────┘
                │ content_id + creator_reasoning
                ▼
  ┌───────────────────────────┐
  │ Update that record:       │
  │ classified → under_review │
  └─────────────┬─────────────┘
                │ + store appeal_reasoning
                ▼
  ┌───────────────────────────┐
  │ Same audit log row:       │
  │ under_review + reasoning  │
  └─────────────┬─────────────┘
                │
                ▼
          JSON response
```

The submission flow validates the request, rate limits the client, generates a `content_id` if the caller did not provide one, runs both signals, combines the scores, selects a label, writes the audit-log row, and returns the JSON response.

Invalid submissions are rejected before the audit log because no attribution decision was made.

The appeal flow accepts `content_id` and `creator_reasoning`, finds that submission's row by `content_id`, and updates it in place — status `classified` → `under_review`, with the `creator_reasoning` stored in the row's `appeal_reasoning` field — so the appeal is recorded on the original classification row rather than as a separate event. The only primary statuses are `classified` and `under_review`.

---

## Detection Signals

| Signal | What it captures | Output | Main misses |
| --- | --- | --- | --- |
| `llm_judgement` | A Groq-hosted LLM judges the passage as a whole, looking for generic phrasing, overly polished structure, repeated rhetorical moves, and voice. | `llm_score` from `0.0` to `1.0`, plus a short `llm_rationale`. | LLMs can be overconfident, polished human writing can look AI-assisted, and edited AI text can look more human. |
| `pattern_analysis` | Deterministic stylometric markers: sentence-length variation (robust MAD/median burstiness), repeated two-word sentence openers, em-dash/semicolon punctuation density, and AI-favored vocabulary/phrases. | `pattern_score` from `0.0` to `1.0`, plus `pattern_markers` (the per-marker scores that are averaged). | Lyrical writing, technical documentation, short-but-valid text, and heavily edited AI text can make the markers misleading. |

Both signals use the same score direction:

- `0.0` means strong human-written evidence.
- `0.5` means mixed or uncertain evidence.
- `1.0` means strong AI-generated or AI-assisted evidence.

`pattern_analysis` measures each marker from the text and maps it to a `0.0`–`1.0` per-marker score, then averages the four equally into `pattern_score`; the raw measurement is only an input to each marker's score, never an output. The markers come in two kinds:

- **Bidirectional** — `sentence-length variation` uses the full range, because bursty sentence lengths are genuine human evidence (`0.0`) and uniform lengths are genuine AI evidence (`1.0`). It uses a *robust* coefficient of variation (median absolute deviation over the median, "rCV") rather than raw stdev/mean, so a single runaway sentence cannot inflate the dispersion and falsely read "bursty/human."
- **Presence detectors** — `repetition` (repeated two-word openers), `punctuation` (em-dash/semicolon overuse), and `lexicon` (AI-favored vocabulary and phrases) only register their tell when it is *present*. Their tell is often absent even in AI text, and absence is **not** evidence of a human (you cannot infer human authorship from "no em-dash"), so an absent tell maps to the neutral midpoint `ABSENCE_FLOOR` (≈`0.5`), never to `0.0`. This is what stops the intermittent markers from burying the burstiness signal: a clearly-AI passage no longer gets dragged to a human score just because it happens to lack repeated phrasing or em-dashes.

The `*_REF` mapping constants and the floor are heuristics to calibrate during Milestone 4, not fixed in this spec.

The marker set was narrowed from an earlier five-marker draft after researching how each behaves for AI vs. human text. Two candidates — lexical diversity (type-token ratio) and readability/complexity — were dropped: their direction flips by domain and comparison group, TTR is also length-fragile, and both are the markers most likely to misfire on this platform's creative content (poems, lyrics), which is the dominant false-positive risk the system is designed to avoid. The `lexicon` marker added later is *not* a return to TTR: it is presence-only (a curated list of AI-favored words, discourse phrases, and `-ing` sentence openers, normalized per 1000 words), so it never claims human authorship and does not re-introduce TTR's direction-flip or length-fragility — an absent tell simply floors at `0.5`. Its residual risk is the mirror of punctuation's: human marketing/self-help copy may use a few listed phrases, so it too is contained as one of four equal, presence-floored markers with a high reference rate; the fix if real traffic shows a false positive is to prune the offending phrase, not widen the floor. `repetition` is keyed on two-word openers rather than single words so ordinary "The…/If…" technical prose is not mistaken for AI anaphora. Burstiness moved from raw CV to rCV after measuring a labeled corpus (`corpus.json`, via `eval_harness.py`): raw stdev/mean was outlier-fragile — one long sentence inflated it into a false "human" reading — and rCV widened the median AI/human separation at the same directional accuracy. Text whose only AI tell is uniform sentence length stays `uncertain` on this signal alone and relies on `llm_judgement` to corroborate, keeping the higher evidence burden on the AI label.

For `pattern_analysis`, short-but-valid submissions or direct signal tests with too little stable text return near `0.5` instead of pretending the metrics are reliable. The `/submit` endpoint still rejects text under 50 trimmed characters.

---

## Confidence Scoring

The implementation uses the `confidence` field as the combined AI-evidence score, not a generic certainty score. A low value on a `likely_human` attribution means low evidence of AI-generated or AI-assisted text, not low confidence in the human label. A `0.60` stays uncertain; a `0.95` means strong AI evidence.

Signal scores are combined with a simple average:

```text
confidence = (llm_score + pattern_score) / 2
```

This also handles signal disagreement. If one signal is AI-leaning and the other is human-leaning, the average moves the attribution toward the uncertain middle.

| Confidence range | Attribution | Meaning |
| --- | --- | --- |
| `0.00 <= confidence < 0.40` | `likely_human` | The signals found low AI evidence. |
| `0.40 <= confidence <= 0.65` | `uncertain` | The signals are mixed or too close to the middle. |
| `0.65 < confidence <= 1.00` | `likely_ai` | The signals found substantial AI-like evidence. |

The thresholds are asymmetric: text must clear `0.65` to be labeled `likely_ai` but only drop below `0.40` to be labeled `likely_human`, putting a higher evidence burden on the AI label. That asymmetry — plus cautious label wording and the appeals path — is how the system reflects that mislabeling a human's work as AI is the more harmful error, without a separate false-positive subsystem.

---

## Transparency Labels

These exact strings are returned by the API and repeated in the README.

| Attribution | Label text |
| --- | --- |
| `likely_ai` | "This submission appears likely to include substantial AI-generated or AI-assisted text based on our review. The creator may request review if they believe this label is wrong." |
| `likely_human` | "This submission appears likely to be human-written based on our review. AI detection is not definitive, but our signals did not find substantial AI-generated or AI-assisted content." |
| `uncertain` | "Attribution uncertain: our review could not confidently determine whether this submission is human-written or includes substantial AI-generated or AI-assisted text." |

The labels avoid claiming proof of authorship. They communicate the attribution direction, acknowledge uncertainty, and leave room for appeal.

---

## Appeals Workflow

A creator can appeal any content item whose status is `classified` by submitting the `content_id` and `creator_reasoning`. The API looks up that submission's audit row by `content_id`, rejects unknown IDs and content already `under_review` (a duplicate active appeal), and updates the row in place — setting `status` to `under_review` and storing the `creator_reasoning` in the row's `appeal_reasoning` field. The appeal is recorded on the same row as the original classification, not as a separate event.

A human reviewer would see the original text or text preview, attribution result, confidence score, individual signal scores, the appeal reasoning, current status, and timestamps.

---

## API Surface

### `POST /submit`

Accepts submitted text for attribution analysis.

Request body:

```json
{
  "text": "[submitted content]",
  "creator_id": "[creator id]",
  "content_id": "[optional; API generates one if missing]"
}
```

Success response:

```json
{
  "content_id": "[content id]",
  "attribution": "[likely_human | uncertain | likely_ai]",
  "confidence": 0.0,
  "label": "[transparency label text]",
  "status": "classified",
  "signals": [
    {"name": "llm_judgement", "score": 0.0},
    {"name": "pattern_analysis", "score": 0.0}
  ]
}
```

Validation errors: missing `text`, blank `text`, text under 50 trimmed characters, or missing/blank `creator_id`.

### `POST /appeal`

Accepts a creator appeal for an existing classification.

Request body:

```json
{
  "content_id": "[content id]",
  "creator_reasoning": "[creator appeal reason]"
}
```

Success response:

```json
{
  "content_id": "[content id]",
  "status": "under_review",
  "message": "Appeal received; this submission is now under review."
}
```

Validation errors: missing `content_id`, unknown `content_id`, missing/blank `creator_reasoning`, or content already `under_review`.

### `GET /log`

Returns recent SQLite-backed audit entries as JSON, wrapped as `{"entries": [...]}`, for grading evidence and local inspection.

---

## Audit Log

SQLite is the storage choice because the app needs to look up classifications by `content_id`, update status on appeal, prevent duplicate active appeals, and return recent structured entries through `GET /log`.

There is one row per submission, keyed by `content_id`. A classification writes the row; an appeal updates that same row in place. Each row stores:

- `content_id`
- `creator_id`
- `timestamp`
- `attribution`
- `confidence`
- `status` — `classified`, flipped to `under_review` when the content is appealed
- `llm_score`
- `llm_rationale`
- `pattern_score`
- `pattern_markers`
- `appeal_reasoning` — `null` until an appeal is filed, then set to the creator's reasoning

The transparency `label` text is not stored — it is derived from `attribution` when building the `/submit` response, so storing it would only duplicate the attribution category.

An appeal is not a separate record: `POST /appeal` runs an `UPDATE ... WHERE content_id = ?` that sets `status = under_review` and fills `appeal_reasoning`, leaving the original `attribution`, `confidence`, and signal scores intact. "Whether an appeal has been filed" is therefore visible on the row itself — `appeal_reasoning` is populated and `status` is `under_review`.

For the README evidence, `GET /log` shows at least three rows, at least one of which has been appealed (its `status` is `under_review` and its `appeal_reasoning` is populated).

---

## Rate Limiting

Rate limit `POST /submit` to:

```text
10 per minute; 100 per day
```

The limit is per client IP. A normal creator may submit a few drafts in a session, but should not need more than 10 classifications in a minute. The daily limit allows normal testing and use while limiting sustained abuse. When the limit is hit, the API returns HTTP `429 Too Many Requests`.

---

## Anticipated Edge Cases

**Poems or song lyrics:** lyrical work may use repetition, fragments, short lines, or unusual punctuation on purpose. Pattern metrics can misread those choices, so mixed or unstable scores move toward `uncertain`.

**Technical papers or documentation:** technical writing often repeats terms, uses standardized formatting, and stays structurally consistent. That can look AI-like to both signals, so middle-range scores remain `uncertain` and AI-leaning labels stay cautious and appealable.

---

## AI Tool Plan

**Milestone 3 - Submission endpoint and first signal:**

I will use Claude Code in a new session, starting in plan mode. The inputs are the Architecture section and its diagram, plus the Detection Signals, API Surface, and Audit Log sections from this planning document. From that, Claude Code writes a plan for the Flask app skeleton, `POST /submit`, `llm_judgement`, SQLite setup, and `GET /log`. I review the plan against this spec and adjust anything that is off before implementation. I will verify request validation, content ID generation, the first signal output, the response fields, and the first audit entries. In Milestone 3, `/submit` returns the full response shape with placeholders for what cannot yet be computed: a generated `content_id`, the `attribution` from the single signal, a `confidence` score, a placeholder transparency `label`, `status`, and a `signals` array carrying the `llm_judgement` score. With only one signal live, `confidence` equals `llm_score`, and `attribution` comes from applying the Confidence Scoring thresholds to that value; Milestone 4 changes `confidence` to the `(llm_score + pattern_score) / 2` average and recomputes `attribution` from it. The audit log starts simple — `content_id`, `creator_id`, `timestamp`, `attribution`, `confidence`, `llm_score`, `llm_rationale`, and `status` — and grows in Milestone 4 (the `pattern_analysis` score) and Milestone 5 (appeal status). The real `label` transparency text arrives in Milestone 5.

Deliverables:
- Flask app skeleton and configuration
- SQLite audit log schema and `GET /log`
- `POST /submit` with request validation, content ID generation, and the full response shape (`attribution` from signal 1, `confidence` = `llm_score`, placeholder `label`, and the `signals` array)
- `llm_judgement` signal wired to Groq (`llm_score` + `llm_rationale`)
- Verify: valid submissions write an audit-log row, validation errors reject before the log, audit entries are readable via `GET /log`

**Milestone 4 - Second signal and confidence scoring:**

In a new Claude Code session, I will provide the Detection Signals, Confidence Scoring, Architecture (with its diagram), API Surface, and Audit Log sections, plus the Milestone 3 implementation. From that, Claude Code writes a plan for `pattern_analysis` and the scoring function. I review the plan against this spec, especially the score direction and threshold ranges, before implementation. I will test both signals independently and then together on at least four inputs: clearly AI-like, clearly human-like, a technical/formal borderline case, and a lightly edited AI-style case.

Deliverables:
- `pattern_analysis` signal with per-marker scores (sentence-length variation via robust rCV, repetition, punctuation, and AI-favored lexicon) and averaging into `pattern_score`
- Confidence scoring updated to `(llm_score + pattern_score) / 2`
- `attribution` mapping confirmed against the `0.40` / `0.65` thresholds, now applied to the combined `confidence` (the single-signal version shipped in Milestone 3)
- Audit log extended to record `pattern_score` alongside `llm_score` and the combined `confidence`
- Test both signals independently and together on at least four inputs: clearly AI-like, clearly human-like, technical/formal borderline, lightly edited AI-style
- Verify: score direction, threshold boundaries, and both signal scores stored in the audit log

**Milestone 5 - Production layer:**

In a new Claude Code session, I will provide the Transparency Labels, API Surface, Appeals Workflow, Audit Log, Rate Limiting, and Architecture (with its diagram) sections, plus the current implementation. From that, Claude Code writes a plan for label mapping, `POST /appeal`, Flask-Limiter setup, and final audit-log updates. I review the plan against this spec before implementation. I will verify that all three labels are reachable, appeals update status to `under_review`, duplicate active appeals are rejected, `GET /log` shows the appeal, and rate limiting returns `429`. Once Milestone 5 passes local verification, a fresh Codex GPT review session with a different model reviews the completed source files against this planning document and the README draft to catch missing evidence, mismatched labels or thresholds, validation gaps, audit-log gaps, and spec drift before final submission.

Deliverables:
- Transparency label strings mapped to `likely_human`, `uncertain`, and `likely_ai`, reachable by submitting inputs at different confidence levels
- `POST /appeal` (accepts `content_id` + `creator_reasoning`) with status update (`classified` → `under_review`), the reasoning stored as `appeal_reasoning` alongside the original decision, and rejection of duplicate active appeals
- Flask-Limiter rate limiting on `POST /submit` (`10/min`, `100/day` per IP via `storage_uri="memory://"`, returning `429`), with the chosen limits documented in the README
- Final audit log coverage: each row carries both signal scores, the combined `confidence`, and (when appealed) `under_review` status with `appeal_reasoning` populated
- Verify: all three labels are reachable, appeal updates status and appears in `GET /log`, rate limiting triggers `429`
- Fresh Codex GPT review of the completed source against this spec and the README draft
