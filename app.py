import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify

import db
import signals

MIN_TEXT_LENGTH = 50
PLACEHOLDER_LABEL = "<a clearly-temporary string; real transparency text arrives in Milestone 5>"

app = Flask(__name__)

# Create the audit_log table once at import so the first request can write.
db.init_db()


def classify_attribution(confidence: float) -> str:
    if confidence < 0.40:
        return "likely_human"
    if confidence <= 0.65:
        return "uncertain"
    return "likely_ai"


@app.route("/submit", methods=["POST"])
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
    supplied_content_id = body.get("content_id")
    if isinstance(supplied_content_id, str) and supplied_content_id.strip():
        content_id = supplied_content_id
    else:
        content_id = str(uuid.uuid4())

    try:
        result = signals.run_llm_judgement(text)
    except signals.LLMJudgementError:
        return jsonify({"error": "Detection signal unavailable; submission not recorded"}), 503

    llm_score = result["llm_score"]
    confidence = llm_score
    attribution = classify_attribution(confidence)

    entry = {
        "content_id": content_id,
        "creator_id": creator_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attribution": attribution,
        "confidence": confidence,
        "llm_score": llm_score,
        "llm_rationale": result["llm_rationale"],
        "status": "classified",
    }
    db.log_event(entry)

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": confidence,
        "label": PLACEHOLDER_LABEL,
        "status": "classified",
        "signals": [{"name": "llm_judgement", "score": llm_score}],
    })


@app.route("/log", methods=["GET"])
def log():
    return jsonify({"entries": db.get_log()})


if __name__ == "__main__":
    app.run(debug=True)
