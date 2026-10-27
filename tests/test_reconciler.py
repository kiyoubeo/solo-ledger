"""Unit tests for the 3-stage reconciliation engine."""

from sololedger.models import ReconcileItem
from sololedger.reconciler import reconcile_single_item


def test_reconcile_stage1_approval_no_match(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '1234', '87654321', '2026-10-20 12:00:00', '스타벅스',
            11000, 10000, 1000, '811', '복리후생비', 'DEDUCTIBLE', 'SMS_ONLY'
        )
        """
    )
    tx_id = cur.lastrowid

    item = ReconcileItem(
        card_last4="1234",
        approval_no="87654321",
        transacted_at="2026-10-20 12:05:00",
        merchant_name="스타벅스 코리아",
        biz_reg_no="201-81-21515",
        total_amount=11000,
        tax_type_code="GENERAL",
        biz_status="ACTIVE",
    )
    res = reconcile_single_item(cur, item)
    assert res["tx_id"] == tx_id
    assert res["action"] == "updated"
    assert res["match_stage"] == "STAGE_1_APPROVAL_NO"

    cur.execute("SELECT biz_reg_no, reconcile_status FROM card_transactions WHERE tx_id = ?", (tx_id,))
    row = cur.fetchone()
    assert row["biz_reg_no"] == "2018121515"
    assert row["reconcile_status"] == "HOMETAX_VERIFIED"


def test_reconcile_stage2_time_window_match_and_simplified_tax_override(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '9876', NULL, '2026-10-20 18:30:00', '동네식당',
            22000, 20000, 2000, '811', '복리후생비', 'DEDUCTIBLE', 'SMS_ONLY'
        )
        """
    )
    tx_id = cur.lastrowid

    # Timestamp 4 minutes later, simplified taxpayer -> should override to NON_DEDUCTIBLE
    item = ReconcileItem(
        card_last4="9876",
        approval_no="99887766",
        transacted_at="2026-10-20 18:34:00",
        merchant_name="동네식당",
        biz_reg_no="123-45-67890",
        total_amount=22000,
        tax_type_code="SIMPLIFIED",
        biz_status="ACTIVE",
    )
    res = reconcile_single_item(cur, item)
    assert res["tx_id"] == tx_id
    assert res["action"] == "updated"
    assert res["match_stage"] == "STAGE_2_AMOUNT_TIME_WINDOW"

    cur.execute(
        "SELECT approval_no, vat_deduction_type, non_deduct_reason FROM card_transactions WHERE tx_id = ?",
        (tx_id,),
    )
    row = cur.fetchone()
    assert row["approval_no"] == "99887766"
    assert row["vat_deduction_type"] == "NON_DEDUCTIBLE"
    assert row["non_deduct_reason"] == "SIMPLIFIED_TAX"


def test_reconcile_stage3_force_insert(db_conn):
    cur = db_conn.cursor()
    item = ReconcileItem(
        card_last4="5555",
        approval_no="11223344",
        transacted_at="2026-10-21 15:00:00",
        merchant_name="AWS CLOUD",
        biz_reg_no="999-88-77777",
        total_amount=33000,
        tax_type_code="GENERAL",
        biz_status="ACTIVE",
    )
    res = reconcile_single_item(cur, item)
    assert res["action"] == "inserted"
    assert res["match_stage"] == "STAGE_3_FORCE_INSERT"

    cur.execute("SELECT account_code, account_name FROM card_transactions WHERE tx_id = ?", (res["tx_id"],))
    row = cur.fetchone()
    assert row["account_code"] == "831"
    assert row["account_name"] == "지급수수료"
