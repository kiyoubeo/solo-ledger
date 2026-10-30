"""Self-hosted Card Sales Slip & E-Commerce Order Scraping Engine for SoloLedger.

Replaces paid third-party scraping relays (Barobill, Popbill, CODEF) with:
1. Multi-format Card Statement & Hometax Scraper Parser (JSON XHR, HTML Table/.xls, CSV/TSV)
   supporting all 7 major Korean card issuers (Woori, KB, Samsung, Hyundai, Shinhan, Lotte, Hana),
   Hometax (hometax.go.kr), and Credit Finance Association (cardsales.or.kr).
2. E-Commerce Order Item Matcher (Coupang, NaverPay, KakaoPay, 11st) that resolves PG item memos.
3. Official Electronic Credit Card Sales Slip (매출전표 영수증) Batch Generator saving per-card
   receipt files under archive/normalized/by_card/<카드명>/.
4. Optional Playwright / Chrome CDP Browser Session Collector.
"""

import csv
import html
import io
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger.archiver import (
    build_archived_filename,
    build_receipt_print_title,
    compute_sha256_hex,
    save_receipt_files,
)
from sololedger.config import settings
from sololedger.importers import (
    _extract_card_last4,
    _map_tax_type_and_status,
    _normalize_datetime_str,
    _parse_int_amount,
    _pick_col,
    parse_hometax_csv,
)
from sololedger.models import ReconcileItem
from sololedger.reconciler import reconcile_single_item
from sololedger.sms_parser import format_card_label, sanitize_card_folder_name
from sololedger.tax_engine import calculate_supply_and_vat
from sololedger.telegram_bot import (
    _detect_memo_account_override,
    _refresh_receipt_archived_filename,
)

SUPPORTED_SCRAPER_TARGETS: Dict[str, Dict[str, str]] = {
    "HOMETAX": {
        "name": "국세청 홈택스 (사업용신용카드 통합조회)",
        "url": "https://www.hometax.go.kr",
        "type": "CARD_SLIP",
    },
    "CARDSALES": {
        "name": "여신금융협회 (카드매출·이용내역 통합조회)",
        "url": "https://www.cardsales.or.kr",
        "type": "CARD_SLIP",
    },
    "WOORI": {
        "name": "우리카드 (승인·매입 매출전표)",
        "url": "https://pc.wooricard.com",
        "type": "CARD_SLIP",
    },
    "KB": {
        "name": "KB국민카드 (승인·매입 매출전표)",
        "url": "https://card.kbcard.com",
        "type": "CARD_SLIP",
    },
    "SAMSUNG": {
        "name": "삼성카드 (승인·매입 매출전표)",
        "url": "https://www.samsungcard.com",
        "type": "CARD_SLIP",
    },
    "HYUNDAI": {
        "name": "현대카드 (승인·매입 매출전표)",
        "url": "https://www.hyundaicard.com",
        "type": "CARD_SLIP",
    },
    "SHINHAN": {
        "name": "신한카드 (승인·매입 매출전표)",
        "url": "https://www.shinhancard.com",
        "type": "CARD_SLIP",
    },
    "LOTTE": {
        "name": "롯데카드 (승인·매입 매출전표)",
        "url": "https://www.lottecard.co.kr",
        "type": "CARD_SLIP",
    },
    "HANA": {
        "name": "하나카드 (승인·매입 매출전표)",
        "url": "https://www.hanacard.co.kr",
        "type": "CARD_SLIP",
    },
    "COUPANG": {
        "name": "쿠팡 (주문목록 상품명·영수증 수집)",
        "url": "https://mc.coupang.com/ssr/desktop/order/list",
        "type": "ECOMMERCE_ORDER",
    },
    "NAVERPAY": {
        "name": "네이버페이 (결제내역 상품명·영수증 수집)",
        "url": "https://order.pay.naver.com/home",
        "type": "ECOMMERCE_ORDER",
    },
}

