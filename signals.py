import os
import json
import re
import statistics

from dotenv import load_dotenv
from groq import Groq

# Load .env so GROQ_API_KEY is available in the environment.
load_dotenv()

# Fail fast at import: a missing key surfaces here, not on the first request.
api_key = os.environ.get("GROQ_API_KEY")
if not api_key:
    raise RuntimeError(
        "GROQ_API_KEY is missing or empty; set it in .env or the environment."
    )

# Construct the Groq client once at module load.
client = Groq(api_key=api_key)

MODEL = "llama-3.3-70b-versatile"
TEMPERATURE = 0.2
MAX_TOKENS = 200
REQUEST_TIMEOUT = 30


class LLMJudgementError(Exception):
    pass


SYSTEM_PROMPT = """You are an expert reviewer for a creative-sharing platform. Judge whether a
submitted passage was written by a human or generated or assisted by an AI
language model. Consider the passage as a whole: generic phrasing, overly
polished or uniform structure, repeated rhetorical moves, and the presence or
absence of a distinct human voice.

Return a single JSON object with exactly two fields:
- "llm_score": a number from 0.0 to 1.0 for how much evidence there is that the
  text is AI-generated or AI-assisted. Use the full continuous range; the values
  below are reference anchors describing the scale, not the only allowed values:
    0.00 - strong evidence the text is human-written
    0.25 - leans human
    0.50 - mixed or genuinely uncertain
    0.75 - leans AI-generated or AI-assisted
    1.00 - strong evidence the text is AI-generated or AI-assisted
- "llm_rationale": one or two sentences explaining the score in plain language.

AI detection is not certain. When the evidence is weak or conflicting, score near
0.5 rather than guessing. Output only the JSON object, with no surrounding text."""


def _call_groq(text: str) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_object"},
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        timeout=REQUEST_TIMEOUT,
    )
    return response.choices[0].message.content


def _parse_and_validate(content: str) -> dict | None:
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    llm_score = parsed.get("llm_score")
    # bool is a subclass of int, so reject it explicitly before the numeric check.
    if isinstance(llm_score, bool) or not isinstance(llm_score, (int, float)):
        return None
    if not (0.0 <= llm_score <= 1.0):
        return None

    llm_rationale = parsed.get("llm_rationale")
    if not isinstance(llm_rationale, str) or not llm_rationale.strip():
        return None

    return {"llm_score": float(llm_score), "llm_rationale": llm_rationale}


def run_llm_judgement(text: str) -> dict:
    for _ in range(2):
        try:
            content = _call_groq(text)
        except Exception as exc:
            # Transport/API failure: raise immediately, no retry.
            raise LLMJudgementError("Groq request failed") from exc

        result = _parse_and_validate(content)
        if result is not None:
            return result
        # Bad/unparseable content: fall through and retry once.

    raise LLMJudgementError("Model returned invalid output after one retry")


# --- pattern_analysis: deterministic stylometric markers --------------------
#
# Score direction matches llm_judgement: 0.0 = strong human evidence, 0.5 =
# uncertain, 1.0 = strong AI evidence.
#
# Two kinds of marker, averaged equally:
#   * sentence_length_variation is BIDIRECTIONAL — bursty sentence lengths are
#     genuine human evidence and uniform lengths are genuine AI evidence, so it
#     uses the full 0.0–1.0 range.
#   * repetition and punctuation are PRESENCE detectors — their tell (repeated
#     phrasing, em-dash/semicolon overuse) is often absent even in AI text, and
#     absence is not evidence of a human. So absence maps to ABSENCE_FLOOR (the
#     neutral midpoint — no information, not a human lean) and a present tell
#     pushes up toward 1.0.
#
# The *_REF values and the floor are starting calibration heuristics (Milestone
# 4), not fixed truths — tune them during verification.
CV_REF = 0.6          # sentence-length CV at/above which text reads fully human
OPENER_REF = 0.5      # repeated-opener fraction at/above which text reads fully AI
PUNCT_REF = 0.8       # em-dash+semicolon per-sentence rate that reads fully AI
ABSENCE_FLOOR = 0.5   # neutral score for a presence-only marker whose tell is absent
# Stability gate: below this little text the markers are noise. Three sentences
# is the real floor (variance needs spread); the word floor only rules out
# ultra-short text — a handful of short sentences is still measurable.
MIN_STABLE_WORDS = 25
MIN_STABLE_SENTENCES = 3

