"""Lightweight Web Dashboard and Inline Ledger Management for SoloLedger."""

import calendar
import html
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sololedger import __version__
from sololedger.archiver import compute_sha256_hex, save_receipt_files
from sololedger.config import settings
from sololedger.sms_parser import (
    format_card_label,
    load_corp_card_policy,
    sanitize_card_folder_name,
)
from sololedger.tax_engine import (
    calculate_supply_and_vat,
    evaluate_tax_rules,
    generate_default_memo,
    is_uncertain_item_merchant,
    learn_merchant_rule,
)
from sololedger.telegram_bot import _refresh_receipt_archived_filename
from sololedger.vehicle_tracker import get_vehicle_expense_summary

ACCOUNT_CODE_CATALOG: Dict[str, str] = {
    "501": "원재료비",
    "511": "외주용역비",
    "811": "복리후생비",
    "812": "여비교통비",
    "813": "기업업무추진비",
    "821": "보험료",
    "822": "차량유지비",
    "826": "도서인쇄비",
    "830": "소모품비",
    "831": "지급수수료",
    "212": "비품",
    "999": "가계정(미분류)",
}


def update_transaction_classification(
    cursor: sqlite3.Cursor,
    tx_id: int,
    account_code: Optional[str] = None,
    account_name: Optional[str] = None,
    vat_deduction_type: Optional[str] = None,
    non_deduct_reason: Optional[str] = None,
    counterparty_info: Optional[str] = None,
    apply_to_same_merchant: bool = True,
) -> Dict[str, Any]:
    """Update a transaction's account classification, VAT deduction status, or memo."""
    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"Transaction #{tx_id} not found.")

    resolved_code = account_code or row["account_code"]
    resolved_name = account_name or ACCOUNT_CODE_CATALOG.get(resolved_code, row["account_name"])
    resolved_vat_type = vat_deduction_type or row["vat_deduction_type"]
    resolved_reason = non_deduct_reason if non_deduct_reason is not None else row["non_deduct_reason"]

    if resolved_code == "813" and vat_deduction_type is None:
        resolved_vat_type = "NON_DEDUCTIBLE"
        resolved_reason = resolved_reason or "ENTERTAINMENT"
    elif resolved_code == "822" and vat_deduction_type is None:
        resolved_vat_type = "NON_DEDUCTIBLE"
        resolved_reason = resolved_reason or "PASSENGER_CAR"
    elif resolved_code != "999" and resolved_vat_type in ("PENDING", "PENDING_CHECK"):
        resolved_vat_type = "DEDUCTIBLE"

    is_vehicle = 1 if resolved_code == "822" else int(row["is_vehicle_expense"])
    supply_value, vat_amount = calculate_supply_and_vat(int(row["total_amount"]), resolved_vat_type)
    raw_memo = counterparty_info if counterparty_info is not None else row["counterparty_info"]
    resolved_memo = (
        raw_memo
        if (raw_memo or "").strip()
        else generate_default_memo(resolved_code, row["raw_merchant_name"], False)
    )

    cursor.execute(
        """
        UPDATE card_transactions
        SET account_code = ?,
            account_name = ?,
            vat_deduction_type = ?,
            non_deduct_reason = ?,
            is_vehicle_expense = ?,
            supply_value = ?,
            vat_amount = ?,
            counterparty_info = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE tx_id = ?
        """,
        (
            resolved_code,
            resolved_name,
            resolved_vat_type,
            resolved_reason,
            is_vehicle,
            supply_value,
            vat_amount,
            resolved_memo,
            tx_id,
        ),
    )

    propagated_count = 0
    if apply_to_same_merchant and resolved_code != "999":
        learn_merchant_rule(
            cursor,
            row["raw_merchant_name"],
            resolved_code,
            resolved_name,
            resolved_vat_type,
            resolved_reason,
        )
        cursor.execute(
            """
            SELECT tx_id, total_amount, counterparty_info
            FROM card_transactions
            WHERE raw_merchant_name = ?
              AND tx_id != ?
              AND (account_code = '999' OR counterparty_info IS NULL OR TRIM(counterparty_info) = '')
            """,
            (row["raw_merchant_name"], tx_id),
        )
        peer_rows = cursor.fetchall()
        for peer in peer_rows:
            p_supply, p_vat = calculate_supply_and_vat(
                int(peer["total_amount"]), resolved_vat_type
            )
            p_memo = (peer["counterparty_info"] or "").strip() or resolved_memo
            cursor.execute(
                """
                UPDATE card_transactions
                SET account_code = ?,
                    account_name = ?,
                    vat_deduction_type = ?,
                    non_deduct_reason = ?,
                    is_vehicle_expense = ?,
                    supply_value = ?,
                    vat_amount = ?,
                    counterparty_info = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tx_id = ?
                """,
                (
                    resolved_code,
                    resolved_name,
                    resolved_vat_type,
                    resolved_reason,
                    is_vehicle,
                    p_supply,
                    p_vat,
                    p_memo,
                    peer["tx_id"],
                ),
            )
            propagated_count += 1

    archived_file_name = _refresh_receipt_archived_filename(cursor, tx_id)
    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    updated = dict(cursor.fetchone())
    updated["archived_file_name"] = archived_file_name
    updated["propagated_merchant_count"] = propagated_count
    return updated


_SYSTEM_AUTO_MEMOS = {
    "사무실 소모품 및 생활잡화 구입",
    "업무용 IT구독·통신·교통 수수료",
    "업무용 IT구독·통신 수수료",
    "대표자(임직원) 식대 및 복리후생",
    "현장 시공·디자인 업무 식대 및 복리후생",
    "현장 시공 부자재·마감용품 및 사무실 소모품 구입",
    "공간디자인·인테리어 현장 스타일링 소품 및 샘플 구입",
    "디자인·현장 업무용 IT 주변기기 및 소모품 구입",
    "디자인·현장 소모품 및 사무용품 구입",
    "인테리어·공간디자인 현장 가구·조명·스타일링 자재 구입",
    "사무용 문구 및 소모품 구입",
    "업무용승용차 주유비",
    "차량 주차료",
    "업무용승용차 정비·유지",
    "하이패스 통행료",
    "업무용승용차 자동차세 납부",
}


def reapply_tax_rules_to_ledger(cursor: sqlite3.Cursor) -> Dict[str, Any]:
    """Re-evaluate tax classification rules and populate smart default memos across ledger rows."""
    cursor.execute(
        """
        SELECT DISTINCT tx_id
        FROM receipt_ocr_metadata
        WHERE tx_id IS NOT NULL AND source_channel != 'AUTO_SLIP'
        """
    )
    confirmed_tx_ids = {int(row[0]) for row in cursor.fetchall()}

    cursor.execute(
        """
        SELECT tx_id, raw_merchant_name, biz_reg_no, total_amount,
               supply_value, vat_amount, reconcile_status,
               account_code, account_name, vat_deduction_type,
               non_deduct_reason, counterparty_info
        FROM card_transactions
        WHERE reconcile_status != 'CANCELED'
        """
    )
    rows = cursor.fetchall()
    reclassified_count = 0
    auto_memo_filled_count = 0

    for r in rows:
        tx_id = int(r["tx_id"])
        m_name = str(r["raw_merchant_name"] or "")
        cur_code = str(r["account_code"] or "999")
        cur_memo = str(r["counterparty_info"] or "").strip()
        has_confirmed_evidence = tx_id in confirmed_tx_ids
        has_user_custom_memo = bool(cur_memo) and (cur_memo not in _SYSTEM_AUTO_MEMOS)

        rule_eval = evaluate_tax_rules(cursor, m_name, r["biz_reg_no"])
        new_code = cur_code
        should_reclassify = False

        if not has_confirmed_evidence and not has_user_custom_memo:
            if rule_eval["account_code"] == "999" and cur_code != "999":
                if is_uncertain_item_merchant(m_name) or cur_code != "811":
                    should_reclassify = True
            elif rule_eval["account_code"] != "999":
                if cur_code == "999" or (
                    cur_code in ("830", "831") and rule_eval["account_code"] != cur_code
                ):
                    should_reclassify = True

        if should_reclassify:
            new_code = rule_eval["account_code"]
            new_name = rule_eval["account_name"]
            if new_code == "999":
                keep_tax_status = r["vat_deduction_type"] == "EXEMPT" or (
                    r["vat_deduction_type"] == "NON_DEDUCTIBLE"
                    and r["non_deduct_reason"] == "SIMPLIFIED_TAX"
                )
                new_vat_type = r["vat_deduction_type"] if keep_tax_status else "PENDING"
                new_reason = r["non_deduct_reason"] if keep_tax_status else None
                is_veh = 0
                if r["reconcile_status"] == "HOMETAX_VERIFIED":
                    sup, vat = int(r["supply_value"]), int(r["vat_amount"])
                else:
                    sup, vat = calculate_supply_and_vat(int(r["total_amount"]), new_vat_type)
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET account_code = '999',
                        account_name = '미분류',
                        vat_deduction_type = ?,
                        non_deduct_reason = ?,
                        is_vehicle_expense = 0,
                        supply_value = ?,
                        vat_amount = ?,
                        counterparty_info = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tx_id = ?
                    """,
                    (new_vat_type, new_reason, sup, vat, tx_id),
                )
                cur_memo = ""
            else:
                new_vat_type = (
                    r["vat_deduction_type"]
                    if r["reconcile_status"] == "HOMETAX_VERIFIED"
                    and r["vat_deduction_type"] in ("EXEMPT", "NON_DEDUCTIBLE")
                    else rule_eval["vat_deduction_type"]
                )
                new_reason = rule_eval["non_deduct_reason"]
                is_veh = 1 if rule_eval["is_vehicle_expense"] else 0
                if r["reconcile_status"] == "HOMETAX_VERIFIED":
                    sup, vat = int(r["supply_value"]), int(r["vat_amount"])
                else:
                    sup, vat = calculate_supply_and_vat(int(r["total_amount"]), new_vat_type)
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET account_code = ?,
                        account_name = ?,
                        vat_deduction_type = ?,
                        non_deduct_reason = COALESCE(non_deduct_reason, ?),
                        is_vehicle_expense = ?,
                        supply_value = ?,
                        vat_amount = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tx_id = ?
                    """,
                    (new_code, new_name, new_vat_type, new_reason, is_veh, sup, vat, tx_id),
                )
            _refresh_receipt_archived_filename(cursor, tx_id)
            reclassified_count += 1

        if new_code == "999":
            if not has_confirmed_evidence and not has_user_custom_memo and cur_memo:
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET counterparty_info = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tx_id = ?
                    """,
                    (tx_id,),
                )
                _refresh_receipt_archived_filename(cursor, tx_id)
        elif not cur_memo or cur_memo in _SYSTEM_AUTO_MEMOS or should_reclassify:
            req_prompt = bool(rule_eval.get("require_memo_prompt", False))
            auto_memo = generate_default_memo(new_code, m_name, req_prompt)
            if auto_memo and auto_memo != cur_memo:
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET counterparty_info = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tx_id = ?
                    """,
                    (auto_memo, tx_id),
                )
                _refresh_receipt_archived_filename(cursor, tx_id)
                auto_memo_filled_count += 1

    return {
        "status": "reapplied",
        "total_scanned": len(rows),
        "reclassified_count": reclassified_count,
        "auto_memo_filled_count": auto_memo_filled_count,
    }


def _normalize_date_str(val: Optional[str]) -> Optional[str]:
    """Normalize YYYY-MM-DD or YYYYMMDD date input string."""
    if not val:
        return None
    s = val.strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _quarter_date_bounds(year_val: int, quarter_val: int) -> Tuple[str, str]:
    """Return ('YYYY-MM-DD', 'YYYY-MM-DD') start/end bounds for a calendar quarter (1..4)."""
    q = max(1, min(4, int(quarter_val)))
    s_month = (q - 1) * 3 + 1
    e_month = q * 3
    last_day = calendar.monthrange(year_val, e_month)[1]
    return f"{year_val:04d}-{s_month:02d}-01", f"{year_val:04d}-{e_month:02d}-{last_day:02d}"


def _parse_quarter_spec(
    raw_spec: Optional[str], fallback_year: int
) -> Optional[Tuple[int, int, str, str]]:
    """Parse quarter token (e.g. '2026Q1', '2026-Q2', 'Q3', '4분기', '1') into (year, q, start, end)."""
    if not raw_spec:
        return None
    token = raw_spec.strip().upper()
    if not token:
        return None

    m_yq = re.fullmatch(r"(\d{4})\s*[-_/\s년]*\s*(?:Q([1-4])|([1-4])\s*(?:Q|분기))", token)
    if m_yq:
        y_i = int(m_yq.group(1))
        q_i = int(m_yq.group(2) or m_yq.group(3))
        s_dt, e_dt = _quarter_date_bounds(y_i, q_i)
        return y_i, q_i, s_dt, e_dt

    m_q = re.fullmatch(r"(?:Q([1-4])|([1-4])\s*(?:Q|분기)?)", token)
    if m_q:
        q_i = int(m_q.group(1) or m_q.group(2))
        s_dt, e_dt = _quarter_date_bounds(fallback_year, q_i)
        return fallback_year, q_i, s_dt, e_dt

    return None


def _detect_exact_quarter(start_ymd: str, end_ymd: str) -> Optional[str]:
    """Return 'YYYYQn' if start_ymd..end_ymd exactly spans one calendar quarter, else None."""
    if len(start_ymd) < 10 or len(end_ymd) < 10:
        return None
    if not (start_ymd[:4].isdigit() and end_ymd[:4].isdigit()):
        return None
    s_y, e_y = int(start_ymd[:4]), int(end_ymd[:4])
    if s_y != e_y:
        return None
    for q_i in (1, 2, 3, 4):
        qs, qe = _quarter_date_bounds(s_y, q_i)
        if start_ymd == qs and end_ymd == qe:
            return f"{s_y:04d}Q{q_i}"
    return None


def build_dashboard_summary(
    cursor: sqlite3.Cursor,
    year_month: Optional[str] = None,
    year: Optional[int] = None,
    card_last4: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_code: Optional[str] = None,
    quarter: Optional[str] = None,
) -> Dict[str, Any]:
    """Build aggregated dashboard metrics, per-card breakdown, pending action items, and vehicle limit."""
    now = datetime.now()
    target_card = (card_last4 or "ALL").strip()
    target_account = (account_code or "ALL").strip()

    cursor.execute(
        """
        SELECT MIN(SUBSTR(transacted_at, 1, 10)), MAX(SUBSTR(transacted_at, 1, 10))
        FROM card_transactions
        WHERE LENGTH(transacted_at) >= 10
          AND reconcile_status != 'CANCELED'
        """
    )
    bound_row = cursor.fetchone()
    today_str = now.strftime("%Y-%m-%d")
    min_tx_date = (bound_row[0] if bound_row and bound_row[0] else None) or f"{now.year}-01-01"
    bound_max = bound_row[1] if bound_row and bound_row[1] else None
    max_tx_date = max(str(bound_max), today_str) if bound_max else today_str

    cursor.execute(
        """
        SELECT SUBSTR(transacted_at, 1, 4) || SUBSTR(transacted_at, 6, 2) AS ym,
               COUNT(*) AS cnt
        FROM card_transactions
        WHERE LENGTH(transacted_at) >= 7
          AND reconcile_status != 'CANCELED'
        GROUP BY ym
        ORDER BY ym DESC
        """
    )
    ym_rows = cursor.fetchall()
    monthly_counts: Dict[str, int] = {str(r[0]): int(r[1]) for r in ym_rows if r[0]}
    available_months = [str(r[0]) for r in ym_rows if r[0]]
    current_ym = now.strftime("%Y%m")
    if current_ym not in available_months:
        available_months.insert(0, current_ym)

    monthly_periods: List[Dict[str, Any]] = []
    for ym_val in available_months:
        if len(ym_val) >= 6 and ym_val[:4].isdigit() and ym_val[4:6].isdigit():
            y_i = int(ym_val[:4])
            m_i = int(ym_val[4:6])
            if 1 <= m_i <= 12:
                last_d = calendar.monthrange(y_i, m_i)[1]
                m_start = f"{y_i:04d}-{m_i:02d}-01"
                m_end = f"{y_i:04d}-{m_i:02d}-{last_d:02d}"
                cnt = monthly_counts.get(ym_val, 0)
                monthly_periods.append(
                    {
                        "ym": ym_val,
                        "year": y_i,
                        "month": m_i,
                        "label": f"{y_i}년 {m_i:02d}월 ({cnt}건)",
                        "short_label": f"{str(y_i)[2:]}.{m_i:02d}월",
                        "start_date": m_start,
                        "end_date": m_end,
                        "tx_count": cnt,
                    }
                )

    norm_start = _normalize_date_str(start_date)
    norm_end = _normalize_date_str(end_date)
    parsed_quarter = _parse_quarter_spec(quarter, year or now.year)
    if parsed_quarter is None and year_month and (
        "Q" in year_month.upper() or "분기" in year_month
    ):
        parsed_quarter = _parse_quarter_spec(year_month, year or now.year)

    active_quarter: Optional[str] = None

    if norm_start is not None or norm_end is not None:
        resolved_start = norm_start or min_tx_date
        resolved_end = norm_end or max_tx_date
        if resolved_start > resolved_end:
            resolved_start, resolved_end = resolved_end, resolved_start
        end_yr = int(resolved_end[:4]) if resolved_end[:4].isdigit() else now.year
        target_year = year or end_yr
        default_ym = (
            resolved_end[:4] + resolved_end[5:7]
            if len(resolved_end) >= 7
            else now.strftime("%Y%m")
        )
        clean_ym = (year_month or "").strip().upper()
        target_ym = (
            clean_ym
            if (clean_ym == "ALL" or (len(clean_ym) == 6 and clean_ym.isdigit()))
            else default_ym
        )
        active_quarter = _detect_exact_quarter(resolved_start, resolved_end)
        cursor.execute(
            """
            SELECT t.*,
                   (
                       SELECT r.archived_file_name
                       FROM receipt_ocr_metadata r
                       WHERE r.tx_id = t.tx_id
                       ORDER BY r.receipt_id DESC
                       LIMIT 1
                   ) AS archived_file_name
            FROM card_transactions t
            WHERE SUBSTR(t.transacted_at, 1, 10) >= ?
              AND SUBSTR(t.transacted_at, 1, 10) <= ?
              AND t.reconcile_status != 'CANCELED'
            ORDER BY t.transacted_at DESC, t.tx_id DESC
            """,
            (resolved_start, resolved_end),
        )
    elif parsed_quarter is not None:
        q_year, q_num, resolved_start, resolved_end = parsed_quarter
        target_year = year or q_year
        target_ym = f"{q_year:04d}{q_num * 3:02d}"
        active_quarter = f"{q_year:04d}Q{q_num}"
        cursor.execute(
            """
            SELECT t.*,
                   (
                       SELECT r.archived_file_name
                       FROM receipt_ocr_metadata r
                       WHERE r.tx_id = t.tx_id
                       ORDER BY r.receipt_id DESC
                       LIMIT 1
                   ) AS archived_file_name
            FROM card_transactions t
            WHERE SUBSTR(t.transacted_at, 1, 10) >= ?
              AND SUBSTR(t.transacted_at, 1, 10) <= ?
              AND t.reconcile_status != 'CANCELED'
            ORDER BY t.transacted_at DESC, t.tx_id DESC
            """,
            (resolved_start, resolved_end),
        )
    else:
        target_ym = (year_month or now.strftime("%Y%m")).strip().upper()
        if target_ym == "ALL":
            target_year = year or now.year
            resolved_start = min_tx_date
            resolved_end = max_tx_date
            ym_prefix = "%"
        else:
            y_int = int(target_ym[:4]) if target_ym[:4].isdigit() else now.year
            m_int = (
                int(target_ym[4:6])
                if len(target_ym) >= 6 and target_ym[4:6].isdigit()
                else now.month
            )
            target_year = year or y_int
            last_day = calendar.monthrange(y_int, m_int)[1]
            resolved_start = f"{y_int:04d}-{m_int:02d}-01"
            resolved_end = f"{y_int:04d}-{m_int:02d}-{last_day:02d}"
            ym_prefix = f"{y_int:04d}-{m_int:02d}-%"

        cursor.execute(
            """
            SELECT t.*,
                   (
                       SELECT r.archived_file_name
                       FROM receipt_ocr_metadata r
                       WHERE r.tx_id = t.tx_id
                       ORDER BY r.receipt_id DESC
                       LIMIT 1
                   ) AS archived_file_name
            FROM card_transactions t
            WHERE t.transacted_at LIKE ?
              AND t.reconcile_status != 'CANCELED'
            ORDER BY t.transacted_at DESC, t.tx_id DESC
            """,
            (ym_prefix,),
        )
    all_period_rows = [dict(r) for r in cursor.fetchall()]

    quarterly_counts: Dict[str, int] = {}
    for ym_val, cnt in monthly_counts.items():
        if len(ym_val) >= 6 and ym_val[:4].isdigit() and ym_val[4:6].isdigit():
            y_i = int(ym_val[:4])
            m_i = int(ym_val[4:6])
            if 1 <= m_i <= 12:
                q_i = (m_i - 1) // 3 + 1
                yq_key = f"{y_i:04d}Q{q_i}"
                quarterly_counts[yq_key] = quarterly_counts.get(yq_key, 0) + cnt

    quarter_keys_set = set(quarterly_counts.keys())
    for base_y in {now.year, target_year}:
        for q_i in (1, 2, 3, 4):
            quarter_keys_set.add(f"{base_y:04d}Q{q_i}")
    if active_quarter:
        quarter_keys_set.add(active_quarter)

    available_quarters = sorted(quarter_keys_set, reverse=True)
    quarterly_periods: List[Dict[str, Any]] = []
    for yq_val in available_quarters:
        y_i = int(yq_val[:4])
        q_i = int(yq_val[5:6])
        q_start, q_end = _quarter_date_bounds(y_i, q_i)
        s_m = (q_i - 1) * 3 + 1
        e_m = q_i * 3
        cnt = quarterly_counts.get(yq_val, 0)
        quarterly_periods.append(
            {
                "yq": yq_val,
                "year": y_i,
                "quarter": q_i,
                "label": f"{y_i}년 {q_i}분기 ({s_m:02d}~{e_m:02d}월, {cnt}건)",
                "short_label": f"{str(y_i)[2:]}.{q_i}분기",
                "start_date": q_start,
                "end_date": q_end,
                "tx_count": cnt,
            }
        )

    card_stats: Dict[str, Dict[str, Any]] = {}
    for r in all_period_rows:
        c_code = str(r.get("card_last4") or "0000")
        c_label = format_card_label(c_code)
        r["card_label"] = c_label
        r["card_folder"] = sanitize_card_folder_name(c_code)

        entry = card_stats.setdefault(
            c_code,
            {
                "card_last4": c_code,
                "card_label": c_label,
                "card_folder": r["card_folder"],
                "tx_count": 0,
                "total_expenditure": 0,
                "deductible_vat": 0,
                "non_deductible_total": 0,
                "receipt_count": 0,
            },
        )
        amt = int(r["total_amount"])
        vat = int(r["vat_amount"])
        entry["tx_count"] += 1
        entry["total_expenditure"] += amt
        if r["vat_deduction_type"] == "DEDUCTIBLE":
            entry["deductible_vat"] += vat
        elif r["vat_deduction_type"] == "NON_DEDUCTIBLE":
            entry["non_deductible_total"] += amt
        if r.get("archived_file_name"):
            entry["receipt_count"] += 1

    card_breakdown = sorted(
        card_stats.values(),
        key=lambda x: (str(x["card_label"]), str(x["card_last4"])),
    )

    if target_card and target_card != "ALL":
        card_rows = [
            r for r in all_period_rows if str(r.get("card_last4") or "0000") == target_card
        ]
    else:
        card_rows = all_period_rows

    acc_stats: Dict[str, Dict[str, Any]] = {}
    account_transactions: Dict[str, List[Dict[str, Any]]] = {}
    card_total_expenditure = 0

    for r in card_rows:
        amt = int(r["total_amount"])
        sup = int(r["supply_value"])
        vat = int(r["vat_amount"])
        a_code = str(r.get("account_code") or "999")
        a_name = str(r.get("account_name") or ACCOUNT_CODE_CATALOG.get(a_code, "미분류"))

        a_entry = acc_stats.setdefault(
            a_code,
            {
                "account_code": a_code,
                "account_name": a_name,
                "tx_count": 0,
                "total_expenditure": 0,
                "supply_value": 0,
                "deductible_vat": 0,
                "non_deductible_total": 0,
                "ratio_pct": 0.0,
            },
        )
        a_entry["tx_count"] += 1
        a_entry["total_expenditure"] += amt
        a_entry["supply_value"] += sup
        card_total_expenditure += amt
        if r["vat_deduction_type"] == "DEDUCTIBLE":
            a_entry["deductible_vat"] += vat
        elif r["vat_deduction_type"] == "NON_DEDUCTIBLE":
            a_entry["non_deductible_total"] += amt
        account_transactions.setdefault(a_code, []).append(r)

    for a_entry in acc_stats.values():
        if card_total_expenditure > 0:
            a_entry["ratio_pct"] = round(
                a_entry["total_expenditure"] * 100.0 / card_total_expenditure, 1
            )

    account_breakdown = sorted(
        acc_stats.values(),
        key=lambda x: (x["total_expenditure"], x["tx_count"]),
        reverse=True,
    )

    if target_account and target_account != "ALL":
        rows = [r for r in card_rows if str(r.get("account_code") or "999") == target_account]
    else:
        rows = card_rows

    total_expenditure = 0
    deductible_vat = 0
    non_deductible_vat = 0
    non_deductible_total = 0
    pending_items: List[Dict[str, Any]] = []

    for r in rows:
        amt = int(r["total_amount"])
        vat = int(r["vat_amount"])
        total_expenditure += amt
        if r["vat_deduction_type"] == "DEDUCTIBLE":
            deductible_vat += vat
        elif r["vat_deduction_type"] == "NON_DEDUCTIBLE":
            non_deductible_vat += vat
            non_deductible_total += amt

        memo = (r.get("counterparty_info") or "").strip()
        if r["account_code"] == "999" or not memo:
            pending_items.append(r)

    vehicle_summary = get_vehicle_expense_summary(cursor, year=target_year)

    return {
        "app_env": settings.app_env,
        "db_path": str(settings.db_path),
        "nas_backup_dir": str(settings.nas_backup_dir),
        "year_month": target_ym,
        "quarter": active_quarter,
        "start_date": resolved_start,
        "end_date": resolved_end,
        "min_tx_date": min_tx_date,
        "max_tx_date": max_tx_date,
        "card_last4": target_card,
        "account_code": target_account,
        "available_months": available_months,
        "monthly_periods": monthly_periods,
        "available_quarters": available_quarters,
        "quarterly_periods": quarterly_periods,
        "card_breakdown": card_breakdown,
        "account_breakdown": account_breakdown,
        "account_transactions": account_transactions,
        "year": target_year,
        "total_tx_count": len(rows),
        "total_expenditure": total_expenditure,
        "deductible_vat": deductible_vat,
        "non_deductible_vat": non_deductible_vat,
        "non_deductible_total": non_deductible_total,
        "pending_action_count": len(pending_items),
        "pending_transactions": pending_items,
        "transactions": rows,
        "vehicle_limit": vehicle_summary,
        "corp_card_policy": load_corp_card_policy(),
    }


