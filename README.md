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

**Does the score actually vary?** A confidence score is only useful if it moves
with the input instead of hovering near a constant. Four inputs that should land
differently — clearly AI, clearly human, a formal-human borderline, and lightly
edited AI — produce three distinct labels across a wide score range (below). The
stylometric signal was tuned against a labeled 21-sample corpus (`corpus.json`,
via `eval_harness.py`): that is where the burstiness marker's robust-CV reference
came from and where the human/AI separation was measured.

| Submission | `llm_score` | `pattern_score` | `confidence` | Attribution |
| --- | --- | --- | --- | --- |
| Clearly AI (formal essay) | 0.75 | 0.680 | **0.715** | `likely_ai` |
| Formal-human borderline | 0.75 | 0.500 | 0.625 | `uncertain` |
| Casual human (ramen review) | 0.20 | 0.474 | 0.337 | `likely_human` |
| Lightly edited AI | 0.25 | 0.594 | **0.422** | `uncertain` |

All figures are real `/submit` responses, from the same run shown in the audit
log below.

The spread is meaningful. The clearly-AI formal essay scores **0.715**
(`likely_ai`), where both signals agree. The lightly edited AI passage scores
**0.422** (`uncertain`): the LLM reads it as human (0.25) while the structural
markers read AI (0.594), so the average settles in the uncertain band instead of
forcing a verdict. Same pipeline, very different outcomes.

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

For example, 12 rapid `POST /submit` requests against the `10 per minute` limit:

```text
request  1 -> 200
request  2 -> 200
request  3 -> 200
request  4 -> 200
request  5 -> 200
request  6 -> 200
request  7 -> 200
request  8 -> 200
request  9 -> 200
request 10 -> 200
request 11 -> 429
request 12 -> 429
```

The 11th request onward returns the JSON error body:

```json
{"error": "Rate limit exceeded; try again later"}
```

## Audit log

SQLite (`audit_log.db`), one row per submission keyed by `content_id`. A
classification writes the row; an appeal updates that same row in place via
`UPDATE ... WHERE content_id = ?`, so "whether an appeal has been filed" is
visible on the row itself (`status` is `under_review` and `appeal_reasoning` is
populated). The transparency `label` is not stored — it is derived from
`attribution` when building the response.

An example `GET /log` response, after submitting the four sample inputs and
appealing the one classified `likely_ai`. Each row keeps both signal scores, the
combined confidence, and a timestamp; the appealed row shows `status:
under_review` with the creator's `appeal_reasoning`:

```json
{
  "entries": [
    {
      "content_id": "32ad31c7-1f62-471c-ab2e-89f5f9f66256",
      "creator_id": "demo-ai-edited",
      "timestamp": "2026-07-01T07:06:41.236410+00:00",
      "attribution": "uncertain",
      "confidence": 0.4219,
      "llm_score": 0.25,
      "llm_rationale": "The passage has a conversational tone and raises a nuanced point about remote work, suggesting a human writer, but its structure and language are clear and polished, which could also be characteristic of AI-assisted writing.",
      "pattern_score": 0.5938,
      "pattern_markers": {"sentence_length_variation": 0.6667, "repetition": 0.5, "punctuation": 0.7083, "lexicon": 0.5},
      "status": "classified",
      "appeal_reasoning": null
    },
    {
      "content_id": "e44ff0ee-0804-4f94-873f-f2b10a1443c0",
      "creator_id": "demo-human-formal",
      "timestamp": "2026-07-01T07:06:40.713894+00:00",
      "attribution": "uncertain",
      "confidence": 0.625,
      "llm_score": 0.75,
      "llm_rationale": "The passage has a formal and polished tone, with generic phrasing and a structured approach, which suggests a potential AI-generated or AI-assisted origin, but the topic-specific terminology and nuanced discussion of economic concepts introduce some uncertainty.",
      "pattern_score": 0.5,
      "pattern_markers": {"sentence_length_variation": 0.5, "repetition": 0.5, "punctuation": 0.5, "lexicon": 0.5},
      "status": "classified",
      "appeal_reasoning": null
    },
    {
      "content_id": "0dcdd5e9-1f83-4311-96e0-2bda837efa2e",
      "creator_id": "demo-human-casual",
      "timestamp": "2026-07-01T07:06:40.186484+00:00",
      "attribution": "likely_human",
      "confidence": 0.33675,
      "llm_score": 0.2,
      "llm_rationale": "The text has a casual, conversational tone and includes personal opinions and experiences, which suggests a human writer, but the language is simple and lacks distinctiveness, which could also be characteristic of AI-generated text.",
      "pattern_score": 0.4735,
      "pattern_markers": {"sentence_length_variation": 0.3939, "repetition": 0.5, "punctuation": 0.5, "lexicon": 0.5},
      "status": "classified",
      "appeal_reasoning": null
    },
    {
      "content_id": "dc4c81b2-518d-4638-9e4b-0ad8cbae35e0",
      "creator_id": "demo-ai-formal",
      "timestamp": "2026-07-01T07:06:39.688994+00:00",
      "attribution": "likely_ai",
      "confidence": 0.7151,
      "llm_score": 0.75,
      "llm_rationale": "The passage has a polished and uniform structure, uses generic phrasing, and lacks a distinct human voice, suggesting that it may be AI-generated or AI-assisted, but the presence of a clear and coherent argument prevents a higher score.",
      "pattern_score": 0.6802,
      "pattern_markers": {"sentence_length_variation": 0.697, "repetition": 0.5238, "punctuation": 0.5, "lexicon": 1.0},
      "status": "under_review",
      "appeal_reasoning": "I wrote this essay myself for a university course. I am a non-native English speaker and my academic writing is more formal and structured than casual prose, which I think the detector mistook for AI."
    }
  ]
}
```

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

This system was designed on paper (`planning.md`) before any code, and two things
stand out looking back.

**What the upfront design bought:** fixing the three label strings and the
confidence bands (`0.40` / `0.65`) before writing the scorer gave the label logic
concrete targets from the start. The uncertain band is a deliberately designed
range, not a fallback — which is what keeps the system from collapsing into a
binary flip at `0.5`.

**Where the build moved off the plan:** `pattern_analysis` started as three
markers (sentence-length variation, repetition, punctuation) with a plain
coefficient of variation for burstiness. It ended up as four, with a *robust* CV
instead. Measuring against the labeled corpus showed plain stdev/mean was
outlier-fragile — one long sentence could inflate it and flip otherwise-uniform
AI text to a false "human" reading — so burstiness moved to median-absolute-
deviation over the median. A presence-only `lexicon` marker was added afterward to
cover a vocabulary dimension the first three missed. The combiner, thresholds, and
response shape never changed. The marker set was always meant to be settled by
measurement rather than guessed up front — and it was.

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

**3. Exploring a third detection signal (rejected).** I had the AI prototype a
compression/entropy-based third signal to see whether it would add real
information. Instead of taking it on faith, I made it **calibrate against the
corpus first** — which showed raw compression ratio was largely a text-length
proxy (correlation ≈ 0.53) with weak class separation, and character entropy gave
no separation at all. I **dropped it** and kept the two-signal design rather than
ship a signal that measured length more than it measured authorship.
