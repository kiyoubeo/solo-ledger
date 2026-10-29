"""3-Stage Reconciliation Engine for merging SMS drafts, OCR receipts, and Hometax records."""

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict

from sololedger.models import ReconcileItem
from sololedger.tax_engine import calculate_supply_and_vat, evaluate_tax_rules


def upsert_merchant(cursor: sqlite3.Cursor, item: ReconcileItem) -> None:
    """Insert or update merchant master record when biz_reg_no is provided."""
    if not item.biz_reg_no:
        return
    clean_biz_no = item.biz_reg_no.replace("-", "").strip()
    if not clean_biz_no:
        return
    cursor.execute(
        """
        INSERT INTO merchants (
            biz_reg_no, merchant_name, tax_type_code, biz_status, nts_verified_at
        )
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(biz_reg_no) DO UPDATE SET
            merchant_name = excluded.merchant_name,
            tax_type_code = excluded.tax_type_code,
            biz_status = excluded.biz_status,
            nts_verified_at = CURRENT_TIMESTAMP
        """,
        (clean_biz_no, item.merchant_name, item.tax_type_code, item.biz_status),
    )


def reconcile_single_item(cursor: sqlite3.Cursor, item: ReconcileItem) -> Dict[str, Any]:
    """Reconcile an incoming Hometax / OCR item using the 3-stage matching algorithm:

    1. Stage 1 (Exact Approval Number + Amount match, or Approval Number match for FX settlement)
    2. Stage 2 (Exact Amount + Timestamp within ±10 minutes on SMS_ONLY record)
    3. Stage 3 (Force Insert new ledger record if no existing match found)
    """
    clean_biz_no = item.biz_reg_no.replace("-", "").strip() if item.biz_reg_no else None
    if clean_biz_no:
        item.biz_reg_no = clean_biz_no
        upsert_merchant(cursor, item)

    nts_non_deductible = item.tax_type_code in ("SIMPLIFIED", "EXEMPT", "UNREGISTERED") or (
        item.biz_status in ("SUSPENDED", "CLOSED")
    )

    matched_tx_id = None
    match_stage = None

    # Stage 1: Approval number match (exact amount first, then approval_no fallback for FX settlement)
    if item.approval_no:
        cursor.execute(
            """
            SELECT tx_id FROM card_transactions
            WHERE approval_no = ? AND ABS(total_amount) = ABS(?)
            ORDER BY tx_id ASC LIMIT 1
            """,
            (item.approval_no, item.total_amount),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                SELECT tx_id FROM card_transactions
                WHERE approval_no = ?
                ORDER BY tx_id ASC LIMIT 1
                """,
                (item.approval_no,),
            )
            row = cursor.fetchone()
        if row:
            matched_tx_id = row[0]
            match_stage = "STAGE_1_APPROVAL_NO"

    # Stage 2: Total amount + transacted_at within ±10 minutes (SMS_ONLY)
    if matched_tx_id is None:
        try:
            dt = datetime.strptime(item.transacted_at[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            window_start = (dt - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            window_end = (dt + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """
                SELECT tx_id FROM card_transactions
                WHERE ABS(total_amount) = ABS(?)
                  AND reconcile_status = 'SMS_ONLY'
                  AND transacted_at BETWEEN ? AND ?
                ORDER BY ABS(strftime('%s', transacted_at) - strftime('%s', ?)) ASC
                LIMIT 1
                """,
                (item.total_amount, window_start, window_end, dt.strftime("%Y-%m-%d %H:%M:%S")),
            )
            row = cursor.fetchone()
            if row:
                matched_tx_id = row[0]
                match_stage = "STAGE_2_AMOUNT_TIME_WINDOW"
        except ValueError:
            pass

    target_status = "CANCELED" if item.is_cancel else item.reconcile_source

    if matched_tx_id is not None:
        if item.tax_type_code == "EXEMPT":
            supply_val, vat_val = calculate_supply_and_vat(item.total_amount, "EXEMPT")
            cursor.execute(
                """
                UPDATE card_transactions
                SET approval_no = COALESCE(?, approval_no),
                    biz_reg_no = COALESCE(?, biz_reg_no),
                    total_amount = ?,
                    supply_value = ?,
                    vat_amount = ?,
                    vat_deduction_type = 'EXEMPT',
                    non_deduct_reason = COALESCE(non_deduct_reason, 'SIMPLIFIED_TAX'),
                    reconcile_status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tx_id = ?
                """,
                (
                    item.approval_no,
                    clean_biz_no,
                    item.total_amount,
                    supply_val,
                    vat_val,
                    target_status,
                    matched_tx_id,
                ),
            )
        elif nts_non_deductible:
            supply_val, vat_val = calculate_supply_and_vat(item.total_amount, "NON_DEDUCTIBLE")
            cursor.execute(
                """
                UPDATE card_transactions
                SET approval_no = COALESCE(?, approval_no),
                    biz_reg_no = COALESCE(?, biz_reg_no),
                    total_amount = ?,
                    supply_value = ?,
                    vat_amount = ?,
                    vat_deduction_type = 'NON_DEDUCTIBLE',
                    non_deduct_reason = COALESCE(non_deduct_reason, 'SIMPLIFIED_TAX'),
                    reconcile_status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tx_id = ?
                """,
                (
                    item.approval_no,
                    clean_biz_no,
                    item.total_amount,
                    supply_val,
                    vat_val,
                    target_status,
                    matched_tx_id,
                ),
            )
        else:
            cursor.execute(
                "SELECT vat_deduction_type FROM card_transactions WHERE tx_id = ?",
                (matched_tx_id,),
            )
            existing_tx = cursor.fetchone()
            existing_vat_type = (
                existing_tx["vat_deduction_type"] if existing_tx else "DEDUCTIBLE"
            )
            supply_val, vat_val = calculate_supply_and_vat(item.total_amount, existing_vat_type)
            cursor.execute(
                """
                UPDATE card_transactions
                SET approval_no = COALESCE(?, approval_no),
                    biz_reg_no = COALESCE(?, biz_reg_no),
                    total_amount = ?,
                    supply_value = ?,
                    vat_amount = ?,
                    reconcile_status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tx_id = ?
                """,
                (
                    item.approval_no,
                    clean_biz_no,
                    item.total_amount,
                    supply_val,
                    vat_val,
                    target_status,
                    matched_tx_id,
                ),
            )
        return {
            "tx_id": matched_tx_id,
            "action": "updated",
            "match_stage": match_stage,
            "reconcile_status": target_status,
        }

    # Stage 3: Force insert new transaction record
    rule_result = evaluate_tax_rules(cursor, item.merchant_name, clean_biz_no)
    vat_ded_type = rule_result["vat_deduction_type"]
    non_ded_reason = rule_result["non_deduct_reason"]
    if item.tax_type_code == "EXEMPT":
        vat_ded_type = "EXEMPT"
    elif nts_non_deductible:
        vat_ded_type = "NON_DEDUCTIBLE"
        non_ded_reason = non_ded_reason or "SIMPLIFIED_TAX"

    supply_val, vat_val = calculate_supply_and_vat(item.total_amount, vat_ded_type)
    cursor.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name,
            biz_reg_no, total_amount, supply_value, vat_amount,
            account_code, account_name, vat_deduction_type,
            non_deduct_reason, is_vehicle_expense, reconcile_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.card_last4,
            item.approval_no,
            item.transacted_at[:19].replace("T", " "),
            item.merchant_name,
            clean_biz_no,
            item.total_amount,
            supply_val,
            vat_val,
            rule_result["account_code"],
            rule_result["account_name"],
            vat_ded_type,
            non_ded_reason,
            rule_result["is_vehicle_expense"],
            target_status,
        ),
    )
    return {
        "tx_id": cursor.lastrowid,
        "action": "inserted",
        "match_stage": "STAGE_3_FORCE_INSERT",
        "reconcile_status": target_status,
    }
