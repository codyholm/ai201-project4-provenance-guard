import os
import json

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
