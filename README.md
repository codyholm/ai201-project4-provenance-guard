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

## Architecture overview

The path a submission takes from input to the label a reader sees:

```text
POST /submit → validation + rate limit → llm_judgement (semantic)
                                        → pattern_analysis (structural)
             → confidence = (llm_score + pattern_score) / 2
             → attribution (likely_human | uncertain | likely_ai)
             → transparency label text
             → SQLite audit row (status = classified)
             → JSON response
```

A request is first validated (non-blank `text` of at least 50 trimmed
characters, non-blank `creator_id`) and rate limited per IP; rejected requests
never reach the audit log because no decision was made. A `content_id` is
generated if the caller did not supply one. The text then runs through both
detection signals, whose scores are averaged into a single `confidence` value.
That value maps to one of three attributions, each attribution maps to a
plain-language transparency label, and the whole decision — both signal scores,
the combined confidence, and the attribution — is written as one row to the
SQLite audit log before the JSON response is returned. A later `POST /appeal`
updates that same row in place. See `planning.md` for the full flow diagrams.

## Detection signals

The two signals are deliberately on **different axes** — one semantic, one
structural — so the combination is more informative than either alone.

### `llm_judgement` — semantic

- **What it measures:** a Groq-hosted LLM (`llama-3.3-70b-versatile`) judges the
  passage as a whole — generic phrasing, overly polished or uniform structure,
  repeated rhetorical moves, and whether a distinct human voice is present.
  Returns `llm_score` (0.0–1.0) and a short `llm_rationale`.
- **Why this signal:** it captures holistic semantic and stylistic coherence that
  no fixed rule can express — the "does this read like a person wrote it"
  judgment. It is the semantic complement to the mechanical second signal.
- **What it misses:** LLMs can be overconfident; polished or formal human writing
  can read as AI-assisted, and lightly edited AI text can read as human. It also
  depends on an external API and is not perfectly deterministic, so it is
  prompted to score near `0.5` when evidence is weak rather than guess.

### `pattern_analysis` — structural

- **What it measures:** four deterministic, offline stylometric markers, each
  mapped to 0.0–1.0 and averaged equally into `pattern_score`:
  - **sentence-length variation** — burstiness via a *robust* CV (median absolute
    deviation over the median). Human writing alternates long and short
    sentences; AI regresses to a uniform length. Bidirectional.
  - **repetition** — repeated two-word sentence openers and verbatim bigrams.
  - **punctuation** — em-dash/semicolon density, a modern AI tell.
  - **lexicon** — AI-favored vocabulary, discourse phrases, and `-ing` sentence
    openers, normalized per 1000 words.
- **Why this signal:** it is transparent, reproducible, and free of any external
  dependency, and it measures the mechanical *shape and vocabulary* of the text —
  a genuinely different property from the LLM's semantic read. Each per-marker
  score is exposed in `pattern_markers` so a decision can be inspected.
- **What it misses:** lyrical/poetic writing, technical documentation, very short
  text, and heavily edited AI can all mislead it. Three of the four markers are
  *presence-only* — their tell (repeated phrasing, em-dashes, AI vocabulary) is
  often absent even in AI text, and absence is **not** evidence of a human, so an
  absent tell floors at the neutral `0.5` rather than claiming "human." Burstiness
  is the one marker that can lean AI on genuinely human but uniform casual prose;
  that lean is contained by averaging and by the LLM signal.

## Confidence scoring

Both signals share one direction — `0.0` = strong human evidence, `0.5` =
uncertain, `1.0` = strong AI evidence — so `confidence` is the AI-evidence score,
not a generic certainty. A low value on a `likely_human` result means low
evidence of AI, not low confidence in the human label. The two signals are
combined with a simple equal-weight average:

```text
confidence = (llm_score + pattern_score) / 2
```

Averaging is also how signal disagreement is resolved: when one signal leans AI
and the other leans human, the mean pulls toward the uncertain middle rather than
committing to either label — the false-positive-safe outcome.

| Confidence | Attribution |
| --- | --- |
| `0.00 <= c < 0.40` | `likely_human` |
| `0.40 <= c <= 0.65` | `uncertain` |
| `0.65 < c <= 1.00` | `likely_ai` |

The thresholds are asymmetric on purpose: text must clear `0.65` to be labeled
`likely_ai` but only drop below `0.40` to be labeled `likely_human`, because
mislabeling a human's work as AI is the more harmful error on a creative platform.

**Validating that the scores are meaningful.** The score has to *vary* across
inputs, not hover near a constant. I tested the four inputs from the project
guide (clearly AI, clearly human, formal-human borderline, lightly edited AI) end
to end and confirmed all three labels are reachable with genuinely different
scores. The deterministic signal was separately calibrated against a labeled
21-sample corpus (`corpus.json`, via `eval_harness.py`), which is how the
burstiness marker's robust-CV reference and the AI/human separation were checked.

