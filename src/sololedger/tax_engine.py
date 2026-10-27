"""Automated tax classification rule engine and VAT calculation logic."""

import re
import sqlite3
from typing import Any, Dict, Tuple


def calculate_supply_and_vat(total_amount: int, vat_deduction_type: str = "DEDUCTIBLE") -> Tuple[int, int]:
    """Calculate supply_value and vat_amount from total KRW amount.

    For EXEMPT transactions, supply_value equals total_amount and vat_amount is 0.
    Otherwise, supply_value is round(total_amount / 1.1) and vat_amount is the remainder.
    """
    if vat_deduction_type == "EXEMPT":
        return total_amount, 0
    supply_value = int(round(total_amount / 1.1))
    vat_amount = total_amount - supply_value
    return supply_value, vat_amount


def evaluate_tax_rules(cursor: sqlite3.Cursor, merchant_name: str) -> Dict[str, Any]:
    """Evaluate merchant_name against active tax_classification_rules ordered by priority ASC."""
    cursor.execute(
        """
        SELECT match_pattern, target_account_code, target_account_name,
               force_non_deductible, non_deduct_reason, set_vehicle_flag, require_memo_prompt
        FROM tax_classification_rules
        WHERE is_active = 1 AND match_field = 'MERCHANT_NAME'
        ORDER BY priority ASC
        """
    )
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
            return {
                "account_code": acc_code,
                "account_name": acc_name,
                "vat_deduction_type": "NON_DEDUCTIBLE" if force_non_ded else "DEDUCTIBLE",
                "non_deduct_reason": reason,
                "is_vehicle_expense": bool(vehicle_flag),
                "require_memo_prompt": bool(memo_prompt),
            }

    # Default fallback when no rule matches
    return {
        "account_code": "999",
        "account_name": "미분류",
        "vat_deduction_type": "PENDING",
        "non_deduct_reason": None,
        "is_vehicle_expense": False,
        "require_memo_prompt": True,
    }
