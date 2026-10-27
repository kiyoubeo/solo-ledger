"""Tests for SQLite schema DDL and seed data integrity."""


def test_core_tables_exist(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    tables = {row[0] for row in cur.fetchall()}
    assert tables == {
        "merchants",
        "card_transactions",
        "receipt_ocr_metadata",
        "tax_classification_rules",
    }


def test_indexes_exist(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='card_transactions'"
    )
    indexes = {row[0] for row in cur.fetchall()}
    assert "idx_tx_transacted_at" in indexes
    assert "idx_tx_approval_no" in indexes
    assert "idx_tx_reconcile_status" in indexes
    assert "idx_tx_exported_ym" in indexes


def test_seed_tax_rules_loaded(db_conn):
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM tax_classification_rules WHERE is_active = 1")
    count = cur.fetchone()[0]
    assert count == 4