def seed_demo_transactions(
    cursor: sqlite3.Cursor,
    year_month: Optional[str] = None,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Insert realistic 1-person corp test transactions and sample receipt files for sandbox testing."""
    now = datetime.now()
    ym = year_month or now.strftime("%Y%m")
    y_str, m_str = ym[:4], ym[4:6]

    demo_rows = [
        (
            "9876",
            "DEMO1001",
            f"{y_str}-{m_str}-05 10:30:00",
            "쿠팡(주)",
            "1208800767",
            45000,
            40909,
            4091,
            "830",
            "소모품비",
            "DEDUCTIBLE",
            None,
            0,
            "모니터암 외 1건",
            "OCR_MATCHED",
        ),
        (
            "1234",
            "DEMO1002",
            f"{y_str}-{m_str}-08 14:20:00",
            "네이버페이",
            "2208162517",
            330000,
            300000,
            30000,
            "212",
            "비품",
            "DEDUCTIBLE",
            None,
            0,
            "LG 27인치 4K 모니터",
            "OCR_MATCHED",
        ),
        (
            "1234",
            "DEMO1003",
            f"{y_str}-{m_str}-12 12:15:00",
            "스타벅스역삼점",
            "2018121515",
            12000,
            10909,
            1091,
            "811",
            "복리후생비",
            "DEDUCTIBLE",
            None,
            0,
            "대표자 단독 식사",
            "HOMETAX_VERIFIED",
        ),
        (
            "1234",
            "DEMO1004",
            f"{y_str}-{m_str}-15 19:10:00",
            "한우명가역삼점",
            "2118754321",
            165000,
            150000,
            15000,
            "813",
            "기업업무추진비",
            "NON_DEDUCTIBLE",
            "ENTERTAINMENT",
            0,
            "A사 김철수 대표 프로젝트 킥오프 미팅",
            "HOMETAX_VERIFIED",
        ),
        (
            "9876",
            "DEMO1005",
            f"{y_str}-{m_str}-18 09:00:00",
            "SK에너지주유소",
            "1168118745",
            110000,
            100000,
            10000,
            "822",
            "차량유지비",
            "NON_DEDUCTIBLE",
            "PASSENGER_CAR",
            1,
            "업무용승용차 주유 60L",
            "HOMETAX_VERIFIED",
        ),
        (
            "5678",
            "DEMO1006",
            f"{y_str}-{m_str}-22 16:40:00",
            "알수없는문구점",
            "2148539210",
            15000,
            13636,
            1364,
            "999",
            "미분류",
            "PENDING_CHECK",
            None,
            0,
            None,
            "SMS_ONLY",
        ),
    ]

    inserted_ids: List[int] = []
    for row in demo_rows:
        cursor.execute(
            "SELECT tx_id FROM card_transactions WHERE approval_no = ?",
            (row[1],),
        )
        if cursor.fetchone():
            continue
        if row[4]:
            cursor.execute(
                """
                INSERT OR IGNORE INTO merchants (
                    biz_reg_no, merchant_name, tax_type_code, biz_status, nts_verified_at
                ) VALUES (?, ?, 'GENERAL', 'ACTIVE', CURRENT_TIMESTAMP)
                """,
                (row[4], row[3]),
            )
        cursor.execute(
            """
            INSERT INTO card_transactions (
                card_last4, approval_no, transacted_at, raw_merchant_name,
                biz_reg_no, total_amount, supply_value, vat_amount,
                account_code, account_name, vat_deduction_type,
                non_deduct_reason, is_vehicle_expense, counterparty_info, reconcile_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        tx_id = int(cursor.lastrowid)
        inserted_ids.append(tx_id)

        # Attach sample receipt for DEMO1001
        if row[1] == "DEMO1001":
            sample_bytes = f"DEMO-RECEIPT-COUPANG-{ym}".encode("utf-8")
            file_hash = compute_sha256_hex(sample_bytes)
            cursor.execute(
                "SELECT receipt_id FROM receipt_ocr_metadata WHERE file_hash_sha256 = ?",
                (file_hash,),
            )
            if not cursor.fetchone():
                arch_name = f"{y_str}{m_str}05_쿠팡_소모품비_모니터암외1건_45000.jpg"
                orig_path, _ = save_receipt_files(
                    sample_bytes,
                    original_file_name="demo_coupang.jpg",
                    archived_file_name=arch_name,
                    archive_dir=archive_dir,
                )
                cursor.execute(
                    """
                    INSERT INTO receipt_ocr_metadata (
                        tx_id, source_channel, original_file_path, archived_file_name,
                        file_hash_sha256, ocr_biz_reg_no, ocr_total_amount,
                        line_items_json, raw_ocr_response, ocr_confidence
                    ) VALUES (?, 'WEB_UPLOAD', ?, ?, ?, '1208800767', 45000, '[]', '{}', 0.99)
                    """,
                    (tx_id, orig_path, arch_name, file_hash),
                )

    return {
        "status": "seeded",
        "year_month": ym,
        "inserted_count": len(inserted_ids),
        "inserted_tx_ids": inserted_ids,
    }


def reset_test_ledger(
    cursor: sqlite3.Cursor,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Clear all transaction, receipt, and merchant rows in the test ledger."""
    cursor.execute("SELECT COUNT(*) FROM card_transactions")
    deleted_tx_count = int(cursor.fetchone()[0])

    cursor.execute("DELETE FROM receipt_ocr_metadata")
    cursor.execute("DELETE FROM card_transactions")
    cursor.execute("DELETE FROM merchants")
    cursor.execute("DELETE FROM tax_classification_rules WHERE priority = 5")
    try:
        cursor.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('card_transactions', 'receipt_ocr_metadata')"
        )
    except sqlite3.OperationalError:
        pass

    target_archive = archive_dir or settings.archive_dir
    for sub in ("original", "normalized", "exports", "by_card"):
        sub_path = target_archive / sub
        if sub_path.exists():
            shutil.rmtree(sub_path, ignore_errors=True)
            if sub != "by_card":
                sub_path.mkdir(parents=True, exist_ok=True)

    return {
        "status": "reset",
        "deleted_transactions": deleted_tx_count,
        "archive_dir_cleaned": str(target_archive),
    }


def render_dashboard_html(
    cursor: sqlite3.Cursor,
    year_month: Optional[str] = None,
    year: Optional[int] = None,
    card_last4: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_code: Optional[str] = None,
    quarter: Optional[str] = None,
) -> str:
    """Render server-side HTML5 management dashboard for SoloLedger."""
    summary = build_dashboard_summary(
        cursor,
        year_month=year_month,
        year=year,
        card_last4=card_last4,
        start_date=start_date,
        end_date=end_date,
        account_code=account_code,
        quarter=quarter,
    )
    veh = summary["vehicle_limit"]
    gauge_pct = min(100.0, float(veh["usage_ratio_pct"]))
    s_date = html.escape(str(summary["start_date"]))
    e_date = html.escape(str(summary["end_date"]))
    min_d = html.escape(str(summary["min_tx_date"]))
    max_d = html.escape(str(summary["max_tx_date"]))
    active_acc_code = str(summary.get("account_code") or "ALL")
    active_q_code = str(summary.get("quarter") or "")
    active_q_meta_html = ""
    if len(active_q_code) >= 6 and active_q_code[:4].isdigit() and "Q" in active_q_code:
        active_q_meta_html = (
            f" &nbsp;|&nbsp; 선택 분기: "
            f"<strong>{html.escape(active_q_code[:4])}년 {html.escape(active_q_code[5:])}분기</strong>"
        )

    card_options_html = []
    c_all_sel = "selected" if summary["card_last4"] == "ALL" else ""
    card_options_html.append(f"<option value='ALL' {c_all_sel}>전체 카드 (ALL)</option>")
    for cb in summary.get("card_breakdown", []):
        c_val = html.escape(str(cb["card_last4"]))
        c_lbl = html.escape(f"{cb['card_label']} ({cb['tx_count']}건)")
        sel = "selected" if summary["card_last4"] == cb["card_last4"] else ""
        card_options_html.append(f"<option value='{c_val}' {sel}>{c_lbl}</option>")
    card_select_html = "".join(card_options_html)

    account_options_html = []
    a_all_sel = "selected" if active_acc_code == "ALL" else ""
    account_options_html.append(f"<option value='ALL' {a_all_sel}>전체 계정 (ALL)</option>")
    for ab in summary.get("account_breakdown", []):
        a_val = html.escape(str(ab["account_code"]))
        a_opt_lbl = html.escape(
            f"{ab['account_code']} {ab['account_name']} ({ab['tx_count']}건)"
        )
        sel = "selected" if active_acc_code == str(ab["account_code"]) else ""
        account_options_html.append(f"<option value='{a_val}' {sel}>{a_opt_lbl}</option>")
    account_select_html = "".join(account_options_html)

    quarter_options_html = ["<option value=''>-- 분기별 선택 --</option>"]
    quarter_chips_list = []
    for qp in summary.get("quarterly_periods", []):
        q_val = f"{qp['start_date']}|{qp['end_date']}"
        q_lbl = html.escape(str(qp["label"]))
        is_q_active = (
            summary["start_date"] == qp["start_date"]
            and summary["end_date"] == qp["end_date"]
        )
        q_sel = "selected" if is_q_active else ""
        quarter_options_html.append(f"<option value='{q_val}' {q_sel}>{q_lbl}</option>")
    quarter_select_html = "".join(quarter_options_html)

    for qp in sorted(summary.get("quarterly_periods", []), key=lambda x: str(x["yq"])):
        q_val = f"{qp['start_date']}|{qp['end_date']}"
        sq_lbl = html.escape(f"{qp['short_label']}({qp['tx_count']})")
        is_q_active = (
            summary["start_date"] == qp["start_date"]
            and summary["end_date"] == qp["end_date"]
        )
        q_btn_cls = "" if is_q_active else "secondary"
        quarter_chips_list.append(
            f"<button class='{q_btn_cls}' style='padding:4px 9px; font-size:12px; margin:2px;' "
            f"onclick=\"selectQuarterRange('{q_val}')\">{sq_lbl}</button>"
        )
    quarter_chips_html = "".join(quarter_chips_list)

    month_options_html = ["<option value=''>-- 달별 선택 --</option>"]
    month_chips_list = []
    for mp in summary.get("monthly_periods", []):
        m_val = f"{mp['start_date']}|{mp['end_date']}"
        m_lbl = html.escape(str(mp["label"]))
        is_active = (
            summary["start_date"] == mp["start_date"]
            and summary["end_date"] == mp["end_date"]
        )
        m_sel = "selected" if is_active else ""
        month_options_html.append(f"<option value='{m_val}' {m_sel}>{m_lbl}</option>")
    month_select_html = "".join(month_options_html)

    for mp in sorted(summary.get("monthly_periods", []), key=lambda x: str(x["ym"])):
        m_val = f"{mp['start_date']}|{mp['end_date']}"
        s_lbl = html.escape(f"{mp['short_label']}({mp['tx_count']})")
        is_active = (
            summary["start_date"] == mp["start_date"]
            and summary["end_date"] == mp["end_date"]
        )
        btn_cls = "" if is_active else "secondary"
        month_chips_list.append(
            f"<button class='{btn_cls}' style='padding:4px 9px; font-size:12px; margin:2px;' "
            f"onclick=\"selectMonthRange('{m_val}')\">{s_lbl}</button>"
        )
    month_chips_html = "".join(month_chips_list)

    card_breakdown_rows = []
    for cb in summary.get("card_breakdown", []):
        c_val = html.escape(str(cb["card_last4"]))
        c_lbl = html.escape(str(cb["card_label"]))
        c_folder = html.escape(str(cb["card_folder"]))
        card_breakdown_rows.append(
            f"<tr>"
            f"<td><span class='badge card'>{c_lbl}</span></td>"
            f"<td class='num'><strong>{cb['tx_count']}건</strong></td>"
            f"<td class='num'>{cb['total_expenditure']:,}원</td>"
            f"<td class='num'>{cb['deductible_vat']:,}원</td>"
            f"<td class='num'>{cb['non_deductible_total']:,}원</td>"
            f"<td class='num'>{cb['receipt_count']}건</td>"
            f"<td><code>by_card/{c_folder}/</code></td>"
            f"<td><button class='secondary' onclick=\"filterByCard('{c_val}')\">이 카드만 보기</button></td>"
            f"</tr>"
        )
    card_breakdown_tbody = (
        "\n".join(card_breakdown_rows)
        if card_breakdown_rows
        else "<tr><td colspan='8' class='empty'>집계된 카드 내역이 없습니다.</td></tr>"
    )

    acc_breakdown_rows = []
    acc_tx_map = summary.get("account_transactions", {})
    for ab in summary.get("account_breakdown", []):
        a_code_raw = str(ab["account_code"])
        a_code_esc = html.escape(a_code_raw)
        a_lbl = html.escape(f"{ab['account_code']} {ab['account_name']}")
        is_acc_open = active_acc_code == a_code_raw
        row_cls = "acc-summary-row active" if is_acc_open else "acc-summary-row"
        arrow_txt = "▼" if is_acc_open else "▶"
        badge_txt = "목록 닫기" if is_acc_open else "분류 목록 보기"
        detail_disp = "table-row" if is_acc_open else "none"

        acc_tx_items = acc_tx_map.get(a_code_raw, [])
        acc_detail_rows_html = []
        for atx in acc_tx_items:
            atx_id = atx["tx_id"]
            ac_lbl = html.escape(
                str(atx.get("card_label") or format_card_label(atx["card_last4"]))
            )
            a_merch = html.escape(str(atx["raw_merchant_name"]))
            a_amt = f"{int(atx['total_amount']):,}원"
            a_sup = f"{int(atx['supply_value']):,}원"
            a_vat = f"{int(atx['vat_amount']):,}원"
            a_ded = html.escape(str(atx["vat_deduction_type"]))
            a_memo = html.escape(str(atx.get("counterparty_info") or ""))
            a_raw_arch = str(atx.get("archived_file_name") or "")
            a_has_stmt = a_raw_arch.startswith("거래명세서_")
            a_arch_str = html.escape(a_raw_arch or "-")
            a_dt = html.escape(str(atx["transacted_at"]))
            a_stmt_badge = (
                "<span style='background:#dcfce7; color:#166534; padding:2px 6px; "
                "border-radius:4px; font-weight:700; font-size:11px;'>📄명세서 완료</span>"
                if a_has_stmt
                else "[📄명세서]"
            )
            a_dt_link = (
                f"<a href='javascript:void(0)' class='tx-date-link' "
                f"onclick='openReceiptModal({atx_id})' "
                f"title='클릭하여 영수증/매출전표 팝업 보기'>{a_dt} 🧾</a>"
                f" <a href='javascript:void(0)' class='tx-date-link' "
                f"style='color:#0f766e; font-size:12px; text-decoration:none;' "
                f"onclick='openStatementModal({atx_id})' "
                f"title='클릭하여 거래명세서 보기/발급'>{a_stmt_badge}</a>"
            )
            a_opts = []
            cur_ac = str(atx["account_code"])
            for ck, cv in ACCOUNT_CODE_CATALOG.items():
                sel_attr = "selected" if ck == cur_ac else ""
                a_opts.append(f"<option value='{ck}' {sel_attr}>{ck} {html.escape(cv)}</option>")
            a_select = (
                f"<select id='acc-list-code-{atx_id}' style='font-size:12px; padding:3px 6px;'>"
                f"{''.join(a_opts)}</select>"
            )
            acc_detail_rows_html.append(
                f"<tr>"
                f"<td>#{atx_id}</td>"
                f"<td>{a_dt_link}</td>"
                f"<td><span class='badge card'>{ac_lbl}</span></td>"
                f"<td><strong>{a_merch}</strong></td>"
                f"<td class='num'><strong>{a_amt}</strong></td>"
                f"<td class='num'>{a_sup}</td>"
                f"<td class='num'>{a_vat}</td>"
                f"<td>{a_ded}</td>"
                f"<td><input type='text' id='acc-list-memo-{atx_id}' value='{a_memo}' "
                f"placeholder='품목 또는 참석자 소명' style='width:100%; min-width:140px; "
                f"box-sizing:border-box; font-size:12px;' /></td>"
                f"<td style='white-space:nowrap;'>{a_select}"
                f"<button style='padding:4px 8px; font-size:12px;' "
                f"onclick='saveAccountListTx({atx_id})'>저장</button></td>"
                f"<td style='font-size:11px; color:#475569;'>{a_arch_str}</td>"
                f"</tr>"
            )
        acc_detail_tbody_str = (
            "\n".join(acc_detail_rows_html)
            if acc_detail_rows_html
            else "<tr><td colspan='11' class='empty'>분류된 전표 내역이 없습니다.</td></tr>"
        )

        acc_breakdown_rows.append(
            f"<tr id='acc-row-{a_code_esc}' class='{row_cls}' "
            f"onclick=\"toggleAccountDetail('{a_code_esc}')\" "
            f"title='{a_lbl} 분류 전표 목록({ab['tx_count']}건) 보기/닫기'>"
            f"<td>"
            f"<a href='javascript:void(0)' class='acc-subject-link' "
            f"onclick=\"event.stopPropagation(); toggleAccountDetail('{a_code_esc}')\">"
            f"<span id='acc-arrow-{a_code_esc}' class='acc-arrow'>{arrow_txt}</span>"
            f"<strong>{a_lbl}</strong>"
            f"<span id='acc-badge-{a_code_esc}' class='acc-view-badge no-print'>"
            f"{badge_txt}</span>"
            f"</a>"
            f"</td>"
            f"<td class='num'><strong>{ab['tx_count']}건</strong></td>"
            f"<td class='num'><strong>{ab['total_expenditure']:,}원</strong></td>"
            f"<td class='num'>{ab['supply_value']:,}원</td>"
            f"<td class='num'>{ab['deductible_vat']:,}원</td>"
            f"<td class='num'>{ab['non_deductible_total']:,}원</td>"
            f"<td class='num'>{ab['ratio_pct']}%</td>"
            f"</tr>"
            f"<tr id='acc-detail-{a_code_esc}' class='acc-detail-row' "
            f"style='display:{detail_disp};'>"
            f"<td colspan='7' style='padding:0; background:#f8fafc; "
            f"border-bottom:2px solid #cbd5e1;'>"
            f"<div class='acc-detail-panel'>"
            f"<div class='acc-detail-header'>"
            f"<div>📂 <strong>[{a_lbl}] 분류 전표 목록</strong> "
            f"<span style='color:#475569; font-size:12px; margin-left:6px;'>"
            f"총 <strong>{ab['tx_count']}건</strong> · "
            f"지출합계 <strong>{ab['total_expenditure']:,}원</strong> · "
            f"공급가액 {ab['supply_value']:,}원 · "
            f"공제부가세 {ab['deductible_vat']:,}원</span></div>"
            f"<div class='no-print' style='display:flex; gap:6px; align-items:center;'>"
            f"<button class='secondary' style='padding:4px 10px; font-size:12px;' "
            f"onclick=\"filterByAccount('{a_code_esc}')\">🔎 이 계정만 단독 조회</button>"
            f"<button class='secondary' style='padding:4px 10px; font-size:12px;' "
            f"onclick=\"toggleAccountDetail('{a_code_esc}')\">✕ 목록 접기</button>"
            f"</div>"
            f"</div>"
            f"<div class='acc-detail-table-wrap'>"
            f"<table class='acc-detail-table'>"
            f"<thead><tr>"
            f"<th class='nowrap'>전표번호</th><th>거래일시 (영수증/명세서)</th>"
            f"<th class='nowrap'>카드구분</th><th>가맹점명</th>"
            f"<th class='num nowrap'>합계금액</th><th class='num nowrap'>공급가액</th>"
            f"<th class='num nowrap'>부가세</th><th class='nowrap'>공제구분</th>"
            f"<th>적요 / 품목 소명</th><th class='nowrap'>계정 재분류 · 저장</th>"
            f"<th>증빙파일명</th>"
            f"</tr></thead>"
            f"<tbody>{acc_detail_tbody_str}</tbody>"
            f"</table>"
            f"</div>"
            f"</div>"
            f"</td>"
            f"</tr>"
        )
    acc_breakdown_tbody = (
        "\n".join(acc_breakdown_rows)
        if acc_breakdown_rows
        else "<tr><td colspan='7' class='empty'>집계된 계정과목 내역이 없습니다.</td></tr>"
    )

    pending_rows_html = []
    for item in summary["pending_transactions"][:150]:
        tx_id = item["tx_id"]
        c_lbl = html.escape(str(item.get("card_label") or format_card_label(item["card_last4"])))
        merchant = html.escape(str(item["raw_merchant_name"]))
        amt_str = f"{int(item['total_amount']):,}원"
        cur_code = str(item["account_code"])
        memo_val = html.escape(str(item.get("counterparty_info") or ""))
        acc_opts = []
        for code_k, code_v in ACCOUNT_CODE_CATALOG.items():
            sel = "selected" if code_k == cur_code else ""
            acc_opts.append(f"<option value='{code_k}' {sel}>{code_k} {html.escape(code_v)}</option>")
        acc_select = f"<select id='acc-{tx_id}'>{''.join(acc_opts)}</select>"
        t_dt = html.escape(str(item["transacted_at"]))
        dt_link = (
            f"<a href='javascript:void(0)' class='tx-date-link' "
            f"onclick='openReceiptModal({tx_id})' title='클릭하여 영수증/매출전표 팝업 보기'>{t_dt} 🧾</a>"
        )
        pending_rows_html.append(
            f"<tr data-tx-id='{tx_id}' data-total-amount='{int(item['total_amount'])}'>"
            f"<td class='tx-pin-cell' title='클릭 시 이 전표 행을 상단 기준행으로 고정/해제'>#{tx_id}</td>"
            f"<td>{dt_link}</td>"
            f"<td><span class='badge card'>{c_lbl}</span></td>"
            f"<td><strong>{merchant}</strong></td>"
            f"<td class='num'>{amt_str}</td>"
            f"<td>{acc_select}</td>"
            f"<td><input type='text' id='memo-{tx_id}' value='{memo_val}' "
            f"placeholder='품목 또는 참석자 입력' /></td>"
            f"<td style='white-space:nowrap;'>"
            f"<button onclick='saveTxMemo({tx_id})'>저장</button>"
            f"<button class='secondary' onclick='openStatementModal({tx_id})'>📄 거래명세서</button>"
            f"<button class='secondary' onclick='triggerRowStatementUpload({tx_id})'>📎 명세서 첨부</button>"
            f"</td>"
            f"</tr>"
        )
    pending_tbody = (
        "\n".join(pending_rows_html)
        if pending_rows_html
        else "<tr><td colspan='8' class='empty'>미분류 또는 소명 누락 전표가 없습니다.</td></tr>"
    )

    tx_rows_html = []
    stmt_tx_count = 0
    for tx in summary["transactions"]:
        tx_id = tx["tx_id"]
        c_lbl = html.escape(str(tx.get("card_label") or format_card_label(tx["card_last4"])))
        merchant = html.escape(str(tx["raw_merchant_name"]))
        amt_str = f"{int(tx['total_amount']):,}원"
        vat_str = f"{int(tx['vat_amount']):,}원"
        tx_acc_code = html.escape(str(tx["account_code"]))
        acc_str = html.escape(f"{tx['account_code']} {tx['account_name']}")
        ded_str = html.escape(str(tx["vat_deduction_type"]))
        memo_str = html.escape(str(tx.get("counterparty_info") or "-"))
        raw_arch = str(tx.get("archived_file_name") or "")
        has_stmt = raw_arch.startswith("거래명세서_")
        if has_stmt:
            stmt_tx_count += 1
        arch_str = html.escape(raw_arch or "-")
        t_dt = html.escape(str(tx["transacted_at"]))
        stmt_badge = (
            "<span style='background:#dcfce7; color:#166534; padding:2px 6px; "
            "border-radius:4px; font-weight:700; font-size:11px;'>📄명세서 완료</span>"
            if has_stmt
            else "[📄명세서]"
        )
        dt_link = (
            f"<a href='javascript:void(0)' class='tx-date-link' "
            f"onclick='openReceiptModal({tx_id})' title='클릭하여 영수증/매출전표 팝업 보기'>{t_dt} 🧾</a>"
            f" <a href='javascript:void(0)' class='tx-date-link' "
            f"style='color:#0f766e; font-size:12px; text-decoration:none;' "
            f"onclick='openStatementModal({tx_id})' title='클릭하여 거래명세서 보기/발급'>{stmt_badge}</a>"
        )
        acc_cell_link = (
            f"<a href='javascript:void(0)' class='tx-date-link' "
            f"style='color:#0f172a; text-decoration:none; border-bottom:1px dashed #64748b;' "
            f"onclick=\"focusAccountSummary('{tx_acc_code}')\" "
            f"title='클릭하여 {acc_str} 분류 목록 보기'>{acc_str}</a>"
        )
        has_stmt_flag = "1" if has_stmt else "0"
        tx_rows_html.append(
            f"<tr data-tx-id='{tx_id}' data-has-stmt='{has_stmt_flag}' data-account-code='{tx_acc_code}' "
            f"data-total-amount='{int(tx['total_amount'])}'>"
            f"<td class='tx-pin-cell' title='클릭 시 이 전표 행을 상단 기준행으로 고정/해제'>#{tx_id}</td>"
            f"<td>{dt_link}</td>"
            f"<td><span class='badge card'>{c_lbl}</span></td>"
            f"<td>{merchant}</td>"
            f"<td class='num'>{amt_str}</td>"
            f"<td class='num'>{vat_str}</td>"
            f"<td>{acc_cell_link}</td>"
            f"<td>{ded_str}</td>"
            f"<td>{memo_str}</td>"
            f"<td>{arch_str}</td>"
            f"</tr>"
        )
    tx_tbody = (
        "\n".join(tx_rows_html)
        if tx_rows_html
        else "<tr><td colspan='10' class='empty'>해당 조건의 거래 내역이 없습니다.</td></tr>"
    )

    corp_policy = summary.get("corp_card_policy") or load_corp_card_policy()
    corp_only_active = bool(corp_policy.get("corp_only_mode", False))
    reg_cards = corp_policy.get("registered_corp_cards", [])
    corp_mode_badge_html = (
        '<span class="badge env" style="background:#dbeafe; color:#1e40af;">🏢 법인카드 전용 모드</span>'
        if corp_only_active
        else ""
    )
    if corp_only_active:
        if reg_cards:
            reg_badges = " ".join(
                f"<span class='badge card' style='background:#0f766e; color:#fff;'>"
                f"{html.escape(str(c.get('label') or c.get('card_last4')))}</span>"
                for c in reg_cards
            )
            reg_summary_html = f"등록된 법인카드({len(reg_cards)}장): {reg_badges}"
        else:
            reg_summary_html = (
                "<span style='color:#b45309; font-weight:700;'>"
                "⚠️ 아직 등록된 법인카드 번호가 없습니다. "
                "우측 버튼으로 법인카드 끝 4자리를 등록하거나 법인카드 명세서(.xls/.xlsx/.csv)를 업로드하세요."
                "</span>"
            )
        corp_banner_html = (
            "<section class='section no-print' "
            "style='background:#f0fdfa; border:2px solid #0d9488; margin-bottom:20px;'>"
            "<div style='display:flex; justify-content:space-between; align-items:center; "
            "flex-wrap:wrap; gap:10px;'>"
            "<div>"
            "<div style='font-size:15px; font-weight:800; color:#115e59; margin-bottom:4px;'>"
            "🏢 법인카드 사용내역 전용 테스트 모드 활성화됨 "
            "(개인카드 SMS·푸시 및 미매칭 개인 쇼핑메일 자동 차단)"
            "</div>"
            f"<div style='font-size:13px; color:#134e4a;'>{reg_summary_html}</div>"
            "</div>"
            "<div style='display:flex; gap:6px; flex-wrap:wrap;'>"
            "<button style='background:#0f766e; font-weight:700;' onclick='openCorpCardModal()'>"
            "💳 법인카드 번호 등록 · 정책 설정</button>"
            "<button onclick=\"document.getElementById('csv-file-input').click()\">"
            "📂 법인카드 명세서(.xls/.xlsx/.csv) 업로드</button>"
            "<button class='secondary' onclick='seedDemoData()'>법인카드 샘플 6건 생성</button>"
            "</div>"
            "</div>"
            "</section>"
        )
    else:
        corp_banner_html = ""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>SoloLedger v{__version__} — 1인 법인 경비·세무증빙 대시보드</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           margin: 0; padding: 24px; background: #f8fafc; color: #0f172a; }}
    header {{ display: flex; flex-direction: column; gap: 14px;
              margin-bottom: 24px; border-bottom: 2px solid #e2e8f0; padding-bottom: 16px; }}
    .header-top {{ display: flex; justify-content: space-between; align-items: flex-start;
                   flex-wrap: wrap; gap: 12px; }}
    .header-title-wrap h1 {{ margin: 0 0 6px 0; font-size: 22px; display: flex;
                             align-items: center; flex-wrap: wrap; gap: 6px; }}
    .header-meta {{ font-size: 13px; color: #475569; }}
    .action-toolbar {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
    .action-toolbar button {{ margin-left: 0; }}
    .filter-panel {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px;
                     padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }}
    .filter-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; }}
    .filter-group {{ display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }}
    .filter-group button {{ margin-left: 0; }}
    .filter-label {{ font-size: 13px; font-weight: 700; color: #334155; white-space: nowrap; }}
    .filter-divider {{ width: 1px; height: 20px; background: #cbd5e1; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
              gap: 16px; margin-bottom: 24px; }}
    .card {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px; }}
    .card h3 {{ margin: 0 0 8px 0; font-size: 13px; color: #64748b; }}
    .card .val {{ font-size: 22px; font-weight: 700; color: #0f172a; }}
    .section {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px;
                padding: 18px; margin-bottom: 24px;
                --sticky-bar-h: 44px; --sticky-thead-h: 40px; }}
    .gauge-track {{ width: 100%; height: 16px; background: #e2e8f0; border-radius: 8px;
                    overflow: hidden; margin: 10px 0; }}
    .gauge-bar {{ height: 100%; background: #2563eb; width: {gauge_pct}%; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e2e8f0; padding: 9px 8px; text-align: left; }}
    th {{ background: #f1f5f9; font-weight: 600; }}
    th.num, td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    th.nowrap {{ white-space: nowrap; }}
    td.empty {{ text-align: center; color: #64748b; padding: 18px; }}
    .section-sticky-bar {{
      display: flex; justify-content: space-between; align-items: center;
      flex-wrap: wrap; gap: 8px; background: #ffffff;
      padding: 10px 0; margin: -8px 0 0 0;
    }}
    .section-sticky-bar h2 {{ margin: 0; font-size: 18px; }}
    body:not(.unfreeze-headers) .section-sticky-bar {{
      position: sticky; top: 0; z-index: 30;
      box-shadow: 0 -4px 0 #ffffff;
    }}
    body:not(.unfreeze-headers) .section.is-stuck > .section-sticky-bar {{
      box-shadow: 0 -4px 0 #ffffff, inset 0 -1px 0 #e2e8f0;
    }}
    body:not(.unfreeze-headers) .sticky-ledger-table > thead > tr > th {{
      position: sticky; top: var(--sticky-bar-h, 44px); z-index: 25;
      background: #f1f5f9; color: #0f172a; border-bottom: none;
      word-break: keep-all;
      box-shadow: 0 -1px 0 #e2e8f0, inset 0 -2px 0 #cbd5e1;
      transition: background-color 0.12s ease, color 0.12s ease, box-shadow 0.12s ease;
    }}
    body:not(.unfreeze-headers) .section.is-stuck > .sticky-ledger-table > thead > tr > th {{
      background: #e2e8f0;
      box-shadow: 0 -1px 0 #cbd5e1, inset 0 -2px 0 #2563eb, 0 4px 10px -2px rgba(15, 23, 42, 0.12);
    }}
    .sticky-ledger-table > thead > tr > th.col-hover-active,
    .acc-detail-table > thead > tr > th.col-hover-active {{
      background: #dbeafe !important; color: #1d4ed8 !important;
      box-shadow: 0 -1px 0 #bfdbfe, inset 0 -3px 0 #1d4ed8 !important;
    }}
    .sticky-ledger-table > tbody > tr {{
      scroll-margin-top: calc(var(--sticky-bar-h, 44px) + var(--sticky-thead-h, 40px) + 12px);
    }}
    .sticky-ledger-table > tbody > tr:not(.acc-detail-row):not(.acc-summary-row):hover > td,
    .acc-detail-table > tbody > tr:hover > td {{
      background: #f8fafc;
    }}
    td.tx-pin-cell {{
      cursor: pointer; white-space: nowrap; font-weight: 600; color: #334155;
      user-select: none;
    }}
    td.tx-pin-cell:hover {{ color: #1d4ed8; background: #eff6ff !important; }}
    td.tx-pin-cell::after {{
      content: ' 📌'; font-size: 10px; opacity: 0; transition: opacity 0.12s ease;
    }}
    td.tx-pin-cell:hover::after,
    tr.pinned-ledger-row > td.tx-pin-cell::after {{ opacity: 1; }}
    body:not(.unfreeze-headers) .sticky-ledger-table > tbody > tr.pinned-ledger-row > td {{
      position: sticky;
      top: var(--pinned-row-top, calc(var(--sticky-bar-h, 44px) + var(--sticky-thead-h, 40px)));
      z-index: 20;
      background: #fefce8 !important;
      border-bottom: none;
      box-shadow: 0 -1px 0 #fde047, inset 0 -2px 0 #eab308, 0 3px 8px -2px rgba(15, 23, 42, 0.10);
    }}
    body.unfreeze-headers .sticky-ledger-table > tbody > tr.pinned-ledger-row > td {{
      background: #fefce8 !important;
      box-shadow: inset 0 -2px 0 #eab308;
    }}
    .freeze-toggle-btn {{
      background: #eff6ff; color: #1d4ed8; border: 1px solid #93c5fd;
      font-weight: 700; font-size: 12px; padding: 5px 10px;
    }}
    .freeze-toggle-btn:hover {{ background: #dbeafe; }}
    .freeze-toggle-btn.off {{
      background: #f1f5f9; color: #475569; border-color: #cbd5e1;
    }}
    .scroll-top-btn {{
      background: #f8fafc; color: #334155; border: 1px solid #cbd5e1;
      font-weight: 700; font-size: 12px; padding: 5px 9px;
    }}
    .scroll-top-btn:hover {{ background: #e2e8f0; color: #0f172a; }}
    .badge.warn {{ background: #fef3c7; color: #92400e; padding: 2px 8px; border-radius: 4px; }}
    .badge.card {{ background: #dbeafe; color: #1e40af; padding: 3px 8px; border-radius: 4px;
                   font-weight: 600; font-size: 12px; }}
    .badge.env {{ background: #dcfce7; color: #166534; padding: 3px 10px; border-radius: 6px;
                  font-size: 12px; font-weight: 700; margin-left: 8px; }}
    button {{ background: #2563eb; color: #fff; border: none; border-radius: 6px;
              padding: 6px 12px; cursor: pointer; margin-left: 4px; }}
    button.secondary {{ background: #475569; }}
    button.danger {{ background: #dc2626; }}
    input[type="text"], input[type="date"], select {{
      padding: 5px 8px; border: 1px solid #cbd5e1; border-radius: 4px;
    }}
    a.tx-date-link {{ color: #1d4ed8; font-weight: 600; text-decoration: underline;
                      text-underline-offset: 3px; cursor: pointer; }}
    a.tx-date-link:hover {{ color: #1e3a8a; background: #eff6ff; border-radius: 4px; }}
    tr.acc-summary-row {{ cursor: pointer; transition: background 0.15s ease; }}
    tr.acc-summary-row:hover {{ background: #eff6ff; }}
    tr.acc-summary-row.active {{ background: #dbeafe; }}
    a.acc-subject-link {{ display: inline-flex; align-items: center; gap: 6px;
                          color: #0f172a; text-decoration: none; font-weight: 700; }}
    a.acc-subject-link:hover strong {{ color: #1d4ed8; text-decoration: underline; }}
    .acc-arrow {{ display: inline-block; width: 14px; font-size: 11px; color: #2563eb; }}
    .acc-view-badge {{ background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe;
                       padding: 1px 7px; border-radius: 999px; font-size: 11px;
                       font-weight: 600; margin-left: 4px; }}
    tr.acc-summary-row.active .acc-view-badge {{ background: #1d4ed8; color: #ffffff;
                                                 border-color: #1d4ed8; }}
    .acc-detail-panel {{ padding: 12px 14px; background: #f8fafc;
                         border-left: 4px solid #2563eb; }}
    .acc-detail-header {{ display: flex; justify-content: space-between; align-items: center;
                          flex-wrap: wrap; gap: 8px; margin-bottom: 8px; font-size: 13px; }}
    .acc-detail-table-wrap {{ max-height: 440px; overflow-y: auto; background: #ffffff;
                              border: 1px solid #cbd5e1; border-radius: 8px; }}
    .acc-detail-table > thead > tr > th {{
      position: sticky; top: 0; background: #eff6ff;
      color: #1e3a8a; z-index: 3; font-size: 12px; border-bottom: none;
      word-break: keep-all;
      box-shadow: 0 -2px 0 #eff6ff, inset 0 -2px 0 #93c5fd;
    }}
    .acc-detail-table td {{ font-size: 12px; padding: 7px 8px; }}
    #email-matched-list thead th {{
      position: sticky; top: 0; z-index: 2; background: #f0fdf4;
      box-shadow: 0 -1px 0 #ffffff, inset 0 -2px 0 #86efac;
    }}
    #toss-scroll-results thead th {{
      position: sticky; top: 0; z-index: 2; background: #e0f2fe;
      box-shadow: 0 -1px 0 #ffffff, inset 0 -2px 0 #7dd3fc;
    }}
    .modal-backdrop {{ display: none; position: fixed; inset: 0; background: rgba(15, 23, 42, 0.58);
                       z-index: 9999; align-items: center; justify-content: center; padding: 16px; }}
    .modal-backdrop.open {{ display: flex; }}
    .receipt-modal, .modal-card {{
      background: #ffffff; width: 100%; max-width: 480px; max-height: 90vh;
      overflow-y: auto; border-radius: 12px; box-shadow: 0 20px 40px rgba(0,0,0,0.28);
      border: 1px solid #cbd5e1; padding: 22px; box-sizing: border-box;
    }}
    .modal-header {{ display: flex; justify-content: space-between; align-items: center;
                     gap: 10px; margin-bottom: 12px; font-size: 16px; }}
    .slip-box {{ background: #fffef7; border: 1px dashed #94a3b8; border-radius: 8px;
                 padding: 16px; font-size: 13px; line-height: 1.65; margin-bottom: 14px; }}
    .slip-title {{ text-align: center; font-size: 16px; font-weight: 800; margin-bottom: 10px;
                   border-bottom: 2px double #64748b; padding-bottom: 8px; }}
    .slip-row {{ display: flex; justify-content: space-between; align-items: flex-start;
                 gap: 12px; margin: 4px 0; }}
    .slip-row > span:first-child {{ white-space: nowrap; flex-shrink: 0; color: #334155; }}
    .slip-row code {{ word-break: break-all; text-align: right; }}
    .slip-divider {{ border-top: 1px dashed #94a3b8; margin: 8px 0; }}
    .slip-total {{ font-size: 17px; font-weight: 800; color: #1d4ed8; }}
    .receipt-img-wrap {{ text-align: center; margin-top: 10px; background: #f8fafc;
                         border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px; }}
    .receipt-img-wrap img {{ max-width: 100%; max-height: 360px; border-radius: 6px; }}
    .ctrlf-bar {{
      position: fixed; top: 14px; right: 20px; z-index: 10050; display: none;
      align-items: center; gap: 6px; flex-wrap: wrap; background: #ffffff;
      border: 2px solid #2563eb; border-radius: 10px; padding: 7px 10px;
      box-shadow: 0 12px 30px rgba(15, 23, 42, 0.24); max-width: calc(100vw - 32px);
    }}
    .ctrlf-bar.open {{ display: flex; }}
    .ctrlf-input {{
      width: 240px; padding: 6px 9px !important; font-size: 13px;
      border: 1px solid #94a3b8 !important; border-radius: 6px !important; outline: none;
    }}
    .ctrlf-input:focus {{
      border-color: #2563eb !important; box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.18);
    }}
    .ctrlf-count {{
      font-size: 12px; font-weight: 700; color: #334155; min-width: 52px;
      text-align: center; font-variant-numeric: tabular-nums;
      padding: 3px 7px; background: #f1f5f9; border-radius: 5px;
    }}
    .ctrlf-count.zero {{ background: #fee2e2; color: #b91c1c; }}
    .ctrlf-nav-btn {{
      background: #eff6ff; color: #1e40af; border: 1px solid #bfdbfe;
      padding: 4px 8px; font-size: 12px; font-weight: 700; margin-left: 0;
    }}
    .ctrlf-nav-btn:hover {{ background: #dbeafe; }}
    .ctrlf-filter-toggle {{
      display: inline-flex; align-items: center; gap: 4px; font-size: 12px;
      font-weight: 700; color: #0f172a; cursor: pointer; user-select: none;
      padding: 3px 7px; border-radius: 5px; background: #f8fafc; border: 1px solid #cbd5e1;
    }}
    .ctrlf-filter-pill {{
      font-size: 11px; font-weight: 700; color: #0f766e; background: #ccfbf1;
      padding: 3px 8px; border-radius: 999px; white-space: nowrap;
    }}
    .ctrlf-close-btn {{
      background: transparent; color: #475569; padding: 4px 7px;
      font-size: 13px; font-weight: 800; margin-left: 0;
    }}
    .ctrlf-close-btn:hover {{ background: #f1f5f9; color: #0f172a; }}
    mark.sl-find-hit {{
      background: #fef08a; color: #0f172a; padding: 0 1px;
      border-radius: 2px; box-shadow: 0 0 0 1px #facc15;
    }}
    mark.sl-find-hit.sl-find-active {{
      background: #fb923c; color: #ffffff; font-weight: 700;
      box-shadow: 0 0 0 2px #ea580c;
    }}
    input.sl-find-input-hit {{ background: #fef9c3 !important; border-color: #eab308 !important; }}
    input.sl-find-input-hit.sl-find-active {{
      background: #ffedd5 !important; border-color: #ea580c !important;
      box-shadow: 0 0 0 2px rgba(234, 88, 12, 0.35) !important;
    }}
    @media print {{
      body.modal-open {{ background: #ffffff !important; padding: 0 !important; margin: 0 !important; }}
      body.modal-open > header,
      body.modal-open > section {{ display: none !important; }}
      body.modal-open #receipt-modal-backdrop {{
        position: static !important;
        display: block !important;
        background: transparent !important;
        padding: 0 !important;
      }}
      body.modal-open .receipt-modal {{
        max-width: 560px !important;
        max-height: none !important;
        overflow: visible !important;
        box-shadow: none !important;
        border: none !important;
        margin: 0 auto !important;
        padding: 8px !important;
        break-inside: avoid;
      }}
      .no-print {{ display: none !important; }}
      .section-sticky-bar,
      .sticky-ledger-table > thead > tr > th,
      .sticky-ledger-table > tbody > tr.pinned-ledger-row > td {{
        position: static !important;
        box-shadow: none !important;
      }}
      mark.sl-find-hit {{
        background: transparent !important; color: inherit !important; box-shadow: none !important;
      }}
    }}
  </style>
</head>
<body>
  <div id="ctrlf-search-bar" class="ctrlf-bar no-print" role="search" aria-label="화면 및 전표 검색">
    <span style="font-size:14px;">🔎</span>
    <input type="text" id="ctrlf-search-input" class="ctrlf-input"
           placeholder="가맹점·품목·금액·전표번호 검색 (Ctrl+F)"
           autocomplete="off" spellcheck="false"
           oninput="onCtrlFInput()" onkeydown="onCtrlFInputKeydown(event)" />
    <span id="ctrlf-match-count" class="ctrlf-count">0 / 0</span>
    <button type="button" class="ctrlf-nav-btn" onclick="stepCtrlFMatch(-1)"
            title="이전 일치 항목 (Shift+Enter / Shift+F3)">▲</button>
    <button type="button" class="ctrlf-nav-btn" onclick="stepCtrlFMatch(1)"
            title="다음 일치 항목 (Enter / F3)">▼</button>
    <span id="ctrlf-filter-summary" class="ctrlf-filter-pill" style="display:none;"></span>
    <label class="ctrlf-filter-toggle"
           title="체크 시 원장 테이블에서 검색어와 일치하는 전표 행만 필터링합니다">
      <input type="checkbox" id="ctrlf-filter-rows-chk" onchange="onCtrlFRowFilterToggle()" />
      전표만 필터
    </label>
    <button type="button" class="ctrlf-close-btn" onclick="closeCtrlFSearch()"
            title="검색창 닫기 (Esc)">✕</button>
  </div>
  <header>
    <div class="header-top">
      <div class="header-title-wrap">
        <h1>SoloLedger 경비·세무증빙 대시보드 (v{__version__})
          <span class="badge env">{html.escape(summary['app_env'].upper())} MODE</span>
          {corp_mode_badge_html}
        </h1>
        <div class="header-meta">
          귀속 연도: <strong>{summary['year']}년</strong>
          &nbsp;|&nbsp;
          조회 구간: <strong>{s_date} ~ {e_date}</strong>{active_q_meta_html}
          &nbsp;|&nbsp;
          원장 DB: <code>{html.escape(Path(summary['db_path']).name)}</code>
        </div>
      </div>
      <div class="action-toolbar no-print">
        <input type="file" id="receipt-file-input" accept="image/*,.pdf"
               style="display:none" onchange="uploadReceiptFile(this)" />
        <input type="file" id="csv-file-input" accept=".csv,.txt,.xls,.xlsx,.html,.json"
               style="display:none" onchange="uploadCsvFile(this)" />
        <button class="secondary" onclick="window.open('/manual', '_blank')">사용 매뉴얼(HTML)</button>
        <button style="background:#0f766e; font-weight:700;"
                onclick="openCorpCardModal()">💳 법인카드 등록·전용모드</button>
        <button onclick="reapplyRules()">스마트 자동분류 실행</button>
        <button onclick="generateElectronicSlips()">전자 매출전표 일괄 생성</button>
        <button onclick="document.getElementById('receipt-file-input').click()">영수증 업로드(OCR)</button>
        <button onclick="document.getElementById('csv-file-input').click()">카드사/홈택스 전표 업로드</button>
        <button onclick="openCoupangModal()">🛒 쿠팡 주문·영수증 자동매칭</button>
        <button onclick="openEmailOrderModal()">📧 이메일 주문내역 실시간 연동</button>
        <button style="background:#0284c7; font-weight:700;"
                onclick="openTossScrollModal()">🟦 토스페이먼츠 법인카드 메일 스크롤링</button>
        <input type="file" id="eml-file-input" accept=".eml,.html,.htm,.txt"
               style="display:none" onchange="uploadEmlFile(this)" />
        <button onclick="importPhoneSms()">USB 휴대폰 문자 가져오기</button>
        <button class="secondary" onclick="seedDemoData()">샘플 데이터 생성</button>
        <button class="danger" onclick="resetTestLedger()">테스트 DB 초기화</button>
        <button onclick="backupToNas()">NAS(Z:\\) 백업 동기화</button>
        <button onclick="exportTaxPack('{summary['year_month']}')">세무사 제출용 ZIP+CSV 생성</button>
      </div>
    </div>

    <div class="filter-panel no-print">
      <div class="filter-row">
        <div class="filter-group">
          <span class="filter-label">📆 조회 기간:</span>
          <input type="date" id="start-date" value="{s_date}" />
          <span>~</span>
          <input type="date" id="end-date" value="{e_date}" />
          <button onclick="applyFilters()">기간 조회</button>
        </div>
        <div class="filter-divider"></div>
        <div class="filter-group">
          <button class="secondary" onclick="setQuickRange('THIS_MONTH')">이번 달</button>
          <button class="secondary" onclick="setQuickRange('LAST_MONTH')">지난 달</button>
          <button class="secondary" onclick="setQuickRange('THIS_QUARTER')">이번 분기</button>
          <button class="secondary" onclick="setQuickRange('LAST_QUARTER')">지난 분기</button>
          <button class="secondary" onclick="setQuickRange('3M')">최근 3개월</button>
          <button class="secondary" onclick="setQuickRange('THIS_YEAR')">올해 전체</button>
          <button class="secondary" onclick="setQuickRange('ALL')">전체 기간</button>
        </div>
        <div class="filter-divider"></div>
        <div class="filter-group">
          <span class="filter-label">💳 카드 선택:</span>
          <select id="card-filter" onchange="applyFilters()">
            {card_select_html}
          </select>
        </div>
        <div class="filter-divider"></div>
        <div class="filter-group">
          <span class="filter-label">📂 계정과목:</span>
          <select id="account-filter" onchange="applyFilters()">
            {account_select_html}
          </select>
        </div>
        <div class="filter-divider"></div>
        <div class="filter-group">
          <button type="button" style="background:#1e40af; font-weight:700;"
                  onclick="openCtrlFSearch()" title="화면 내 찾기 및 원장 전표 실시간 필터 (단축키: Ctrl+F)">
            🔎 빠른 검색 (Ctrl+F)
          </button>
          <button type="button" id="btn-toggle-freeze-header" class="freeze-toggle-btn"
                  onclick="toggleFreezeHeaders()"
                  title="스크롤 시 항목 헤더 행을 엑셀처럼 화면 상단에 고정/해제합니다">
            📌 상단 행 고정 ON
          </button>
        </div>
      </div>

      <div class="filter-row" style="border-top: 1px dashed #e2e8f0; padding-top: 8px;">
        <div class="filter-group">
          <span class="filter-label">📊 분기별 조회:</span>
          <button class="secondary" onclick="shiftQuarter(-1)" title="이전 분기">◀ 이전 분기</button>
          <select id="quarter-filter" onchange="selectQuarterRange(this.value)">
            {quarter_select_html}
          </select>
          <button class="secondary" onclick="shiftQuarter(1)" title="다음 분기">다음 분기 ▶</button>
        </div>
        <div class="filter-divider"></div>
        <div style="display:flex; align-items:center; flex-wrap:wrap; gap:3px;">
          <span class="filter-label" style="margin-right:4px;">분기별 바로가기:</span>
          {quarter_chips_html}
        </div>
      </div>

      <div class="filter-row" style="border-top: 1px dashed #e2e8f0; padding-top: 8px;">
        <div class="filter-group">
          <span class="filter-label">📅 달별 조회:</span>
          <button class="secondary" onclick="shiftMonth(-1)" title="이전 달">◀ 이전 달</button>
          <select id="month-filter" onchange="selectMonthRange(this.value)">
            {month_select_html}
          </select>
          <button class="secondary" onclick="shiftMonth(1)" title="다음 달">다음 달 ▶</button>
        </div>
        <div class="filter-divider"></div>
        <div style="display:flex; align-items:center; flex-wrap:wrap; gap:3px;">
          <span class="filter-label" style="margin-right:4px;">월별 바로가기:</span>
          {month_chips_html}
        </div>
      </div>
    </div>
  </header>

  {corp_banner_html}

  <section class="cards">
    <div class="card">
      <h3>조회 기간 총 지출액 ({summary['total_tx_count']}건)</h3>
      <div class="val">{summary['total_expenditure']:,}원</div>
    </div>
    <div class="card">
      <h3>공제 가능 부가세 합계</h3>
      <div class="val">{summary['deductible_vat']:,}원</div>
    </div>
    <div class="card">
      <h3>불공제 지출 합계 (부가세 {summary['non_deductible_vat']:,}원)</h3>
      <div class="val">{summary['non_deductible_total']:,}원</div>
    </div>
    <div class="card">
      <h3>미분류·소명 필요 건수</h3>
      <div class="val">{summary['pending_action_count']}건</div>
    </div>
  </section>

  <section class="section" id="account-summary-section">
    <div class="section-sticky-bar">
      <h2>
        계정과목별 지출 및 부가세 요약표 ({len(summary.get('account_breakdown', []))}개 계정)
        <span style="font-size:12px; font-weight:500; color:#475569; margin-left:8px;">
          💡 계정과목을 클릭하면 해당 계정의 분류된 전표 목록을 바로 볼 수 있습니다.
        </span>
      </h2>
      <div class="no-print" style="display:flex; gap:6px; align-items:center;">
        <button class="secondary" style="padding:4px 10px; font-size:12px;"
                onclick="toggleAllAccountDetails(true)">▼ 전체 계정 목록 펼치기</button>
        <button class="secondary" style="padding:4px 10px; font-size:12px;"
                onclick="toggleAllAccountDetails(false)">▲ 전체 접기</button>
      </div>
    </div>
    <table class="sticky-ledger-table">
      <thead>
        <tr>
          <th>계정과목 (코드·명칭 · 클릭 시 분류 목록 표시)</th>
          <th class="num nowrap">거래 건수</th>
          <th class="num nowrap">지출 합계</th>
          <th class="num nowrap">공급가액</th>
          <th class="num nowrap">공제 가능 부가세</th>
          <th class="num nowrap">불공제 합계</th>
          <th class="num nowrap">지출 비중</th>
        </tr>
      </thead>
      <tbody>
        {acc_breakdown_tbody}
      </tbody>
    </table>
  </section>

  <section class="section" id="card-summary-section">
    <div class="section-sticky-bar">
      <h2>카드별 지출 및 영수증 분류 현황 ({len(summary.get('card_breakdown', []))}개 카드)</h2>
    </div>
    <table class="sticky-ledger-table">
      <thead>
        <tr>
          <th>카드명 (식별번호)</th>
          <th class="num nowrap">결제 건수</th>
          <th class="num nowrap">지출 합계</th>
          <th class="num nowrap">공제 부가세</th>
          <th class="num nowrap">불공제 합계</th>
          <th class="num nowrap">첨부 영수증</th>
          <th>영수증 보관 폴더</th>
          <th class="nowrap">필터</th>
        </tr>
      </thead>
      <tbody>
        {card_breakdown_tbody}
      </tbody>
    </table>
  </section>

  <section class="section">
    <h2>업무용승용차 연간 1,500만 원 특례 한도 트래커 ({veh['year']}년)</h2>
    <p>누적 사용액: <strong>{veh['total_used']:,}원</strong> / {veh['annual_limit']:,}원
       ({veh['usage_ratio_pct']}%) | 잔여 한도: <strong>{veh['remaining_limit']:,}원</strong>
       | 상태: <strong>{veh['warning_level']}</strong></p>
    <div class="gauge-track"><div class="gauge-bar"></div></div>
    <p><small>{html.escape(veh['advice'])}</small></p>
  </section>

  <section class="section" id="pending-ledger-section">
    <div class="section-sticky-bar">
      <h2>미분류(999) 및 품목·참석자 소명 필요 전표 ({summary['pending_action_count']}건)</h2>
      <div class="no-print" style="display:flex; gap:6px; align-items:center;">
        <button type="button" class="scroll-top-btn" onclick="scrollToDashboardTop()"
                title="상단 조회 기간 및 필터 영역으로 즉시 이동">⬆ 맨 위로</button>
      </div>
    </div>
    <table class="sticky-ledger-table" id="pending-ledger-table">
      <thead>
        <tr>
          <th class="nowrap" title="전표번호 클릭 시 해당 행을 상단 기준행으로 고정/해제">전표번호</th>
          <th class="nowrap">거래일시</th>
          <th class="nowrap">카드구분</th>
          <th>가맹점명</th>
          <th class="num nowrap">금액</th>
          <th class="nowrap">현재 계정</th>
          <th>품목 / 참석자 소명</th>
          <th class="nowrap">작업</th>
        </tr>
      </thead>
      <tbody id="pending-ledger-tbody">
        {pending_tbody}
      </tbody>
    </table>
  </section>

  <section class="section" id="main-ledger-section">
    <div class="section-sticky-bar">
      <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
        <h2 style="margin:0;">조회 기간 법인카드 원장 ({s_date} ~ {e_date}, 총 {summary['total_tx_count']}건)</h2>
        <span id="ledger-account-filter-badge" class="badge card no-print"
              style="display:none; background:#dbeafe; color:#1e3a8a; padding:4px 10px;"></span>
        <span id="ledger-search-filter-badge" class="badge card no-print"
              style="display:none; background:#ccfbf1; color:#0f766e; padding:4px 10px;"></span>
      </div>
      <div class="no-print" style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
        <button type="button" id="btn-clear-pinned-rows" class="secondary"
                style="display:none; background:#ca8a04; font-weight:700;"
                onclick="clearAllPinnedLedgerRows()"
                title="고정된 기준 전표 행을 모두 해제합니다">
          📌 기준행 고정 해제
        </button>
        <button id="btn-clear-search-filter" class="secondary" style="display:none;"
                onclick="closeCtrlFSearch()">
          ✕ 검색 필터 해제
        </button>
        <button id="btn-clear-acc-filter" class="secondary" style="display:none;"
                onclick="clearLedgerAccountFilter()">
          📋 전체 계정 전표 다시 보기
        </button>
        <button id="btn-filter-stmt-only" class="secondary"
                onclick="toggleStatementRowsFilter({stmt_tx_count})">
          📄 거래명세서 발급 전표만 보기 ({stmt_tx_count}건)
        </button>
        <button class="secondary" onclick="openEmailOrderModal()">
          📧 이메일 연동 결과 확인
        </button>
        <button type="button" id="btn-ledger-freeze-header" class="freeze-toggle-btn"
                onclick="toggleFreezeHeaders()"
                title="스크롤 시 상단 항목 행 고정 켜기/끄기 (전표번호 클릭 시 해당 행도 기준행으로 추가 고정 가능)">
          📌 행 고정 ON
        </button>
        <button type="button" class="scroll-top-btn" onclick="scrollToDashboardTop()"
                title="상단 조회 기간 및 필터 영역으로 즉시 이동">
          ⬆ 맨 위로
        </button>
      </div>
    </div>
    <table class="sticky-ledger-table" id="main-ledger-table">
      <thead>
        <tr>
          <th class="nowrap" title="전표번호 클릭 시 해당 행을 상단 기준행으로 추가 고정할 수 있습니다">전표번호</th>
          <th>거래일시 (클릭: 영수증 팝업)</th>
          <th class="nowrap">카드구분</th>
          <th>가맹점명</th>
          <th class="num nowrap">합계금액</th>
          <th class="num nowrap">부가세</th>
          <th class="nowrap">계정과목</th>
          <th class="nowrap">공제구분</th>
          <th>적요/참석자</th>
          <th>증빙파일명</th>
        </tr>
      </thead>
      <tbody id="main-ledger-tbody">
        {tx_tbody}
      </tbody>
    </table>
  </section>

  <div id="receipt-modal-backdrop" class="modal-backdrop" onclick="onBackdropClick(event)">
    <div class="receipt-modal" style="max-width:640px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
        <h3 id="modal-header-title" style="margin:0;">🧾 신용카드 매출전표 / 거래명세서</h3>
        <button class="secondary no-print" onclick="closeReceiptModal()">✕ 닫기</button>
      </div>
      <div class="no-print" style="display:flex; gap:6px; margin-bottom:12px;">
        <button id="tab-btn-slip" onclick="switchModalDocTab('SLIP')">🧾 신용카드 매출전표</button>
        <button id="tab-btn-stmt" class="secondary" onclick="switchModalDocTab('STATEMENT')">
          📄 거래명세서
        </button>
      </div>
      <div id="modal-slip-content" class="slip-box">불러오는 중...</div>
      <div id="modal-image-content" class="receipt-img-wrap"></div>
      <input type="file" id="modal-receipt-input" accept="image/*,.pdf"
             style="display:none" onchange="uploadReceiptForActiveTx(this)" />
      <input type="file" id="row-stmt-file-input"
             accept="image/*,.pdf,.html,.htm,.xls,.csv,.txt,.json"
             style="display:none" onchange="uploadRowStatementFile(this)" />
      <div class="no-print" style="display:flex; justify-content:flex-end; gap:6px; margin-top:14px;">
        <button onclick="document.getElementById('modal-receipt-input').click()">
          📷 영수증 첨부
        </button>
        <button onclick="triggerRowStatementUpload(activeModalTxId)">
          📎 거래명세서 첨부
        </button>
        <button class="secondary" onclick="printReceiptSlip()">🖨️ 인쇄 / PDF 저장</button>
        <button class="secondary" onclick="closeReceiptModal()">닫기</button>
      </div>
    </div>
  </div>

  <div id="coupang-modal-backdrop" class="modal-backdrop no-print" onclick="onCoupangBackdropClick(event)">
    <div class="receipt-modal" style="max-width:600px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
        <h3 style="margin:0;">🛒 쿠팡 주문내역 · 영수증 자동 크롤링 및 매칭</h3>
        <button class="secondary" onclick="closeCoupangModal()">✕ 닫기</button>
      </div>
      <div class="slip-box" style="margin-bottom:12px; font-size:13px; line-height:1.55; border:2px solid #2563eb;">
        <div style="font-weight:700; color:#1d4ed8; margin-bottom:6px; font-size:14px;">
          🔄 [자동 수집 (방법 B)] 전용 브라우저 세션으로 2025~2026년 주문내역 자동 크롤링
        </div>
        <div style="color:#334155; margin-bottom:10px;">
          아래 버튼만 누르면 서버가 전용 브라우저(<code>archive/browser_profile/coupang</code>)를 열어
          2025~2026년 쿠팡 주문내역을 자동으로 긁어와 매칭한 뒤 창을 닫습니다.<br/>
          <span style="color:#64748b; font-size:12px;">
            * 최초 1회 실행 시에만 로그인 창이 뜨며, <b>[자동 로그인]</b>을 체크해 두면
            다음부터는 무인으로 즉시 수집됩니다.
          </span>
        </div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
          <button id="btn-coupang-auto-crawl" onclick="runCoupangAutoCrawl()"
            style="background:#15803d; font-weight:700; padding:8px 16px;">
            🚀 쿠팡 주문내역 자동 크롤링 시작
          </button>
        </div>
        <div id="coupang-crawl-status"
          style="margin-top:8px; font-size:12px; font-weight:600; color:#1e40af;"></div>
      </div>
      <div class="slip-box" style="margin-bottom:12px; font-size:13px; line-height:1.55;">
        <div style="font-weight:700; color:#1e40af; margin-bottom:6px;">
          [수동 보조 1] 현재 열려있는 브라우저 탭에서 스크립트로 수집
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          <button class="secondary"
            onclick="window.open('https://mc.coupang.com/ssr/desktop/order/list', '_blank')">
            🔗 1. 쿠팡 주문목록 열기
          </button>
          <button class="secondary" onclick="copyCoupangExtractorScript()">
            📋 2. 원클릭 수집 스크립트 복사
          </button>
          <button class="secondary" onclick="document.getElementById('csv-file-input').click()">
            📂 다운로드된 coupang_orders.json 업로드
          </button>
        </div>
        <div id="coupang-copy-status" style="margin-top:6px; font-size:12px; color:#15803d;"></div>
      </div>
      <div class="slip-box" style="font-size:13px; line-height:1.55;">
        <div style="font-weight:700; color:#0f172a; margin-bottom:6px;">
          [수동 보조 2] 쿠팡 주문목록 화면 복사(<code>Ctrl+A</code> → <code>Ctrl+C</code>) 바로 붙여넣기
        </div>
        <textarea id="coupang-paste-input" rows="4"
          style="width:100%; box-sizing:border-box; padding:8px;
                 border:1px solid #cbd5e1; border-radius:6px; font-size:12px;"
          placeholder="쿠팡 주문목록에서 Ctrl+A, Ctrl+C로 복사한 텍스트나 JSON을 붙여넣으세요..."
        ></textarea>
        <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:8px;">
          <button onclick="submitCoupangPaste()">⚡ 붙여넣은 주문내역 즉시 매칭 실행</button>
          <button class="secondary" onclick="closeCoupangModal()">닫기</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 이메일 주문내역 실시간 연동 모달 (다중 계정 지원) -->
  <div id="email-modal-backdrop" class="modal-backdrop no-print"
       onclick="onEmailBackdropClick(event)">
    <div class="modal-card" style="max-width:820px;">
      <div class="modal-header">
        <strong>📧 다중 이메일 주문·결제내역 실시간 연동 및 거래명세서 자동 작성</strong>
        <button class="secondary" onclick="closeEmailOrderModal()">닫기 (ESC)</button>
      </div>

      <div class="slip-box"
           style="margin-bottom:10px; font-size:13px; line-height:1.55;
                  border:2px solid #2563eb; background:#eff6ff;">
        <div style="display:flex; justify-content:space-between; align-items:center;
                    flex-wrap:wrap; gap:8px; margin-bottom:6px;">
          <div style="font-weight:800; color:#1e3a8a; font-size:14px;">
            📬 연결된 이메일 계정 목록 (전체 쇼핑몰·PG + 토스페이먼츠 매출전표 통합 스크롤)
          </div>
          <div style="display:flex; gap:6px; flex-wrap:wrap;">
            <button id="btn-email-sync-all" onclick="triggerEmailSyncNow(false, '', this)">
              🚀 연결된 전체 메일함 즉시 통합 스캔
            </button>
            <button id="btn-email-force-sync" class="secondary" onclick="triggerEmailSyncNow(true, '', this)">
              🔄 전체 강제 재매칭
            </button>
          </div>
        </div>
        <div style="font-size:12px; color:#334155; margin-bottom:8px;">
          설정한 <b>스캔 기간(기본 365일)</b> 동안의 전체보관함·받은편지함을 통수 제한 없이
          페이지 단위로 끝까지 스크롤하여 쿠팡·네이버페이·이니시스·KCP 주문내역과
          <b>토스페이먼츠 매출전표(법인카드 판별·사업자번호·부가세 검증)</b>를 한 번에 정리합니다.
        </div>

        <div id="email-accounts-list"
             style="background:#ffffff; border:1px solid #cbd5e1; border-radius:6px;
                    padding:8px; margin-bottom:10px; max-height:160px; overflow-y:auto;">
          <div style="color:#64748b; font-size:12px;">등록된 이메일 계정을 불러오는 중...</div>
        </div>

        <div style="display:flex; align-items:center; justify-content:space-between;
                    flex-wrap:wrap; gap:8px; background:#dbeafe; padding:8px 10px;
                    border-radius:6px; font-size:12px;">
          <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
            <span>스캔 기간(일):</span>
            <input type="text" id="email-lookback-days" value="365" style="width:54px;" />
            <button type="button" class="secondary" style="padding:2px 6px; font-size:11px;"
                    onclick="setEmailLookbackPreset(60)">60일</button>
            <button type="button" class="secondary" style="padding:2px 6px; font-size:11px;"
                    onclick="setEmailLookbackPreset(180)">180일</button>
            <button type="button" class="secondary" style="padding:2px 6px; font-size:11px; font-weight:700;"
                    onclick="setEmailLookbackPreset(365)">1년(365일)</button>
            <span style="margin-left:4px;">감지 주기(초):</span>
            <input type="text" id="email-poll-interval" value="120" style="width:60px;" />
            <label style="font-weight:700; color:#1e40af; cursor:pointer;">
              <input type="checkbox" id="email-auto-poll" /> 실시간 자동 감지 ON
            </label>
          </div>
          <button class="secondary" onclick="saveGlobalEmailPollSettings()">
            💾 실시간 주기 설정 저장
          </button>
        </div>

        <div id="email-sync-status"
             style="margin-top:8px; font-size:12px; font-weight:700; color:#1e40af;"></div>
      </div>

      <details id="toss-scroll-panel" class="slip-box"
               style="margin-bottom:10px; font-size:13px; line-height:1.55;
                      border:1px solid #0284c7; background:#f0f9ff;">
        <summary style="cursor:pointer; font-weight:800; color:#0369a1; font-size:13px;">
          🟦 [보조] 토스페이먼츠(Toss Payments) 단독 기간지정 스크롤링 (위 통합 스캔에 이미 자동 포함됨)
        </summary>
        <div style="margin-top:8px;">
          <div style="display:flex; justify-content:space-between; align-items:center;
                      flex-wrap:wrap; gap:8px; margin-bottom:6px;">
            <div style="font-size:12px; color:#334155;">
              특정 시작일~종료일 구간의 토스페이먼츠 메일만 별도로 다시 검증하고 싶을 때 사용하세요.
            </div>
            <button id="btn-toss-email-scroll" style="background:#0284c7; font-weight:700;"
                    onclick="triggerTossEmailScroll(this)">
              🔍 법인카드 토스페이먼츠 메일 스크롤링 실행
            </button>
          </div>
          <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:12px;">
            <span>탐색 시작일(선택):</span>
            <input type="date" id="toss-scroll-start" style="width:136px;" />
            <span>종료일(선택):</span>
            <input type="date" id="toss-scroll-end" style="width:136px;" />
            <span style="color:#64748b; font-size:11px;">(비워두면 메일함 전체 기간 연속 스크롤)</span>
          </div>
          <div id="toss-scroll-status"
               style="margin-top:8px; font-size:12px; font-weight:700; color:#0369a1;"></div>
          <div id="toss-scroll-results"
               style="display:none; margin-top:8px; background:#ffffff; border:1px solid #bae6fd;
                      border-radius:6px; padding:6px; max-height:200px; overflow-y:auto;"></div>
        </div>
      </details>

      <div class="slip-box"
           style="margin-bottom:10px; font-size:13px; line-height:1.55;
                  border:1px solid #16a34a; background:#f0fdf4;">
        <div style="display:flex; justify-content:space-between; align-items:center;
                    flex-wrap:wrap; gap:6px; margin-bottom:6px;">
          <div style="font-weight:800; color:#166534; font-size:13.5px;">
            📋 이메일·온라인 주문 연동 결과 확인 (거래명세서 자동 작성:
            <span id="email-matched-count">0</span>건)
          </div>
          <span style="font-size:11px; color:#15803d;">
            각 전표의 <b>[📄 명세서 보기]</b>를 클릭하면 품목·VAT 내역을 바로 확인할 수 있습니다.
          </span>
        </div>
        <div id="email-matched-list"
             style="background:#ffffff; border:1px solid #bbf7d0; border-radius:6px;
                    padding:6px; max-height:220px; overflow-y:auto;">
          <div style="color:#64748b; font-size:12px; padding:6px;">
            연동된 거래명세서 내역을 불러오는 중...
          </div>
        </div>
      </div>

      <div class="slip-box" style="margin-bottom:10px; font-size:13px; line-height:1.55;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
          <div id="email-form-title" style="font-weight:800; color:#0f172a;">
            ➕ 새 이메일 계정 추가 (개수 제한 없음)
          </div>
          <button class="secondary" style="padding:3px 8px; font-size:11px;"
                  onclick="resetEmailAccountForm()">입력 초기화</button>
        </div>
        <input type="hidden" id="email-editing-id" value="" />
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px;">
          <div>
            <label style="font-size:11px; font-weight:700; display:block; margin-bottom:3px;">
              계정 별칭 (구분용 이름)
            </label>
            <input type="text" id="email-label"
                   placeholder="예: 대표 네이버메일 / 법인 Gmail" style="width:100%;" />
          </div>
          <div>
            <label style="font-size:11px; font-weight:700; display:block; margin-bottom:3px;">
              메일 서비스 선택
            </label>
            <select id="email-provider" style="width:100%;" onchange="onEmailProviderChange()">
              <option value="NAVER">네이버 메일 (imap.naver.com)</option>
              <option value="GMAIL">Google Gmail (imap.gmail.com)</option>
              <option value="DAUM_KAKAO">다음 / 카카오 메일 (imap.daum.net)</option>
              <option value="WORKSMOBILE">네이버웍스 (imap.worksmobile.com)</option>
              <option value="OUTLOOK">Outlook / M365 (outlook.office365.com)</option>
              <option value="CUSTOM">직접 입력 (사내 웹메일 / 기타 IMAP)</option>
            </select>
          </div>
          <div>
            <label style="font-size:11px; font-weight:700; display:block; margin-bottom:3px;">
              이메일 주소 (로그인 ID)
            </label>
            <input type="text" id="email-address"
                   placeholder="예: mycorp@naver.com" style="width:100%;" />
          </div>
          <div>
            <label style="font-size:11px; font-weight:700; display:block; margin-bottom:3px;">
              IMAP 비밀번호 (2단계 인증 시 앱 비밀번호)
            </label>
            <input type="password" id="email-app-password"
                   placeholder="비밀번호 또는 앱 비밀번호 입력" style="width:100%;" />
          </div>
          <div>
            <label style="font-size:11px; font-weight:700; display:block; margin-bottom:3px;">
              IMAP 호스트
            </label>
            <input type="text" id="email-imap-host" value="imap.naver.com" style="width:100%;" />
          </div>
          <div>
            <label style="font-size:11px; font-weight:700; display:block; margin-bottom:3px;">
              포트(SSL)
            </label>
            <input type="text" id="email-imap-port" value="993" style="width:100%;" />
          </div>
        </div>
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
          <span style="font-size:11px; color:#475569;">
            💡 네이버(환경설정 → POP3/IMAP → IMAP 사용함) · Gmail(Google 계정 → 보안 → 앱 비밀번호)
          </span>
          <div style="display:flex; gap:6px;">
            <button id="btn-email-save-sync" onclick="saveAndSyncEmailWatcher(true)">
              ➕ 계정 저장 후 즉시 스캔
            </button>
            <button class="secondary" onclick="saveAndSyncEmailWatcher(false)">
              계정만 추가·저장
            </button>
          </div>
        </div>
      </div>

      <div class="slip-box" style="font-size:13px; line-height:1.55;">
        <div style="font-weight:700; color:#0f172a; margin-bottom:6px;">
          [수동 즉시 매칭] 수신된 주문/결제 메일 본문 붙여넣기 또는 <code>.eml</code> 메일 파일 업로드
        </div>
        <textarea id="email-paste-input" rows="3"
          style="width:100%; box-sizing:border-box; padding:8px;
                 border:1px solid #cbd5e1; border-radius:6px; font-size:12px;"
          placeholder="이니시스·쿠팡·네이버페이·KCP·토스 결제완료 메일 본문을 복사해 붙여넣으세요..."
        ></textarea>
        <div style="display:flex; justify-content:space-between; gap:8px; margin-top:8px; flex-wrap:wrap;">
          <button class="secondary"
            onclick="document.getElementById('eml-file-input').click()">
            📎 .eml / .html 메일 파일 직접 업로드
          </button>
          <div style="display:flex; gap:8px;">
            <button onclick="submitEmailPaste()">⚡ 붙여넣은 메일 즉시 매칭 · 명세서 생성</button>
            <button class="secondary" onclick="closeEmailOrderModal()">닫기</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div id="corp-card-modal-backdrop" class="modal-backdrop" onclick="onCorpCardBackdropClick(event)">
    <div class="modal-card" style="max-width:740px;">
      <div class="modal-header">
        <strong>💳 법인카드 번호 등록 · 법인카드 전용 테스트 모드 설정</strong>
        <button class="secondary" onclick="closeCorpCardModal()">✕ 닫기</button>
      </div>
      <div class="slip-box" style="background:#f0fdfa; border-color:#5eead4; margin-bottom:12px;">
        <div style="font-weight:800; color:#115e59; margin-bottom:6px;">
          🛡️ 법인카드 전용 필터링 정책 (개인카드·미매칭 개인메일 혼입 방지)
        </div>
        <div style="display:flex; flex-direction:column; gap:6px; font-size:13px;">
          <label style="display:flex; align-items:center; gap:6px; cursor:pointer; font-weight:700;">
            <input type="checkbox" id="corp-only-mode-chk" checked />
            법인카드 전용 모드 활성화 (개인카드 KB 8691/롯데 6*4*/네이버현대/개인우리 6534/삼성 8508 등 차단)
          </label>
          <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
            <input type="checkbox" id="corp-strict-registered-chk" checked />
            등록된 법인카드 번호만 USB 문자(SMS)·앱 푸시에서 수집 (미등록 카드번호 자동 무시)
          </label>
          <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
            <input type="checkbox" id="corp-block-unmatched-email-chk" checked />
            법인카드 전표와 매칭되지 않는 쇼핑몰 이메일의 단독 전표(EMAIL_ORDER) 자동 생성 차단
          </label>
          <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
            <input type="checkbox" id="corp-auto-reg-upload-chk" checked />
            법인카드 이용내역 명세서(.xls/.xlsx/.csv) 업로드 시 포함된 법인카드 번호 자동 등록
          </label>
        </div>
        <div style="display:flex; justify-content:space-between; align-items:center; margin-top:10px;">
          <span id="corp-policy-status" style="font-size:12px; font-weight:700; color:#0f766e;"></span>
          <button style="background:#0f766e;" onclick="saveCorpCardPolicySettings()">
            💾 전용 모드 정책 저장
          </button>
        </div>
      </div>

      <div class="slip-box" style="margin-bottom:12px;">
        <div style="font-weight:800; color:#0f172a; margin-bottom:8px;">
          ➕ 새 법인카드 등록 (카드 끝 4자리 또는 마스킹 번호)
        </div>
        <div style="display:grid; grid-template-columns:1fr 1fr 1.3fr auto; gap:8px; align-items:end;">
          <div>
            <label style="font-size:11px; font-weight:700; display:block; margin-bottom:3px;">카드사 선택</label>
            <select id="corp-card-issuer" style="width:100%;" onchange="onCorpIssuerChange()">
              <option value="WOORI">우리법인카드</option>
              <option value="KB">KB국민법인카드</option>
              <option value="SHINHAN">신한법인카드</option>
              <option value="HANA">하나법인카드</option>
              <option value="SAMSUNG">삼성법인카드</option>
              <option value="HYUNDAI">현대법인카드</option>
              <option value="LOTTE">롯데법인카드</option>
              <option value="NH">NH농협법인카드</option>
              <option value="IBK">IBK기업법인카드</option>
              <option value="BC">BC법인카드</option>
              <option value="CORP">기타 법인카드</option>
            </select>
          </div>
          <div>
            <label style="font-size:11px; font-weight:700; display:block; margin-bottom:3px;">
              카드 끝 4자리 (예: 1234, 9*8*)
            </label>
            <input type="text" id="corp-card-last4" placeholder="예: 4321" style="width:100%;" />
          </div>
          <div>
            <label style="font-size:11px; font-weight:700; display:block; margin-bottom:3px;">
              표시 별칭 (자동완성/수정 가능)
            </label>
            <input type="text" id="corp-card-label" placeholder="예: 우리법인(4321)" style="width:100%;" />
          </div>
          <div>
            <button style="background:#0f766e; padding:7px 14px; font-weight:700;" onclick="registerNewCorpCard()">
              ➕ 법인카드 추가
            </button>
          </div>
        </div>
      </div>

      <div class="slip-box" style="margin-bottom:12px;">
        <div style="font-weight:800; color:#0f172a; margin-bottom:6px;">
          📋 현재 등록된 법인카드 목록 (<span id="corp-reg-count">0</span>장)
        </div>
        <div id="corp-registered-list" style="font-size:13px;">
          등록된 법인카드 목록을 불러오는 중...
        </div>
      </div>

      <div class="slip-box" style="background:#eff6ff; border-color:#93c5fd;">
        <div style="font-weight:800; color:#1e3a8a; margin-bottom:6px;">
          📂 법인카드사 이용대금명세서 / 승인내역 엑셀·CSV 바로 업로드
        </div>
        <div style="font-size:12px; color:#334155; margin-bottom:8px;">
          우리·국민·신한·하나·삼성·현대·비씨 법인카드 홈페이지나 홈택스에서 내려받은
          <code>.xls</code>, <code>.xlsx</code>, <code>.csv</code> 명세서를 업로드하면 법인카드 내역이 등록됩니다.
        </div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
          <select id="corp-upload-portal">
            <option value="HOMETAX">자동 감지 (홈택스 / 전체 카드사 통합)</option>
            <option value="WOORI_CARD">우리법인카드 명세서</option>
            <option value="KB_CARD">KB국민법인카드 명세서</option>
            <option value="SHINHAN_CARD">신한법인카드 명세서</option>
            <option value="SAMSUNG_CARD">삼성법인카드 명세서</option>
            <option value="HYUNDAI_CARD">현대법인카드 명세서</option>
            <option value="LOTTE_CARD">롯데법인카드 명세서</option>
            <option value="CREFIA">여신금융협회 통합매입내역</option>
          </select>
          <input type="file" id="corp-stmt-upload-input" accept=".csv,.txt,.xls,.xlsx,.html,.json"
                 style="display:none" onchange="uploadCorpStatementFile(this)" />
          <button onclick="document.getElementById('corp-stmt-upload-input').click()">
            📂 법인카드 명세서 파일 선택 · 업로드
          </button>
          <button class="secondary" onclick="importPhoneSms()">
            📱 USB 휴대폰에서 등록 법인카드 문자만 가져오기
          </button>
        </div>
      </div>
    </div>
  </div>

  <script>
    const API_HEADERS = {{
      'Content-Type': 'application/json',
      'X-API-Key': '{html.escape(settings.api_key)}'
    }};
    const MIN_TX_DATE = '{min_d}';
    const MAX_TX_DATE = '{max_d}';
    const DEFAULT_DOC_TITLE = document.title;
    let activeModalTxId = null;
    let activeModalPrintTitle = '';
    let activeModalData = null;
    let activeModalMode = 'SLIP';
    let targetStmtUploadTxId = null;

    function sanitizeFileToken(s) {{
      return String(s || '')
        .replace(/\\(주\\)|주식회사|\\(유\\)/g, '')
        .replace(/[\\\\/:*?"<>|\\r\\n\\t]+/g, '')
        .replace(/\\s+/g, '')
        .replace(/^[_.-]+|[_.-]+$/g, '') || '미상';
    }}

    function buildReceiptPrintTitle(d) {{
      if (d.print_title) return d.print_title;
      const digits = String(d.transacted_at || '').replace(/\\D/g, '');
      const ymd = digits.length >= 8 ? digits.slice(0, 8) : '00000000';
      const hm = digits.length >= 12 ? digits.slice(8, 12) : '0000';
      const cardTok = sanitizeFileToken(d.card_folder || d.card_label || d.card_last4);
      const merchTok = sanitizeFileToken(d.raw_merchant_name);
      const accTok = sanitizeFileToken(d.account_name);
      const amtTok = Math.abs(Number(d.total_amount || 0)) + '원';
      return '매출전표_' + ymd + '_' + hm + '_' + cardTok + '_'
        + merchTok + '_' + accTok + '_' + amtTok + '_TX' + d.tx_id;
    }}

    function escHtml(s) {{
      return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }}

    function formatBizRegNo(rawBiz, mpSeller, fallback) {{
      const raw = String(rawBiz || '').trim();
      const digits = raw.replace(/\\D/g, '');
      if (digits.length === 10) {{
        return digits.slice(0, 3) + '-' + digits.slice(3, 5) + '-' + digits.slice(5);
      }}
      if (raw) return escHtml(raw);
      if (mpSeller) {{
        return '미등록 (입점판매자: ' + escHtml(mpSeller) + ' · 홈택스 대사 대기)';
      }}
      return fallback || '미등록 (SMS/RCS·PG 전표)';
    }}

    function renderModalDocView() {{
      if (!activeModalData) return;
      const d = activeModalData;
      const slipEl = document.getElementById('modal-slip-content');
      const tabSlip = document.getElementById('tab-btn-slip');
      const tabStmt = document.getElementById('tab-btn-stmt');
      const fmt = (n) => Number(n || 0).toLocaleString('ko-KR') + '원';
      const bizNo = formatBizRegNo(d.biz_reg_no, d.marketplace_seller, '미등록 (SMS/RCS·PG 전표)');
      const appNo = escHtml(d.approval_no || ('TX-' + d.tx_id));
      const rowMemoEl = document.getElementById('memo-' + d.tx_id);
      const typedMemo = rowMemoEl && rowMemoEl.value ? rowMemoEl.value.trim() : '';
      const memo = escHtml(d.counterparty_info || typedMemo || '-');
      const arch = escHtml(d.archived_file_name
        ? ('by_card/' + d.card_folder + '/' + d.archived_file_name)
        : '실물 증빙 미첨부 (전자 전표)');
      const safeMerch = escHtml(d.raw_merchant_name);
      const safeCard = escHtml(d.card_label);
      const safeAcc = escHtml(d.account_code + ' ' + d.account_name);
      const safeTxAt = escHtml(d.transacted_at);

      if (activeModalMode === 'STATEMENT') {{
        tabSlip.className = 'secondary';
        tabStmt.className = '';
        const baseTitle = d.statement_print_title
          || buildReceiptPrintTitle(d).replace(/^매출전표_/, '거래명세서_');
        activeModalPrintTitle = baseTitle;
        document.title = activeModalPrintTitle;
        document.getElementById('modal-header-title').textContent =
          '📄 거래명세서 (#' + d.tx_id + ')';
        const mdStr = escHtml(String(d.transacted_at || '').slice(5, 10).replace('-', '/'));
        const rawItemStr = String(d.counterparty_info || typedMemo || '').trim();
        const itemVal = rawItemStr
          ? escHtml(rawItemStr)
          : (String(d.account_code) === '999'
              ? '<span style="color:#b45309;">품목 미확인 (미분류 — 아래 입력란에 거래품목 입력)</span>'
              : escHtml(d.raw_merchant_name + ' 구매 품목'));
        const stmtInputVal = escHtml(rawItemStr);
        const totAmt = Number(d.total_amount || 0);
        const supAmt = (d.vat_deduction_type === 'EXEMPT')
          ? totAmt
          : (Number(d.vat_amount || 0) !== 0
              ? Number(d.supply_value || 0)
              : Math.round(totAmt / 1.1));
        const vatAmt = totAmt - supAmt;
        let tbodyHtml = '';
        let totQty = 0;
        if (Array.isArray(d.line_items) && d.line_items.length > 0) {{
          d.line_items.forEach((li) => {{
            const liName = escHtml(li.item || li.name || rawItemStr || '품목 미확인');
            const liSeller = (d.marketplace_seller && li.seller)
              ? (' <span style="color:#64748b; font-size:11px; font-weight:400;">(판매자: ' +
                 escHtml(li.seller) + ')</span>')
              : '';
            const liQty = Math.max(1, parseInt(li.qty || 1, 10) || 1);
            const liTot = parseInt(li.price || li.amount || totAmt, 10);
            const liSup = d.vat_deduction_type === 'EXEMPT'
              ? liTot
              : Math.round(liTot / 1.1);
            const liVat = liTot - liSup;
            totQty += liQty;
            tbodyHtml += '<tr>' +
              '<td style="border:1px solid #cbd5e1; padding:6px; white-space:nowrap;">' + mdStr + '</td>' +
              '<td style="border:1px solid #cbd5e1; padding:6px;"><strong>' + liName + '</strong>' +
              liSeller + '</td>' +
              '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">' +
              liQty + '</td>' +
              '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">' +
              fmt(liSup) + '</td>' +
              '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">' +
              fmt(liVat) + '</td>' +
              '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">' +
              '<strong>' + fmt(liTot) + '</strong></td>' +
              '</tr>';
          }});
        }} else {{
          totQty = 1;
          tbodyHtml = '<tr>' +
            '<td style="border:1px solid #cbd5e1; padding:6px; white-space:nowrap;">' + mdStr + '</td>' +
            '<td style="border:1px solid #cbd5e1; padding:6px;"><strong>' + itemVal + '</strong></td>' +
            '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">1</td>' +
            '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">' +
            fmt(supAmt) + '</td>' +
            '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">' +
            fmt(vatAmt) + '</td>' +
            '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">' +
            '<strong>' + fmt(totAmt) + '</strong></td>' +
            '</tr>';
        }}
        slipEl.innerHTML =
          '<div class="slip-title" style="color:#1e3a8a;">[ 거 래 명 세 서 ]</div>' +
          '<div class="slip-row"><span>거래일시</span><strong>' + safeTxAt + '</strong></div>' +
          '<div class="slip-row"><span>전표/승인번호</span><span>#' + d.tx_id + ' / ' + appNo + '</span></div>' +
          '<div class="slip-divider"></div>' +
          '<div class="slip-row"><span>공급자 상호</span><strong>' + safeMerch + '</strong></div>' +
          '<div class="slip-row"><span>사업자번호</span><span>' + bizNo + '</span></div>' +
          '<div class="slip-row"><span>결제수단</span><strong>' + safeCard + '</strong></div>' +
          '<div class="slip-row"><span>회계계정</span><strong>' + safeAcc + '</strong></div>' +
          '<div class="slip-divider"></div>' +
          '<table style="width:100%; border-collapse:collapse; font-size:12px; margin:8px 0;">' +
          '<thead><tr style="background:#eff6ff;">' +
          '<th style="border:1px solid #cbd5e1; padding:5px 6px; white-space:nowrap;">월/일</th>' +
          '<th style="border:1px solid #cbd5e1; padding:5px 6px;">품목 및 규격</th>' +
          '<th style="border:1px solid #cbd5e1; padding:5px 6px; text-align:right; white-space:nowrap;">수량</th>' +
          '<th style="border:1px solid #cbd5e1; padding:5px 6px; text-align:right; white-space:nowrap;">' +
          'VAT 제외 가격</th>' +
          '<th style="border:1px solid #cbd5e1; padding:5px 6px; text-align:right; white-space:nowrap;">' +
          'VAT 가격</th>' +
          '<th style="border:1px solid #cbd5e1; padding:5px 6px; text-align:right; white-space:nowrap;">' +
          '합계 가격</th>' +
          '</tr></thead><tbody>' + tbodyHtml + '</tbody>' +
          '<tfoot><tr style="background:#f8fafc; font-weight:700;">' +
          '<td colspan="2" style="border:1px solid #cbd5e1; padding:6px; text-align:center;">합 계</td>' +
          '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">' +
          totQty + '</td>' +
          '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">' +
          fmt(supAmt) + '</td>' +
          '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; white-space:nowrap;">' +
          fmt(vatAmt) + '</td>' +
          '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; ' +
          'color:#1e3a8a; white-space:nowrap;">' +
          fmt(totAmt) + '</td>' +
          '</tr></tfoot></table>' +
          '<div class="slip-row"><span>VAT 제외 가격 (공급가액)</span><strong>' +
          fmt(supAmt) + '</strong></div>' +
          '<div class="slip-row"><span>VAT 가격 (부가세액)</span><strong>' +
          fmt(vatAmt) + ' (' + escHtml(d.vat_deduction_type) + ')</strong></div>' +
          '<div class="slip-row slip-total"><span>합계 가격 (총 결제금액)</span><span>' +
          fmt(totAmt) + '</span></div>' +
          '<div class="slip-divider"></div>' +
          '<div class="no-print" style="background:#f8fafc; border:1px solid #cbd5e1; ' +
          'border-radius:6px; padding:10px; margin:8px 0;">' +
          '<div style="font-weight:700; font-size:12px; margin-bottom:6px; color:#1e3a8a;">' +
          '✏️ 거래명세서 품목·규격 입력 및 전자 거래명세서 발급</div>' +
          '<div style="display:flex; gap:6px; flex-wrap:wrap;">' +
          '<input type="text" id="modal-stmt-item" value="' + stmtInputVal + '" ' +
          'style="flex:1; min-width:180px;" placeholder="품목명 또는 거래 내역 입력" />' +
          '<button onclick="saveStatementForActiveTx()">💾 품목 반영 · 명세서 저장</button>' +
          '</div></div>' +
          '<div class="slip-row"><span>보관경로</span><code style="font-size:11px;">' +
          arch + '</code></div>' +
          '<div class="slip-row no-print"><span>PDF 저장명</span>' +
          '<code style="font-size:11px; color:#1d4ed8;">' + escHtml(activeModalPrintTitle) + '.pdf</code></div>';
      }} else {{
        tabSlip.className = '';
        tabStmt.className = 'secondary';
        activeModalPrintTitle = buildReceiptPrintTitle(d);
        document.title = activeModalPrintTitle;
        document.getElementById('modal-header-title').textContent =
          '🧾 신용카드 매출전표 (#' + d.tx_id + ')';
        slipEl.innerHTML =
          '<div class="slip-title">[신용카드 매출전표]</div>' +
          '<div class="slip-row"><span>카드종류</span><strong>' + safeCard + '</strong></div>' +
          '<div class="slip-row"><span>거래일시</span><strong>' + safeTxAt + '</strong></div>' +
          '<div class="slip-row"><span>승인번호</span><span>' + appNo + '</span></div>' +
          '<div class="slip-divider"></div>' +
          '<div class="slip-row"><span>가맹점명</span><strong>' + safeMerch + '</strong></div>' +
          '<div class="slip-row"><span>사업자번호</span><span>' + bizNo + '</span></div>' +
          '<div class="slip-divider"></div>' +
          '<div class="slip-row"><span>공급가액</span><span>' + fmt(d.supply_value) + '</span></div>' +
          '<div class="slip-row"><span>부가세액(VAT)</span><span>' + fmt(d.vat_amount) +
          ' (' + escHtml(d.vat_deduction_type) + ')</span></div>' +
          '<div class="slip-row slip-total"><span>합계금액</span><span>' +
          fmt(d.total_amount) + '</span></div>' +
          '<div class="slip-divider"></div>' +
          '<div class="slip-row"><span>회계계정</span><strong>' + safeAcc + '</strong></div>' +
          '<div class="slip-row"><span>적요/품목</span><span>' + memo + '</span></div>' +
          '<div class="slip-row"><span>대사상태</span><span>' + escHtml(d.reconcile_status) + '</span></div>' +
          '<div class="slip-row"><span>보관경로</span><code style="font-size:11px;">' +
          arch + '</code></div>' +
          '<div class="slip-row no-print"><span>PDF 저장명</span>' +
          '<code style="font-size:11px; color:#1d4ed8;">' + escHtml(activeModalPrintTitle) + '.pdf</code></div>';
      }}
      refreshCtrlFIfOpen();
    }}

    function switchModalDocTab(mode) {{
      activeModalMode = mode || 'SLIP';
      renderModalDocView();
    }}

    async function openStatementModal(txId) {{
      await openReceiptModal(txId, 'STATEMENT');
    }}

    async function openReceiptModal(txId, initialMode) {{
      activeModalTxId = txId;
      activeModalMode = initialMode || 'SLIP';
      const backdrop = document.getElementById('receipt-modal-backdrop');
      const slipEl = document.getElementById('modal-slip-content');
      const imgEl = document.getElementById('modal-image-content');
      document.body.classList.add('modal-open');
      backdrop.classList.add('open');
      slipEl.innerHTML = '전표/명세서 정보를 불러오는 중...';
      imgEl.innerHTML = '';

      const res = await fetch('/api/v1/transactions/' + txId + '/receipt', {{ headers: API_HEADERS }});
      if (!res.ok) {{
        slipEl.innerHTML = '전표 정보를 찾을 수 없습니다.';
        return;
      }}
      const d = await res.json();
      activeModalData = d;
      if (!initialMode && String(d.archived_file_name || '').startsWith('거래명세서_')) {{
        activeModalMode = 'STATEMENT';
      }}
      renderModalDocView();

      if (d.has_receipt_file && d.is_renderable_image) {{
        imgEl.innerHTML =
          '<div style="margin-bottom:6px; font-weight:600; font-size:12px;">첨부된 실물 영수증/명세서 이미지</div>' +
          '<img src="' + d.receipt_file_url + '" alt="영수증 이미지" />' +
          '<div style="margin-top:6px;"><a href="' + d.receipt_file_url +
          '" target="_blank">원본 파일 새 창으로 열기</a></div>';
      }} else if (d.has_receipt_file) {{
        imgEl.innerHTML =
          '<div style="font-size:12px; color:#334155;">' +
          '첨부 증빙 파일이 보관되어 있습니다: <code>' + d.archived_file_name + '</code><br/>' +
          '<a href="' + d.receipt_file_url + '" target="_blank">첨부 증빙 파일 열기 / 다운로드</a>' +
          '</div>';
      }} else {{
        imgEl.innerHTML =
          '<div style="font-size:12px; color:#64748b;">' +
          '실물 영수증·거래명세서 파일이 아직 첨부되지 않았습니다.<br/>' +
          '하단 <b>[📷 영수증 첨부]</b> 또는 <b>[📎 거래명세서 첨부]</b> 버튼으로 파일을 등록할 수 있습니다.' +
          '</div>';
      }}
    }}

    async function saveStatementForActiveTx() {{
      if (!activeModalTxId) return;
      const inp = document.getElementById('modal-stmt-item');
      const itemVal = inp ? inp.value.trim() : '';
      const accSelect = document.getElementById('acc-' + activeModalTxId);
      const bodyObj = {{ item_name: itemVal }};
      if (accSelect && accSelect.value) {{
        bodyObj.account_code = accSelect.value;
      }}
      await fetch('/api/v1/transactions/' + activeModalTxId + '/statement', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify(bodyObj)
      }});
      await openReceiptModal(activeModalTxId, 'STATEMENT');
      window.location.reload();
    }}

    function triggerRowStatementUpload(txId) {{
      if (!txId) return;
      targetStmtUploadTxId = txId;
      const el = document.getElementById('row-stmt-file-input');
      el.value = '';
      el.click();
    }}

    function uploadRowStatementFile(inputEl) {{
      if (!inputEl.files || !inputEl.files[0] || !targetStmtUploadTxId) return;
      const txId = targetStmtUploadTxId;
      const file = inputEl.files[0];
      const memoEl = document.getElementById('memo-' + txId);
      const modalItemEl = document.getElementById('modal-stmt-item');
      const accEl = document.getElementById('acc-' + txId);
      const itemHint = (memoEl && memoEl.value.trim())
        || (modalItemEl && activeModalTxId === txId && modalItemEl.value.trim())
        || '';
      const reader = new FileReader();
      reader.onload = async function(ev) {{
        const dataUrl = String(ev.target.result || '');
        const b64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
        const bodyObj = {{
          file_name: file.name,
          file_content_b64: b64,
          item_name: itemHint
        }};
        if (accEl && accEl.value) {{
          bodyObj.account_code = accEl.value;
        }}
        await fetch('/api/v1/transactions/' + txId + '/statement', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify(bodyObj)
        }});
        window.location.reload();
      }};
      reader.readAsDataURL(file);
    }}

    function closeReceiptModal() {{
      document.body.classList.remove('modal-open');
      document.getElementById('receipt-modal-backdrop').classList.remove('open');
      activeModalPrintTitle = '';
      document.title = DEFAULT_DOC_TITLE;
      refreshCtrlFIfOpen();
    }}

    function printReceiptSlip() {{
      document.body.classList.add('modal-open');
      if (activeModalPrintTitle) {{
        document.title = activeModalPrintTitle;
      }}
      window.print();
    }}

    function onBackdropClick(ev) {{
      if (ev.target && ev.target.id === 'receipt-modal-backdrop') {{
        closeReceiptModal();
      }}
    }}

    function uploadReceiptForActiveTx(inputEl) {{
      if (!inputEl.files || !inputEl.files[0] || !activeModalTxId) return;
      const file = inputEl.files[0];
      const reader = new FileReader();
      reader.onload = async function(ev) {{
        const dataUrl = String(ev.target.result || '');
        const b64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
        await fetch('/api/v1/receipts/upload', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify({{
            file_name: file.name,
            file_content_b64: b64,
            source_channel: 'WEB_UPLOAD',
            tx_id: activeModalTxId
          }})
        }});
        await openReceiptModal(activeModalTxId);
        window.location.reload();
      }};
      reader.readAsDataURL(file);
    }}
    function applyFilters() {{
      const s = document.getElementById('start-date').value;
      const e = document.getElementById('end-date').value;
      const card = document.getElementById('card-filter').value;
      const accEl = document.getElementById('account-filter');
      const acc = accEl ? accEl.value : 'ALL';
      const q = '?start_date=' + encodeURIComponent(s)
              + '&end_date=' + encodeURIComponent(e)
              + '&card_last4=' + encodeURIComponent(card)
              + '&account_code=' + encodeURIComponent(acc);
      window.location.href = '/dashboard' + q;
    }}
    function filterByCard(cardCode) {{
      const s = document.getElementById('start-date').value;
      const e = document.getElementById('end-date').value;
      const accEl = document.getElementById('account-filter');
      const acc = accEl ? accEl.value : 'ALL';
      const q = '?start_date=' + encodeURIComponent(s)
              + '&end_date=' + encodeURIComponent(e)
              + '&card_last4=' + encodeURIComponent(cardCode)
              + '&account_code=' + encodeURIComponent(acc);
      window.location.href = '/dashboard' + q;
    }}
    function filterByAccount(accCode) {{
      const s = document.getElementById('start-date').value;
      const e = document.getElementById('end-date').value;
      const card = document.getElementById('card-filter').value;
      const q = '?start_date=' + encodeURIComponent(s)
              + '&end_date=' + encodeURIComponent(e)
              + '&card_last4=' + encodeURIComponent(card)
              + '&account_code=' + encodeURIComponent(accCode || 'ALL');
      window.location.href = '/dashboard' + q;
    }}
    let activeLedgerAccountFilter = '';
    function toggleAccountDetail(accCode) {{
      const detailRow = document.getElementById('acc-detail-' + accCode);
      const summaryRow = document.getElementById('acc-row-' + accCode);
      const arrowEl = document.getElementById('acc-arrow-' + accCode);
      const badgeEl = document.getElementById('acc-badge-' + accCode);
      if (!detailRow) return;
      const isHidden = detailRow.style.display === 'none' || !detailRow.style.display;
      if (isHidden) {{
        detailRow.style.display = 'table-row';
        if (summaryRow) summaryRow.classList.add('active');
        if (arrowEl) arrowEl.textContent = '▼';
        if (badgeEl) badgeEl.textContent = '목록 닫기';
        activeLedgerAccountFilter = String(accCode);
      }} else {{
        detailRow.style.display = 'none';
        if (summaryRow) summaryRow.classList.remove('active');
        if (arrowEl) arrowEl.textContent = '▶';
        if (badgeEl) badgeEl.textContent = '분류 목록 보기';
        if (activeLedgerAccountFilter === String(accCode)) {{
          activeLedgerAccountFilter = '';
        }}
      }}
      applyMainLedgerRowVisibility();
      refreshCtrlFIfOpen();
    }}
    function toggleAllAccountDetails(openAll) {{
      const detailRows = document.querySelectorAll('tr.acc-detail-row');
      detailRows.forEach((dr) => {{
        const code = String(dr.id || '').replace('acc-detail-', '');
        const summaryRow = document.getElementById('acc-row-' + code);
        const arrowEl = document.getElementById('acc-arrow-' + code);
        const badgeEl = document.getElementById('acc-badge-' + code);
        dr.style.display = openAll ? 'table-row' : 'none';
        if (summaryRow) {{
          if (openAll) summaryRow.classList.add('active');
          else summaryRow.classList.remove('active');
        }}
        if (arrowEl) arrowEl.textContent = openAll ? '▼' : '▶';
        if (badgeEl) badgeEl.textContent = openAll ? '목록 닫기' : '분류 목록 보기';
      }});
      if (!openAll) {{
        activeLedgerAccountFilter = '';
        applyMainLedgerRowVisibility();
      }}
      refreshCtrlFIfOpen();
    }}
    function focusAccountSummary(accCode) {{
      const detailRow = document.getElementById('acc-detail-' + accCode);
      const summaryRow = document.getElementById('acc-row-' + accCode);
      if (detailRow && (detailRow.style.display === 'none' || !detailRow.style.display)) {{
        toggleAccountDetail(accCode);
      }} else {{
        activeLedgerAccountFilter = String(accCode);
        applyMainLedgerRowVisibility();
      }}
      if (summaryRow && summaryRow.scrollIntoView) {{
        summaryRow.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }}
    }}
    function clearLedgerAccountFilter() {{
      activeLedgerAccountFilter = '';
      applyMainLedgerRowVisibility();
      const accEl = document.getElementById('account-filter');
      if (accEl && accEl.value !== 'ALL') {{
        accEl.value = 'ALL';
        applyFilters();
      }}
    }}
    async function saveAccountListTx(txId) {{
      const memoInput = document.getElementById('acc-list-memo-' + txId);
      const accSelect = document.getElementById('acc-list-code-' + txId);
      const bodyObj = {{}};
      if (memoInput) {{
        bodyObj.counterparty_info = memoInput.value;
      }}
      if (accSelect && accSelect.value) {{
        bodyObj.account_code = accSelect.value;
      }}
      await fetch('/api/v1/transactions/' + txId, {{
        method: 'PATCH',
        headers: API_HEADERS,
        body: JSON.stringify(bodyObj)
      }});
      window.location.reload();
    }}
    function selectQuarterRange(val) {{
      if (!val || !val.includes('|')) return;
      const parts = val.split('|');
      document.getElementById('start-date').value = parts[0];
      document.getElementById('end-date').value = parts[1];
      applyFilters();
    }}
    function shiftQuarter(delta) {{
      const curStart = document.getElementById('start-date').value || '';
      const pad = (n) => String(n).padStart(2, '0');
      let y = new Date().getFullYear();
      let m = new Date().getMonth() + 1;
      if (curStart.length >= 7) {{
        const py = parseInt(curStart.slice(0, 4), 10);
        const pm = parseInt(curStart.slice(5, 7), 10);
        if (!isNaN(py) && !isNaN(pm)) {{
          y = py;
          m = pm;
        }}
      }}
      const curQIndex = Math.floor((m - 1) / 3);
      const targetStart = new Date(y, (curQIndex + delta) * 3, 1);
      const ty = targetStart.getFullYear();
      const tsMonth = targetStart.getMonth() + 1;
      const teMonth = tsMonth + 2;
      const lastDay = new Date(ty, teMonth, 0).getDate();
      document.getElementById('start-date').value = ty + '-' + pad(tsMonth) + '-01';
      document.getElementById('end-date').value = ty + '-' + pad(teMonth) + '-' + pad(lastDay);
      applyFilters();
    }}
    function selectMonthRange(val) {{
      if (!val || !val.includes('|')) return;
      const parts = val.split('|');
      document.getElementById('start-date').value = parts[0];
      document.getElementById('end-date').value = parts[1];
      applyFilters();
    }}
    function shiftMonth(delta) {{
      const curStart = document.getElementById('start-date').value || '';
      const pad = (n) => String(n).padStart(2, '0');
      let y = new Date().getFullYear();
      let m = new Date().getMonth() + 1;
      if (curStart.length >= 7) {{
        const py = parseInt(curStart.slice(0, 4), 10);
        const pm = parseInt(curStart.slice(5, 7), 10);
        if (!isNaN(py) && !isNaN(pm)) {{
          y = py;
          m = pm;
        }}
      }}
      const target = new Date(y, m - 1 + delta, 1);
      const ty = target.getFullYear();
      const tm = target.getMonth() + 1;
      const lastDay = new Date(ty, tm, 0).getDate();
      document.getElementById('start-date').value = ty + '-' + pad(tm) + '-01';
      document.getElementById('end-date').value = ty + '-' + pad(tm) + '-' + pad(lastDay);
      applyFilters();
    }}
    function setQuickRange(mode) {{
      const now = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      const y = now.getFullYear();
      const m = now.getMonth() + 1;
      const lastDay = new Date(y, m, 0).getDate();
      let s = MIN_TX_DATE;
      let e = MAX_TX_DATE;
      if (mode === 'THIS_MONTH') {{
        s = y + '-' + pad(m) + '-01';
        e = y + '-' + pad(m) + '-' + pad(lastDay);
      }} else if (mode === 'LAST_MONTH') {{
        const prev = new Date(y, m - 2, 1);
        const py = prev.getFullYear();
        const pm = prev.getMonth() + 1;
        const pLast = new Date(py, pm, 0).getDate();
        s = py + '-' + pad(pm) + '-01';
        e = py + '-' + pad(pm) + '-' + pad(pLast);
      }} else if (mode === 'THIS_QUARTER') {{
        const q = Math.floor((m - 1) / 3) + 1;
        const qsMonth = (q - 1) * 3 + 1;
        const qeMonth = q * 3;
        const qLast = new Date(y, qeMonth, 0).getDate();
        s = y + '-' + pad(qsMonth) + '-01';
        e = y + '-' + pad(qeMonth) + '-' + pad(qLast);
      }} else if (mode === 'LAST_QUARTER') {{
        const curQ = Math.floor((m - 1) / 3);
        const prevQStart = new Date(y, (curQ - 1) * 3, 1);
        const pqy = prevQStart.getFullYear();
        const pqStartM = prevQStart.getMonth() + 1;
        const pqEndM = pqStartM + 2;
        const pqLast = new Date(pqy, pqEndM, 0).getDate();
        s = pqy + '-' + pad(pqStartM) + '-01';
        e = pqy + '-' + pad(pqEndM) + '-' + pad(pqLast);
      }} else if (mode === '3M') {{
        const d3 = new Date(y, m - 3, 1);
        s = d3.getFullYear() + '-' + pad(d3.getMonth() + 1) + '-01';
        e = y + '-' + pad(m) + '-' + pad(lastDay);
      }} else if (mode === 'THIS_YEAR') {{
        s = y + '-01-01';
        e = y + '-12-31';
      }}
      document.getElementById('start-date').value = s;
      document.getElementById('end-date').value = e;
      applyFilters();
    }}
    async function saveTxMemo(txId) {{
      const memoInput = document.getElementById('memo-' + txId);
      const accSelect = document.getElementById('acc-' + txId);
      if (!memoInput) return;
      const bodyObj = {{ counterparty_info: memoInput.value }};
      if (accSelect && accSelect.value) {{
        bodyObj.account_code = accSelect.value;
      }}
      await fetch('/api/v1/transactions/' + txId, {{
        method: 'PATCH',
        headers: API_HEADERS,
        body: JSON.stringify(bodyObj)
      }});
      window.location.reload();
    }}
    async function importPhoneSms() {{
      await fetch('/api/v1/system/import-phone-sms', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{ limit: 1500 }})
      }});
      window.location.href = '/dashboard?year_month=ALL';
    }}
    async function reapplyRules() {{
      await fetch('/api/v1/system/reapply-rules', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{}})
      }});
      window.location.reload();
    }}
    async function generateElectronicSlips() {{
      await fetch('/api/v1/scraper/generate-slips', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{ force_regenerate: false }})
      }});
      window.location.reload();
    }}
    function uploadReceiptFile(inputEl) {{
      if (!inputEl.files || !inputEl.files[0]) return;
      const file = inputEl.files[0];
      const reader = new FileReader();
      reader.onload = async function(ev) {{
        const dataUrl = String(ev.target.result || '');
        const b64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
        await fetch('/api/v1/receipts/upload', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify({{
            file_name: file.name,
            file_content_b64: b64,
            source_channel: 'WEB_UPLOAD'
          }})
        }});
        window.location.reload();
      }};
      reader.readAsDataURL(file);
    }}
    function uploadCsvFile(inputEl) {{
      if (!inputEl.files || !inputEl.files[0]) return;
      const file = inputEl.files[0];
      const fName = (file.name || '').toLowerCase();
      let portal = 'HOMETAX';
      if (fName.includes('coupang') || fName.includes('쿠팡')) portal = 'COUPANG';
      else if (fName.includes('naver') || fName.includes('네이버')) portal = 'NAVERPAY';
      const reader = new FileReader();
      reader.onload = async function(ev) {{
        const dataUrl = String(ev.target.result || '');
        const b64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
        await fetch('/api/v1/scraper/ingest', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify({{
            portal: portal,
            content_b64: b64,
            auto_generate_slips: true
          }})
        }});
        window.location.href = '/dashboard?year_month=ALL';
      }};
      reader.readAsDataURL(file);
    }}
    async function exportTaxPack(yearMonth) {{
      await fetch('/api/v1/exports/tax-pack', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{ year_month: yearMonth, force_reexport: true }})
      }});
      window.location.reload();
    }}
    async function backupToNas() {{
      await fetch('/api/v1/system/backup-nas', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{}})
      }});
      window.location.reload();
    }}
    async function seedDemoData() {{
      await fetch('/api/v1/system/seed-demo', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{ year_month: '{summary['year_month']}' }})
      }});
      window.location.reload();
    }}
    async function resetTestLedger() {{
      await fetch('/api/v1/system/reset-test', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{}})
      }});
      window.location.reload();
    }}
    function openCoupangModal() {{
      document.getElementById('coupang-modal-backdrop').classList.add('open');
    }}
    function closeCoupangModal() {{
      document.getElementById('coupang-modal-backdrop').classList.remove('open');
    }}
    function onCoupangBackdropClick(ev) {{
      if (ev.target && ev.target.id === 'coupang-modal-backdrop') {{
        closeCoupangModal();
      }}
    }}
    async function runCoupangAutoCrawl() {{
      const btn = document.getElementById('btn-coupang-auto-crawl');
      const st = document.getElementById('coupang-crawl-status');
      btn.disabled = true;
      st.style.color = '#1e40af';
      st.textContent =
        '⏳ 전용 브라우저가 실행되었습니다. (로그인 창이 뜨면 1회 로그인해 주세요. 이후 자동 수집 후 창이 닫힙니다...)';
      try {{
        const res = await fetch('/api/v1/scraper/coupang-auto-crawl', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify({{ max_wait_sec: 180 }})
        }});
        const data = await res.json();
        if (data.status === 'crawled') {{
          st.style.color = '#15803d';
          st.textContent =
            `✅ 쿠팡 주문내역 ${{data.collected_orders_count || 0}}건 수집 · `
            + `카드전표 ${{data.matched_orders || 0}}건 매칭 및 전자 거래명세서 자동 작성 완료!`;
          setTimeout(() => {{ window.location.href = '/dashboard?year_month=ALL'; }}, 1200);
        }} else {{
          st.style.color = '#b91c1c';
          st.textContent = `⚠️ ${{data.message || '수집이 중단되었습니다.'}}`;
          btn.disabled = false;
        }}
      }} catch (err) {{
        st.style.color = '#b91c1c';
        st.textContent = '⚠️ 자동 수집 요청 중 오류가 발생했습니다.';
        btn.disabled = false;
      }}
    }}
    async function copyCoupangExtractorScript() {{
      const res = await fetch('/api/v1/scraper/coupang-script');
      const data = await res.json();
      await navigator.clipboard.writeText(data.script);
      const st = document.getElementById('coupang-copy-status');
      st.textContent =
        '✅ 수집 스크립트 복사 완료! 쿠팡 주문목록 탭에서 F12 -> Console에 붙여넣고 Enter를 누르세요.';
    }}
    async function submitCoupangPaste() {{
      const txt = (document.getElementById('coupang-paste-input').value || '').trim();
      if (!txt) return;
      await fetch('/api/v1/scraper/ingest', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{
          portal: 'COUPANG',
          raw_content: txt,
          auto_generate_slips: true
        }})
      }});
      window.location.href = '/dashboard?year_month=ALL';
    }}
    const EMAIL_HOST_MAP = {{
      NAVER: {{ host: 'imap.naver.com', port: 993 }},
      GMAIL: {{ host: 'imap.gmail.com', port: 993 }},
      DAUM_KAKAO: {{ host: 'imap.daum.net', port: 993 }},
      WORKSMOBILE: {{ host: 'imap.worksmobile.com', port: 993 }},
      OUTLOOK: {{ host: 'outlook.office365.com', port: 993 }},
      CUSTOM: {{ host: '', port: 993 }}
    }};
    let cachedEmailAccounts = [];
    function onEmailProviderChange() {{
      const prov = document.getElementById('email-provider').value;
      const preset = EMAIL_HOST_MAP[prov];
      if (preset && preset.host) {{
        document.getElementById('email-imap-host').value = preset.host;
        document.getElementById('email-imap-port').value = preset.port;
      }}
    }}
    function resetEmailAccountForm() {{
      document.getElementById('email-editing-id').value = '';
      document.getElementById('email-form-title').textContent =
        '➕ 새 이메일 계정 추가 (개수 제한 없음)';
      document.getElementById('email-label').value = '';
      document.getElementById('email-provider').value = 'NAVER';
      document.getElementById('email-imap-host').value = 'imap.naver.com';
      document.getElementById('email-imap-port').value = 993;
      document.getElementById('email-address').value = '';
      document.getElementById('email-app-password').value = '';
    }}
    function editEmailAccountByIdx(idx) {{
      const acc = cachedEmailAccounts[idx];
      if (!acc) return;
      document.getElementById('email-editing-id').value = acc.account_id || '';
      document.getElementById('email-form-title').textContent =
        '✏️ 이메일 계정 수정 (' + (acc.email_address || '') + ')';
      document.getElementById('email-label').value = acc.label || '';
      document.getElementById('email-provider').value = acc.provider || 'CUSTOM';
      document.getElementById('email-imap-host').value = acc.imap_host || '';
      document.getElementById('email-imap-port').value = acc.imap_port || 993;
      document.getElementById('email-address').value = acc.email_address || '';
      document.getElementById('email-app-password').value = acc.app_password || '';
    }}
    function escHtml(val) {{
      return String(val || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }}
    function scanEmailAccountByIdx(idx, btnEl) {{
      const acc = cachedEmailAccounts[idx];
      if (!acc) return;
      const stLine = document.getElementById('email-acc-status-' + idx);
      if (stLine) {{
        stLine.style.color = '#1e40af';
        stLine.textContent = '⏳ IMAP 메일함 접속 및 최근 주문·결제 메일 정밀 스캔 중...';
      }}
      triggerEmailSyncNow(false, acc.account_id, btnEl);
    }}
    async function toggleEmailAccountByIdx(idx) {{
      const acc = cachedEmailAccounts[idx];
      if (!acc) return;
      const nextEnabled = !(acc.enabled !== false);
      await toggleEmailAccount(acc.account_id, nextEnabled);
    }}
    async function deleteEmailAccountByIdx(idx) {{
      const acc = cachedEmailAccounts[idx];
      if (!acc) return;
      await deleteEmailAccount(acc.account_id);
    }}
    function renderEmailAccountsList(cfg) {{
      const box = document.getElementById('email-accounts-list');
      cachedEmailAccounts = Array.isArray(cfg.accounts) ? cfg.accounts : [];
      if (cachedEmailAccounts.length === 0) {{
        box.innerHTML =
          '<div style="color:#64748b; font-size:12px; padding:6px;">' +
          '아직 등록된 이메일 계정이 없습니다. 아래 폼에서 네이버·Gmail·회사메일 등을 추가해 주세요.' +
          '</div>';
        return;
      }}
      let htmlRows = '';
      cachedEmailAccounts.forEach((acc, idx) => {{
        const isOn = acc.enabled !== false;
        const badgeColor = isOn ? '#dcfce7' : '#f1f5f9';
        const badgeTxtColor = isOn ? '#166534' : '#64748b';
        const badgeLabel = isOn ? '감시중 ON' : '일시정지 OFF';
        const stInfo = acc.last_sync_at
          ? (acc.last_status === 'error'
              ? '⚠️ ' + escHtml(acc.last_message || '오류')
              : '✅ ' + escHtml(acc.last_sync_at) + ' · ' + escHtml(acc.last_message || '완료'))
          : '대기중';
        htmlRows +=
          '<div style="display:flex; justify-content:space-between; align-items:center; ' +
          'gap:8px; flex-wrap:wrap; padding:6px 4px; border-bottom:1px solid #f1f5f9;">' +
          '<div style="font-size:12px;">' +
          '<span style="background:' + badgeColor + '; color:' + badgeTxtColor + '; ' +
          'padding:2px 6px; border-radius:4px; font-weight:700; font-size:11px; margin-right:6px;">' +
          badgeLabel + '</span>' +
          '<strong>' + escHtml(acc.label || acc.provider) + '</strong> · ' +
          '<code>' + escHtml(acc.email_address) + '</code> ' +
          '<span style="color:#64748b; font-size:11px;">(' +
          escHtml(acc.imap_host) + ':' + escHtml(acc.imap_port) + ')</span>' +
          '<div id="email-acc-status-' + idx + '" style="font-size:11px; color:#475569; margin-top:2px;">' +
          stInfo + '</div>' +
          '</div>' +
          '<div style="display:flex; gap:4px; flex-wrap:wrap;">' +
          '<button class="secondary" style="padding:3px 7px; font-size:11px;" ' +
          'onclick="scanEmailAccountByIdx(' + idx + ', this)">스캔</button>' +
          '<button class="secondary" style="padding:3px 7px; font-size:11px;" ' +
          'onclick="toggleEmailAccountByIdx(' + idx + ')">' +
          (isOn ? '일시정지' : '켜기') + '</button>' +
          '<button class="secondary" style="padding:3px 7px; font-size:11px;" ' +
          'onclick="editEmailAccountByIdx(' + idx + ')">수정</button>' +
          '<button class="danger" style="padding:3px 7px; font-size:11px;" ' +
          'onclick="deleteEmailAccountByIdx(' + idx + ')">삭제</button>' +
          '</div></div>';
      }});
      box.innerHTML = htmlRows;
    }}
    let stmtFilterOnly = false;
    let ctrlFRowFilterOnly = false;
    let activeCtrlFQuery = '';
    let activeCtrlFRegex = null;
    let ctrlFHitElements = [];
    let ctrlFActiveIndex = -1;

    function getLedgerRowSearchText(tr) {{
      let txt = tr.textContent || '';
      const inputs = tr.querySelectorAll('input[type="text"]');
      inputs.forEach((inp) => {{
        if (inp.value) txt += ' ' + inp.value;
      }});
      return txt;
    }}

    function rowMatchesCtrlF(tr, regex) {{
      if (!regex) return true;
      regex.lastIndex = 0;
      return regex.test(getLedgerRowSearchText(tr));
    }}

    function applyMainLedgerRowVisibility() {{
      const tbody = document.getElementById('main-ledger-tbody');
      const pendingTbody = document.getElementById('pending-ledger-tbody');
      const badgeEl = document.getElementById('ledger-account-filter-badge');
      const clearBtn = document.getElementById('btn-clear-acc-filter');
      const searchBadgeEl = document.getElementById('ledger-search-filter-badge');
      const clearSearchBtn = document.getElementById('btn-clear-search-filter');
      let matchAccCount = 0;
      let matchSearchCount = 0;
      let matchSearchAmt = 0;
      if (tbody) {{
        const rows = tbody.querySelectorAll('tr[data-has-stmt]');
        rows.forEach((tr) => {{
          const hasStmt = tr.getAttribute('data-has-stmt') === '1';
          const rowAcc = tr.getAttribute('data-account-code') || '';
          const rowAmt = parseInt(tr.getAttribute('data-total-amount') || '0', 10) || 0;
          const stmtOk = !stmtFilterOnly || hasStmt;
          const accOk = !activeLedgerAccountFilter || rowAcc === activeLedgerAccountFilter;
          const hitsQuery = !activeCtrlFRegex || rowMatchesCtrlF(tr, activeCtrlFRegex);
          if (stmtOk && accOk && activeCtrlFRegex && hitsQuery) {{
            matchSearchCount += 1;
            matchSearchAmt += rowAmt;
          }}
          const searchOk = !ctrlFRowFilterOnly || hitsQuery;
          const visible = stmtOk && accOk && searchOk;
          tr.style.display = visible ? '' : 'none';
          if (accOk) matchAccCount += 1;
        }});
      }}
      if (pendingTbody) {{
        const pRows = pendingTbody.querySelectorAll('tr[data-tx-id]');
        pRows.forEach((tr) => {{
          const pOk = !ctrlFRowFilterOnly
            || !activeCtrlFRegex
            || rowMatchesCtrlF(tr, activeCtrlFRegex);
          tr.style.display = pOk ? '' : 'none';
        }});
      }}
      if (badgeEl && clearBtn) {{
        if (activeLedgerAccountFilter) {{
          badgeEl.style.display = 'inline-block';
          badgeEl.textContent = '📂 계정 필터: ' + activeLedgerAccountFilter + ' (' + matchAccCount + '건)';
          clearBtn.style.display = 'inline-block';
        }} else {{
          badgeEl.style.display = 'none';
          badgeEl.textContent = '';
          clearBtn.style.display = 'none';
        }}
      }}
      if (searchBadgeEl && clearSearchBtn) {{
        if (ctrlFRowFilterOnly && activeCtrlFQuery) {{
          const fmtAmt = matchSearchAmt.toLocaleString('ko-KR') + '원';
          searchBadgeEl.style.display = 'inline-block';
          searchBadgeEl.textContent =
            '🔎 검색 필터: "' + activeCtrlFQuery + '" (' + matchSearchCount + '건 · ' + fmtAmt + ')';
          clearSearchBtn.style.display = 'inline-block';
        }} else {{
          searchBadgeEl.style.display = 'none';
          searchBadgeEl.textContent = '';
          clearSearchBtn.style.display = 'none';
        }}
      }}
      if (typeof syncStickySectionOffsets === 'function') {{
        syncStickySectionOffsets();
      }}
      return {{ matchSearchCount, matchSearchAmt }};
    }}
    function toggleStatementRowsFilter(stmtCnt) {{
      stmtFilterOnly = !stmtFilterOnly;
      applyMainLedgerRowVisibility();
      refreshCtrlFIfOpen();
      const btn = document.getElementById('btn-filter-stmt-only');
      if (btn) {{
        btn.className = stmtFilterOnly ? '' : 'secondary';
        btn.textContent = stmtFilterOnly
          ? '📋 전체 전표 다시 보기'
          : ('📄 거래명세서 발급 전표만 보기 (' + (stmtCnt || 0) + '건)');
      }}
    }}
    let cachedEmailMatchedItems = [];
    async function openMatchedStatementByIdx(idx) {{
      const item = cachedEmailMatchedItems[idx];
      if (!item || !item.tx_id) return;
      closeEmailOrderModal();
      await openStatementModal(item.tx_id);
    }}
    function renderEmailMatchedList(cfg) {{
      const box = document.getElementById('email-matched-list');
      const cntEl = document.getElementById('email-matched-count');
      cachedEmailMatchedItems = Array.isArray(cfg.recent_matched_items)
        ? cfg.recent_matched_items
        : [];
      if (cntEl) {{
        cntEl.textContent = String(cachedEmailMatchedItems.length);
      }}
      if (!box) return;
      if (cachedEmailMatchedItems.length === 0) {{
        box.innerHTML =
          '<div style="color:#64748b; font-size:12px; padding:8px;">' +
          '아직 자동 작성된 거래명세서가 없습니다. 위 <b>[🚀 연결된 전체 메일함 즉시 통합 스캔]</b>을 실행해 보세요.' +
          '</div>';
        return;
      }}
      let rows =
        '<table style="width:100%; border-collapse:collapse; font-size:12px;">' +
        '<thead><tr style="background:#f0fdf4;">' +
        '<th style="padding:5px 6px; white-space:nowrap;">전표</th>' +
        '<th style="padding:5px 6px; white-space:nowrap;">거래일시</th>' +
        '<th style="padding:5px 6px; white-space:nowrap;">가맹점</th>' +
        '<th style="padding:5px 6px;">추출 품목명 (거래명세서 내역)</th>' +
        '<th style="padding:5px 6px; text-align:right; white-space:nowrap;">결제금액</th>' +
        '<th style="padding:5px 6px; text-align:center; white-space:nowrap;">확인</th>' +
        '</tr></thead><tbody>';
      cachedEmailMatchedItems.forEach((it, idx) => {{
        const amtFmt = Number(it.total_amount || 0).toLocaleString('ko-KR') + '원';
        const dtShort = escHtml(String(it.transacted_at || '').slice(0, 16));
        const liCnt = Number(it.line_items_count || 1);
        const liBadge = liCnt > 1
          ? (' <span style="background:#dbeafe; color:#1e40af; padding:1px 5px; ' +
             'border-radius:4px; font-size:10px; font-weight:700;">상세 ' + liCnt + '품목</span>')
          : '';
        const newBadge = it.is_created
          ? (' <span style="background:#fef3c7; color:#b45309; padding:1px 5px; ' +
             'border-radius:4px; font-size:10px; font-weight:700;">⚡실시간 자동전표</span>')
          : '';
        const bizSub = it.biz_reg_no
          ? ('<div style="font-size:10px; color:#15803d; font-weight:600;">' +
             escHtml(it.biz_reg_no) + '</div>')
          : (it.marketplace_seller
              ? ('<div style="font-size:10px; color:#b45309;">입점: ' +
                 escHtml(it.marketplace_seller) + '</div>')
              : '');
        rows +=
          '<tr>' +
          '<td style="padding:5px 6px; white-space:nowrap;"><strong>#' + it.tx_id + '</strong></td>' +
          '<td style="padding:5px 6px; white-space:nowrap;">' + dtShort + '</td>' +
          '<td style="padding:5px 6px; white-space:nowrap;">' +
          escHtml(it.raw_merchant_name) + bizSub + '</td>' +
          '<td style="padding:5px 6px;"><strong>' + escHtml(it.item_name) + '</strong>' +
          liBadge + newBadge + '</td>' +
          '<td style="padding:5px 6px; text-align:right; white-space:nowrap;"><strong>' +
          amtFmt + '</strong></td>' +
          '<td style="padding:5px 6px; text-align:center; white-space:nowrap;">' +
          '<button style="padding:3px 8px; font-size:11px; background:#0f766e;" ' +
          'onclick="openMatchedStatementByIdx(' + idx + ')">📄 명세서 보기</button></td>' +
          '</tr>';
      }});
      rows += '</tbody></table>';
      box.innerHTML = rows;
    }}
    function setEmailLookbackPreset(days) {{
      const el = document.getElementById('email-lookback-days');
      if (el) el.value = String(days);
    }}
    async function refreshEmailWatcherState() {{
      const res = await fetch('/api/v1/scraper/email-config');
      if (!res.ok) return null;
      const cfg = await res.json();
      renderEmailAccountsList(cfg);
      renderEmailMatchedList(cfg);
      document.getElementById('email-lookback-days').value = cfg.lookback_days || 365;
      document.getElementById('email-poll-interval').value = cfg.poll_interval_sec || 120;
      document.getElementById('email-auto-poll').checked = !!cfg.auto_poll_enabled;
      const st = document.getElementById('email-sync-status');
      if (cfg.last_sync_at) {{
        st.textContent = `최근 통합 동기화 (${{cfg.last_sync_at}}): ${{cfg.last_sync_message || ''}}`;
      }} else {{
        st.textContent = cfg.last_sync_message || '';
      }}
      return cfg;
    }}
    async function openEmailOrderModal() {{
      document.getElementById('email-modal-backdrop').classList.add('open');
      resetEmailAccountForm();
      try {{
        await refreshEmailWatcherState();
      }} catch (e) {{}}
    }}
    function closeEmailOrderModal() {{
      document.getElementById('email-modal-backdrop').classList.remove('open');
    }}
    function onEmailBackdropClick(ev) {{
      if (ev.target && ev.target.id === 'email-modal-backdrop') {{
        closeEmailOrderModal();
      }}
    }}
    async function toggleEmailAccount(accountId, nextEnabled) {{
      const res = await fetch('/api/v1/scraper/email-accounts/' + encodeURIComponent(accountId), {{
        method: 'PATCH',
        headers: API_HEADERS,
        body: JSON.stringify({{ enabled: !!nextEnabled }})
      }});
      if (res.ok) {{
        const cfg = await res.json();
        renderEmailAccountsList(cfg);
      }}
    }}
    async function deleteEmailAccount(accountId) {{
      const res = await fetch('/api/v1/scraper/email-accounts/' + encodeURIComponent(accountId), {{
        method: 'DELETE',
        headers: API_HEADERS
      }});
      if (res.ok) {{
        const cfg = await res.json();
        renderEmailAccountsList(cfg);
      }}
    }}
    async function saveGlobalEmailPollSettings() {{
      const st = document.getElementById('email-sync-status');
      const bodyObj = {{
        lookback_days: parseInt(document.getElementById('email-lookback-days').value || '365', 10),
        poll_interval_sec: parseInt(document.getElementById('email-poll-interval').value || '120', 10),
        auto_poll_enabled: document.getElementById('email-auto-poll').checked
      }};
      const res = await fetch('/api/v1/scraper/email-config', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify(bodyObj)
      }});
      if (res.ok) {{
        st.style.color = '#15803d';
        st.textContent = '✅ 실시간 자동 감지 주기 및 스캔 기간 설정이 저장되었습니다.';
      }}
    }}
    async function saveAndSyncEmailWatcher(syncNow) {{
      const st = document.getElementById('email-sync-status');
      const btn = document.getElementById('btn-email-save-sync');
      const addr = document.getElementById('email-address').value.trim();
      if (!addr) {{
        st.style.color = '#b91c1c';
        st.textContent = '⚠️ 추가할 이메일 주소를 입력해 주세요.';
        return;
      }}
      btn.disabled = true;
      st.style.color = '#1e40af';
      st.textContent = syncNow
        ? '⏳ 계정을 저장하고 연결된 메일함을 스캔하여 거래명세서를 작성 중입니다...'
        : '⏳ 이메일 계정을 저장하는 중...';
      try {{
        const bodyObj = {{
          account_id: document.getElementById('email-editing-id').value.trim() || undefined,
          label: document.getElementById('email-label').value.trim(),
          provider: document.getElementById('email-provider').value,
          imap_host: document.getElementById('email-imap-host').value.trim(),
          imap_port: parseInt(document.getElementById('email-imap-port').value || '993', 10),
          email_address: addr,
          app_password: document.getElementById('email-app-password').value,
          lookback_days: parseInt(document.getElementById('email-lookback-days').value || '365', 10),
          poll_interval_sec: parseInt(document.getElementById('email-poll-interval').value || '120', 10),
          auto_poll_enabled: document.getElementById('email-auto-poll').checked,
          sync_now: !!syncNow
        }};
        const res = await fetch('/api/v1/scraper/email-config', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify(bodyObj)
        }});
        const data = await res.json();
        btn.disabled = false;
        renderEmailAccountsList(data);
        renderEmailMatchedList(data);
        resetEmailAccountForm();
        if (syncNow && data.sync_result) {{
          const sr = data.sync_result;
          if (sr.status === 'synced') {{
            st.style.color = '#15803d';
            st.textContent = `✅ ${{sr.message}}`;
            if (sr.matched_count > 0) {{
              setTimeout(() => {{ window.location.reload(); }}, 1100);
            }}
          }} else {{
            st.style.color = '#b91c1c';
            st.textContent = `⚠️ ${{sr.message || '동기화 실패'}}`;
          }}
        }} else {{
          st.style.color = '#15803d';
          st.textContent =
            `✅ 이메일 계정이 저장되었습니다 (현재 총 ${{data.total_accounts_count || 1}}개 연결됨).`;
        }}
      }} catch (err) {{
        btn.disabled = false;
        st.style.color = '#b91c1c';
        st.textContent = '⚠️ 이메일 계정 저장 또는 동기화 중 오류가 발생했습니다.';
      }}
    }}
    async function triggerEmailSyncNow(forceRescan, accountId, btnEl) {{
      const st = document.getElementById('email-sync-status');
      const prevTxt = btnEl ? btnEl.textContent : '';
      const lbDays = parseInt(document.getElementById('email-lookback-days').value || '365', 10) || 365;
      if (btnEl) {{
        btnEl.disabled = true;
        btnEl.textContent = '⏳ 통합 스크롤 중...';
      }}
      st.style.color = '#1e40af';
      st.textContent = accountId
        ? `⏳ 선택한 이메일 계정의 최근 ${{lbDays}}일 주문·결제 및 토스페이먼츠 메일을 통합 스크롤 중입니다...`
        : `⏳ 연결된 모든 활성 계정의 최근 ${{lbDays}}일 주문·결제 및 토스페이먼츠 메일을 통합 스크롤 중입니다...`;
      try {{
        const res = await fetch('/api/v1/scraper/email-sync', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify({{
            force_rescan: !!forceRescan,
            account_id: accountId || '',
            lookback_days: lbDays,
            include_toss_scroll: true
          }})
        }});
        const sr = await res.json();
        await refreshEmailWatcherState();
        if (sr.status === 'synced') {{
          const hasNew = Number(sr.created_count || 0) > 0 || Number(sr.newly_stated_count || 0) > 0;
          st.style.color = '#15803d';
          if (hasNew) {{
            st.textContent =
              `✅ ${{sr.message}} — 원장 테이블에 즉시 반영합니다...`;
            setTimeout(() => {{ window.location.reload(); }}, 1200);
          }} else {{
            st.textContent =
              `✅ ${{sr.message}} (추가 미반영 주문 없음 · 최신 동기화 상태)`;
          }}
        }} else {{
          st.style.color = '#b91c1c';
          st.textContent = `⚠️ ${{sr.message || '동기화 실패'}}`;
        }}
      }} catch (err) {{
        st.style.color = '#b91c1c';
        st.textContent = '⚠️ 이메일 스캔 요청 중 네트워크 오류가 발생했습니다.';
      }} finally {{
        if (btnEl) {{
          btnEl.disabled = false;
          btnEl.textContent = prevTxt;
        }}
      }}
    }}
    async function openTossScrollModal() {{
      await openEmailOrderModal();
      const pnl = document.getElementById('toss-scroll-panel');
      if (pnl) {{
        pnl.open = true;
        if (pnl.scrollIntoView) {{
          pnl.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
        }}
      }}
    }}
    async function triggerTossEmailScroll(btnEl) {{
      const st = document.getElementById('toss-scroll-status');
      const resBox = document.getElementById('toss-scroll-results');
      const prevTxt = btnEl ? btnEl.textContent : '';
      if (btnEl) {{
        btnEl.disabled = true;
        btnEl.textContent = '⏳ 토스 메일 연속 스크롤링 중...';
      }}
      st.style.color = '#0369a1';
      st.textContent =
        '⏳ 메일함(전체보관함·받은편지함)에서 토스페이먼츠 결제 메일 및 매출전표 카드번호를 스크롤 탐색 중입니다...';
      try {{
        const sDt = (document.getElementById('toss-scroll-start').value || '').trim();
        const eDt = (document.getElementById('toss-scroll-end').value || '').trim();
        const res = await fetch('/api/v1/scraper/email-scroll-toss', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify({{
            start_date: sDt || undefined,
            end_date: eDt || undefined,
            page_size: 50,
            max_pages: 50,
            verify_receipt_slip: true,
            auto_create_corp_tx: true
          }})
        }});
        const data = await res.json();
        await refreshEmailWatcherState();
        if (data.status === 'scrolled') {{
          st.style.color = '#15803d';
          st.textContent = `✅ ${{data.message}}`;
          const items = Array.isArray(data.items) ? data.items : [];
          if (resBox) {{
            if (items.length === 0) {{
              resBox.style.display = 'block';
              resBox.innerHTML =
                '<div style="color:#64748b; font-size:12px; padding:6px;">' +
                '해당 기간 내 법인카드로 결제된 토스페이먼츠 구매내역이 없습니다 ' +
                `(탐색 ${{data.parsed_toss_count || 0}}통 · 개인카드 ${{data.skipped_personal_count || 0}}건 제외).` +
                '</div>';
            }} else {{
              let tbl =
                '<table style="width:100%; border-collapse:collapse; font-size:12px;">' +
                '<thead><tr style="background:#e0f2fe;">' +
                '<th style="padding:5px 6px;">전표</th>' +
                '<th style="padding:5px 6px;">결제일시</th>' +
                '<th style="padding:5px 6px;">법인카드</th>' +
                '<th style="padding:5px 6px;">판매가맹점</th>' +
                '<th style="padding:5px 6px;">구매상품</th>' +
                '<th style="padding:5px 6px; text-align:right;">결제금액</th>' +
                '<th style="padding:5px 6px; text-align:center;">증빙</th>' +
                '</tr></thead><tbody>';
              items.forEach((it) => {{
                const amtFmt = Number(it.total_amount || 0).toLocaleString('ko-KR') + '원';
                const bdgSty = 'padding:1px 5px;border-radius:4px;font-size:10px;font-weight:700;';
                const actBadge = it.is_created
                  ? `<span style="background:#dbeafe;color:#1e40af;${{bdgSty}}">신규생성</span>`
                  : `<span style="background:#dcfce7;color:#166534;${{bdgSty}}">전표매칭</span>`;
                const bizInfo = it.biz_reg_no
                  ? `<div style="font-size:10px;color:#15803d;">${{escHtml(it.biz_reg_no)}}</div>`
                  : '';
                const dtTxt = escHtml(String(it.transacted_at || '').slice(0, 16));
                tbl +=
                  '<tr>' +
                  `<td style="padding:5px 6px;white-space:nowrap;"><strong>#${{it.tx_id}}</strong> ${{actBadge}}</td>` +
                  `<td style="padding:5px 6px;white-space:nowrap;">${{dtTxt}}</td>` +
                  `<td style="padding:5px 6px;white-space:nowrap;">${{escHtml(it.card_label || it.card_last4)}}</td>` +
                  `<td style="padding:5px 6px;">${{escHtml(it.raw_merchant_name)}}${{bizInfo}}</td>` +
                  `<td style="padding:5px 6px;"><strong>${{escHtml(it.item_name)}}</strong></td>` +
                  `<td style="padding:5px 6px;text-align:right;white-space:nowrap;"><strong>${{amtFmt}}</strong></td>` +
                  `<td style="padding:5px 6px;text-align:center;white-space:nowrap;">` +
                  `<button style="padding:3px 8px;font-size:11px;background:#0f766e;" ` +
                  `onclick="closeEmailOrderModal(); openStatementModal(${{it.tx_id}})">📄 명세서</button></td>` +
                  '</tr>';
              }});
              tbl += '</tbody></table>';
              resBox.style.display = 'block';
              resBox.innerHTML = tbl;
            }}
          }}
          if (Number(data.corp_created_count || 0) > 0) {{
            setTimeout(() => {{ window.location.reload(); }}, 1500);
          }}
        }} else {{
          st.style.color = '#b91c1c';
          st.textContent = `⚠️ ${{data.message || '토스페이먼츠 메일 스크롤링 실패'}}`;
        }}
      }} catch (err) {{
        st.style.color = '#b91c1c';
        st.textContent = '⚠️ 토스페이먼츠 메일 스크롤링 요청 중 오류가 발생했습니다.';
      }} finally {{
        if (btnEl) {{
          btnEl.disabled = false;
          btnEl.textContent = prevTxt;
        }}
      }}
    }}
    async function submitEmailPaste() {{
      const txt = (document.getElementById('email-paste-input').value || '').trim();
      if (!txt) return;
      const st = document.getElementById('email-sync-status');
      st.style.color = '#1e40af';
      st.textContent = '⏳ 붙여넣은 이메일 내역을 분석하여 전표 매칭 및 거래명세서를 작성 중입니다...';
      const res = await fetch('/api/v1/scraper/email-webhook', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{ raw_content: txt }})
      }});
      const data = await res.json();
      if (data.matched_count > 0) {{
        st.style.color = '#15803d';
        st.textContent =
          `✅ 주문 ${{data.parsed_orders_count}}건 추출 · 전표 ${{data.matched_count}}건 매칭 및 거래명세서 작성 완료!`;
        setTimeout(() => {{ window.location.reload(); }}, 900);
      }} else {{
        st.style.color = '#b91c1c';
        st.textContent =
          `⚠️ 추출된 주문(${{data.parsed_orders_count || 0}}건)과 일치하는 금액·날짜의 카드 전표를 찾지 못했습니다.`;
      }}
    }}
    function uploadEmlFile(inputEl) {{
      if (!inputEl.files || !inputEl.files[0]) return;
      const file = inputEl.files[0];
      const reader = new FileReader();
      reader.onload = async function(ev) {{
        const dataUrl = String(ev.target.result || '');
        const b64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
        const res = await fetch('/api/v1/scraper/email-webhook', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify({{ content_b64: b64 }})
        }});
        const data = await res.json();
        const st = document.getElementById('email-sync-status');
        if (st) {{
          st.style.color = data.matched_count > 0 ? '#15803d' : '#b91c1c';
          st.textContent =
            `✅ 파일(${{file.name}}) 주문 ${{data.parsed_orders_count || 0}}건 추출 · ` +
            `전표 ${{data.matched_count || 0}}건 매칭 완료!`;
        }}
        window.location.reload();
      }};
      reader.readAsDataURL(file);
    }}
    function isCtrlFSearchOpen() {{
      const bar = document.getElementById('ctrlf-search-bar');
      return !!(bar && bar.classList.contains('open'));
    }}

    function escapeCtrlFRegExp(rawStr) {{
      return String(rawStr || '').replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
    }}

    function buildCtrlFRegex(rawQuery) {{
      const trimmed = String(rawQuery || '').trim();
      if (!trimmed) return null;
      const litPat = escapeCtrlFRegExp(trimmed);
      const numCand = trimmed.replace(/[\\s,]/g, '').replace(/원$/, '');
      if (/^\\d{{2,}}$/.test(numCand)) {{
        const sep = numCand.length === 10 ? '[-,]?' : ',?';
        const numPat = numCand.split('').join(sep);
        const combined = litPat === numPat ? litPat : (litPat + '|' + numPat);
        return new RegExp(combined, 'gi');
      }}
      return new RegExp(litPat, 'gi');
    }}

    function clearCtrlFHighlights() {{
      const marks = document.querySelectorAll('mark.sl-find-hit');
      marks.forEach((m) => {{
        const parent = m.parentNode;
        if (parent) {{
          parent.replaceChild(document.createTextNode(m.textContent || ''), m);
          parent.normalize();
        }}
      }});
      const inputHits = document.querySelectorAll('.sl-find-input-hit');
      inputHits.forEach((inp) => {{
        inp.classList.remove('sl-find-input-hit', 'sl-find-active');
      }});
      ctrlFHitElements = [];
      ctrlFActiveIndex = -1;
    }}

    function isCtrlFElementVisible(el, openModal) {{
      if (!el) return false;
      if (openModal && !openModal.contains(el)) return false;
      let cur = el;
      while (cur && cur !== document.body) {{
        const tag = (cur.tagName || '').toUpperCase();
        if (
          tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT'
          || tag === 'OPTION' || tag === 'SELECT' || tag === 'TEXTAREA'
        ) {{
          return false;
        }}
        if (cur.id === 'ctrlf-search-bar') return false;
        if (!openModal && cur.classList && cur.classList.contains('modal-backdrop')) {{
          return false;
        }}
        if (cur.hidden) return false;
        if (cur.style && cur.style.display === 'none') return false;
        if (window.getComputedStyle) {{
          const cs = window.getComputedStyle(cur);
          if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        }}
        cur = cur.parentElement;
      }}
      return true;
    }}

    function highlightCtrlFMatches(regex) {{
      const openModal = document.querySelector('.modal-backdrop.open');
      const rootNode = openModal || document.body;
      if (!rootNode || !regex) return;

      const textNodes = [];
      const walker = document.createTreeWalker(rootNode, NodeFilter.SHOW_TEXT, null);
      let curNode;
      while ((curNode = walker.nextNode())) {{
        const val = curNode.nodeValue || '';
        if (!val.trim()) continue;
        if (!isCtrlFElementVisible(curNode.parentElement, openModal)) continue;
        regex.lastIndex = 0;
        if (regex.test(val)) {{
          textNodes.push(curNode);
        }}
      }}

      textNodes.forEach((tNode) => {{
        const text = tNode.nodeValue || '';
        regex.lastIndex = 0;
        const frag = document.createDocumentFragment();
        let lastIdx = 0;
        let m;
        while ((m = regex.exec(text)) !== null) {{
          if (m[0].length === 0) {{
            regex.lastIndex += 1;
            continue;
          }}
          if (m.index > lastIdx) {{
            frag.appendChild(document.createTextNode(text.slice(lastIdx, m.index)));
          }}
          const mark = document.createElement('mark');
          mark.className = 'sl-find-hit';
          mark.textContent = m[0];
          frag.appendChild(mark);
          ctrlFHitElements.push(mark);
          lastIdx = m.index + m[0].length;
        }}
        if (lastIdx < text.length) {{
          frag.appendChild(document.createTextNode(text.slice(lastIdx)));
        }}
        if (tNode.parentNode) {{
          tNode.parentNode.replaceChild(frag, tNode);
        }}
      }});

      const inputs = rootNode.querySelectorAll('input[type="text"]');
      let hasInputHit = false;
      inputs.forEach((inp) => {{
        if (inp.id === 'ctrlf-search-input') return;
        const v = inp.value || '';
        if (!v.trim()) return;
        if (!isCtrlFElementVisible(inp, openModal)) return;
        regex.lastIndex = 0;
        if (regex.test(v)) {{
          inp.classList.add('sl-find-input-hit');
          ctrlFHitElements.push(inp);
          hasInputHit = true;
        }}
      }});

      if (hasInputHit && ctrlFHitElements.length > 1) {{
        ctrlFHitElements.sort((a, b) => {{
          if (a === b) return 0;
          const pos = a.compareDocumentPosition(b);
          return (pos & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1;
        }});
      }}
    }}

    function updateCtrlFCounterUI(ledgerStats) {{
      const cntEl = document.getElementById('ctrlf-match-count');
      const pillEl = document.getElementById('ctrlf-filter-summary');
      const total = ctrlFHitElements.length;
      if (cntEl) {{
        if (!activeCtrlFQuery) {{
          cntEl.textContent = '0 / 0';
          cntEl.classList.remove('zero');
        }} else if (total === 0) {{
          cntEl.textContent = '0 / 0';
          cntEl.classList.add('zero');
        }} else {{
          cntEl.textContent = (ctrlFActiveIndex + 1) + ' / ' + total;
          cntEl.classList.remove('zero');
        }}
      }}
      if (pillEl) {{
        if (activeCtrlFQuery && ledgerStats) {{
          const fmtAmt = Number(ledgerStats.matchSearchAmt || 0).toLocaleString('ko-KR') + '원';
          pillEl.style.display = 'inline-block';
          pillEl.textContent =
            '원장 ' + (ledgerStats.matchSearchCount || 0) + '건 · ' + fmtAmt;
        }} else {{
          pillEl.style.display = 'none';
          pillEl.textContent = '';
        }}
      }}
    }}

    function activateCtrlFMatch(idx, shouldScroll) {{
      const total = ctrlFHitElements.length;
      if (total === 0) {{
        ctrlFActiveIndex = -1;
        return;
      }}
      if (ctrlFActiveIndex >= 0 && ctrlFActiveIndex < total) {{
        ctrlFHitElements[ctrlFActiveIndex].classList.remove('sl-find-active');
      }}
      const normalized = ((idx % total) + total) % total;
      ctrlFActiveIndex = normalized;
      const target = ctrlFHitElements[normalized];
      if (target) {{
        target.classList.add('sl-find-active');
        if (shouldScroll && target.scrollIntoView) {{
          target.scrollIntoView({{ behavior: 'smooth', block: 'center', inline: 'nearest' }});
        }}
      }}
    }}

    function executeCtrlFSearch(shouldScrollToFirst) {{
      const inp = document.getElementById('ctrlf-search-input');
      const rawVal = inp ? inp.value : '';
      clearCtrlFHighlights();
      activeCtrlFQuery = String(rawVal || '').trim();
      activeCtrlFRegex = buildCtrlFRegex(activeCtrlFQuery);
      const ledgerStats = applyMainLedgerRowVisibility();
      if (activeCtrlFRegex) {{
        highlightCtrlFMatches(activeCtrlFRegex);
        if (ctrlFHitElements.length > 0) {{
          activateCtrlFMatch(0, !!shouldScrollToFirst);
        }}
      }}
      updateCtrlFCounterUI(ledgerStats);
    }}

    function refreshCtrlFIfOpen() {{
      if (isCtrlFSearchOpen() && activeCtrlFQuery) {{
        executeCtrlFSearch(false);
      }}
    }}

    function stepCtrlFMatch(delta) {{
      if (!activeCtrlFQuery) return;
      if (ctrlFHitElements.length === 0) {{
        executeCtrlFSearch(true);
        return;
      }}
      const nextIdx = ctrlFActiveIndex < 0 ? 0 : (ctrlFActiveIndex + delta);
      activateCtrlFMatch(nextIdx, true);
      updateCtrlFCounterUI(applyMainLedgerRowVisibility());
    }}

    function onCtrlFInput() {{
      executeCtrlFSearch(true);
    }}

    function onCtrlFRowFilterToggle() {{
      const chk = document.getElementById('ctrlf-filter-rows-chk');
      ctrlFRowFilterOnly = !!(chk && chk.checked);
      executeCtrlFSearch(true);
    }}

    function onCtrlFInputKeydown(ev) {{
      if (ev.key === 'Enter' && !ev.isComposing && ev.keyCode !== 229) {{
        ev.preventDefault();
        stepCtrlFMatch(ev.shiftKey ? -1 : 1);
      }}
    }}

    function openCtrlFSearch(presetQuery) {{
      const bar = document.getElementById('ctrlf-search-bar');
      const inp = document.getElementById('ctrlf-search-input');
      if (!bar || !inp) return;
      bar.classList.add('open');
      if (typeof presetQuery === 'string' && presetQuery.trim()) {{
        inp.value = presetQuery.trim();
      }} else if (window.getSelection) {{
        const sel = String(window.getSelection().toString() || '').trim();
        if (sel && sel.length <= 80 && !sel.includes('\\n')) {{
          inp.value = sel;
        }}
      }}
      inp.focus();
      inp.select();
      if (inp.value.trim()) {{
        executeCtrlFSearch(true);
      }}
    }}

    function closeCtrlFSearch() {{
      const bar = document.getElementById('ctrlf-search-bar');
      const chk = document.getElementById('ctrlf-filter-rows-chk');
      if (bar) bar.classList.remove('open');
      if (chk) chk.checked = false;
      ctrlFRowFilterOnly = false;
      activeCtrlFQuery = '';
      activeCtrlFRegex = null;
      clearCtrlFHighlights();
      applyMainLedgerRowVisibility();
      updateCtrlFCounterUI(null);
    }}

    document.addEventListener('keydown', function(ev) {{
      const isFindKey = (ev.ctrlKey || ev.metaKey)
        && !ev.altKey
        && !ev.shiftKey
        && (ev.key === 'f' || ev.key === 'F' || ev.code === 'KeyF');
      if (isFindKey) {{
        ev.preventDefault();
        openCtrlFSearch();
        return;
      }}
      if (ev.key === 'F3') {{
        ev.preventDefault();
        if (!isCtrlFSearchOpen()) {{
          openCtrlFSearch();
        }} else {{
          stepCtrlFMatch(ev.shiftKey ? -1 : 1);
        }}
        return;
      }}
      if (ev.key === 'Escape') {{
        if (isCtrlFSearchOpen()) {{
          ev.preventDefault();
          closeCtrlFSearch();
          return;
        }}
        closeReceiptModal();
        closeCoupangModal();
        closeEmailOrderModal();
        closeCorpCardModal();
      }}
    }});

    const FREEZE_HEADER_STORAGE_KEY = 'sololedger_freeze_header';
    let freezeHeadersEnabled = true;
    let activeHoverTh = null;
    let stickyScrollRafPending = false;

    function readSavedFreezePreference() {{
      try {{
        const saved = window.localStorage.getItem(FREEZE_HEADER_STORAGE_KEY);
        if (saved === '0' || saved === 'false') return false;
      }} catch (e) {{}}
      return true;
    }}

    function saveFreezePreference(enabled) {{
      try {{
        window.localStorage.setItem(FREEZE_HEADER_STORAGE_KEY, enabled ? '1' : '0');
      }} catch (e) {{}}
    }}

    function setHoverHeaderCell(nextTh) {{
      if (activeHoverTh === nextTh) return;
      if (activeHoverTh) activeHoverTh.classList.remove('col-hover-active');
      activeHoverTh = nextTh || null;
      if (activeHoverTh) activeHoverTh.classList.add('col-hover-active');
    }}

    function syncStickySectionOffsets() {{
      const sections = document.querySelectorAll('.section');
      let totalPinnedRows = 0;
      sections.forEach((sec) => {{
        let bar = null;
        let tbl = null;
        for (let i = 0; i < sec.children.length; i += 1) {{
          const child = sec.children[i];
          if (!bar && child.classList && child.classList.contains('section-sticky-bar')) {{
            bar = child;
          }} else if (!tbl && child.classList && child.classList.contains('sticky-ledger-table')) {{
            tbl = child;
          }}
        }}
        const barH = bar ? bar.getBoundingClientRect().height : 0;
        const thead = tbl && tbl.tHead ? tbl.tHead : null;
        const theadH = thead ? thead.getBoundingClientRect().height : 0;
        sec.style.setProperty('--sticky-bar-h', barH.toFixed(2) + 'px');
        sec.style.setProperty('--sticky-thead-h', theadH.toFixed(2) + 'px');

        if (tbl && tbl.tBodies && tbl.tBodies[0]) {{
          let cumulativeTop = barH + theadH;
          const pinnedRows = tbl.tBodies[0].querySelectorAll('tr.pinned-ledger-row');
          pinnedRows.forEach((tr) => {{
            if (tr.style.display === 'none') return;
            totalPinnedRows += 1;
            tr.style.setProperty('--pinned-row-top', cumulativeTop.toFixed(2) + 'px');
            cumulativeTop += tr.getBoundingClientRect().height;
          }});
        }}
      }});

      const clearPinnedBtn = document.getElementById('btn-clear-pinned-rows');
      if (clearPinnedBtn) {{
        if (totalPinnedRows > 0) {{
          clearPinnedBtn.style.display = 'inline-block';
          clearPinnedBtn.textContent = '📌 기준행 고정 해제 (' + totalPinnedRows + '건)';
        }} else {{
          clearPinnedBtn.style.display = 'none';
        }}
      }}
    }}

    function updateStickyStuckClasses() {{
      const sections = document.querySelectorAll('.section');
      if (!freezeHeadersEnabled || document.body.classList.contains('unfreeze-headers')) {{
        sections.forEach((sec) => sec.classList.remove('is-stuck'));
        return;
      }}
      sections.forEach((sec) => {{
        let hasTable = false;
        for (let i = 0; i < sec.children.length; i += 1) {{
          if (sec.children[i].classList && sec.children[i].classList.contains('sticky-ledger-table')) {{
            hasTable = true;
            break;
          }}
        }}
        if (!hasTable) return;
        const rect = sec.getBoundingClientRect();
        const isStuck = rect.top <= 2 && rect.bottom > 110;
        sec.classList.toggle('is-stuck', isStuck);
      }});
    }}

    function applyFreezeHeaderState(enabled) {{
      freezeHeadersEnabled = !!enabled;
      document.body.classList.toggle('unfreeze-headers', !freezeHeadersEnabled);
      const topBtn = document.getElementById('btn-toggle-freeze-header');
      const ledgerBtn = document.getElementById('btn-ledger-freeze-header');
      if (topBtn) {{
        topBtn.classList.toggle('off', !freezeHeadersEnabled);
        topBtn.textContent = freezeHeadersEnabled ? '📌 상단 행 고정 ON' : '📌 상단 행 고정 OFF';
      }}
      if (ledgerBtn) {{
        ledgerBtn.classList.toggle('off', !freezeHeadersEnabled);
        ledgerBtn.textContent = freezeHeadersEnabled ? '📌 행 고정 ON' : '📌 행 고정 OFF';
      }}
      syncStickySectionOffsets();
      updateStickyStuckClasses();
    }}

    function toggleFreezeHeaders() {{
      const nextState = !freezeHeadersEnabled;
      saveFreezePreference(nextState);
      applyFreezeHeaderState(nextState);
    }}

    function clearAllPinnedLedgerRows() {{
      const pinned = document.querySelectorAll('tr.pinned-ledger-row');
      pinned.forEach((tr) => {{
        tr.classList.remove('pinned-ledger-row');
        tr.style.removeProperty('--pinned-row-top');
      }});
      syncStickySectionOffsets();
    }}

    function scrollToDashboardTop() {{
      window.scrollTo({{ top: 0, behavior: 'smooth' }});
    }}

    (function initStickyLedgerFreeze() {{
      applyFreezeHeaderState(readSavedFreezePreference());

      window.addEventListener('scroll', function() {{
        if (stickyScrollRafPending) return;
        stickyScrollRafPending = true;
        window.requestAnimationFrame(function() {{
          stickyScrollRafPending = false;
          updateStickyStuckClasses();
        }});
      }}, {{ passive: true }});

      window.addEventListener('resize', function() {{
        syncStickySectionOffsets();
        updateStickyStuckClasses();
      }});

      if (typeof ResizeObserver !== 'undefined') {{
        const ro = new ResizeObserver(function() {{
          syncStickySectionOffsets();
        }});
        document.querySelectorAll('.section-sticky-bar, .sticky-ledger-table > thead').forEach((el) => {{
          ro.observe(el);
        }});
      }}

      document.addEventListener('mouseover', function(ev) {{
        const target = ev.target;
        const td = target && target.closest ? target.closest('tbody td') : null;
        if (!td || td.colSpan > 1) {{
          setHoverHeaderCell(null);
          return;
        }}
        const tr = td.parentElement;
        if (!tr || tr.classList.contains('acc-detail-row')) {{
          setHoverHeaderCell(null);
          return;
        }}
        const tbl = tr.closest('table.sticky-ledger-table, table.acc-detail-table');
        if (!tbl || !tbl.tHead || !tbl.tHead.rows || !tbl.tHead.rows[0]) {{
          setHoverHeaderCell(null);
          return;
        }}
        const headCell = tbl.tHead.rows[0].cells[td.cellIndex] || null;
        setHoverHeaderCell(headCell);
      }});

      document.addEventListener('mouseleave', function() {{
        setHoverHeaderCell(null);
      }});

      document.addEventListener('click', function(ev) {{
        const target = ev.target;
        const pinCell = target && target.closest ? target.closest('td.tx-pin-cell') : null;
        if (!pinCell) return;
        const tr = pinCell.parentElement;
        if (!tr) return;
        tr.classList.toggle('pinned-ledger-row');
        if (!tr.classList.contains('pinned-ledger-row')) {{
          tr.style.removeProperty('--pinned-row-top');
        }}
        syncStickySectionOffsets();
      }});
    }})();

    (function initUrlFindQuery() {{
      try {{
        const params = new URLSearchParams(window.location.search || '');
        const initialFind = params.get('find') || params.get('q') || '';
        if (initialFind.trim()) {{
          openCtrlFSearch(initialFind.trim());
        }}
      }} catch (e) {{}}
    }})();

    const CORP_ISSUER_PREFIX_MAP = {{
      WOORI: '우리법인',
      KB: 'KB국민법인',
      SHINHAN: '신한법인',
      HANA: '하나법인',
      SAMSUNG: '삼성법인',
      HYUNDAI: '현대법인',
      LOTTE: '롯데법인',
      NH: 'NH농협법인',
      IBK: 'IBK기업법인',
      BC: 'BC법인',
      CORP: '법인카드'
    }};

    function onCorpIssuerChange() {{
      const iss = document.getElementById('corp-card-issuer').value;
      const last4 = (document.getElementById('corp-card-last4').value || '').trim();
      const prefix = CORP_ISSUER_PREFIX_MAP[iss] || '법인카드';
      if (last4) {{
        document.getElementById('corp-card-label').value = `${{prefix}}(${{last4}})`;
      }}
    }}

    async function refreshCorpCardPolicyState() {{
      const res = await fetch('/api/v1/system/corp-card-policy', {{ headers: API_HEADERS }});
      if (!res.ok) return null;
      const pol = await res.json();
      document.getElementById('corp-only-mode-chk').checked = !!pol.corp_only_mode;
      document.getElementById('corp-strict-registered-chk').checked = !!pol.strict_registered_only;
      document.getElementById('corp-block-unmatched-email-chk').checked = !!pol.block_unmatched_email_orders;
      document.getElementById('corp-auto-reg-upload-chk').checked = !!pol.auto_register_on_statement_upload;
      const cards = Array.isArray(pol.registered_corp_cards) ? pol.registered_corp_cards : [];
      cachedRegisteredCorpCards = cards;
      document.getElementById('corp-reg-count').textContent = String(cards.length);
      const listEl = document.getElementById('corp-registered-list');
      if (cards.length === 0) {{
        listEl.innerHTML =
          '<div style="color:#b45309; font-size:12px; padding:6px;">' +
          '등록된 법인카드가 없습니다. 위 입력창에서 끝 4자리를 추가하거나 법인카드 명세서를 업로드해 주세요.' +
          '</div>';
      }} else {{
        let rows = '';
        cards.forEach((c, idx) => {{
          const c4 = escHtml(c.card_last4);
          const lbl = escHtml(c.card_label || c.label || c.card_last4);
          const iss = escHtml(c.issuer || 'CORP');
          rows +=
            '<div style="display:flex; justify-content:space-between; align-items:center; ' +
            'padding:6px 4px; border-bottom:1px solid #e2e8f0;">' +
            '<div><span class="badge card" style="background:#0f766e; color:#fff; margin-right:8px;">' +
            lbl + '</span>' +
            '<code style="font-size:12px;">끝 4자리/식별자: ' + c4 + '</code> ' +
            '<span style="color:#64748b; font-size:11px;">(' + iss + ')</span></div>' +
            '<button class="danger" style="padding:3px 8px; font-size:11px;" ' +
            'onclick="deleteRegisteredCorpCardByIdx(' + idx + ')">삭제</button>' +
            '</div>';
        }});
        listEl.innerHTML = rows;
      }}
      return pol;
    }}

    async function openCorpCardModal() {{
      document.getElementById('corp-card-modal-backdrop').classList.add('open');
      await refreshCorpCardPolicyState();
    }}

    function closeCorpCardModal() {{
      document.getElementById('corp-card-modal-backdrop').classList.remove('open');
    }}

    function onCorpCardBackdropClick(ev) {{
      if (ev.target && ev.target.id === 'corp-card-modal-backdrop') {{
        closeCorpCardModal();
      }}
    }}

    async function saveCorpCardPolicySettings() {{
      const st = document.getElementById('corp-policy-status');
      const bodyObj = {{
        corp_only_mode: document.getElementById('corp-only-mode-chk').checked,
        strict_registered_only: document.getElementById('corp-strict-registered-chk').checked,
        block_unmatched_email_orders: document.getElementById('corp-block-unmatched-email-chk').checked,
        auto_register_on_statement_upload: document.getElementById('corp-auto-reg-upload-chk').checked
      }};
      const res = await fetch('/api/v1/system/corp-card-policy', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify(bodyObj)
      }});
      if (res.ok) {{
        st.textContent = '✅ 법인카드 전용 모드 정책이 저장되었습니다.';
        setTimeout(() => {{ window.location.reload(); }}, 700);
      }}
    }}

    async function registerNewCorpCard() {{
      const st = document.getElementById('corp-policy-status');
      const iss = document.getElementById('corp-card-issuer').value;
      const last4 = (document.getElementById('corp-card-last4').value || '').trim();
      let label = (document.getElementById('corp-card-label').value || '').trim();
      if (!last4) {{
        st.style.color = '#b91c1c';
        st.textContent = '⚠️ 법인카드 끝 4자리 번호를 입력해 주세요.';
        return;
      }}
      if (!label) {{
        const prefix = CORP_ISSUER_PREFIX_MAP[iss] || '법인카드';
        label = `${{prefix}}(${{last4}})`;
      }}
      const res = await fetch('/api/v1/system/corp-card-policy', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{
          issuer: iss,
          card_last4: last4,
          label: label
        }})
      }});
      if (res.ok) {{
        document.getElementById('corp-card-last4').value = '';
        document.getElementById('corp-card-label').value = '';
        st.style.color = '#0f766e';
        st.textContent = `✅ 법인카드 [${{label}}] 등록 완료!`;
        await refreshCorpCardPolicyState();
      }}
    }}

    let cachedRegisteredCorpCards = [];
    async function deleteRegisteredCorpCardByIdx(idx) {{
      const entry = cachedRegisteredCorpCards[idx];
      if (!entry || !entry.card_last4) return;
      await deleteRegisteredCorpCard(entry.card_last4);
    }}

    async function deleteRegisteredCorpCard(cardLast4) {{
      const res = await fetch('/api/v1/system/corp-cards/' + encodeURIComponent(cardLast4), {{
        method: 'DELETE',
        headers: API_HEADERS
      }});
      if (res.ok) {{
        await refreshCorpCardPolicyState();
      }}
    }}

    function uploadCorpStatementFile(inputEl) {{
      if (!inputEl.files || !inputEl.files[0]) return;
      const file = inputEl.files[0];
      const portal = document.getElementById('corp-upload-portal').value || 'HOMETAX';
      const reader = new FileReader();
      reader.onload = async function(ev) {{
        const dataUrl = String(ev.target.result || '');
        const b64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
        await fetch('/api/v1/scraper/ingest', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify({{
            portal: portal,
            content_b64: b64,
            auto_generate_slips: true
          }})
        }});
        window.location.href = '/dashboard?year_month=ALL';
      }};
      reader.readAsDataURL(file);
    }}
  </script>
</body>
</html>"""
