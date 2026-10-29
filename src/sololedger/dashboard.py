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
    "813": "기업업무추진비",
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
        SELECT DISTINCT SUBSTR(transacted_at, 1, 4) || SUBSTR(transacted_at, 6, 2) AS ym
        FROM card_transactions
        WHERE LENGTH(transacted_at) >= 7
        ORDER BY ym DESC
        """
    )
    available_months = [r[0] for r in cursor.fetchall() if r[0]]
    current_ym = now.strftime("%Y%m")
    if current_ym not in available_months:
        available_months.insert(0, current_ym)

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
        key=lambda x: (x["tx_count"], x["total_expenditure"]),
        reverse=True,
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
            f"<td><button onclick='saveTxMemo({tx_id})'>저장</button></td>"
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
    header {{ display: flex; justify-content: space-between; align-items: center;
              margin-bottom: 24px; border-bottom: 2px solid #e2e8f0; padding-bottom: 12px; }}
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
    .slip-row {{ display: flex; justify-content: space-between; margin: 4px 0; }}
    .slip-divider {{ border-top: 1px dashed #94a3b8; margin: 8px 0; }}
    .slip-total {{ font-size: 17px; font-weight: 800; color: #1d4ed8; }}
    .receipt-img-wrap {{ text-align: center; margin-top: 10px; background: #f8fafc;
                         border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px; }}
    .receipt-img-wrap img {{ max-width: 100%; max-height: 360px; border-radius: 6px; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>SoloLedger 경비·세무증빙 대시보드 (v{__version__})
        <span class="badge env">{html.escape(summary['app_env'].upper())} MODE</span>
      </h1>
      <p>조회 기간:
         <input type="date" id="start-date" value="{s_date}" />
         ~
         <input type="date" id="end-date" value="{e_date}" />
         <button onclick="applyFilters()">기간 조회</button>
         <button class="secondary" onclick="setQuickRange('THIS_MONTH')">이번 달</button>
         <button class="secondary" onclick="setQuickRange('3M')">최근 3개월</button>
         <button class="secondary" onclick="setQuickRange('THIS_YEAR')">올해 전체</button>
         <button class="secondary" onclick="setQuickRange('ALL')">전체 기간</button>
         | 카드 선택:
         <select id="card-filter" onchange="applyFilters()">
           {card_select_html}
         </select>
         | 귀속 연도: <strong>{summary['year']}</strong>
         | DB: <code>{html.escape(Path(summary['db_path']).name)}</code></p>
    </div>
    <div>
      <input type="file" id="receipt-file-input" accept="image/*,.pdf"
             style="display:none" onchange="uploadReceiptFile(this)" />
      <input type="file" id="csv-file-input" accept=".csv,.txt"
             style="display:none" onchange="uploadCsvFile(this)" />
      <button class="secondary" onclick="window.open('/manual', '_blank')">사용 매뉴얼(HTML)</button>
      <button onclick="reapplyRules()">스마트 자동분류 실행</button>
      <button onclick="document.getElementById('receipt-file-input').click()">영수증 업로드(OCR)</button>
      <button onclick="document.getElementById('csv-file-input').click()">카드사/홈택스 CSV 업로드</button>
      <button onclick="importPhoneSms()">USB 휴대폰 문자 가져오기</button>
      <button class="secondary" onclick="seedDemoData()">샘플 데이터 생성</button>
      <button class="danger" onclick="resetTestLedger()">테스트 DB 초기화</button>
      <button onclick="backupToNas()">NAS(Z:\\) 백업 동기화</button>
      <button onclick="exportTaxPack('{summary['year_month']}')">세무사 제출용 ZIP+CSV 생성</button>
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
    <div class="receipt-modal">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
        <h3 id="modal-header-title" style="margin:0;">🧾 신용카드 매출전표 / 영수증</h3>
        <button class="secondary" onclick="closeReceiptModal()">✕ 닫기</button>
      </div>
      <div id="modal-slip-content" class="slip-box">불러오는 중...</div>
      <div id="modal-image-content" class="receipt-img-wrap"></div>
      <input type="file" id="modal-receipt-input" accept="image/*,.pdf"
             style="display:none" onchange="uploadReceiptForActiveTx(this)" />
      <div style="display:flex; justify-content:flex-end; gap:6px; margin-top:14px;">
        <button onclick="document.getElementById('modal-receipt-input').click()">
          📷 이 전표에 영수증 첨부
        </button>
        <button class="secondary" onclick="window.print()">🖨️ 전표 인쇄</button>
        <button class="secondary" onclick="closeReceiptModal()">닫기</button>
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
    let activeModalTxId = null;

    async function openReceiptModal(txId) {{
      activeModalTxId = txId;
      const backdrop = document.getElementById('receipt-modal-backdrop');
      const slipEl = document.getElementById('modal-slip-content');
      const imgEl = document.getElementById('modal-image-content');
      backdrop.classList.add('open');
      slipEl.innerHTML = '영수증 정보를 불러오는 중...';
      imgEl.innerHTML = '';

      const res = await fetch('/api/v1/transactions/' + txId + '/receipt');
      if (!res.ok) {{
        slipEl.innerHTML = '전표 정보를 찾을 수 없습니다.';
        return;
      }}
      const d = await res.json();
      document.getElementById('modal-header-title').textContent =
        '🧾 신용카드 매출전표 (#' + d.tx_id + ')';

      const fmt = (n) => Number(n || 0).toLocaleString('ko-KR') + '원';
      const bizNo = d.biz_reg_no || '미등록 (SMS/RCS 수집 전표)';
      const appNo = d.approval_no || '문자 승인전표';
      const memo = d.counterparty_info || '-';
      const arch = d.archived_file_name
        ? ('by_card/' + d.card_folder + '/' + d.archived_file_name)
        : '실물 영수증 미첨부 (전자 전표)';

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
        arch + '</code></div>';

      if (d.has_receipt_file && d.is_renderable_image) {{
        imgEl.innerHTML =
          '<div style="margin-bottom:6px; font-weight:600; font-size:12px;">첨부된 실물 영수증 이미지</div>' +
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
          '실물 영수증 사진이 아직 첨부되지 않았습니다.<br/>' +
          '하단 <b>[📷 이 전표에 영수증 첨부]</b> 버튼으로 영수증/쇼핑몰 캡처를 등록할 수 있습니다.' +
          '</div>';
      }}
    }}

    function closeReceiptModal() {{
      document.getElementById('receipt-modal-backdrop').classList.remove('open');
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
      const reader = new FileReader();
      reader.onload = async function(ev) {{
        const dataUrl = String(ev.target.result || '');
        const b64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
        await fetch('/api/v1/imports/hometax-csv', {{
          method: 'POST',
          headers: API_HEADERS,
          body: JSON.stringify({{ csv_content_b64: b64 }})
        }});
        window.location.reload();
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
  </script>
</body>
</html>"""