_HTML_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HTML_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_tags(raw_html: str) -> str:
    text = _HTML_TAG_RE.sub(" ", raw_html or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def extract_rows_from_html_table(html_text: str) -> List[Dict[str, str]]:
    """Extract header-mapped row dictionaries from HTML <table> (used by card issuer .xls/DOM)."""
    tr_matches = _HTML_TR_RE.findall(html_text or "")
    if not tr_matches:
        return []

    header_cols: List[str] = []
    data_rows: List[Dict[str, str]] = []
    header_keywords = ("일자", "일시", "가맹점", "상호", "금액", "승인", "카드", "상품", "주문")

    for tr_inner in tr_matches:
        cells = [_strip_html_tags(c) for c in _HTML_CELL_RE.findall(tr_inner)]
        if not cells or all(not c for c in cells):
            continue
        if not header_cols:
            joined = " ".join(cells)
            if any(k in joined for k in header_keywords):
                header_cols = cells
                continue
        if header_cols and len(cells) >= 3:
            row_dict: Dict[str, str] = {}
            for idx, col_name in enumerate(header_cols):
                if idx < len(cells) and col_name:
                    row_dict[col_name] = cells[idx]
            if row_dict:
                data_rows.append(row_dict)

    return data_rows


def _row_dict_to_reconcile_item(row: Dict[str, Any]) -> Optional[ReconcileItem]:
    """Convert a normalized or raw dictionary (JSON/HTML/CSV) from any Korean card issuer."""
    raw_dt = _pick_col(
        row,
        [
            "거래일시",
            "승인일시",
            "이용일시",
            "결제일시",
            "transacted_at",
            "trnsdt",
            "aprvdt",
            "usedate",
        ],
    )
    if not raw_dt:
        d_part = _pick_col(
            row,
            ["거래일자", "승인일자", "이용일자", "결제일자", "일자", "aprvdy", "trnsdy"],
        )
        t_part = _pick_col(
            row,
            ["거래시간", "승인시간", "이용시간", "결제시간", "시간", "aprvtm", "trnstm"],
            "12:00:00",
        )
        if d_part and len(re.sub(r"\D", "", d_part)) == 8:
            digits = re.sub(r"\D", "", d_part)
            d_part = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        if t_part and len(re.sub(r"\D", "", t_part)) == 6:
            td = re.sub(r"\D", "", t_part)
            t_part = f"{td[:2]}:{td[2:4]}:{td[4:6]}"
        raw_dt = f"{d_part} {t_part}".strip()

    if not raw_dt:
        return None

    transacted_at = _normalize_datetime_str(raw_dt)
    if len(transacted_at) < 10 or not transacted_at[:4].isdigit():
        return None

    card_raw = _pick_col(
        row,
        ["카드번호", "카드뒷자리", "카드명", "이용카드", "card_last4", "cardno", "cardnum"],
        "0000",
    )
    card_last4 = _extract_card_last4(card_raw)
    approval_no = (
        _pick_col(row, ["승인번호", "approval_no", "aprvno", "authno", "confirmno"]) or None
    )
    merchant_name = _pick_col(
        row,
        [
            "가맹점명",
            "가맹점상호",
            "이용가맹점",
            "가맹점",
            "상호",
            "merchant_name",
            "mrchnm",
            "storename",
        ],
        "미상가맹점",
    )
    biz_raw = _pick_col(
        row,
        [
            "사업자등록번호",
            "가맹점사업자번호",
            "사업자번호",
            "biz_reg_no",
            "mrchbzno",
            "bizno",
            "corpregno",
        ],
    )
    biz_reg_no = re.sub(r"\D", "", biz_raw) or None
    if biz_reg_no and len(biz_reg_no) != 10:
        biz_reg_no = None

    total_str = _pick_col(
        row,
        [
            "합계금액",
            "합계",
            "결제금액",
            "승인금액",
            "이용금액",
            "매입금액",
            "국내이용금액",
            "total_amount",
            "aprvamt",
            "totamt",
        ],
    )
    if total_str:
        total_amount = _parse_int_amount(total_str)
    else:
        supply = _parse_int_amount(
            _pick_col(row, ["공급가액", "공급가", "supply_value", "splyamt"])
        )
        vat = _parse_int_amount(
            _pick_col(row, ["부가세", "부가가치세", "세액", "vat_amount", "vatamt"])
        )
        total_amount = supply + vat

    if total_amount == 0:
        return None

    cancel_hint = _pick_col(
        row, ["취소여부", "거래구분", "승인구분", "매입상태", "상태", "is_cancel", "cncllyn"]
    )
    is_cancel = (
        ("취소" in cancel_hint)
        or (cancel_hint.lower() in ("true", "y", "cancel", "canceled"))
        or (total_amount < 0)
    )
    if is_cancel and total_amount > 0:
        total_amount = -total_amount

    tax_type_raw = _pick_col(
        row,
        ["과세유형", "가맹점유형", "사업자유형", "공제여부", "업종", "tax_type_code", "taxtype"],
        "일반과세자",
    )
    tax_type_code, biz_status = _map_tax_type_and_status(tax_type_raw)

    return ReconcileItem(
        card_last4=card_last4,
        approval_no=approval_no,
        transacted_at=transacted_at,
        merchant_name=merchant_name,
        biz_reg_no=biz_reg_no,
        total_amount=total_amount,
        tax_type_code=tax_type_code,
        biz_status=biz_status,
        reconcile_source="CANCELED" if is_cancel else "HOMETAX_VERIFIED",
        is_cancel=is_cancel,
    )


def parse_scraped_card_statement(raw_content: str) -> List[ReconcileItem]:
    """Auto-detect and parse JSON XHR payload, HTML table (.xls/DOM), or CSV from any card issuer."""
    content = (raw_content or "").strip()
    if not content:
        return []

    # 1. JSON XHR / API response format
    if content.startswith("{") or content.startswith("["):
        try:
            parsed_json = json.loads(content)
            rows_list: List[Dict[str, Any]] = []
            if isinstance(parsed_json, list):
                rows_list = [r for r in parsed_json if isinstance(r, dict)]
            elif isinstance(parsed_json, dict):
                for key in ("items", "data", "list", "rows", "result", "cardLogList"):
                    val = parsed_json.get(key)
                    if isinstance(val, list):
                        rows_list = [r for r in val if isinstance(r, dict)]
                        break
                if not rows_list:
                    rows_list = [parsed_json]
            items = []
            for r in rows_list:
                item = _row_dict_to_reconcile_item(r)
                if item is not None:
                    items.append(item)
            return items
        except Exception:
            pass

    # 2. HTML Table format (Card issuer .xls download or scraped DOM table)
    if "<table" in content.lower() or "<tr" in content.lower():
        html_rows = extract_rows_from_html_table(content)
        items = []
        for r in html_rows:
            item = _row_dict_to_reconcile_item(r)
            if item is not None:
                items.append(item)
        if items:
            return items

    # 3. Multi-issuer CSV / TSV format (skip preamble lines before header row if present)
    lines = [ln for ln in content.splitlines() if ln.strip()]
    header_idx = 0
    for idx, ln in enumerate(lines[:15]):
        if any(k in ln for k in ("일자", "일시", "가맹점", "상호", "승인번호", "transacted_at")):
            header_idx = idx
            break
    trimmed_csv = "\n".join(lines[header_idx:])
    delimiter = "\t" if "\t" in lines[header_idx] and "," not in lines[header_idx] else ","
    reader = csv.DictReader(io.StringIO(trimmed_csv), delimiter=delimiter)
    items = []
    for r in reader:
        item = _row_dict_to_reconcile_item(r)
        if item is not None:
            items.append(item)
    if items:
        return items

    return parse_hometax_csv(trimmed_csv)


def reconcile_scraped_ecommerce_orders(
    cursor: sqlite3.Cursor,
    raw_content: str,
) -> Dict[str, Any]:
    """Match scraped Coupang / NaverPay / KakaoPay order items against PG card transactions."""
    content = (raw_content or "").strip()
    if not content:
        return {"status": "empty", "matched_count": 0, "updated_tx_ids": []}

    order_rows: List[Dict[str, Any]] = []
    if content.startswith("{") or content.startswith("["):
        try:
            data = json.loads(content)
            if isinstance(data, list):
                order_rows = [r for r in data if isinstance(r, dict)]
            elif isinstance(data, dict):
                for k in ("orders", "items", "data", "list"):
                    if isinstance(data.get(k), list):
                        order_rows = [r for r in data[k] if isinstance(r, dict)]
                        break
        except Exception:
            pass
    elif "<tr" in content.lower():
        order_rows = extract_rows_from_html_table(content)
    else:
        reader = csv.DictReader(io.StringIO(content))
        order_rows = list(reader)

    updated_tx_ids: List[int] = []
    for row in order_rows:
        raw_dt = _pick_col(
            row, ["주문일시", "주문일자", "결제일시", "결제일자", "일자", "order_date", "date"]
        )
        item_name = _pick_col(
            row, ["상품명", "품목명", "주문상품", "상품", "내역", "item_name", "product_name", "title"]
        )
        amt_str = _pick_col(
            row, ["결제금액", "주문금액", "합계금액", "금액", "total_amount", "amount", "price"]
        )
        if not raw_dt or not item_name or not amt_str:
            continue

        norm_dt = _normalize_datetime_str(raw_dt)
        date_prefix = norm_dt[:10]
        amount = abs(_parse_int_amount(amt_str))
        if amount <= 0 or len(date_prefix) < 10:
            continue

        cursor.execute(
            """
            SELECT * FROM card_transactions
            WHERE total_amount = ?
              AND ABS(julianday(SUBSTR(transacted_at, 1, 10)) - julianday(?)) <= 2.5
              AND reconcile_status != 'CANCELED'
            ORDER BY ABS(julianday(SUBSTR(transacted_at, 1, 10)) - julianday(?)) ASC, tx_id DESC
            LIMIT 1
            """,
            (amount, date_prefix, date_prefix),
        )
        tx_row = cursor.fetchone()
        if tx_row is None:
            continue

        tx_id = int(tx_row["tx_id"])
        acc_code = str(tx_row["account_code"])
        acc_name = str(tx_row["account_name"])
        vat_type = str(tx_row["vat_deduction_type"])
        reason = tx_row["non_deduct_reason"]

        inferred = _detect_memo_account_override(item_name)
        if inferred:
            inf_code, inf_name, force_non_ded, inf_reason = inferred
            if not (acc_code == "830" and inf_code == "212" and amount < 300_000):
                acc_code = inf_code
                acc_name = inf_name
                vat_type = "NON_DEDUCTIBLE" if force_non_ded else "DEDUCTIBLE"
                reason = inf_reason
        elif acc_code == "999":
            acc_code = "830"
            acc_name = "소모품비"
            vat_type = "DEDUCTIBLE"

        sup, vat = calculate_supply_and_vat(amount, vat_type)
        cursor.execute(
            """
            UPDATE card_transactions
            SET account_code = ?,
                account_name = ?,
                vat_deduction_type = ?,
                non_deduct_reason = ?,
                supply_value = ?,
                vat_amount = ?,
                counterparty_info = ?,
                reconcile_status = CASE
                    WHEN reconcile_status = 'SMS_ONLY' THEN 'OCR_MATCHED'
                    ELSE reconcile_status
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE tx_id = ?
            """,
            (acc_code, acc_name, vat_type, reason, sup, vat, item_name, tx_id),
        )
        _refresh_receipt_archived_filename(cursor, tx_id)
        updated_tx_ids.append(tx_id)

    return {
        "status": "matched",
        "parsed_orders_count": len(order_rows),
        "matched_count": len(updated_tx_ids),
        "updated_tx_ids": updated_tx_ids,
    }


def render_electronic_slip_html(tx_row: Dict[str, Any]) -> str:
    """Render a standalone printable Korean Electronic Credit Card Sales Slip (매출전표 영수증)."""
    c_code = str(tx_row.get("card_last4") or "0000")
    c_label = html.escape(format_card_label(c_code))
    tx_id = int(tx_row["tx_id"])
    t_dt = html.escape(str(tx_row["transacted_at"]))
    app_no = html.escape(str(tx_row.get("approval_no") or f"SMS-{tx_id:06d}"))
    merchant = html.escape(str(tx_row["raw_merchant_name"]))
    biz_no = html.escape(str(tx_row.get("biz_reg_no") or "문자수집(추후대사)"))
    total_amt = int(tx_row["total_amount"])
    supply_val = int(tx_row["supply_value"])
    vat_amt = int(tx_row["vat_amount"])
    acc_str = html.escape(f"{tx_row['account_code']} {tx_row['account_name']}")
    ded_str = html.escape(str(tx_row["vat_deduction_type"]))
    memo_str = html.escape(str(tx_row.get("counterparty_info") or "-"))
    rec_status = html.escape(str(tx_row.get("reconcile_status") or "SMS_ONLY"))
    print_title = html.escape(
        build_receipt_print_title(
            tx_id=tx_id,
            transacted_at=str(tx_row["transacted_at"]),
            card_last4=c_code,
            merchant_name=str(tx_row["raw_merchant_name"]),
            account_name=str(tx_row["account_name"]),
            total_amount=total_amt,
        )
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>{print_title}</title>
  <style>
    body {{ font-family: 'Malgun Gothic', -apple-system, sans-serif; background: #f1f5f9;
           padding: 24px; color: #0f172a; }}
    .slip {{ max-width: 400px; margin: 0 auto; background: #fffef8; border: 1px solid #94a3b8;
             border-radius: 8px; padding: 20px; box-shadow: 0 6px 18px rgba(0,0,0,0.08); }}
    h1 {{ text-align: center; font-size: 18px; margin: 0 0 12px 0;
          padding-bottom: 10px; border-bottom: 2px double #475569; }}
    .row {{ display: flex; justify-content: space-between; margin: 6px 0; font-size: 13px; }}
    .hr {{ border-top: 1px dashed #94a3b8; margin: 10px 0; }}
    .tot {{ font-size: 17px; font-weight: 800; color: #1d4ed8; }}
    .foot {{ margin-top: 14px; font-size: 11px; color: #64748b; text-align: center; }}
  </style>
</head>
<body>
  <div class="slip">
    <h1>신용카드 매출전표 (적격증빙)</h1>
    <div class="row"><span>전표번호</span><strong>#{tx_id}</strong></div>
    <div class="row"><span>카드종류</span><strong>{c_label}</strong></div>
    <div class="row"><span>거래일시</span><strong>{t_dt}</strong></div>
    <div class="row"><span>승인번호</span><span>{app_no}</span></div>
    <div class="hr"></div>
    <div class="row"><span>가맹점명</span><strong>{merchant}</strong></div>
    <div class="row"><span>사업자번호</span><span>{biz_no}</span></div>
    <div class="hr"></div>
    <div class="row"><span>공급가액</span><span>{supply_val:,}원</span></div>
    <div class="row"><span>부가세액(VAT)</span><span>{vat_amt:,}원 ({ded_str})</span></div>
    <div class="row tot"><span>합계금액</span><span>{total_amt:,}원</span></div>
    <div class="hr"></div>
    <div class="row"><span>계정과목</span><strong>{acc_str}</strong></div>
    <div class="row"><span>적요/품목</span><span>{memo_str}</span></div>
    <div class="row"><span>증빙상태</span><span>{rec_status}</span></div>
    <div class="foot">SoloLedger 전자 매출전표 아카이브 (부가가치세법 제46조 제3항 준거)</div>
  </div>
</body>
</html>"""


def generate_electronic_slips_for_ledger(
    cursor: sqlite3.Cursor,
    archive_dir: Optional[Path] = None,
    force_regenerate: bool = False,
) -> Dict[str, Any]:
    """Batch-generate per-card electronic sales slip receipt files (.html) for ledger transactions."""
    if force_regenerate:
        cursor.execute(
            """
            SELECT t.*
            FROM card_transactions t
            WHERE t.reconcile_status != 'CANCELED'
            ORDER BY t.transacted_at DESC, t.tx_id DESC
            """
        )
    else:
        cursor.execute(
            """
            SELECT t.*
            FROM card_transactions t
            LEFT JOIN receipt_ocr_metadata r ON r.tx_id = t.tx_id
            WHERE t.reconcile_status != 'CANCELED'
              AND r.receipt_id IS NULL
            ORDER BY t.transacted_at DESC, t.tx_id DESC
            """
        )

    rows = [dict(r) for r in cursor.fetchall()]
    generated_count = 0
    by_card_counts: Dict[str, int] = {}

    for tx in rows:
        tx_id = int(tx["tx_id"])
        c_code = str(tx.get("card_last4") or "0000")
        c_folder = sanitize_card_folder_name(c_code)
        slip_html = render_electronic_slip_html(tx)
        slip_bytes = slip_html.encode("utf-8")
        file_hash = compute_sha256_hex(slip_bytes)

        cursor.execute(
            "SELECT receipt_id FROM receipt_ocr_metadata WHERE file_hash_sha256 = ?",
            (file_hash,),
        )
        if cursor.fetchone():
            continue

        orig_name = f"card_slip_tx_{tx_id}.html"
        arch_name = build_archived_filename(
            transacted_at=str(tx["transacted_at"]),
            merchant_name=str(tx["raw_merchant_name"]),
            account_name=str(tx["account_name"]),
            total_amount=int(tx["total_amount"]),
            item_summary=tx.get("counterparty_info"),
            original_file_name=orig_name,
        )
        orig_path, _ = save_receipt_files(
            slip_bytes,
            original_file_name=orig_name,
            archived_file_name=arch_name,
            archive_dir=archive_dir,
            card_last4=c_code,
        )
        cursor.execute(
            """
            INSERT INTO receipt_ocr_metadata (
                tx_id, source_channel, original_file_path, archived_file_name,
                file_hash_sha256, ocr_biz_reg_no, ocr_total_amount,
                line_items_json, raw_ocr_response, ocr_confidence
            ) VALUES (?, 'AUTO_SLIP', ?, ?, ?, ?, ?, '[]', '{"type":"ELECTRONIC_CARD_SLIP"}', 1.0)
            """,
            (
                tx_id,
                orig_path,
                arch_name,
                file_hash,
                tx.get("biz_reg_no"),
                int(tx["total_amount"]),
            ),
        )
        generated_count += 1
        by_card_counts[c_folder] = by_card_counts.get(c_folder, 0) + 1

    return {
        "status": "generated",
        "generated_slips_count": generated_count,
        "by_card_counts": by_card_counts,
        "archive_by_card_dir": str((archive_dir or settings.archive_dir) / "normalized" / "by_card"),
    }


def ingest_scraped_payload(
    cursor: sqlite3.Cursor,
    target_portal: str,
    raw_content: str,
    auto_generate_slips: bool = True,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Ingest scraped data from a card issuer, Hometax, or e-commerce portal into the ledger."""
    portal_key = (target_portal or "HOMETAX").strip().upper()
    portal_info = SUPPORTED_SCRAPER_TARGETS.get(portal_key, SUPPORTED_SCRAPER_TARGETS["HOMETAX"])

    if portal_info["type"] == "ECOMMERCE_ORDER":
        ecom_res = reconcile_scraped_ecommerce_orders(cursor, raw_content)
        slip_res = (
            generate_electronic_slips_for_ledger(cursor, archive_dir=archive_dir)
            if auto_generate_slips
            else {"generated_slips_count": 0}
        )
        return {
            "status": "ingested",
            "portal": portal_key,
            "portal_name": portal_info["name"],
            "mode": "ECOMMERCE_ORDER",
            "matched_orders": ecom_res["matched_count"],
            "updated_tx_ids": ecom_res["updated_tx_ids"],
            "generated_slips_count": slip_res["generated_slips_count"],
        }

    items = parse_scraped_card_statement(raw_content)
    results = [reconcile_single_item(cursor, item) for item in items]
    stage_counts: Dict[str, int] = {}
    for r in results:
        st = r["match_stage"]
        stage_counts[st] = stage_counts.get(st, 0) + 1

    slip_res = (
        generate_electronic_slips_for_ledger(cursor, archive_dir=archive_dir)
        if auto_generate_slips
        else {"generated_slips_count": 0}
    )

    return {
        "status": "ingested",
        "portal": portal_key,
        "portal_name": portal_info["name"],
        "mode": "CARD_SLIP",
        "scraped_rows_count": len(results),
        "stage_breakdown": stage_counts,
        "generated_slips_count": slip_res["generated_slips_count"],
    }
