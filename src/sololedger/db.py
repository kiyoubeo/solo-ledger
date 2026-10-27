"""SQLite database initialization and connection utilities."""

import sqlite3
from pathlib import Path
from typing import Optional

from sololedger.config import settings


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Create a SQLite connection with foreign keys and row factory enabled."""
    target_path = db_path or settings.db_path
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Optional[str] = None, schema_path: Optional[Path] = None) -> None:
    """Initialize database schema and seed rules idempotently."""
    sql_file = schema_path or settings.schema_path
    ddl_script = sql_file.read_text(encoding="utf-8")
    conn = get_connection(db_path)
    try:
        conn.executescript(ddl_script)
        conn.commit()
    finally:
        conn.close()