| Submission | `llm_score` | `pattern_score` | `confidence` | Attribution |
| --- | --- | --- | --- | --- |
| Clearly AI (formal essay) | 0.75 | 0.680 | **0.715** | `likely_ai` |
| Formal-human borderline | 0.75 | 0.500 | 0.625 | `uncertain` |
| Casual human (ramen review) | 0.20 | 0.549 | 0.375 | `likely_human` |
| Lightly edited AI | 0.25 | 0.677 | **0.464** | `uncertain` |

**Two examples with noticeably different confidence:** the clearly-AI formal essay
scores **0.715** (`likely_ai`) — a high-confidence result where both signals
agree. The lightly edited AI passage scores **0.464** (`uncertain`) — a
lower-confidence result where the signals disagree (the LLM reads it as human at
0.25 while the structural markers read AI at 0.677), so the average lands in the
uncertain band instead of forcing a verdict. Same pipeline, very different scores.

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

## Known limitations

**Uniform, formal human writing** is the content type this system is most likely
to misclassify — think academic abstracts, cover letters, legal or financial
prose. Both signals push the same wrong direction on it: `llm_judgement` reads
polished, generic, structurally consistent prose as AI-assisted, and the
`pattern_analysis` burstiness marker reads uniform sentence lengths as the AI
tell. Neither signal can distinguish *disciplined human formality* from AI
uniformity, because that distinction is not present in sentence shape or surface
style. In testing, the formal-human monetary-policy passage scored `0.625` —
`uncertain`, right at the edge of `likely_ai`. This is why the AI label carries
the higher evidence burden (`> 0.65`) and why appeals exist: a formal human
writer who lands in `uncertain` is never *accused*, and can contest.

A second known-weak case is **poetry and song lyrics**, where deliberate
repetition, fragments, and unconventional punctuation can trip the presence
markers; those inputs tend toward `uncertain` rather than a confident verdict.

## Spec reflection

**Where the spec helped:** writing the three transparency-label strings and the
confidence-band thresholds (`0.40` / `0.65`) into `planning.md` *before* any code
meant Milestones 4–5 had concrete targets to implement against. The scoring
function and label selector were built to fixed thresholds and exact label text,
which is what kept confidence from collapsing into a binary flip at `0.5` — the
uncertain band was a designed range, not an afterthought.

**Where implementation diverged:** the spec described `pattern_analysis` as
*three* markers (sentence-length variation, repetition, punctuation) using a raw
coefficient of variation for burstiness. The implementation diverged to *four*
markers using a *robust* CV. Calibrating against a labeled corpus showed raw
stdev/mean was outlier-fragile — a single long sentence inflated it and flipped
otherwise-uniform AI text to a false "human" reading — so burstiness moved to
median-absolute-deviation over the median. A fourth, presence-only `lexicon`
marker was then added to cover an AI-vocabulary dimension the original three
missed. The combiner, thresholds, and response shape stayed exactly as specified.
This divergence was anticipated: the spec explicitly labeled the marker set and
`*_REF` constants as "heuristics to calibrate during Milestone 4, not fixed," so
measurement was expected to refine them.

## AI usage

**1. Designing the second signal's marker set.** I directed the AI to propose
stylometric markers for `pattern_analysis`. It produced a five-marker draft that
included type-token ratio (lexical diversity) and a readability/complexity score.
I **overrode** both: their direction flips by domain and comparison group, and
they are exactly the markers most likely to misfire on creative content (poems,
lyrics) — the platform's core false-positive risk. I narrowed the set and, in a
later pass, directed the switch of burstiness from raw CV to robust rCV after
corpus measurement, and the addition of a presence-only `lexicon` marker. I
**revised** the lexicon's integration to stay one-of-four equal and floored at
`0.5` (rather than a heavier weight) after confirming on the labeled corpus that
it produced zero false positives on human samples.

**2. Confidence thresholds and the meaning of the score.** I directed the AI to
implement the confidence-to-label mapping. I **overrode** the intuitive symmetric
split around `0.5` and set asymmetric thresholds (`0.40` / `0.65`) so the AI label
requires more evidence than the human label — reflecting that a false AI
accusation is the more harmful error. I also **decided** that the `confidence`
field means *AI-evidence*, not generic certainty, and revised the label wording
and documentation to match so a low score on a human verdict is not misread as
"low confidence."

**3. Exploring a third signal for the ensemble stretch (rejected).** I directed
the AI to prototype a compression/entropy-based third signal to chase the
ensemble-detection bonus. Rather than accept it, I had it **calibrate against the
corpus first** — which showed raw compression ratio was largely a text-length
proxy (correlation ≈ 0.53) with weak class separation, and character entropy gave
no separation at all. I **overrode** the plan and kept the two-signal design
rather than ship a signal that measured length more than authorship for the
points.
