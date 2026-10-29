"""Pytest fixtures for isolated SQLite database, archive directory, and API testing."""

import pytest
from fastapi.testclient import TestClient

from sololedger.api import app
from sololedger.db import get_connection, init_db


@pytest.fixture()
def test_db_path(tmp_path, monkeypatch):
    """Provide a temporary SQLite database and archive directory initialized with schema.sql."""
    db_file = tmp_path / "test_sololedger.db"
    archive_dir = tmp_path / "archive"
    monkeypatch.setenv("SOLOLEDGER_DB_PATH", str(db_file))
    monkeypatch.setenv("SOLOLEDGER_ARCHIVE_DIR", str(archive_dir))
    monkeypatch.setenv("SOLOLEDGER_API_KEY", "test-secret-key")
    init_db(str(db_file))
    return str(db_file)


@pytest.fixture()
def db_conn(test_db_path):
    """Yield a SQLite connection to the initialized test database."""
    conn = get_connection(test_db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def client(test_db_path):
    """Provide a FastAPI TestClient wired to the isolated test DB."""
    with TestClient(app) as c:
        yield c
