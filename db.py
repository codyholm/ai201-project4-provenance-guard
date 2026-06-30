import json
import sqlite3

DB_PATH = "audit_log.db"

# Used for both the INSERT column list and the get_log SELECT projection,
# so the stored and returned shapes always match.
ENTRY_COLUMNS = [
    "content_id", "creator_id", "timestamp", "attribution",
    "confidence", "llm_score", "llm_rationale",
    "pattern_score", "pattern_markers", "status",
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
                content_id TEXT NOT NULL,
                creator_id TEXT,
                timestamp TEXT NOT NULL,
                attribution TEXT,
                confidence REAL,
                llm_score REAL,
                llm_rationale TEXT,
                pattern_score REAL,
                pattern_markers TEXT,
                status TEXT
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


def get_log(limit: int = 50) -> list[dict]:
    conn = _connect()
    try:
        cursor = conn.execute(
            f"SELECT {COLUMNS_SQL} FROM audit_log ORDER BY rowid DESC LIMIT ?",
            (limit,),
        )
        entries = []
        for row in cursor.fetchall():
            record = dict(row)
            for col in JSON_COLUMNS:
                if record.get(col) is not None:
                    record[col] = json.loads(record[col])
            entries.append(record)
        return entries
    finally:
        conn.close()
