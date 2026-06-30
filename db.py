import sqlite3

DB_PATH = "audit_log.db"

# Used for both the INSERT column list and the get_log SELECT projection,
# so the stored and returned shapes always match.
ENTRY_COLUMNS = [
    "content_id", "creator_id", "timestamp", "attribution",
    "confidence", "llm_score", "llm_rationale", "status",
]

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
                status TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def log_event(entry: dict) -> int:
    placeholders = ", ".join("?" for _ in ENTRY_COLUMNS)
    values = [entry.get(col) for col in ENTRY_COLUMNS]
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
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
