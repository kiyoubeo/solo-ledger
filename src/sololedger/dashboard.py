"""Lightweight Web Dashboard and Inline Ledger Management for SoloLedger."""

import calendar
import html
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger import __version__
from sololedger.archiver import compute_sha256_hex, save_receipt_files
from sololedger.config import settings
from sololedger.sms_parser import format_card_label, sanitize_card_folder_name
from sololedger.tax_engine import (
    calculate_supply_and_vat,
    evaluate_tax_rules,
    generate_default_memo,
    learn_merchant_rule,
)
from sololedger.telegram_bot import _refresh_receipt_archived_filename
from sololedger.vehicle_tracker import get_vehicle_expense_summary

ACCOUNT_CODE_CATALOG: Dict[str, str] = {
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


def reapply_tax_rules_to_ledger(cursor: sqlite3.Cursor) -> Dict[str, Any]:
    """Re-evaluate tax classification rules and populate smart default memos across ledger rows."""
    cursor.execute(
        """
        SELECT tx_id, raw_merchant_name, biz_reg_no, total_amount,
               account_code, account_name, vat_deduction_type, counterparty_info
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

        rule_eval = evaluate_tax_rules(cursor, m_name, r["biz_reg_no"])
        new_code = cur_code
        if cur_code == "999" and rule_eval["account_code"] != "999":
            new_code = rule_eval["account_code"]
            new_name = rule_eval["account_name"]
            new_vat_type = rule_eval["vat_deduction_type"]
            new_reason = rule_eval["non_deduct_reason"]
            is_veh = 1 if rule_eval["is_vehicle_expense"] else 0
            sup, vat = calculate_supply_and_vat(int(r["total_amount"]), new_vat_type)
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
                    updated_at = CURRENT_TIMESTAMP
                WHERE tx_id = ?
                """,
                (new_code, new_name, new_vat_type, new_reason, is_veh, sup, vat, tx_id),
            )
            reclassified_count += 1

        if not cur_memo:
            req_prompt = bool(rule_eval.get("require_memo_prompt", False))
            auto_memo = generate_default_memo(new_code, m_name, req_prompt)
            if auto_memo:
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET counterparty_info = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tx_id = ?
                    """,
                    (auto_memo, tx_id),
                )
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


def build_dashboard_summary(
    cursor: sqlite3.Cursor,
    year_month: Optional[str] = None,
    year: Optional[int] = None,
    card_last4: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Build aggregated dashboard metrics, per-card breakdown, pending action items, and vehicle limit."""
    now = datetime.now()
    target_card = (card_last4 or "ALL").strip()

    cursor.execute(
        """
        SELECT MIN(SUBSTR(transacted_at, 1, 10)), MAX(SUBSTR(transacted_at, 1, 10))
        FROM card_transactions
        WHERE LENGTH(transacted_at) >= 10
          AND reconcile_status != 'CANCELED'
        """
    )
    bound_row = cursor.fetchone()
    min_tx_date = (bound_row[0] if bound_row and bound_row[0] else None) or f"{now.year}-01-01"
    max_tx_date = (bound_row[1] if bound_row and bound_row[1] else None) or now.strftime("%Y-%m-%d")

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
        target_ym = (year_month or default_ym).strip().upper()
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
        rows = [r for r in all_period_rows if str(r.get("card_last4") or "0000") == target_card]
    else:
        rows = all_period_rows

    total_expenditure = 0
    deductible_vat = 0
    non_deductible_vat = 0
    non_deductible_total = 0
    pending_items: List[Dict[str, Any]] = []
    acc_stats: Dict[str, Dict[str, Any]] = {}

    for r in rows:
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

        total_expenditure += amt
        if r["vat_deduction_type"] == "DEDUCTIBLE":
            deductible_vat += vat
            a_entry["deductible_vat"] += vat
        elif r["vat_deduction_type"] == "NON_DEDUCTIBLE":
            non_deductible_vat += vat
            non_deductible_total += amt
            a_entry["non_deductible_total"] += amt

        memo = (r.get("counterparty_info") or "").strip()
        if r["account_code"] == "999" or not memo:
            pending_items.append(r)

    for a_entry in acc_stats.values():
        if total_expenditure > 0:
            a_entry["ratio_pct"] = round(a_entry["total_expenditure"] * 100.0 / total_expenditure, 1)

    account_breakdown = sorted(
        acc_stats.values(),
        key=lambda x: (x["total_expenditure"], x["tx_count"]),
        reverse=True,
    )

    vehicle_summary = get_vehicle_expense_summary(cursor, year=target_year)

    return {
        "app_env": settings.app_env,
        "db_path": str(settings.db_path),
        "nas_backup_dir": str(settings.nas_backup_dir),
        "year_month": target_ym,
        "start_date": resolved_start,
        "end_date": resolved_end,
        "min_tx_date": min_tx_date,
        "max_tx_date": max_tx_date,
        "card_last4": target_card,
        "available_months": available_months,
        "monthly_periods": monthly_periods,
        "card_breakdown": card_breakdown,
        "account_breakdown": account_breakdown,
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

    target_archive = archive_dir or settings.archive_dir
    for sub in ("original", "normalized", "exports"):
        sub_path = target_archive / sub
        if sub_path.exists():
            shutil.rmtree(sub_path, ignore_errors=True)
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
) -> str:
    """Render server-side HTML5 management dashboard for SoloLedger."""
    summary = build_dashboard_summary(
        cursor,
        year_month=year_month,
        year=year,
        card_last4=card_last4,
        start_date=start_date,
        end_date=end_date,
    )
    veh = summary["vehicle_limit"]
    gauge_pct = min(100.0, float(veh["usage_ratio_pct"]))
    s_date = html.escape(str(summary["start_date"]))
    e_date = html.escape(str(summary["end_date"]))
    min_d = html.escape(str(summary["min_tx_date"]))
    max_d = html.escape(str(summary["max_tx_date"]))

    card_options_html = []
    c_all_sel = "selected" if summary["card_last4"] == "ALL" else ""
    card_options_html.append(f"<option value='ALL' {c_all_sel}>전체 카드 (ALL)</option>")
    for cb in summary.get("card_breakdown", []):
        c_val = html.escape(str(cb["card_last4"]))
        c_lbl = html.escape(f"{cb['card_label']} ({cb['tx_count']}건)")
        sel = "selected" if summary["card_last4"] == cb["card_last4"] else ""
        card_options_html.append(f"<option value='{c_val}' {sel}>{c_lbl}</option>")
    card_select_html = "".join(card_options_html)

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
    for ab in summary.get("account_breakdown", []):
        a_lbl = html.escape(f"{ab['account_code']} {ab['account_name']}")
        acc_breakdown_rows.append(
            f"<tr>"
            f"<td><strong>{a_lbl}</strong></td>"
            f"<td class='num'>{ab['tx_count']}건</td>"
            f"<td class='num'><strong>{ab['total_expenditure']:,}원</strong></td>"
            f"<td class='num'>{ab['supply_value']:,}원</td>"
            f"<td class='num'>{ab['deductible_vat']:,}원</td>"
            f"<td class='num'>{ab['non_deductible_total']:,}원</td>"
            f"<td class='num'>{ab['ratio_pct']}%</td>"
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
            f"<tr data-tx-id='{tx_id}'>"
            f"<td>#{tx_id}</td>"
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
    for tx in summary["transactions"]:
        tx_id = tx["tx_id"]
        c_lbl = html.escape(str(tx.get("card_label") or format_card_label(tx["card_last4"])))
        merchant = html.escape(str(tx["raw_merchant_name"]))
        amt_str = f"{int(tx['total_amount']):,}원"
        vat_str = f"{int(tx['vat_amount']):,}원"
        acc_str = html.escape(f"{tx['account_code']} {tx['account_name']}")
        ded_str = html.escape(str(tx["vat_deduction_type"]))
        memo_str = html.escape(str(tx.get("counterparty_info") or "-"))
        arch_str = html.escape(str(tx.get("archived_file_name") or "-"))
        t_dt = html.escape(str(tx["transacted_at"]))
        dt_link = (
            f"<a href='javascript:void(0)' class='tx-date-link' "
            f"onclick='openReceiptModal({tx_id})' title='클릭하여 영수증/매출전표 팝업 보기'>{t_dt} 🧾</a>"
            f" <a href='javascript:void(0)' class='tx-date-link' style='color:#0f766e; font-size:12px;' "
            f"onclick='openStatementModal({tx_id})' title='클릭하여 거래명세서 보기/발급'>[📄명세서]</a>"
        )
        tx_rows_html.append(
            f"<tr>"
            f"<td>#{tx_id}</td>"
            f"<td>{dt_link}</td>"
            f"<td><span class='badge card'>{c_lbl}</span></td>"
            f"<td>{merchant}</td>"
            f"<td class='num'>{amt_str}</td>"
            f"<td class='num'>{vat_str}</td>"
            f"<td>{acc_str}</td>"
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
                padding: 18px; margin-bottom: 24px; }}
    .gauge-track {{ width: 100%; height: 16px; background: #e2e8f0; border-radius: 8px;
                    overflow: hidden; margin: 10px 0; }}
    .gauge-bar {{ height: 100%; background: #2563eb; width: {gauge_pct}%; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e2e8f0; padding: 9px 8px; text-align: left; }}
    th {{ background: #f1f5f9; font-weight: 600; }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    td.empty {{ text-align: center; color: #64748b; padding: 18px; }}
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
    .modal-backdrop {{ display: none; position: fixed; inset: 0; background: rgba(15, 23, 42, 0.58);
                       z-index: 9999; align-items: center; justify-content: center; padding: 16px; }}
    .modal-backdrop.open {{ display: flex; }}
    .receipt-modal {{ background: #ffffff; width: 100%; max-width: 480px; max-height: 90vh;
                      overflow-y: auto; border-radius: 12px; box-shadow: 0 20px 40px rgba(0,0,0,0.28);
                      border: 1px solid #cbd5e1; padding: 22px; }}
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
        max-width: 420px !important;
        max-height: none !important;
        overflow: visible !important;
        box-shadow: none !important;
        border: none !important;
        margin: 0 auto !important;
        padding: 8px !important;
        break-inside: avoid;
      }}
      .no-print {{ display: none !important; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-top">
      <div class="header-title-wrap">
        <h1>SoloLedger 경비·세무증빙 대시보드 (v{__version__})
          <span class="badge env">{html.escape(summary['app_env'].upper())} MODE</span>
        </h1>
        <div class="header-meta">
          귀속 연도: <strong>{summary['year']}년</strong>
          &nbsp;|&nbsp;
          원장 DB: <code>{html.escape(Path(summary['db_path']).name)}</code>
        </div>
      </div>
      <div class="action-toolbar no-print">
        <input type="file" id="receipt-file-input" accept="image/*,.pdf"
               style="display:none" onchange="uploadReceiptFile(this)" />
        <input type="file" id="csv-file-input" accept=".csv,.txt,.xls,.html,.json"
               style="display:none" onchange="uploadCsvFile(this)" />
        <button class="secondary" onclick="window.open('/manual', '_blank')">사용 매뉴얼(HTML)</button>
        <button onclick="reapplyRules()">스마트 자동분류 실행</button>
        <button onclick="generateElectronicSlips()">전자 매출전표 일괄 생성</button>
        <button onclick="document.getElementById('receipt-file-input').click()">영수증 업로드(OCR)</button>
        <button onclick="document.getElementById('csv-file-input').click()">카드사/홈택스 전표 업로드</button>
        <button onclick="openCoupangModal()">🛒 쿠팡 주문·영수증 자동매칭</button>
        <button onclick="openEmailOrderModal()">📧 이메일 주문내역 실시간 연동</button>
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

  <section class="section">
    <h2>계정과목별 지출 및 부가세 요약표 ({len(summary.get('account_breakdown', []))}개 계정)</h2>
    <table>
      <thead>
        <tr>
          <th>계정과목 (코드·명칭)</th><th>거래 건수</th><th>지출 합계</th>
          <th>공급가액</th><th>공제 가능 부가세</th><th>불공제 합계</th><th>지출 비중</th>
        </tr>
      </thead>
      <tbody>
        {acc_breakdown_tbody}
      </tbody>
    </table>
  </section>

  <section class="section">
    <h2>카드별 지출 및 영수증 분류 현황 ({len(summary.get('card_breakdown', []))}개 카드)</h2>
    <table>
      <thead>
        <tr>
          <th>카드명 (식별번호)</th><th>결제 건수</th><th>지출 합계</th>
          <th>공제 부가세</th><th>불공제 합계</th><th>첨부 영수증</th>
          <th>영수증 보관 폴더</th><th>필터</th>
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

  <section class="section">
    <h2>미분류(999) 및 품목·참석자 소명 필요 전표 ({summary['pending_action_count']}건)</h2>
    <table>
      <thead>
        <tr>
          <th>전표번호</th><th>거래일시</th><th>카드구분</th><th>가맹점명</th><th>금액</th>
          <th>현재 계정</th><th>품목 / 참석자 소명</th><th>작업</th>
        </tr>
      </thead>
      <tbody>
        {pending_tbody}
      </tbody>
    </table>
  </section>

  <section class="section">
    <h2>조회 기간 법인카드 원장 ({s_date} ~ {e_date}, 총 {summary['total_tx_count']}건)</h2>
    <table>
      <thead>
        <tr>
          <th>전표번호</th><th>거래일시 (클릭: 영수증 팝업)</th><th>카드구분</th><th>가맹점명</th><th>합계금액</th>
          <th>부가세</th><th>계정과목</th><th>공제구분</th><th>적요/참석자</th><th>증빙파일명</th>
        </tr>
      </thead>
      <tbody>
        {tx_tbody}
      </tbody>
    </table>
  </section>

  <div id="receipt-modal-backdrop" class="modal-backdrop" onclick="onBackdropClick(event)">
    <div class="receipt-modal" style="max-width:540px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
        <h3 id="modal-header-title" style="margin:0;">🧾 신용카드 매출전표 / 거래명세서</h3>
        <button class="secondary no-print" onclick="closeReceiptModal()">✕ 닫기</button>
      </div>
      <div class="no-print" style="display:flex; gap:6px; margin-bottom:12px;">
        <button id="tab-btn-slip" onclick="switchModalDocTab('SLIP')">🧾 신용카드 매출전표</button>
        <button id="tab-btn-stmt" class="secondary" onclick="switchModalDocTab('STATEMENT')">
          📄 거래명세서 (공급받는자 보관용)
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
    <div class="modal-card" style="max-width:760px;">
      <div class="modal-header">
        <strong>📧 다중 이메일 주문·결제내역 실시간 연동 및 거래명세서 자동 작성</strong>
        <button class="secondary" onclick="closeEmailOrderModal()">닫기 (ESC)</button>
      </div>

      <div class="slip-box"
           style="margin-bottom:10px; font-size:13px; line-height:1.55;
                  border:2px solid #2563eb; background:#eff6ff;">
        <div style="display:flex; justify-content:space-between; align-items:center;
                    flex-wrap:wrap; gap:8px; margin-bottom:8px;">
          <div style="font-weight:800; color:#1e3a8a; font-size:14px;">
            📬 연결된 이메일 계정 목록 (네이버 · Gmail · 카카오 · 회사메일 동시 감시)
          </div>
          <div style="display:flex; gap:6px; flex-wrap:wrap;">
            <button onclick="triggerEmailSyncNow(false, '')">
              🚀 연결된 전체 메일함 즉시 통합 스캔
            </button>
            <button class="secondary" onclick="triggerEmailSyncNow(true, '')">
              🔄 전체 강제 재매칭
            </button>
          </div>
        </div>

        <div id="email-accounts-list"
             style="background:#ffffff; border:1px solid #cbd5e1; border-radius:6px;
                    padding:8px; margin-bottom:10px; max-height:190px; overflow-y:auto;">
          <div style="color:#64748b; font-size:12px;">등록된 이메일 계정을 불러오는 중...</div>
        </div>

        <div style="display:flex; align-items:center; justify-content:space-between;
                    flex-wrap:wrap; gap:8px; background:#dbeafe; padding:8px 10px;
                    border-radius:6px; font-size:12px;">
          <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
            <span>스캔 기간(일):</span>
            <input type="text" id="email-lookback-days" value="60" style="width:54px;" />
            <span>감지 주기(초):</span>
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

    function renderModalDocView() {{
      if (!activeModalData) return;
      const d = activeModalData;
      const slipEl = document.getElementById('modal-slip-content');
      const tabSlip = document.getElementById('tab-btn-slip');
      const tabStmt = document.getElementById('tab-btn-stmt');
      const fmt = (n) => Number(n || 0).toLocaleString('ko-KR') + '원';
      const bizNo = d.biz_reg_no || '미등록 (SMS/RCS·PG 전표)';
      const appNo = d.approval_no || ('TX-' + d.tx_id);
      const rowMemoEl = document.getElementById('memo-' + d.tx_id);
      const typedMemo = rowMemoEl && rowMemoEl.value ? rowMemoEl.value.trim() : '';
      const memo = d.counterparty_info || typedMemo || '-';
      const arch = d.archived_file_name
        ? ('by_card/' + d.card_folder + '/' + d.archived_file_name)
        : '실물 증빙 미첨부 (전자 전표)';

      if (activeModalMode === 'STATEMENT') {{
        tabSlip.className = 'secondary';
        tabStmt.className = '';
        const baseTitle = d.statement_print_title
          || buildReceiptPrintTitle(d).replace(/^매출전표_/, '거래명세서_');
        activeModalPrintTitle = baseTitle;
        document.title = activeModalPrintTitle;
        document.getElementById('modal-header-title').textContent =
          '📄 거래명세서 (#' + d.tx_id + ')';
        const mdStr = String(d.transacted_at || '').slice(5, 10).replace('-', '/');
        const itemVal = (d.counterparty_info || typedMemo || (d.raw_merchant_name + ' 구매 품목'))
          .replace(/"/g, '&quot;');
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
            const liName = String(li.item || li.name || itemVal).replace(/</g, '&lt;');
            const liQty = Math.max(1, parseInt(li.qty || 1, 10) || 1);
            const liTot = parseInt(li.price || li.amount || totAmt, 10);
            const liSup = d.vat_deduction_type === 'EXEMPT'
              ? liTot
              : Math.round(liTot / 1.1);
            const liVat = liTot - liSup;
            totQty += liQty;
            tbodyHtml += '<tr>' +
              '<td style="border:1px solid #cbd5e1; padding:6px;">' + mdStr + '</td>' +
              '<td style="border:1px solid #cbd5e1; padding:6px;"><strong>' + liName + '</strong></td>' +
              '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;">' + liQty + '</td>' +
              '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;">' +
              fmt(liSup) + '</td>' +
              '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;">' +
              fmt(liVat) + '</td>' +
              '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;"><strong>' +
              fmt(liTot) + '</strong></td>' +
              '</tr>';
          }});
        }} else {{
          totQty = 1;
          tbodyHtml = '<tr>' +
            '<td style="border:1px solid #cbd5e1; padding:6px;">' + mdStr + '</td>' +
            '<td style="border:1px solid #cbd5e1; padding:6px;"><strong>' + itemVal + '</strong></td>' +
            '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;">1</td>' +
            '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;">' +
            fmt(supAmt) + '</td>' +
            '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;">' +
            fmt(vatAmt) + '</td>' +
            '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;"><strong>' +
            fmt(totAmt) + '</strong></td>' +
            '</tr>';
        }}
        slipEl.innerHTML =
          '<div class="slip-title" style="color:#1e3a8a;">[ 거 래 명 세 서 ] (공급받는자 보관용)</div>' +
          '<div class="slip-row"><span>거래일시</span><strong>' + d.transacted_at + '</strong></div>' +
          '<div class="slip-row"><span>전표/승인번호</span><span>#' + d.tx_id + ' / ' + appNo + '</span></div>' +
          '<div class="slip-divider"></div>' +
          '<div class="slip-row"><span>공급자 상호</span><strong>' + d.raw_merchant_name + '</strong></div>' +
          '<div class="slip-row"><span>사업자번호</span><span>' + bizNo + '</span></div>' +
          '<div class="slip-row"><span>결제수단</span><strong>' + d.card_label + '</strong></div>' +
          '<div class="slip-row"><span>회계계정</span><strong>' +
          d.account_code + ' ' + d.account_name + '</strong></div>' +
          '<div class="slip-divider"></div>' +
          '<table style="width:100%; border-collapse:collapse; font-size:12px; margin:8px 0;">' +
          '<thead><tr style="background:#eff6ff;">' +
          '<th style="border:1px solid #cbd5e1; padding:5px;">월/일</th>' +
          '<th style="border:1px solid #cbd5e1; padding:5px;">품목 및 규격</th>' +
          '<th style="border:1px solid #cbd5e1; padding:5px; text-align:right;">수량</th>' +
          '<th style="border:1px solid #cbd5e1; padding:5px; text-align:right;">VAT 제외 가격</th>' +
          '<th style="border:1px solid #cbd5e1; padding:5px; text-align:right;">VAT 가격</th>' +
          '<th style="border:1px solid #cbd5e1; padding:5px; text-align:right;">합계 가격</th>' +
          '</tr></thead><tbody>' + tbodyHtml + '</tbody>' +
          '<tfoot><tr style="background:#f8fafc; font-weight:700;">' +
          '<td colspan="2" style="border:1px solid #cbd5e1; padding:6px; text-align:center;">합 계</td>' +
          '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;">' + totQty + '</td>' +
          '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;">' +
          fmt(supAmt) + '</td>' +
          '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right;">' +
          fmt(vatAmt) + '</td>' +
          '<td style="border:1px solid #cbd5e1; padding:6px; text-align:right; color:#1e3a8a;">' +
          fmt(totAmt) + '</td>' +
          '</tr></tfoot></table>' +
          '<div class="slip-row"><span>VAT 제외 가격 (공급가액)</span><strong>' +
          fmt(supAmt) + '</strong></div>' +
          '<div class="slip-row"><span>VAT 가격 (부가세액)</span><strong>' +
          fmt(vatAmt) + ' (' + d.vat_deduction_type + ')</strong></div>' +
          '<div class="slip-row slip-total"><span>합계 가격 (총 결제금액)</span><span>' +
          fmt(totAmt) + '</span></div>' +
          '<div class="slip-divider"></div>' +
          '<div class="no-print" style="background:#f8fafc; border:1px solid #cbd5e1; ' +
          'border-radius:6px; padding:10px; margin:8px 0;">' +
          '<div style="font-weight:700; font-size:12px; margin-bottom:6px; color:#1e3a8a;">' +
          '✏️ 거래명세서 품목·규격 입력 및 전자 거래명세서 발급</div>' +
          '<div style="display:flex; gap:6px; flex-wrap:wrap;">' +
          '<input type="text" id="modal-stmt-item" value="' + itemVal + '" ' +
          'style="flex:1; min-width:180px;" placeholder="품목명 또는 거래 내역 입력" />' +
          '<button onclick="saveStatementForActiveTx()">💾 품목 반영 · 명세서 저장</button>' +
          '</div></div>' +
          '<div class="slip-row"><span>보관경로</span><code style="font-size:11px;">' +
          arch + '</code></div>' +
          '<div class="slip-row no-print"><span>PDF 저장명</span>' +
          '<code style="font-size:11px; color:#1d4ed8;">' + activeModalPrintTitle + '.pdf</code></div>';
      }} else {{
        tabSlip.className = '';
        tabStmt.className = 'secondary';
        activeModalPrintTitle = buildReceiptPrintTitle(d);
        document.title = activeModalPrintTitle;
        document.getElementById('modal-header-title').textContent =
          '🧾 신용카드 매출전표 (#' + d.tx_id + ')';
        slipEl.innerHTML =
          '<div class="slip-title">[신용카드 매출전표 / 적격증빙]</div>' +
          '<div class="slip-row"><span>카드종류</span><strong>' + d.card_label + '</strong></div>' +
          '<div class="slip-row"><span>거래일시</span><strong>' + d.transacted_at + '</strong></div>' +
          '<div class="slip-row"><span>승인번호</span><span>' + appNo + '</span></div>' +
          '<div class="slip-divider"></div>' +
          '<div class="slip-row"><span>가맹점명</span><strong>' + d.raw_merchant_name + '</strong></div>' +
          '<div class="slip-row"><span>사업자번호</span><span>' + bizNo + '</span></div>' +
          '<div class="slip-divider"></div>' +
          '<div class="slip-row"><span>공급가액</span><span>' + fmt(d.supply_value) + '</span></div>' +
          '<div class="slip-row"><span>부가세액(VAT)</span><span>' + fmt(d.vat_amount) +
          ' (' + d.vat_deduction_type + ')</span></div>' +
          '<div class="slip-row slip-total"><span>합계금액</span><span>' +
          fmt(d.total_amount) + '</span></div>' +
          '<div class="slip-divider"></div>' +
          '<div class="slip-row"><span>회계계정</span><strong>' +
          d.account_code + ' ' + d.account_name + '</strong></div>' +
          '<div class="slip-row"><span>적요/품목</span><span>' + memo + '</span></div>' +
          '<div class="slip-row"><span>대사상태</span><span>' + d.reconcile_status + '</span></div>' +
          '<div class="slip-row"><span>보관경로</span><code style="font-size:11px;">' +
          arch + '</code></div>' +
          '<div class="slip-row no-print"><span>PDF 저장명</span>' +
          '<code style="font-size:11px; color:#1d4ed8;">' + activeModalPrintTitle + '.pdf</code></div>';
      }}
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

      const res = await fetch('/api/v1/transactions/' + txId + '/receipt');
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
      const q = '?start_date=' + encodeURIComponent(s)
              + '&end_date=' + encodeURIComponent(e)
              + '&card_last4=' + encodeURIComponent(card);
      window.location.href = '/dashboard' + q;
    }}
    function filterByCard(cardCode) {{
      const s = document.getElementById('start-date').value;
      const e = document.getElementById('end-date').value;
      const q = '?start_date=' + encodeURIComponent(s)
              + '&end_date=' + encodeURIComponent(e)
              + '&card_last4=' + encodeURIComponent(cardCode);
      window.location.href = '/dashboard' + q;
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
    function scanEmailAccountByIdx(idx) {{
      const acc = cachedEmailAccounts[idx];
      if (!acc) return;
      triggerEmailSyncNow(false, acc.account_id);
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
          '<div style="font-size:11px; color:#475569; margin-top:2px;">' + stInfo + '</div>' +
          '</div>' +
          '<div style="display:flex; gap:4px; flex-wrap:wrap;">' +
          '<button class="secondary" style="padding:3px 7px; font-size:11px;" ' +
          'onclick="scanEmailAccountByIdx(' + idx + ')">스캔</button>' +
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
    async function refreshEmailWatcherState() {{
      const res = await fetch('/api/v1/scraper/email-config');
      if (!res.ok) return null;
      const cfg = await res.json();
      renderEmailAccountsList(cfg);
      document.getElementById('email-lookback-days').value = cfg.lookback_days || 60;
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
        lookback_days: parseInt(document.getElementById('email-lookback-days').value || '60', 10),
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
          lookback_days: parseInt(document.getElementById('email-lookback-days').value || '60', 10),
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
    async function triggerEmailSyncNow(forceRescan, accountId) {{
      const st = document.getElementById('email-sync-status');
      st.style.color = '#1e40af';
      st.textContent = accountId
        ? '⏳ 선택한 이메일 계정의 주문·결제 메일을 스캔 중입니다...'
        : '⏳ 연결된 모든 활성 이메일 계정을 통합 스캔 중입니다...';
      const res = await fetch('/api/v1/scraper/email-sync', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{ force_rescan: !!forceRescan, account_id: accountId || '' }})
      }});
      const sr = await res.json();
      await refreshEmailWatcherState();
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
    document.addEventListener('keydown', function(ev) {{
      if (ev.key === 'Escape') {{
        closeReceiptModal();
        closeCoupangModal();
        closeEmailOrderModal();
      }}
    }});
  </script>
</body>
</html>"""
