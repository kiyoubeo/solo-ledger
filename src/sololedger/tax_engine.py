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


_BUILTIN_BOOKSTORE_RE = re.compile(
    r"(교보문고|영풍문고|알라딘|예스24|오피스디포|알파문구)",
    re.IGNORECASE,
)
_BUILTIN_OFFLINE_MART_RE = re.compile(
    r"(다이소|아성다이소|이마트|홈플러스|롯데마트|롯데쇼핑|신세계|아울렛|"
    r"코스트코|트레이더스|하나로마트|케이마트|휴마트|할인마트|홈마트)",
    re.IGNORECASE,
)


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

    if matched_result is None and _BUILTIN_BOOKSTORE_RE.search(merchant_name or ""):
        matched_result = {
            "account_code": "826",
            "account_name": "도서인쇄비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
    elif matched_result is None and _BUILTIN_OFFLINE_MART_RE.search(merchant_name or ""):
        matched_result = {
            "account_code": "830",
            "account_name": "소모품비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }

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


def generate_default_memo(
    account_code: str,
    merchant_name: str,
    require_memo_prompt: bool = False,
) -> Optional[str]:
    """Generate a standard accounting memo for clear categories (Ledgerline Smart Queue style).

    Returns None for unclassified (999), entertainment (813), or PG/online shopping (830 with prompt)
    so those remain in the human-in-the-loop review queue.
    """
    if account_code in ("999", "813"):
        return None
    if account_code == "830" and require_memo_prompt:
        return None

    m_name = (merchant_name or "").strip()
    if account_code == "822":
        if "하이패스" in m_name:
            return "하이패스 통행료"
        if "주차" in m_name or "파크" in m_name:
            return "차량 주차료"
        if any(k in m_name for k in ("정비", "스피드메이트", "오토큐", "모터스", "타이어", "세차")):
            return "업무용승용차 정비·유지"
        return "업무용승용차 주유비"
    if account_code == "811":
        return "대표자(임직원) 식대 및 복리후생"
    if account_code == "826":
        return "업무용 도서 구입 및 인쇄"
    if account_code == "830":
        return "사무실 소모품 및 생활잡화 구입"
    if account_code == "831":
        return "업무용 IT구독·통신·교통 수수료"
    if account_code == "212":
        return "사무용 비품 취득"
    return None


def learn_merchant_rule(
    cursor: sqlite3.Cursor,
    merchant_name: str,
    account_code: str,
    account_name: str,
    vat_deduction_type: str,
    non_deduct_reason: Optional[str] = None,
) -> None:
    """Persist a Firefly III-style learned rule (priority 5) for an exact merchant name."""
    clean_name = (merchant_name or "").strip()
    if not clean_name or account_code == "999":
        return
    pattern = f"^{re.escape(clean_name)}$"
    force_non_ded = 1 if vat_deduction_type == "NON_DEDUCTIBLE" else 0
    is_vehicle = 1 if account_code == "822" else 0
    require_memo = 1 if account_code == "813" else 0
    cursor.execute(
        """
        DELETE FROM tax_classification_rules
        WHERE priority = 5 AND match_field = 'MERCHANT_NAME' AND match_pattern = ?
        """,
        (pattern,),
    )
    cursor.execute(
        """
        INSERT INTO tax_classification_rules (
            priority, match_field, match_pattern, target_account_code,
            target_account_name, force_non_deductible, non_deduct_reason,
            set_vehicle_flag, require_memo_prompt, is_active
        ) VALUES (5, 'MERCHANT_NAME', ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            pattern,
            account_code,
            account_name,
            force_non_ded,
            non_deduct_reason,
            is_vehicle,
            require_memo,
        ),
    )

