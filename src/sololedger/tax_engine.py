"""Automated tax classification rule engine and VAT calculation logic."""

import re
import sqlite3
from typing import Any, Dict, Optional, Tuple

from sololedger.merchant_service import ACCOUNT_CODE_NAMES


def calculate_supply_and_vat(
    total_amount: int, vat_deduction_type: str = "DEDUCTIBLE"
) -> Tuple[int, int]:
    """Calculate supply_value and vat_amount from total KRW amount.

    For EXEMPT transactions, supply_value equals total_amount and vat_amount is 0.
    Otherwise, supply_value is round(total_amount / 1.1) and vat_amount is the remainder.
    """
    if vat_deduction_type == "EXEMPT":
        return total_amount, 0
    supply_value = int(round(total_amount / 1.1))
    vat_amount = total_amount - supply_value
    return supply_value, vat_amount


def _apply_cached_merchant_overrides(
    cursor: sqlite3.Cursor,
    base_result: Dict[str, Any],
    merchant_name: str,
    biz_reg_no: Optional[str] = None,
) -> Dict[str, Any]:
    """Enrich rule evaluation result with cached merchant tax status and default account."""
    row = None
    if biz_reg_no:
        clean_no = biz_reg_no.replace("-", "").strip()
        cursor.execute("SELECT * FROM merchants WHERE biz_reg_no = ?", (clean_no,))
        row = cursor.fetchone()
    if row is None and merchant_name:
        cursor.execute(
            "SELECT * FROM merchants WHERE merchant_name = ? ORDER BY nts_verified_at DESC LIMIT 1",
            (merchant_name,),
        )
        row = cursor.fetchone()

    if row is None:
        return base_result

    result = dict(base_result)
    if row["default_account_code"] and result["account_code"] == "999":
        code = row["default_account_code"]
        result["account_code"] = code
        result["account_name"] = ACCOUNT_CODE_NAMES.get(code, "지정계정")

    if row["is_pg_provider"]:
        result["require_memo_prompt"] = True

    tax_type = row["tax_type_code"]
    biz_status = row["biz_status"]
    if tax_type == "EXEMPT":
        result["vat_deduction_type"] = "EXEMPT"
        result["non_deduct_reason"] = result["non_deduct_reason"] or "SIMPLIFIED_TAX"
    elif tax_type in ("SIMPLIFIED", "UNREGISTERED") or biz_status in ("SUSPENDED", "CLOSED"):
        result["vat_deduction_type"] = "NON_DEDUCTIBLE"
        result["non_deduct_reason"] = result["non_deduct_reason"] or "SIMPLIFIED_TAX"
    elif tax_type == "GENERAL" and biz_status == "ACTIVE" and result["vat_deduction_type"] == "PENDING":
        result["vat_deduction_type"] = "DEDUCTIBLE"

    return result


def evaluate_tax_rules(
    cursor: sqlite3.Cursor, merchant_name: str, biz_reg_no: Optional[str] = None
) -> Dict[str, Any]:
    """Evaluate merchant_name against active tax_classification_rules and cached merchant data."""
    cursor.execute(
        """
        SELECT match_pattern, target_account_code, target_account_name,
               force_non_deductible, non_deduct_reason, set_vehicle_flag, require_memo_prompt
        FROM tax_classification_rules
        WHERE is_active = 1 AND match_field = 'MERCHANT_NAME'
        ORDER BY priority ASC
        """
    )
    matched_result = None
    for (
        pattern,
        acc_code,
        acc_name,
        force_non_ded,
        reason,
        vehicle_flag,
        memo_prompt,
    ) in cursor.fetchall():
        if re.search(pattern, merchant_name, re.IGNORECASE):
            matched_result = {
                "account_code": acc_code,
                "account_name": acc_name,
                "vat_deduction_type": "NON_DEDUCTIBLE" if force_non_ded else "DEDUCTIBLE",
                "non_deduct_reason": reason,
                "is_vehicle_expense": bool(vehicle_flag),
                "require_memo_prompt": bool(memo_prompt),
            }
            break

    if matched_result is None:
        matched_result = {
            "account_code": "999",
            "account_name": "미분류",
            "vat_deduction_type": "PENDING",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": True,
        }

    return _apply_cached_merchant_overrides(cursor, matched_result, merchant_name, biz_reg_no)