_SENTENCE_SPLIT = re.compile(r"[.!?]+")
_WORD = re.compile(r"\b\w+\b")


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _presence_score(raw_signal: float, ref: float) -> float:
    # Presence-only marker: an absent tell (raw 0) sits at the floor; a tell at
    # or above `ref` reaches 1.0. Absence is neutral (no information), not human
    # evidence, so the floor is the midpoint.
    return ABSENCE_FLOOR + (1 - ABSENCE_FLOOR) * _clamp(raw_signal / ref)


def _sentence_length_variation(words_per_sentence: list[int]) -> float:
    # Human writing is "bursty" — long and short sentences alternate. AI text
    # regresses to a uniform mean length, so low variation is the AI tell. This
    # marker is bidirectional: high variation is real human evidence (no floor).
    mean = statistics.mean(words_per_sentence)
    if mean == 0:
        return 0.5
    cv = statistics.stdev(words_per_sentence) / mean
    return _clamp(1 - cv / CV_REF)


def _repetition(sentences: list[str], words: list[str]) -> float:
    # AI re-uses phrasing far more than humans. Repeated TWO-WORD sentence
    # openers ("We must… We must…", "It is… It is…") are the most reliable tell.
    # Single-word openers over-fire on ordinary "The…/If…" technical prose, so we
    # key on the first two words; exact repeated bigrams catch verbatim copying.
    sent_words = [_WORD.findall(s.lower()) for s in sentences]
    openers = [tuple(w[:2]) for w in sent_words if len(w) >= 2]
    opener_rep = 1 - len(set(openers)) / len(openers) if openers else 0.0
    bigrams = list(zip(words, words[1:]))
    bigram_rep = 1 - len(set(bigrams)) / len(bigrams) if bigrams else 0.0
    return _presence_score(max(opener_rep, bigram_rep), OPENER_REF)


def _punctuation(text: str, sentence_count: int) -> float:
    # Em-dash / semicolon overuse is the strongest modern AI punctuation tell.
    # Presence-only: its absence does not prove human authorship, and a human who
    # favors em-dashes should not be dragged far — hence the floor and the high
    # PUNCT_REF, so a stray dash barely moves this one of three equal markers.
    marks = text.count("—") + text.count("--") + text.count("–") + text.count(";")
    return _presence_score(marks / sentence_count, PUNCT_REF)


def run_pattern_analysis(text: str) -> dict:
    sentences = [s for s in (part.strip() for part in _SENTENCE_SPLIT.split(text)) if s]
    words = _WORD.findall(text.lower())

    # Too little stable text to measure: stay neutral rather than pretend the
    # metrics are reliable (the /submit 50-char minimum still lets short-but-
    # valid text reach this gate, where it correctly lands at 0.5).
    if len(words) < MIN_STABLE_WORDS or len(sentences) < MIN_STABLE_SENTENCES:
        neutral = {
            "sentence_length_variation": 0.5,
            "repetition": 0.5,
            "punctuation": 0.5,
        }
        return {"pattern_score": 0.5, "pattern_markers": neutral}

    words_per_sentence = [len(_WORD.findall(s)) for s in sentences]
    markers = {
        "sentence_length_variation": round(_sentence_length_variation(words_per_sentence), 4),
        "repetition": round(_repetition(sentences, words), 4),
        "punctuation": round(_punctuation(text, len(sentences)), 4),
    }
    pattern_score = round(statistics.mean(markers.values()), 4)
    return {"pattern_score": pattern_score, "pattern_markers": markers}
