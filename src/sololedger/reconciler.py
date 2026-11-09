"""3-Stage Reconciliation Engine for merging SMS drafts, OCR receipts, and Hometax records."""

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sololedger.models import ReconcileItem
from sololedger.tax_engine import (
    calculate_supply_and_vat,
    evaluate_tax_rules,
    generate_default_memo,
)


def _resolve_supply_and_vat(item: ReconcileItem, vat_deduction_type: str) -> tuple[int, int]:
    """Use explicit Hometax supply_value/vat_amount when provided and valid, else calculate."""
    if (
        item.supply_value is not None
        and item.vat_amount is not None
        and (item.supply_value + item.vat_amount == item.total_amount)
    ):
        return item.supply_value, item.vat_amount
    return calculate_supply_and_vat(item.total_amount, vat_deduction_type)


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


def reconcile_single_item(
    cursor: sqlite3.Cursor,
    item: ReconcileItem,
    exclude_tx_ids: Optional[set[int]] = None,
) -> Dict[str, Any]:
    """Reconcile an incoming Hometax / OCR item using the 3-stage matching algorithm:

    1. Stage 1 (Exact Approval Number + Amount match, or Approval Number match for FX settlement)
    2. Stage 2 (Exact Amount + Timestamp within ±10 minutes on SMS_ONLY record)
    3. Stage 3 (Force Insert new ledger record if no existing match found)
    """
    excluded = exclude_tx_ids or set()
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
            ORDER BY tx_id ASC
            """,
            (item.approval_no, item.total_amount),
        )
        rows = [r[0] for r in cursor.fetchall() if r[0] not in excluded]
        if not rows:
            cursor.execute(
                """
                SELECT tx_id FROM card_transactions
                WHERE approval_no = ?
                ORDER BY tx_id ASC
                """,
                (item.approval_no,),
            )
            rows = [r[0] for r in cursor.fetchall() if r[0] not in excluded]
        if rows:
            matched_tx_id = rows[0]
            match_stage = "STAGE_1_APPROVAL_NO"

    # Stage 2: Total amount + transacted_at within ±10 minutes (SMS_ONLY, EMAIL_ORDER, or CANCELED)
    if matched_tx_id is None:
        try:
            dt = datetime.strptime(item.transacted_at[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            window_start = (dt - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            window_end = (dt + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            allowed_statuses = (
                ("SMS_ONLY", "EMAIL_ORDER", "CANCELED")
                if item.is_cancel
                else ("SMS_ONLY", "EMAIL_ORDER")
            )
            placeholders = ",".join("?" for _ in allowed_statuses)
            cursor.execute(
                f"""
                SELECT tx_id FROM card_transactions
                WHERE ABS(total_amount) = ABS(?)
                  AND reconcile_status IN ({placeholders})
                  AND transacted_at BETWEEN ? AND ?
                ORDER BY ABS(strftime('%s', transacted_at) - strftime('%s', ?)) ASC
                """,
                (
                    item.total_amount,
                    *allowed_statuses,
                    window_start,
                    window_end,
                    dt.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            rows = [r[0] for r in cursor.fetchall() if r[0] not in excluded]
            if rows:
                matched_tx_id = rows[0]
                match_stage = "STAGE_2_AMOUNT_TIME_WINDOW"
        except ValueError:
            pass

    # Stage 2b: Same-day fallback for billing statements without approval_no (e.g. Hyundai Card / Hometax .xls)
    if (
        matched_tx_id is None
        and not item.approval_no
        and item.reconcile_source != "OCR_MATCHED"
        and len(item.transacted_at) >= 10
    ):
        date_prefix = item.transacted_at[:10]
        cursor.execute(
            """
            SELECT tx_id FROM card_transactions
            WHERE SUBSTR(transacted_at, 1, 10) = ?
              AND (card_last4 = ? OR reconcile_status = 'EMAIL_ORDER')
              AND total_amount = ?
              AND (reconcile_status IN ('SMS_ONLY', 'EMAIL_ORDER') OR raw_merchant_name = ?)
            ORDER BY CASE WHEN card_last4 = ? THEN 0 ELSE 1 END ASC, tx_id ASC
            """,
            (
                date_prefix,
                item.card_last4,
                item.total_amount,
                item.merchant_name,
                item.card_last4,
            ),
        )
        rows = [r[0] for r in cursor.fetchall() if r[0] not in excluded]
        if rows:
            matched_tx_id = rows[0]
            match_stage = "STAGE_2_AMOUNT_TIME_WINDOW"

    target_status = "CANCELED" if item.is_cancel else item.reconcile_source

    if matched_tx_id is not None:
        cursor.execute(
            """
            SELECT card_last4, raw_merchant_name, account_code, account_name,
                   vat_deduction_type, non_deduct_reason, counterparty_info, reconcile_status
            FROM card_transactions WHERE tx_id = ?
            """,
            (matched_tx_id,),
        )
        existing_tx = cursor.fetchone()
        old_card = str(existing_tx["card_last4"] or "0000") if existing_tx else "0000"
        old_status = str(existing_tx["reconcile_status"] or "") if existing_tx else ""
        resolved_card = (
            item.card_last4
            if item.card_last4
            and item.card_last4 != "0000"
            and (old_card == "0000" or old_status == "EMAIL_ORDER")
            else old_card
        )
        old_merchant = str(existing_tx["raw_merchant_name"] or "") if existing_tx else ""
        old_code = str(existing_tx["account_code"] or "999") if existing_tx else "999"
        old_name = str(existing_tx["account_name"] or "미분류") if existing_tx else "미분류"
        old_vat = (
            str(existing_tx["vat_deduction_type"] or "DEDUCTIBLE")
            if existing_tx
            else "DEDUCTIBLE"
        )
        old_reason = existing_tx["non_deduct_reason"] if existing_tx else None
        old_memo = existing_tx["counterparty_info"] if existing_tx else None

        new_merchant = old_merchant
        is_statement_sync = item.reconcile_source != "OCR_MATCHED"
        if is_statement_sync and item.merchant_name and item.merchant_name != "미상가맹점":
            if "보험" in item.merchant_name or old_code == "999":
                new_merchant = item.merchant_name

        re_eval = evaluate_tax_rules(
            cursor,
            new_merchant,
            clean_biz_no,
            biz_category=item.biz_category,
            biz_item=item.biz_item,
        )
        if is_statement_sync and (
            old_code == "999"
            or (new_merchant != old_merchant and re_eval["account_code"] != "999")
        ):
            old_code = re_eval["account_code"]
            old_name = re_eval["account_name"]
            old_vat = re_eval["vat_deduction_type"]
            old_reason = re_eval["non_deduct_reason"]

        if is_statement_sync and not old_memo:
            old_memo = generate_default_memo(
                old_code, new_merchant, re_eval.get("require_memo_prompt", False)
            )

        if item.tax_type_code == "EXEMPT":
            supply_val, vat_val = _resolve_supply_and_vat(item, "EXEMPT")
            cursor.execute(
                """
                UPDATE card_transactions
                SET card_last4 = ?,
                    approval_no = COALESCE(?, approval_no),
                    biz_reg_no = COALESCE(?, biz_reg_no),
                    raw_merchant_name = ?,
                    account_code = ?,
                    account_name = ?,
                    counterparty_info = COALESCE(counterparty_info, ?),
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
                    resolved_card,
                    item.approval_no,
                    clean_biz_no,
                    new_merchant,
                    old_code,
                    old_name,
                    old_memo,
                    item.total_amount,
                    supply_val,
                    vat_val,
                    target_status,
                    matched_tx_id,
                ),
            )
        elif nts_non_deductible:
            supply_val, vat_val = _resolve_supply_and_vat(item, "NON_DEDUCTIBLE")
            cursor.execute(
                """
                UPDATE card_transactions
                SET card_last4 = ?,
                    approval_no = COALESCE(?, approval_no),
                    biz_reg_no = COALESCE(?, biz_reg_no),
                    raw_merchant_name = ?,
                    account_code = ?,
                    account_name = ?,
                    counterparty_info = COALESCE(counterparty_info, ?),
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
                    resolved_card,
                    item.approval_no,
                    clean_biz_no,
                    new_merchant,
                    old_code,
                    old_name,
                    old_memo,
                    item.total_amount,
                    supply_val,
                    vat_val,
                    target_status,
                    matched_tx_id,
                ),
            )
        else:
            supply_val, vat_val = _resolve_supply_and_vat(item, old_vat)
            cursor.execute(
                """
                UPDATE card_transactions
                SET card_last4 = ?,
                    approval_no = COALESCE(?, approval_no),
                    biz_reg_no = COALESCE(?, biz_reg_no),
                    raw_merchant_name = ?,
                    account_code = ?,
                    account_name = ?,
                    vat_deduction_type = ?,
                    non_deduct_reason = ?,
                    counterparty_info = COALESCE(counterparty_info, ?),
                    total_amount = ?,
                    supply_value = ?,
                    vat_amount = ?,
                    reconcile_status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tx_id = ?
                """,
                (
                    resolved_card,
                    item.approval_no,
                    clean_biz_no,
                    new_merchant,
                    old_code,
                    old_name,
                    old_vat,
                    old_reason,
                    old_memo,
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
    rule_result = evaluate_tax_rules(
        cursor,
        item.merchant_name,
        clean_biz_no,
        biz_category=item.biz_category,
        biz_item=item.biz_item,
    )
    vat_ded_type = rule_result["vat_deduction_type"]
    non_ded_reason = rule_result["non_deduct_reason"]
    if item.tax_type_code == "EXEMPT":
        vat_ded_type = "EXEMPT"
        non_ded_reason = non_ded_reason or "SIMPLIFIED_TAX"
    elif nts_non_deductible:
        vat_ded_type = "NON_DEDUCTIBLE"
        non_ded_reason = non_ded_reason or "SIMPLIFIED_TAX"

    default_memo = (
        generate_default_memo(
            rule_result["account_code"],
            item.merchant_name,
            rule_result.get("require_memo_prompt", False),
        )
        if item.reconcile_source != "OCR_MATCHED"
        else None
    )
    supply_val, vat_val = _resolve_supply_and_vat(item, vat_ded_type)
    cursor.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name,
            biz_reg_no, total_amount, supply_value, vat_amount,
            account_code, account_name, vat_deduction_type,
            non_deduct_reason, counterparty_info, is_vehicle_expense, reconcile_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            default_memo,
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
