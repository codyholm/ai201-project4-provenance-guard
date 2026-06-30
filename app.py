import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import db
import signals

MIN_TEXT_LENGTH = 50

# Transparency text returned to readers, keyed by attribution. These exact
# strings are the spec's §Transparency Labels and are repeated in the README.
LABELS = {
    "likely_ai": (
        "This submission appears likely to include substantial AI-generated or "
        "AI-assisted text based on our review. The creator may request review if "
        "they believe this label is wrong."
    ),
    "likely_human": (
        "This submission appears likely to be human-written based on our review. "
        "AI detection is not definitive, but our signals did not find substantial "
        "AI-generated or AI-assisted content."
    ),
    "uncertain": (
        "Attribution uncertain: our review could not confidently determine "
        "whether this submission is human-written or includes substantial "
        "AI-generated or AI-assisted text."
    ),
}

app = Flask(__name__)

# Per-IP rate limiting on /submit. memory:// keeps counters in-process (they
# reset on restart), which is enough for this single-instance app.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="memory://",
)

# Create the audit_log table once at import so the first request can write.
db.init_db()


def classify_attribution(confidence: float) -> str:
    if confidence < 0.40:
        return "likely_human"
    if confidence <= 0.65:
        return "uncertain"
    return "likely_ai"


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute")
@limiter.limit("100 per day")
def submit():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    # Validate in order; return on the first failure and write nothing.
    text = body.get("text")
    stripped = text.strip() if isinstance(text, str) else ""
    if not stripped:
        return jsonify({"error": "Field 'text' is required and must be a non-empty string"}), 400
    if len(stripped) < MIN_TEXT_LENGTH:
        return jsonify({"error": f"Field 'text' must be at least {MIN_TEXT_LENGTH} characters"}), 400

    creator_id = body.get("creator_id")
    if not isinstance(creator_id, str) or not creator_id.strip():
        return jsonify({"error": "Field 'creator_id' is required and must be a non-empty string"}), 400

    # Use a caller-supplied non-blank content_id as-is, otherwise generate one.
    # A supplied id must be new: reject a collision here (before the Groq call) so
    # the one-row-per-content_id invariant the appeal flow relies on holds. The
    # UNIQUE column constraint is the backstop; generated UUIDs cannot collide.
    supplied_content_id = body.get("content_id")
    if isinstance(supplied_content_id, str) and supplied_content_id.strip():
        content_id = supplied_content_id
        if db.get_event(content_id) is not None:
            return jsonify({"error": "content_id already exists; submit a new id or omit it"}), 409
    else:
        content_id = str(uuid.uuid4())

    try:
        result = signals.run_llm_judgement(text)
    except signals.LLMJudgementError:
        return jsonify({"error": "Detection signal unavailable; submission not recorded"}), 503

    llm_score = result["llm_score"]

    # Second signal is deterministic and offline; it has no failure path.
    pattern = signals.run_pattern_analysis(text)
    pattern_score = pattern["pattern_score"]

    confidence = (llm_score + pattern_score) / 2
    attribution = classify_attribution(confidence)

    entry = {
        "content_id": content_id,
        "creator_id": creator_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attribution": attribution,
        "confidence": confidence,
        "llm_score": llm_score,
        "llm_rationale": result["llm_rationale"],
        "pattern_score": pattern_score,
        "pattern_markers": pattern["pattern_markers"],
        "status": "classified",
    }
    db.log_event(entry)

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": confidence,
        "label": LABELS[attribution],
        "status": "classified",
        "signals": [
            {"name": "llm_judgement", "score": llm_score},
            {"name": "pattern_analysis", "score": pattern_score},
        ],
    })


@app.route("/appeal", methods=["POST"])
def appeal():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    # Validate inputs first, then look up the row, then check its status.
    content_id = body.get("content_id")
    if not isinstance(content_id, str) or not content_id.strip():
        return jsonify({"error": "Field 'content_id' is required and must be a non-empty string"}), 400

    creator_reasoning = body.get("creator_reasoning")
    if not isinstance(creator_reasoning, str) or not creator_reasoning.strip():
        return jsonify({"error": "Field 'creator_reasoning' is required and must be a non-empty string"}), 400

    record = db.get_event(content_id)
    if record is None:
        return jsonify({"error": "Unknown content_id"}), 404
    if record["status"] == "under_review":
        return jsonify({"error": "This submission is already under review"}), 409

    db.update_appeal(content_id, creator_reasoning)
    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": "Appeal received; this submission is now under review.",
    })


@app.route("/log", methods=["GET"])
def log():
    return jsonify({"entries": db.get_log()})


@app.errorhandler(429)
def ratelimit_exceeded(error):
    # Flask-Limiter raises a 429; return it as JSON so it matches our other errors.
    return jsonify({"error": "Rate limit exceeded; try again later"}), 429


if __name__ == "__main__":
    app.run(debug=True)
