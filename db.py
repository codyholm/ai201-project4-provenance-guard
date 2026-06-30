import json
import sqlite3

DB_PATH = "audit_log.db"

# Used for both the INSERT column list and the get_log SELECT projection,
# so the stored and returned shapes always match.
ENTRY_COLUMNS = [
    "content_id", "creator_id", "timestamp", "attribution",
    "confidence", "llm_score", "llm_rationale",
    "pattern_score", "pattern_markers", "status",
    "appeal_reasoning",
]

# Columns whose Python value is a dict/list and is stored as JSON text.
JSON_COLUMNS = {"pattern_markers"}

# Precomputed projection string shared by the INSERT and SELECT below.
COLUMNS_SQL = ", ".join(ENTRY_COLUMNS)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                content_id TEXT NOT NULL UNIQUE,
                creator_id TEXT,
                timestamp TEXT NOT NULL,
                attribution TEXT,
                confidence REAL,
                llm_score REAL,
                llm_rationale TEXT,
                pattern_score REAL,
                pattern_markers TEXT,
                status TEXT,
                appeal_reasoning TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def log_event(entry: dict) -> int:
    placeholders = ", ".join("?" for _ in ENTRY_COLUMNS)
    values = []
    for col in ENTRY_COLUMNS:
        value = entry.get(col)
        if col in JSON_COLUMNS and value is not None:
            value = json.dumps(value)
        values.append(value)
    conn = _connect()
    try:
        cursor = conn.execute(
            f"INSERT INTO audit_log ({COLUMNS_SQL}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    # Decode JSON-stored columns back to Python objects; shared by the readers
    # so the returned shape always matches what log_event stored.
    record = dict(row)
    for col in JSON_COLUMNS:
        if record.get(col) is not None:
            record[col] = json.loads(record[col])
    return record


def get_log(limit: int = 50) -> list[dict]:
    conn = _connect()
    try:
        cursor = conn.execute(
            f"SELECT {COLUMNS_SQL} FROM audit_log ORDER BY rowid DESC LIMIT ?",
            (limit,),
        )
        return [_row_to_dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_event(content_id: str) -> dict | None:
    # Look up a single submission by content_id for the appeal flow. Returns None
    # for an unknown id so the caller can distinguish 404 from already-appealed.
    conn = _connect()
    try:
        cursor = conn.execute(
            f"SELECT {COLUMNS_SQL} FROM audit_log WHERE content_id = ? LIMIT 1",
            (content_id,),
        )
        row = cursor.fetchone()
        return _row_to_dict(row) if row is not None else None
    finally:
        conn.close()


def update_appeal(content_id: str, reasoning: str) -> int:
    # In-place appeal update: flip status and store the reasoning on the original
    # row, leaving attribution/confidence/signal scores intact. Returns rowcount.
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE audit_log SET status = 'under_review', appeal_reasoning = ? "
            "WHERE content_id = ?",
            (reasoning, content_id),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
