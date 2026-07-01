# Provenance Guard

A Flask API for a creative-sharing platform. It accepts submitted text, runs two
attribution signals, combines them into a confidence score, returns the
transparency label a reader would see, and records the decision in a SQLite audit
log for review or appeal.

The system does not claim AI detection is certain. Scores near the middle return
`uncertain`, the AI label carries a higher evidence burden than the human label,
and creators can appeal a classification they believe is wrong.

See `planning.md` for the full design rationale.

## Setup

```bash
pip install -r requirements.txt
```

Set `GROQ_API_KEY` in a `.env` file (used by the `llm_judgement` signal):

```text
GROQ_API_KEY=your_key_here
```

Run the app:

```bash
python app.py            # or: flask --app app run
```

## Detection signals

| Signal | What it captures | Output |
| --- | --- | --- |
| `llm_judgement` | A Groq-hosted LLM judges the passage as a whole (generic phrasing, overly polished structure, repeated rhetorical moves, voice). | `llm_score` 0.0–1.0 + a short rationale. |
| `pattern_analysis` | Deterministic stylometric markers: sentence-length variation, repeated two-word openers, em-dash/semicolon density. | `pattern_score` 0.0–1.0 + per-marker scores. |

Both use the same direction: `0.0` = strong human evidence, `0.5` = uncertain,
`1.0` = strong AI evidence. Confidence is their average:

```text
confidence = (llm_score + pattern_score) / 2
```

| Confidence | Attribution |
| --- | --- |
| `0.00 <= c < 0.40` | `likely_human` |
| `0.40 <= c <= 0.65` | `uncertain` |
| `0.65 < c <= 1.00` | `likely_ai` |

The thresholds are asymmetric on purpose: text must clear `0.65` to be labeled
`likely_ai` but only drop below `0.40` to be labeled `likely_human`, because
mislabeling a human's work as AI is the more harmful error.

## Transparency labels

Each attribution returns one of these exact strings:

- **`likely_ai`** — "This submission appears likely to include substantial
  AI-generated or AI-assisted text based on our review. The creator may request
  review if they believe this label is wrong."
- **`likely_human`** — "This submission appears likely to be human-written based
  on our review. AI detection is not definitive, but our signals did not find
  substantial AI-generated or AI-assisted content."
- **`uncertain`** — "Attribution uncertain: our review could not confidently
  determine whether this submission is human-written or includes substantial
  AI-generated or AI-assisted text."

## API

### `POST /submit`

Accepts submitted text for attribution analysis.

Request:

```json
{
  "text": "[submitted content]",
  "creator_id": "[creator id]",
  "content_id": "[optional; the API generates one if missing]"
}
```

Response:

```json
{
  "content_id": "[content id]",
  "attribution": "likely_human | uncertain | likely_ai",
  "confidence": 0.0,
  "label": "[transparency label text]",
  "status": "classified",
  "signals": [
    {"name": "llm_judgement", "score": 0.0},
    {"name": "pattern_analysis", "score": 0.0}
  ]
}
```

Validation errors (`400`): missing or blank `text`, `text` under 50 trimmed
characters, or missing/blank `creator_id`. A caller-supplied `content_id` that
already exists returns `409` (one row per `content_id`). If the detection signal
is unavailable the API returns `503` and records nothing.

### `POST /appeal`

Files a creator appeal against an existing classification. It updates the original
audit row in place — `status` `classified` → `under_review`, with the reasoning
stored in `appeal_reasoning` — rather than writing a separate record.

Request:

```json
{
  "content_id": "[content id]",
  "creator_reasoning": "[creator appeal reason]"
}
```

Response:

```json
{
  "content_id": "[content id]",
  "status": "under_review",
  "message": "Appeal received; this submission is now under review."
}
```

Errors: missing/blank `content_id` or `creator_reasoning` (`400`), unknown
`content_id` (`404`), submission already `under_review` (`409`, a duplicate
active appeal).

### `GET /log`

Returns recent audit entries as `{"entries": [...]}`. Each row carries
`content_id`, `creator_id`, `timestamp`, `attribution`, `confidence`, both signal
scores (`llm_score`/`llm_rationale`, `pattern_score`/`pattern_markers`), `status`,
and `appeal_reasoning` (`null` until appealed).

## Rate limiting

`POST /submit` is rate limited **per client IP** to:

```text
10 per minute
100 per day
```

A normal creator may submit a few drafts in a session but should not need more
than 10 classifications a minute; the daily cap allows normal use while limiting
sustained abuse. Exceeding either limit returns HTTP `429 Too Many Requests`.
Counters are held in-process (`storage_uri="memory://"`) and reset on restart.

## Audit log

SQLite (`audit_log.db`), one row per submission keyed by `content_id`. A
classification writes the row; an appeal updates that same row in place via
`UPDATE ... WHERE content_id = ?`, so "whether an appeal has been filed" is
visible on the row itself (`status` is `under_review` and `appeal_reasoning` is
populated). The transparency `label` is not stored — it is derived from
`attribution` when building the response.

For grading evidence, `GET /log` shows at least three rows, at least one of which
has been appealed.
