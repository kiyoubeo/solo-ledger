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

import base64
import csv
import html
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import time
import urllib.parse
import urllib.request
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
    _extract_year_hint,
    _map_tax_type_and_status,
    _normalize_datetime_str,
    _parse_int_amount,
    _pick_col,
    parse_hometax_csv,
)
from sololedger.models import ReconcileItem
from sololedger.reconciler import reconcile_single_item
from sololedger.sms_parser import format_card_label, sanitize_card_folder_name
from sololedger.tax_engine import calculate_supply_and_vat, evaluate_tax_rules
from sololedger.telegram_bot import _detect_memo_account_override

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

_HTML_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table\s*>", re.IGNORECASE | re.DOTALL)
_HTML_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr\s*>", re.IGNORECASE | re.DOTALL)
_HTML_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]\s*>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_tags(raw_html: str) -> str:
    text = _HTML_TAG_RE.sub(" ", raw_html or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def extract_rows_from_html_table(html_text: str) -> List[Dict[str, str]]:
    """Extract header-mapped row dictionaries from HTML <table> (used by card issuer .xls/DOM)."""
    raw = html_text or ""
    table_blocks = _HTML_TABLE_RE.findall(raw) if "<table" in raw.lower() else [raw]
    data_rows: List[Dict[str, str]] = []
    header_keywords = ("일자", "일시", "이용일", "가맹점", "상호", "금액", "승인", "카드", "상품", "주문")
    detail_markers = ("가맹점", "상호", "이용하신곳", "승인번호", "상품", "품목")

    for block in table_blocks:
        tr_matches = _HTML_TR_RE.findall(block)
        if not tr_matches:
            continue
        header_cols: List[str] = []
        for tr_inner in tr_matches:
            cells = [_strip_html_tags(c) for c in _HTML_CELL_RE.findall(tr_inner)]
            if not cells or all(not c for c in cells):
                continue
            joined = " ".join(cells)
            is_summary_hdr = (
                ("건수합계" in joined or "금액합계" in joined or "조회기간" in cells[0])
                and not any(m in joined for m in detail_markers)
            )
            if not is_summary_hdr and len(cells) >= 3:
                if not header_cols and any(k in joined for k in header_keywords):
                    header_cols = cells
                    continue
                if "<th" in tr_inner.lower() and sum(1 for k in header_keywords if k in joined) >= 2:
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


def _row_dict_to_reconcile_item(
    row: Dict[str, Any], default_year: Optional[str] = None
) -> Optional[ReconcileItem]:
    """Convert a normalized or raw dictionary (JSON/HTML/CSV) from any Korean card issuer."""
    raw_dt = _pick_col(
        row,
        [
            "거래일시",
            "승인일시",
            "이용일시",
            "결제일시",
            "이용일",
            "transacted_at",
            "trnsdt",
            "aprvdt",
            "usedate",
        ],
    )
    if raw_dt and ":" not in raw_dt:
        t_extra = _pick_col(
            row,
            ["거래시간", "승인시간", "이용시간", "결제시간", "시간", "aprvtm", "trnstm"],
        )
        if t_extra:
            if len(re.sub(r"\D", "", t_extra)) == 6 and ":" not in t_extra:
                td = re.sub(r"\D", "", t_extra)
                t_extra = f"{td[:2]}:{td[2:4]}:{td[4:6]}"
            raw_dt = f"{raw_dt} {t_extra}".strip()
    if not raw_dt:
        d_part = _pick_col(
            row,
            ["거래일자", "승인일자", "이용일자", "결제일자", "일자", "aprvdy", "trnsdy"],
        )
        if d_part:
            t_part = _pick_col(
                row,
                ["거래시간", "승인시간", "이용시간", "결제시간", "시간", "aprvtm", "trnstm"],
                "12:00:00",
            )
            if len(re.sub(r"\D", "", d_part)) == 8:
                digits = re.sub(r"\D", "", d_part)
                d_part = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
            if t_part and len(re.sub(r"\D", "", t_part)) == 6:
                td = re.sub(r"\D", "", t_part)
                t_part = f"{td[:2]}:{td[2:4]}:{td[4:6]}"
            raw_dt = f"{d_part} {t_part}".strip()

    if not raw_dt:
        return None

    row_year = default_year or _extract_year_hint(_pick_col(row, ["결제예정일", "청구일자"]))
    transacted_at = _normalize_datetime_str(raw_dt, default_year=row_year)
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
            "이용하신곳",
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
            "국내이용금액",
            "이용금액",
            "매입금액",
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

    cancel_amt = abs(
        _parse_int_amount(_pick_col(row, ["취소금액", "취소원금", "cancel_amount", "cnclamt"]))
    )
    cancel_hint = _pick_col(
        row,
        ["취소여부", "거래구분", "승인구분", "매입상태", "상태", "결제예정일", "is_cancel", "cncllyn"],
    )
    if 0 < cancel_amt < abs(total_amount):
        total_amount = abs(total_amount) - cancel_amt
        is_cancel = False
    else:
        is_cancel = (
            ("취소" in cancel_hint)
            or (cancel_hint.lower() in ("true", "y", "cancel", "canceled"))
            or (total_amount < 0)
            or (cancel_amt > 0 and cancel_amt >= abs(total_amount))
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

    default_year = _extract_year_hint(content[:400])

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
                item = _row_dict_to_reconcile_item(r, default_year=default_year)
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
            item = _row_dict_to_reconcile_item(r, default_year=default_year)
            if item is not None:
                items.append(item)
        if items:
            return items

    # 3. Multi-issuer CSV / TSV format (skip preamble lines and handle multiline quoted headers)
    first_line = next((ln for ln in content.splitlines() if ln.strip()), "")
    delimiter = "\t" if "\t" in first_line and "," not in first_line else ","
    all_csv_rows = list(csv.reader(io.StringIO(content), delimiter=delimiter))
    header_idx = 0
    hdr_keys = ("일자", "일시", "이용일", "가맹점", "이용하신곳", "상호", "승인번호", "transacted_at")
    for idx, cells in enumerate(all_csv_rows[:30]):
        joined = " ".join(str(c) for c in cells)
        if sum(1 for k in hdr_keys if k in joined) >= 2:
            header_idx = idx
            break
    else:
        for idx, cells in enumerate(all_csv_rows[:30]):
            joined = " ".join(str(c) for c in cells)
            if any(k in joined for k in hdr_keys):
                header_idx = idx
                break

    if header_idx < len(all_csv_rows):
        headers = [re.sub(r"\s+", "", str(h)) for h in all_csv_rows[header_idx]]
        items = []
        for cells in all_csv_rows[header_idx + 1 :]:
            if not any(str(c).strip() for c in cells):
                continue
            row_dict = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}
            item = _row_dict_to_reconcile_item(row_dict, default_year=default_year)
            if item is not None:
                items.append(item)
        if items:
            return items

    return parse_hometax_csv(content)


_COUPANG_NOISE_RE = re.compile(
    r"^(주문상세보기|배송조회|교환,\s*반품\s*신청|반품\s*신청|교환\s*신청|장바구니\s*담기|"
    r"리뷰\s*작성하기|구매후기\s*쓰기|배송완료|결제완료|주문취소|취소완료|상품준비중|배송중|"
    r"배송시작|새벽배송|로켓배송|로켓와우|로켓프레시|판매자\s*문의|영수증\s*조회|거래명세서|"
    r"카드가맹점전표|신용카드\s*전표|주문목록|주문내역|취소/반품/교환\s*목록|정기배송|"
    r"할인금액|배송비|결제정보|총\s*결제금액|카드결제|쿠페이|\d{1,2}/\d{1,2}\([월화수목금토일]\).*)$",
    re.IGNORECASE,
)


def _walk_coupang_ssr_json(node: Any, collected: List[Dict[str, Any]]) -> None:
    """Recursively extract orders from Coupang SSR API JSON or __NEXT_DATA__ state."""
    if isinstance(node, list):
        for child in node:
            _walk_coupang_ssr_json(child, collected)
        return
    if not isinstance(node, dict):
        return

    raw_dt = node.get("orderedAt") or node.get("orderDate") or node.get("paidAt")
    if raw_dt and any(k in node for k in ("deliveryGroupList", "productList", "bundleReceiptList", "items")):
        if isinstance(raw_dt, (int, float)) and raw_dt > 1_000_000_000_000:
            from datetime import datetime as _dt

            dt_str = _dt.fromtimestamp(raw_dt / 1000.0).strftime("%Y-%m-%d")
        else:
            dt_str = str(raw_dt)[:10]

        prod_items: List[tuple[str, int, int]] = []

        def _collect_prods(sub: Any) -> None:
            if isinstance(sub, list):
                for el in sub:
                    _collect_prods(el)
            elif isinstance(sub, dict):
                p_name = (
                    sub.get("productName")
                    or sub.get("itemName")
                    or sub.get("title")
                    or sub.get("name")
                )
                p_price = (
                    sub.get("combinedPrice")
                    or sub.get("totalPrice")
                    or sub.get("unitPrice")
                    or sub.get("price")
                    or sub.get("amount")
                )
                p_qty_raw = sub.get("quantity") or sub.get("qty") or sub.get("count") or 1
                if p_name and p_price is not None:
                    amt_val = _parse_int_amount(str(p_price))
                    qty_val = max(1, _parse_int_amount(str(p_qty_raw)) or 1)
                    if amt_val > 0:
                        prod_items.append((str(p_name).strip(), qty_val, amt_val))
                for v in sub.values():
                    if isinstance(v, (list, dict)):
                        _collect_prods(v)

        for k in ("deliveryGroupList", "productList", "bundleReceiptList", "items"):
            if k in node:
                _collect_prods(node[k])

        for p_name, p_qty, p_amt in prod_items:
            collected.append(
                {
                    "주문일자": dt_str,
                    "상품명": p_name,
                    "결제금액": str(p_amt),
                    "품목내역": [{"item": p_name, "qty": p_qty, "price": p_amt}],
                }
            )

        tot_pay = (
            node.get("totalPayedPrice")
            or node.get("totalProductPrice")
            or node.get("paymentAmount")
            or (sum(x[2] for x in prod_items) if len(prod_items) >= 2 else 0)
        )
        tot_int = _parse_int_amount(str(tot_pay or 0))
        if prod_items and tot_int > 0 and len(prod_items) >= 2:
            summary_title = f"{prod_items[0][0]} 외 {len(prod_items) - 1}건"
            collected.append(
                {
                    "주문일자": dt_str,
                    "상품명": summary_title,
                    "결제금액": str(tot_int),
                    "품목내역": [
                        {"item": n, "qty": q, "price": a} for n, q, a in prod_items
                    ],
                }
            )

    for val in node.values():
        if isinstance(val, (dict, list)):
            _walk_coupang_ssr_json(val, collected)


def extract_coupang_orders_from_content(raw_content: str) -> List[Dict[str, Any]]:
    """Extract order rows from JSON, Coupang SSR API JSON, HTML table, DOM HTML, or copied screen text."""
    content = (raw_content or "").strip()
    if not content:
        return []

    if content.startswith("{") or content.startswith("["):
        try:
            data = json.loads(content)
            direct_rows: List[Dict[str, Any]] = []
            if isinstance(data, list):
                direct_rows = [r for r in data if isinstance(r, dict)]
            elif isinstance(data, dict):
                for k in ("orders", "items", "data", "list"):
                    if isinstance(data.get(k), list):
                        direct_rows = [r for r in data[k] if isinstance(r, dict)]
                        break
            if direct_rows and any(
                _pick_col(r, ["상품명", "품목명", "주문상품", "item_name", "product_name", "title"])
                for r in direct_rows
            ):
                return direct_rows
            ssr_rows: List[Dict[str, Any]] = []
            _walk_coupang_ssr_json(data, ssr_rows)
            if ssr_rows:
                return ssr_rows
        except Exception:
            pass

    if "<tr" in content.lower():
        tbl_rows = extract_rows_from_html_table(content)
        if tbl_rows and any(
            _pick_col(r, ["상품명", "품목명", "주문상품", "상품", "내역", "item_name", "product_name"])
            for r in tbl_rows
        ):
            return tbl_rows

    # Parse Coupang Order List DOM HTML (Ctrl+S) or copied screen text (Ctrl+A, Ctrl+C)
    plain = re.sub(r"<script[^>]*>.*?</script\s*>", "\n", content, flags=re.I | re.S)
    plain = re.sub(r"<style[^>]*>.*?</style\s*>", "\n", plain, flags=re.I | re.S)
    plain = re.sub(r"<(?:br|div|p|li|tr|h[1-6]|section|article)[^>]*>", "\n", plain, flags=re.I)
    plain = html.unescape(_HTML_TAG_RE.sub(" ", plain))

    date_hdr_re = re.compile(
        r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?\s*주문"
    )
    matches = list(date_hdr_re.finditer(plain))
    if matches:
        extracted: List[Dict[str, Any]] = []
        for idx, m in enumerate(matches):
            y, mo, d = m.groups()
            dt_str = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            start_pos = m.end()
            end_pos = matches[idx + 1].start() if idx + 1 < len(matches) else len(plain)
            block = plain[start_pos:end_pos]
            lines = [re.sub(r"\s+", " ", ln).strip() for ln in block.splitlines()]
            lines = [ln for ln in lines if ln and not _COUPANG_NOISE_RE.match(ln)]

            block_items: List[tuple[str, int, int]] = []
            last_candidate_title = ""
            for ln in lines:
                price_m = re.search(r"([\d,]{3,})\s*원", ln)
                if price_m:
                    amt = _parse_int_amount(price_m.group(1))
                    after_price = ln[price_m.end() :]
                    qty_m = re.search(r"(\d+)\s*개", after_price)
                    qty_val = int(qty_m.group(1)) if qty_m else 1
                    before_price = ln[: price_m.start()].strip(" ·-:|")
                    title = before_price if len(before_price) >= 2 else last_candidate_title
                    if title and amt > 0:
                        block_items.append((title, qty_val, amt))
                        last_candidate_title = ""
                elif len(ln) >= 2 and not ln.isdigit():
                    last_candidate_title = ln

            for p_name, p_qty, p_amt in block_items:
                extracted.append(
                    {
                        "주문일자": dt_str,
                        "상품명": p_name,
                        "결제금액": str(p_amt),
                        "품목내역": [{"item": p_name, "qty": p_qty, "price": p_amt}],
                    }
                )
            if len(block_items) >= 2:
                total_sum = sum(x[2] for x in block_items)
                summary_title = f"{block_items[0][0]} 외 {len(block_items) - 1}건"
                extracted.append(
                    {
                        "주문일자": dt_str,
                        "상품명": summary_title,
                        "결제금액": str(total_sum),
                        "품목내역": [
                            {"item": n, "qty": q, "price": a} for n, q, a in block_items
                        ],
                    }
                )
        if extracted:
            return extracted

    reader = csv.DictReader(io.StringIO(content))
    return list(reader)


def build_coupang_browser_extractor_script(
    api_url: str = "http://127.0.0.1:8000",
    api_key: str = "",
) -> str:
    """Build a 1-click browser console / bookmarklet JS collector for mc.coupang.com."""
    safe_url = api_url.rstrip("/")
    safe_key = api_key or settings.api_key
    return f"""(async () => {{
  const API_URL = {json.dumps(safe_url)};
  const API_KEY = {json.dumps(safe_key)};
  const banner = document.createElement('div');
  banner.style.cssText = 'position:fixed;top:16px;right:16px;z-index:999999;background:#1e40af;color:#fff;'
    + 'padding:14px 20px;border-radius:10px;font-size:14px;font-weight:700;box-shadow:0 8px 24px rgba(0,0,0,0.25);';
  banner.textContent = '🛒 SoloLedger 쿠팡 주문내역·영수증 수집 중...';
  document.body.appendChild(banner);

  const orders = [];
  const seen = new Set();
  const addOrder = (dt, name, amt, lineItems) => {{
    if (!dt || !name || !amt || amt <= 0) return;
    const key = dt + '|' + name + '|' + amt;
    if (seen.has(key)) return;
    seen.add(key);
    orders.push({{
      주문일자: dt,
      상품명: String(name).trim(),
      결제금액: Number(amt),
      품목내역: Array.isArray(lineItems) && lineItems.length > 0
        ? lineItems
        : [{{ item: String(name).trim(), qty: 1, price: Number(amt) }}]
    }});
  }};

  // 1. Try Coupang SSR internal JSON API across 2026 & 2025 pages
  for (const yr of [2026, 2025, 0]) {{
    for (let page = 0; page < 20; page++) {{
      try {{
        banner.textContent = `🛒 쿠팡 주문목록 수집 중 (${{yr || '최근6개월'}} - 페이지 ${{page + 1}})...`;
        const u = `/ssr/api/myorders/model/page?requestYear=${{yr}}&pageIndex=${{page}}&size=10`;
        const resp = await fetch(u, {{ credentials: 'include' }});
        if (!resp.ok) break;
        const data = await resp.json();
        const str = JSON.stringify(data);
        if (!str || str.length < 50) break;
        const list = data.orderList || data.orders || (data.data && data.data.orderList) || [];
        if (Array.isArray(list) && list.length === 0) break;
        const walk = (node) => {{
          if (!node || typeof node !== 'object') return;
          if (Array.isArray(node)) {{ node.forEach(walk); return; }}
          const rawDt = node.orderedAt || node.orderDate || node.paidAt;
          if (rawDt) {{
            const dt = typeof rawDt === 'number'
              ? new Date(rawDt + 9 * 3600 * 1000).toISOString().slice(0, 10)
              : String(rawDt).slice(0, 10).replace(/\\./g, '-');
            const items = [];
            const findProds = (s) => {{
              if (!s || typeof s !== 'object') return;
              if (Array.isArray(s)) {{ s.forEach(findProds); return; }}
              const pName = s.productName || s.itemName || s.title;
              const pAmt = s.combinedPrice || s.totalPrice || s.unitPrice || s.price;
              const pQty = parseInt(String(s.quantity || s.qty || s.count || 1), 10) || 1;
              if (pName && pAmt) {{
                const nAmt = parseInt(String(pAmt).replace(/[^0-9]/g, ''), 10);
                if (nAmt > 0) items.push([String(pName).trim(), pQty, nAmt]);
              }}
              Object.values(s).forEach(findProds);
            }};
            findProds(node.deliveryGroupList || node.productList || node.bundleReceiptList || node.items);
            items.forEach(([n, q, a]) => addOrder(dt, n, a, [{{ item: n, qty: q, price: a }}]));
            const tot = parseInt(String(node.totalPayedPrice || node.totalProductPrice || 0).replace(/[^0-9]/g, ''), 10)
              || items.reduce((acc, x) => acc + x[2], 0);
            if (items.length >= 2 && tot > 0) {{
              const lines = items.map(([n, q, a]) => ({{ item: n, qty: q, price: a }}));
              addOrder(dt, `${{items[0][0]}} 외 ${{items.length - 1}}건`, tot, lines);
            }}
          }}
          Object.values(node).forEach(walk);
        }};
        walk(data);
      }} catch (e) {{ break; }}
    }}
  }}

  // 2. Also parse currently visible DOM text on the page
  const pageText = document.body.innerText || '';
  const payload = orders.length > 0 ? JSON.stringify(orders, null, 2) : pageText;

  // 3. Always download backup file coupang_orders.json
  const blob = new Blob([payload], {{ type: 'application/json;charset=utf-8' }});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'coupang_orders_' + new Date().toISOString().slice(0, 10).replace(/-/g, '') + '.json';
  document.body.appendChild(a);
  a.click();
  a.remove();

  // 4. Send directly to local SoloLedger server
  try {{
    const res = await fetch(API_URL + '/api/v1/scraper/ingest', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'X-API-Key': API_KEY }},
      body: JSON.stringify({{ portal: 'COUPANG', raw_content: payload, auto_generate_slips: true }})
    }});
    const resJson = await res.json();
    banner.style.background = '#15803d';
    banner.textContent = `✅ SoloLedger 쿠팡 매칭 완료! (${{resJson.matched_orders || 0}}건 업데이트)`;
    alert(`✅ 쿠팡 주문내역 수집 및 SoloLedger 자동 매칭 완료!\\n`
      + `- 추출 주문 항목: ${{orders.length}}건\\n`
      + `- 원장 전표 매칭: ${{resJson.matched_orders || 0}}건\\n`
      + `(다운로드 폴더에 백업 파일도 함께 저장되었습니다.)`);
  }} catch (err) {{
    banner.style.background = '#0284c7';
    banner.textContent = '📥 coupang_orders.json 다운로드 완료! 대시보드에서 업로드해 주세요.';
    alert('📥 쿠팡 주문내역 파일(coupang_orders.json)이 다운로드 폴더에 저장되었습니다!\\n\\n'
      + 'SoloLedger 대시보드의 [🛒 쿠팡 주문·영수증 자동매칭] 창에서 해당 파일을 올려주세요.');
  }}
}})();"""


def _find_local_chromium_executable() -> Optional[str]:
    """Locate an installed Chromium-based browser (Chrome, Brave, Edge) on the host."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    for cmd in ("chrome", "google-chrome", "brave", "msedge", "chromium"):
        found = shutil.which(cmd)
        if found:
            return found
    return None


class _CdpWebSocket:
    """Minimal RFC 6455 WebSocket client for local Chrome DevTools Protocol (CDP)."""

    def __init__(self, ws_url: str, timeout: float = 45.0) -> None:
        parsed = urllib.parse.urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9333
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = bytearray()
        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))
        header = self._read_until(b"\r\n\r\n")
        if b"101" not in header.split(b"\r\n", 1)[0]:
            self.sock.close()
            raise RuntimeError("CDP WebSocket handshake rejected")

    def _read_until(self, delimiter: bytes) -> bytes:
        while delimiter not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("Socket closed during CDP handshake")
            self.buf.extend(chunk)
        idx = self.buf.index(delimiter) + len(delimiter)
        out = bytes(self.buf[:idx])
        del self.buf[:idx]
        return out

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise EOFError("Socket closed while reading CDP frame")
            self.buf.extend(chunk)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def send_json(self, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        mask = b"\x37\xfa\x21\x3d"
        length = len(raw)
        if length < 126:
            header = struct.pack("!BB", 0x81, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(raw))
        self.sock.sendall(header + mask + masked)

    def recv_json(self) -> Dict[str, Any]:
        msg_bytes = bytearray()
        while True:
            b0, b1 = self._read_exact(2)
            fin = (b0 & 0x80) != 0
            opcode = b0 & 0x0F
            masked = (b1 & 0x80) != 0
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask_key = self._read_exact(4) if masked else b""
            data = self._read_exact(length)
            if masked:
                data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))
            if opcode == 0x8:
                raise EOFError("CDP WebSocket closed by browser")
            if opcode == 0x9:
                # Reply Pong
                self.sock.sendall(b"\x8a\x80\x00\x00\x00\x00")
                continue
            if opcode in (0x0, 0x1, 0x2):
                msg_bytes.extend(data)
                if fin:
                    return json.loads(msg_bytes.decode("utf-8", errors="replace"))

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, msg_id: int = 1) -> Dict[str, Any]:
        self.send_json({"id": msg_id, "method": method, "params": params or {}})
        while True:
            msg = self.recv_json()
            if msg.get("id") == msg_id:
                return msg

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


_CDP_STATUS_JS = """(() => {
  const href = location.href || '';
  const host = location.hostname || '';
  const hasPw = !!document.querySelector('input[type="password"]');
  if (host.includes('login.coupang.com') || hasPw) {
    if (!document.getElementById('sl-login-guide')) {
      const b = document.createElement('div');
      b.id = 'sl-login-guide';
      b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:999999;background:#1e40af;color:#fff;'
        + 'padding:12px 18px;font-size:14px;font-weight:700;text-align:center;box-shadow:0 4px 12px rgba(0,0,0,0.25);';
      b.textContent = '🔐 [SoloLedger 자동 수집] 최초 1회 쿠팡 로그인을 진행해 주세요 (자동 로그인 체크 권장). '
        + '로그인 즉시 2025~2026년 주문내역을 자동 수집하고 창이 닫힙니다.';
      if (document.body) document.body.appendChild(b);
    }
    return { state: 'LOGIN_REQUIRED', url: href };
  }
  const bodyTxt = (document.body && document.body.innerText) || '';
  if (host.includes('coupang.com') && document.readyState === 'complete'
      && (href.includes('/order/list') || bodyTxt.includes('주문목록'))) {
    return { state: 'READY', url: href };
  }
  return { state: 'LOADING', url: href };
})()"""

_CDP_COLLECT_JS = """(async () => {
  const banner = document.createElement('div');
  banner.style.cssText = 'position:fixed;top:16px;right:16px;z-index:999999;background:#1e40af;color:#fff;'
    + 'padding:14px 20px;border-radius:10px;font-size:14px;font-weight:700;box-shadow:0 8px 24px rgba(0,0,0,0.25);';
  banner.textContent = '🛒 SoloLedger 쿠팡 주문내역 자동 수집 중...';
  if (document.body) document.body.appendChild(banner);

  const orders = [];
  const seen = new Set();
  const addOrder = (dt, name, amt, lineItems) => {
    if (!dt || !name || !amt || amt <= 0) return;
    const key = dt + '|' + name + '|' + amt;
    if (seen.has(key)) return;
    seen.add(key);
    orders.push({
      주문일자: dt,
      상품명: String(name).trim(),
      결제금액: Number(amt),
      품목내역: Array.isArray(lineItems) && lineItems.length > 0
        ? lineItems
        : [{ item: String(name).trim(), qty: 1, price: Number(amt) }]
    });
  };

  for (let sIdx = 0; sIdx < 4; sIdx++) {
    window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
    await new Promise((r) => setTimeout(r, 350));
  }
  const domTexts = [document.body ? document.body.innerText : ''];
  for (const yr of [2026, 2025, 0]) {
    for (let page = 0; page < 25; page++) {
      try {
        banner.textContent = `🛒 쿠팡 주문내역·거래명세서 수집 중 (${yr || '최근6개월'} - 페이지 ${page + 1})...`;
        const u = `/ssr/api/myorders/model/page?requestYear=${yr}&pageIndex=${page}&size=10`;
        const resp = await fetch(u, { credentials: 'include' });
        if (!resp.ok) break;
        const data = await resp.json();
        const str = JSON.stringify(data);
        if (!str || str.length < 50) break;
        const list = data.orderList || data.orders || (data.data && data.data.orderList) || [];
        if (Array.isArray(list) && list.length === 0) break;
        const walk = (node) => {
          if (!node || typeof node !== 'object') return;
          if (Array.isArray(node)) { node.forEach(walk); return; }
          const rawDt = node.orderedAt || node.orderDate || node.paidAt;
          if (rawDt) {
            const dt = typeof rawDt === 'number'
              ? new Date(rawDt + 9 * 3600 * 1000).toISOString().slice(0, 10)
              : String(rawDt).slice(0, 10).replace(/\\./g, '-');
            const items = [];
            const findProds = (s) => {
              if (!s || typeof s !== 'object') return;
              if (Array.isArray(s)) { s.forEach(findProds); return; }
              const pName = s.productName || s.itemName || s.title;
              const pAmt = s.combinedPrice || s.totalPrice || s.unitPrice || s.price;
              const pQty = parseInt(String(s.quantity || s.qty || s.count || 1), 10) || 1;
              if (pName && pAmt) {
                const nAmt = parseInt(String(pAmt).replace(/[^0-9]/g, ''), 10);
                if (nAmt > 0) items.push([String(pName).trim(), pQty, nAmt]);
              }
              Object.values(s).forEach(findProds);
            };
            findProds(node.deliveryGroupList || node.productList || node.bundleReceiptList || node.items);
            items.forEach(([n, q, a]) => addOrder(dt, n, a, [{ item: n, qty: q, price: a }]));
            const tot = parseInt(String(node.totalPayedPrice || node.totalProductPrice || 0).replace(/[^0-9]/g, ''), 10)
              || items.reduce((acc, x) => acc + x[2], 0);
            if (items.length >= 2 && tot > 0) {
              const lines = items.map(([n, q, a]) => ({ item: n, qty: q, price: a }));
              addOrder(dt, `${items[0][0]} 외 ${items.length - 1}건`, tot, lines);
            }
          }
          Object.values(node).forEach(walk);
        };
        walk(data);
      } catch (e) { break; }
    }
  }

  banner.style.background = '#15803d';
  banner.textContent = `✅ 수집 완료 (${orders.length}건)! SoloLedger에 반영 중...`;
  return {
    ordersCount: orders.length,
    jsonPayload: JSON.stringify(orders),
    domText: domTexts.join('\\n')
  };
})()"""


def _fetch_cdp_page_ws_url(debug_port: int) -> Optional[str]:
    """Return the active Coupang page WebSocket debugger URL from local CDP port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=2.5) as resp:
            targets = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    for p in pages:
        if "coupang.com" in str(p.get("url", "")):
            return str(p["webSocketDebuggerUrl"])
    return str(pages[0]["webSocketDebuggerUrl"]) if pages else None


def auto_crawl_coupang_orders(
    cursor: sqlite3.Cursor,
    archive_dir: Optional[Path] = None,
    max_wait_sec: int = 180,
    debug_port: int = 9333,
) -> Dict[str, Any]:
    """Launch a persistent local browser session via CDP, collect Coupang orders, and reconcile."""
    base_archive = archive_dir or settings.archive_dir
    profile_dir = base_archive / "browser_profile" / "coupang"
    profile_dir.mkdir(parents=True, exist_ok=True)

    browser_exe = _find_local_chromium_executable()
    if not browser_exe:
        return {
            "status": "error",
            "message": "로컬 PC에서 Chrome/Brave/Edge 브라우저를 찾을 수 없습니다.",
            "matched_orders": 0,
        }

    proc: Optional[subprocess.Popen[bytes]] = None
    ws_url = _fetch_cdp_page_ws_url(debug_port)
    if not ws_url:
        cmd = [
            browser_exe,
            f"--user-data-dir={profile_dir}",
            f"--remote-debugging-port={debug_port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1180,860",
            "https://mc.coupang.com/ssr/desktop/order/list",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    deadline = time.time() + max(10, max_wait_sec)
    collected_result: Optional[Dict[str, Any]] = None
    last_state = "STARTING"

    try:
        while time.time() < deadline:
            if proc is not None and proc.poll() is not None:
                return {
                    "status": "closed_by_user",
                    "message": "수집 완료 전 브라우저 창이 닫혔습니다.",
                    "matched_orders": 0,
                }
            ws_url = _fetch_cdp_page_ws_url(debug_port)
            if not ws_url:
                time.sleep(1.0)
                continue
            try:
                ws = _CdpWebSocket(ws_url, timeout=30.0)
                try:
                    st_msg = ws.call(
                        "Runtime.evaluate",
                        {"expression": _CDP_STATUS_JS, "returnByValue": True},
                        msg_id=1,
                    )
                    st_val = st_msg.get("result", {}).get("result", {}).get("value") or {}
                    last_state = str(st_val.get("state") or "LOADING")
                    if last_state == "READY":
                        col_msg = ws.call(
                            "Runtime.evaluate",
                            {
                                "expression": _CDP_COLLECT_JS,
                                "awaitPromise": True,
                                "returnByValue": True,
                            },
                            msg_id=2,
                        )
                        collected_result = col_msg.get("result", {}).get("result", {}).get("value")
                        try:
                            ws.call("Browser.close", {}, msg_id=3)
                        except Exception:
                            pass
                        break
                finally:
                    ws.close()
            except Exception:
                pass
            time.sleep(1.5)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    if not collected_result:
        return {
            "status": "timeout",
            "last_state": last_state,
            "message": "브라우저 로그인 또는 페이지 로딩 대기 시간이 초과되었습니다.",
            "matched_orders": 0,
        }

    json_payload = str(collected_result.get("jsonPayload") or "[]")
    dom_text = str(collected_result.get("domText") or "")
    orders_count = int(collected_result.get("ordersCount") or 0)
    raw_to_ingest = json_payload if orders_count > 0 else dom_text

    backup_path = base_archive / "coupang_orders_auto.json"
    try:
        backup_path.write_text(raw_to_ingest, encoding="utf-8")
    except Exception:
        pass

    outcome = ingest_scraped_payload(
        cursor,
        target_portal="COUPANG",
        raw_content=raw_to_ingest,
        auto_generate_slips=True,
        archive_dir=base_archive,
    )
    if orders_count > 0 and dom_text.strip():
        dom_outcome = reconcile_scraped_ecommerce_orders(
            cursor, dom_text, archive_dir=base_archive
        )
        if dom_outcome.get("matched_count", 0) > 0:
            outcome["matched_orders"] = int(outcome.get("matched_orders", 0)) + int(
                dom_outcome["matched_count"]
            )
            generate_electronic_slips_for_ledger(cursor, archive_dir=base_archive)

    outcome["status"] = "crawled"
    outcome["browser_used"] = Path(browser_exe).name
    outcome["collected_orders_count"] = orders_count
    outcome["saved_backup_path"] = str(backup_path)
    return outcome


_ONLINE_OR_PG_MERCHANT_KEYWORDS = (
    "쿠팡",
    "쿠페이",
    "네이버",
    "카카오",
    "이니시스",
    "케이지이니시스",
    "토스",
    "케이씨피",
    "kcp",
    "나이스",
    "다날",
    "페이코",
    "이지페이",
    "한국정보통신",
    "kicc",
    "모빌리언스",
    "스마트로",
    "갤럭시아",
    "세틀뱅크",
    "11번가",
    "지마켓",
    "옥션",
    "무신사",
    "컬리",
    "ssg",
    "신세계",
    "오늘의집",
    "우아한형제들",
    "배달의민족",
    "요기요",
    "교보문고",
    "예스24",
    "알라딘",
    "배틀넷",
    "battle",
    "블리자드",
    "google",
    "apple",
    "steam",
    "아마존",
    "알리익스프레스",
    "테무",
)


def _norm_merchant_token(name: str) -> str:
    return re.sub(r"\(주\)|㈜|주식회사|\(유\)|유한회사|[\s\-_()\[\]]+", "", str(name or "")).lower()


def _norm_alnum_ko(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]+", "", str(text or "")).lower()


def _resolve_card_last4_from_issuer(
    cursor: sqlite3.Cursor,
    card_hint: str,
    merchant_name: str,
) -> str:
    """Resolve card_last4 from PG email issuer hint ('국민', '현대', etc.) or merchant history."""
    hint = str(card_hint or "").strip()
    m_digits = re.search(r"(\d[\d\*]{3})", hint)
    if m_digits:
        return m_digits.group(1)

    issuer_kw = ""
    hint_low = hint.lower()
    for token, canonical in (
        ("국민", "국민"),
        ("kb", "국민"),
        ("현대", "현대"),
        ("롯데", "롯데"),
        ("우리", "우리"),
        ("신한", "신한"),
        ("삼성", "삼성"),
        ("하나", "하나"),
        ("농협", "농협"),
        ("nh", "농협"),
        ("기업", "기업"),
        ("ibk", "기업"),
    ):
        if token in hint_low:
            issuer_kw = canonical
            break

    cursor.execute(
        """
        SELECT card_last4, COUNT(*) AS cnt
        FROM card_transactions
        WHERE reconcile_status != 'CANCELED'
          AND card_last4 != '0000'
        GROUP BY card_last4
        ORDER BY cnt DESC
        """
    )
    active_cards = [str(r["card_last4"]) for r in cursor.fetchall() if r["card_last4"]]

    if issuer_kw:
        for c_code in active_cards:
            lbl = format_card_label(c_code)
            if issuer_kw in lbl and "하이패스" not in lbl:
                return c_code
        for c_code in active_cards:
            if issuer_kw in format_card_label(c_code):
                return c_code
        return f"{issuer_kw}카드"

    merch_low = str(merchant_name or "").lower()
    core_kw = ""
    for kw in ("쿠팡", "네이버", "카카오", "교보문고", "11번가", "지마켓", "옥션", "컬리", "무신사"):
        if kw in merch_low:
            core_kw = kw
            break
    if core_kw:
        cursor.execute(
            """
            SELECT card_last4, COUNT(*) AS cnt
            FROM card_transactions
            WHERE reconcile_status != 'CANCELED'
              AND card_last4 != '0000'
              AND raw_merchant_name LIKE ?
            GROUP BY card_last4
            ORDER BY cnt DESC
            LIMIT 1
            """,
            (f"%{core_kw}%",),
        )
        m_row = cursor.fetchone()
        if m_row and m_row["card_last4"]:
            return str(m_row["card_last4"])

    return "0000"


def _infer_merchant_for_email_order(
    cursor: sqlite3.Cursor,
    row: Dict[str, Any],
) -> str:
    """Infer canonical ledger merchant name for a newly created email order transaction."""
    shop_name = str(row.get("상점명") or row.get("가맹점명") or "").strip()
    portal_hint = str(row.get("발신처") or "").strip()
    item_name = str(row.get("상품명") or "").strip()
    comb = f"{shop_name} {portal_hint}".lower()

    if "쿠팡" in comb:
        if "와우" in item_name and "멤버십" in item_name:
            return "쿠팡(와우멤버십)"
        cursor.execute(
            """
            SELECT raw_merchant_name, COUNT(*) AS cnt
            FROM card_transactions
            WHERE raw_merchant_name LIKE '%쿠팡%'
              AND raw_merchant_name NOT LIKE '%와우%'
              AND raw_merchant_name NOT LIKE '%이츠%'
              AND reconcile_status != 'CANCELED'
            GROUP BY raw_merchant_name
            ORDER BY cnt DESC
            LIMIT 1
            """
        )
        m_row = cursor.fetchone()
        if m_row and m_row["raw_merchant_name"]:
            return str(m_row["raw_merchant_name"])
        return shop_name or "쿠팡(주)"

    return shop_name or portal_hint or "온라인결제(이메일)"


def reconcile_scraped_ecommerce_orders(
    cursor: sqlite3.Cursor,
    raw_content: str,
    archive_dir: Optional[Path] = None,
    orders_list: Optional[List[Dict[str, Any]]] = None,
    auto_create_after_date: Optional[str] = None,
    auto_create_all: bool = False,
) -> Dict[str, Any]:
    """Match scraped Coupang / NaverPay / KakaoPay / PG order items against card transactions."""
    content = (raw_content or "").strip()
    if not content and not orders_list:
        return {
            "status": "empty",
            "matched_count": 0,
            "created_count": 0,
            "newly_stated_count": 0,
            "created_tx_ids": [],
            "newly_stated_tx_ids": [],
            "updated_tx_ids": [],
            "matched_items": [],
        }

    order_rows = orders_list if orders_list is not None else extract_coupang_orders_from_content(content)

    updated_tx_ids: List[int] = []
    created_tx_ids: List[int] = []
    newly_stated_tx_ids: List[int] = []
    matched_items: List[Dict[str, Any]] = []
    used_tx_ids: set[int] = set()
    for row in order_rows:
        raw_dt = _pick_col(
            row, ["주문일시", "결제일시", "주문일자", "결제일자", "일자", "order_date", "date"]
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

        shop_name = str(row.get("상점명") or row.get("가맹점명") or "").strip()
        app_no = str(row.get("승인번호") or "").strip()
        card_hint = str(row.get("카드사") or row.get("결제기관") or "").strip()
        norm_shop = _norm_merchant_token(shop_name)

        cursor.execute(
            """
            SELECT * FROM card_transactions
            WHERE total_amount = ?
              AND ABS(julianday(SUBSTR(transacted_at, 1, 10)) - julianday(?)) <= 3.0
              AND reconcile_status != 'CANCELED'
            ORDER BY
              CASE
                WHEN ? != '' AND approval_no = ? THEN 0
                ELSE 1
              END ASC,
              CASE
                WHEN raw_merchant_name LIKE '%쿠팡%'
                  OR raw_merchant_name LIKE '%네이버%'
                  OR raw_merchant_name LIKE '%카카오%'
                  OR raw_merchant_name LIKE '%이니시스%'
                  OR raw_merchant_name LIKE '%토스%'
                  OR raw_merchant_name LIKE '%케이씨피%'
                  OR raw_merchant_name LIKE '%KCP%'
                  OR raw_merchant_name LIKE '%나이스%'
                  OR raw_merchant_name LIKE '%다날%'
                  OR raw_merchant_name LIKE '%페이코%'
                  OR raw_merchant_name LIKE '%11번가%'
                  OR raw_merchant_name LIKE '%지마켓%' THEN 0
                ELSE 1
              END ASC,
              CASE
                WHEN counterparty_info IS NULL OR TRIM(counterparty_info) = '' THEN 0
                ELSE 1
              END ASC,
              ABS(julianday(SUBSTR(transacted_at, 1, 10)) - julianday(?)) ASC,
              tx_id DESC
            """,
            (amount, date_prefix, app_no, app_no, date_prefix),
        )
        candidates = cursor.fetchall()
        tx_row = None
        for cand in candidates:
            cid = int(cand["tx_id"])
            if cid in used_tx_ids:
                continue
            cand_merch = str(cand["raw_merchant_name"] or "")
            cand_merch_low = cand_merch.lower()
            norm_cand = _norm_merchant_token(cand_merch)
            cand_app = str(cand["approval_no"] or "").strip()
            is_app_match = bool(app_no and cand_app == app_no)
            is_shop_match = bool(
                norm_shop
                and norm_cand
                and (norm_shop in norm_cand or norm_cand in norm_shop)
            )
            is_online_or_unclassified = (
                str(cand["account_code"] or "") == "999"
                or str(cand["reconcile_status"] or "") == "EMAIL_ORDER"
                or any(kw in cand_merch_low for kw in _ONLINE_OR_PG_MERCHANT_KEYWORDS)
            )
            if is_app_match or is_shop_match or is_online_or_unclassified:
                tx_row = cand
                break

        is_newly_created = False
        if tx_row is None:
            should_auto_create = auto_create_all or (
                auto_create_after_date is not None and date_prefix > auto_create_after_date
            )
            if not should_auto_create:
                continue

            # Skip auto-creating if a canceled transaction with the same amount/date already exists
            cursor.execute(
                """
                SELECT 1 FROM card_transactions
                WHERE ABS(total_amount) = ?
                  AND ABS(julianday(SUBSTR(transacted_at, 1, 10)) - julianday(?)) <= 3.0
                  AND reconcile_status = 'CANCELED'
                LIMIT 1
                """,
                (amount, date_prefix),
            )
            if cursor.fetchone():
                continue

            full_ts = (
                norm_dt[:19]
                if len(norm_dt) >= 19 and norm_dt[11:19] != "00:00:00"
                else f"{date_prefix} 12:00:00"
            )
            inferred_merchant = _infer_merchant_for_email_order(cursor, row)
            resolved_card = _resolve_card_last4_from_issuer(cursor, card_hint, inferred_merchant)
            rule_eval = evaluate_tax_rules(cursor, inferred_merchant, None)
            sup_val, vat_val = calculate_supply_and_vat(amount, rule_eval["vat_deduction_type"])
            cursor.execute(
                """
                INSERT INTO card_transactions (
                    card_last4, approval_no, transacted_at, raw_merchant_name,
                    total_amount, supply_value, vat_amount,
                    account_code, account_name, vat_deduction_type,
                    non_deduct_reason, is_vehicle_expense, counterparty_info, reconcile_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'EMAIL_ORDER')
                """,
                (
                    resolved_card,
                    app_no or None,
                    full_ts,
                    inferred_merchant,
                    amount,
                    sup_val,
                    vat_val,
                    rule_eval["account_code"],
                    rule_eval["account_name"],
                    rule_eval["vat_deduction_type"],
                    rule_eval["non_deduct_reason"],
                    1 if rule_eval["is_vehicle_expense"] else 0,
                    item_name,
                ),
            )
            new_tx_id = int(cursor.lastrowid)
            cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (new_tx_id,))
            tx_row = cursor.fetchone()
            if tx_row is None:
                continue
            is_newly_created = True
            created_tx_ids.append(new_tx_id)
        else:
            # Backfill approval_no or card_last4 onto an existing EMAIL_ORDER/SMS row if newly discovered
            ex_id = int(tx_row["tx_id"])
            ex_app = str(tx_row["approval_no"] or "").strip()
            ex_card = str(tx_row["card_last4"] or "0000")
            ex_status = str(tx_row["reconcile_status"] or "")
            new_card = (
                _resolve_card_last4_from_issuer(cursor, card_hint, str(tx_row["raw_merchant_name"]))
                if card_hint and (ex_card == "0000" or ex_status == "EMAIL_ORDER")
                else ex_card
            )
            if (app_no and not ex_app) or (new_card != ex_card and new_card != "0000"):
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET approval_no = COALESCE(approval_no, ?),
                        card_last4 = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tx_id = ?
                    """,
                    (app_no or None, new_card if new_card != "0000" else ex_card, ex_id),
                )
                cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (ex_id,))
                tx_row = cursor.fetchone() or tx_row

        tx_id = int(tx_row["tx_id"])
        used_tx_ids.add(tx_id)
        final_item_name = item_name
        if shop_name and shop_name not in final_item_name:
            norm_tx_merch = _norm_merchant_token(str(tx_row["raw_merchant_name"] or ""))
            if norm_shop and norm_shop not in norm_tx_merch and norm_tx_merch not in norm_shop:
                final_item_name = f"[{shop_name}] {final_item_name}"

        raw_lines = row.get("품목내역") or row.get("line_items")
        row_line_items = raw_lines if isinstance(raw_lines, list) else None

        cursor.execute(
            """
            SELECT archived_file_name, line_items_json
            FROM receipt_ocr_metadata
            WHERE tx_id = ? AND source_channel = 'STATEMENT'
            ORDER BY receipt_id DESC LIMIT 1
            """,
            (tx_id,),
        )
        prev_stmt = cursor.fetchone()
        prev_memo = str(tx_row["counterparty_info"] or "").strip()

        # Prevent truncated PG 1-liners from overwriting a fuller or multi-item existing statement
        if prev_stmt and (not row_line_items or len(row_line_items) <= 1):
            keep_existing = False
            if re.search(r"포함\s*총\s*\d+", final_item_name) and prev_stmt["line_items_json"]:
                try:
                    prev_lines = json.loads(str(prev_stmt["line_items_json"]))
                    if isinstance(prev_lines, list) and len(prev_lines) >= 2:
                        keep_existing = True
                except Exception:
                    pass
            if not keep_existing and prev_memo:
                norm_prev = _norm_alnum_ko(prev_memo)
                norm_new = _norm_alnum_ko(final_item_name)
                if (
                    len(norm_new) >= 6
                    and len(norm_prev) > len(norm_new)
                    and norm_prev.startswith(norm_new)
                ):
                    keep_existing = True
            if keep_existing:
                continue

        if is_newly_created or prev_stmt is None or prev_memo != final_item_name:
            newly_stated_tx_ids.append(tx_id)

        stmt_res = generate_electronic_statement_for_tx(
            cursor,
            tx_id=tx_id,
            item_name=final_item_name,
            line_items=row_line_items,
            archive_dir=archive_dir,
        )
        updated_tx_ids.append(tx_id)
        matched_items.append(
            {
                "tx_id": tx_id,
                "transacted_at": str(tx_row["transacted_at"]),
                "card_label": format_card_label(str(tx_row["card_last4"] or "0000")),
                "raw_merchant_name": str(tx_row["raw_merchant_name"]),
                "item_name": stmt_res["item_name"],
                "line_items_count": len(stmt_res.get("line_items") or []),
                "total_amount": int(tx_row["total_amount"]),
                "account_code": stmt_res["account_code"],
                "account_name": stmt_res["account_name"],
                "archived_file_name": stmt_res["archived_file_name"],
                "is_created": is_newly_created,
            }
        )

    return {
        "status": "matched",
        "parsed_orders_count": len(order_rows),
        "matched_count": len(updated_tx_ids),
        "created_count": len(created_tx_ids),
        "newly_stated_count": len(newly_stated_tx_ids),
        "created_tx_ids": created_tx_ids,
        "newly_stated_tx_ids": newly_stated_tx_ids,
        "updated_tx_ids": updated_tx_ids,
        "matched_items": matched_items,
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


def render_electronic_statement_html(
    tx_row: Dict[str, Any],
    line_items: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Render a standalone printable Korean Transaction Statement (거래명세서)."""
    c_code = str(tx_row.get("card_last4") or "0000")
    c_label = html.escape(format_card_label(c_code))
    tx_id = int(tx_row["tx_id"])
    t_dt = html.escape(str(tx_row["transacted_at"]))
    md_str = html.escape(str(tx_row["transacted_at"])[5:10].replace("-", "/"))
    app_no = html.escape(str(tx_row.get("approval_no") or f"STMT-{tx_id:06d}"))
    merchant = html.escape(str(tx_row["raw_merchant_name"]))
    biz_no = html.escape(str(tx_row.get("biz_reg_no") or "미등록 (온라인/PG 가맹점)"))
    total_amt = int(tx_row["total_amount"])
    vat_type = str(tx_row.get("vat_deduction_type") or "DEDUCTIBLE")
    supply_val = int(tx_row["supply_value"])
    vat_amt = int(tx_row["vat_amount"])
    if vat_type != "EXEMPT" and vat_amt == 0 and total_amt != 0:
        supply_val, vat_amt = calculate_supply_and_vat(total_amt, "DEDUCTIBLE")
    acc_str = html.escape(f"{tx_row['account_code']} {tx_row['account_name']}")
    raw_memo = str(tx_row.get("counterparty_info") or "").strip()
    item_title = html.escape(raw_memo if raw_memo else f"{tx_row['raw_merchant_name']} 구매 품목")
    slip_title = build_receipt_print_title(
        tx_id=tx_id,
        transacted_at=str(tx_row["transacted_at"]),
        card_last4=c_code,
        merchant_name=str(tx_row["raw_merchant_name"]),
        account_name=str(tx_row["account_name"]),
        total_amount=total_amt,
    )
    stmt_title = html.escape(slip_title.replace("매출전표_", "거래명세서_", 1))

    tbody_rows: List[str] = []
    tot_qty = 0
    if line_items and len(line_items) > 0:
        for li in line_items:
            li_name = html.escape(str(li.get("item") or li.get("상품명") or item_title))
            li_qty = max(1, int(li.get("qty") or 1))
            li_price = int(li.get("price") or total_amt)
            li_sup, li_vat = calculate_supply_and_vat(li_price, vat_type)
            tot_qty += li_qty
            tbody_rows.append(
                f"<tr>"
                f"<td>{md_str}</td>"
                f"<td><strong>{li_name}</strong></td>"
                f"<td class='num'>{li_qty}</td>"
                f"<td class='num'>{li_sup:,}원</td>"
                f"<td class='num'>{li_vat:,}원</td>"
                f"<td class='num'><strong>{li_price:,}원</strong></td>"
                f"</tr>"
            )
    else:
        tot_qty = 1
        tbody_rows.append(
            f"<tr>"
            f"<td>{md_str}</td>"
            f"<td><strong>{item_title}</strong></td>"
            f"<td class='num'>1</td>"
            f"<td class='num'>{supply_val:,}원</td>"
            f"<td class='num'>{vat_amt:,}원</td>"
            f"<td class='num'><strong>{total_amt:,}원</strong></td>"
            f"</tr>"
        )
    tbody_html = "\n        ".join(tbody_rows)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>{stmt_title}</title>
  <style>
    body {{ font-family: 'Malgun Gothic', -apple-system, sans-serif; background: #f1f5f9;
           padding: 24px; color: #0f172a; }}
    .stmt {{ max-width: 580px; margin: 0 auto; background: #ffffff; border: 2px solid #1e3a8a;
             border-radius: 6px; padding: 22px; box-shadow: 0 6px 18px rgba(0,0,0,0.08); }}
    h1 {{ text-align: center; font-size: 20px; color: #1e3a8a; letter-spacing: 4px;
          margin: 0 0 4px 0; }}
    .sub {{ text-align: center; font-size: 12px; color: #475569; margin-bottom: 14px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 12px; }}
    th, td {{ border: 1px solid #94a3b8; padding: 7px 8px; text-align: left; }}
    th {{ background: #eff6ff; color: #1e3a8a; font-weight: 700; width: 22%; }}
    td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .tot-bar {{ background: #eff6ff; border: 1px solid #1e3a8a; padding: 10px 14px;
                display: flex; justify-content: space-between; font-weight: 800;
                font-size: 15px; color: #1e3a8a; margin-bottom: 12px; }}
    .foot {{ margin-top: 10px; font-size: 11px; color: #64748b; text-align: center; }}
  </style>
</head>
<body>
  <div class="stmt">
    <h1>거 래 명 세 서</h1>
    <div class="sub">(공급받는자 보관용 · 전표번호 #{tx_id})</div>
    <table>
      <tr>
        <th>거래일시</th><td>{t_dt}</td>
        <th>승인/문서번호</th><td>{app_no}</td>
      </tr>
      <tr>
        <th>공급자 상호</th><td><strong>{merchant}</strong></td>
        <th>사업자번호</th><td>{biz_no}</td>
      </tr>
      <tr>
        <th>결제수단</th><td>{c_label}</td>
        <th>회계계정</th><td><strong>{acc_str}</strong></td>
      </tr>
    </table>
    <table>
      <thead>
        <tr>
          <th style="width:12%;">월/일</th>
          <th style="width:36%;">품목 및 규격</th>
          <th class="num" style="width:8%;">수량</th>
          <th class="num" style="width:15%;">VAT 제외 가격</th>
          <th class="num" style="width:14%;">VAT 가격</th>
          <th class="num" style="width:15%;">합계 가격</th>
        </tr>
      </thead>
      <tbody>
        {tbody_html}
      </tbody>
      <tfoot>
        <tr>
          <th colspan="2">합 계</th>
          <td class="num"><strong>{tot_qty}</strong></td>
          <td class="num"><strong>{supply_val:,}원</strong></td>
          <td class="num"><strong>{vat_amt:,}원</strong></td>
          <td class="num"><strong>{total_amt:,}원</strong></td>
        </tr>
      </tfoot>
    </table>
    <table>
      <tr>
        <th style="width:33%;">VAT 제외 가격 (공급가액)</th>
        <td class="num"><strong>{supply_val:,}원</strong></td>
      </tr>
      <tr>
        <th>VAT 가격 (부가세액)</th>
        <td class="num"><strong>{vat_amt:,}원 ({vat_type})</strong></td>
      </tr>
      <tr>
        <th>합계 가격 (총 결제금액)</th>
        <td class="num"><strong style="color:#1e3a8a; font-size:14px;">{total_amt:,}원</strong></td>
      </tr>
    </table>
    <div class="tot-bar">
      <span>합계 가격 (VAT 포함)</span>
      <span>금 {total_amt:,} 원정 (₩{total_amt:,})</span>
    </div>
    <div class="foot">SoloLedger 전자 거래명세서 및 세무 소명 증빙 아카이브</div>
  </div>
</body>
</html>"""


def generate_electronic_statement_for_tx(
    cursor: sqlite3.Cursor,
    tx_id: int,
    item_name: Optional[str] = None,
    account_code: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    file_name: Optional[str] = None,
    line_items: Optional[List[Dict[str, Any]]] = None,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate or attach a Transaction Statement (거래명세서) for a specific transaction."""
    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"Transaction #{tx_id} not found.")
    tx = dict(row)

    resolved_item = (item_name or "").strip()
    resolved_lines: Optional[List[Dict[str, Any]]] = line_items
    if not resolved_item and file_bytes:
        try:
            decoded_txt = file_bytes.decode("utf-8", errors="ignore")
            extracted = extract_coupang_orders_from_content(decoded_txt)
            if extracted:
                first_row = extracted[0]
                resolved_item = str(
                    first_row.get("상품명")
                    or first_row.get("품목명")
                    or first_row.get("item_name")
                    or ""
                ).strip()
                if not resolved_lines and isinstance(first_row.get("품목내역"), list):
                    resolved_lines = first_row["품목내역"]
        except Exception:
            pass
    if not resolved_item and file_name:
        stem = Path(file_name).stem
        clean_stem = re.sub(r"^(거래명세서|명세서|영수증|전표)[_\-\s]*", "", stem).strip()
        if clean_stem and not clean_stem.isdigit():
            resolved_item = f"거래명세서({clean_stem})"
        else:
            resolved_item = f"거래명세서 첨부({file_name})"
    if not resolved_item:
        resolved_item = str(tx.get("counterparty_info") or "").strip()
    if not resolved_item:
        resolved_item = f"{tx['raw_merchant_name']} 거래명세서 확인 품목"

    new_code = account_code or str(tx["account_code"])
    new_name = str(tx["account_name"])
    new_vat_type = str(tx["vat_deduction_type"])
    new_reason = tx.get("non_deduct_reason")

    inferred = _detect_memo_account_override(resolved_item)
    if inferred and not account_code:
        inf_code, inf_name, force_non_ded, inf_reason = inferred
        if not (new_code == "830" and inf_code == "212" and int(tx["total_amount"]) < 300_000):
            new_code = inf_code
            new_name = inf_name
            new_vat_type = "NON_DEDUCTIBLE" if force_non_ded else "DEDUCTIBLE"
            new_reason = inf_reason
    elif new_code == "999":
        new_code = "830"
        new_name = "소모품비"
        new_vat_type = "DEDUCTIBLE"

    sup, vat = calculate_supply_and_vat(int(tx["total_amount"]), new_vat_type)
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
        (new_code, new_name, new_vat_type, new_reason, sup, vat, resolved_item, tx_id),
    )

    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    updated_tx = dict(cursor.fetchone())
    c_code = str(updated_tx.get("card_last4") or "0000")

    final_lines = (
        resolved_lines
        if resolved_lines
        else [{"item": resolved_item, "qty": 1, "price": int(updated_tx["total_amount"])}]
    )

    if file_bytes:
        payload_bytes = file_bytes
        orig_name = file_name or f"statement_tx_{tx_id}.pdf"
    else:
        stmt_html = render_electronic_statement_html(updated_tx, line_items=final_lines)
        payload_bytes = stmt_html.encode("utf-8")
        orig_name = f"statement_tx_{tx_id}.html"

    file_hash = compute_sha256_hex(payload_bytes)
    base_arch = build_archived_filename(
        transacted_at=str(updated_tx["transacted_at"]),
        merchant_name=str(updated_tx["raw_merchant_name"]),
        account_name=str(updated_tx["account_name"]),
        total_amount=int(updated_tx["total_amount"]),
        item_summary=resolved_item,
        original_file_name=orig_name,
    )
    arch_name = f"거래명세서_{base_arch}"

    orig_path, _ = save_receipt_files(
        payload_bytes,
        original_file_name=orig_name,
        archived_file_name=arch_name,
        archive_dir=archive_dir,
        card_last4=c_code,
    )

    cursor.execute(
        "DELETE FROM receipt_ocr_metadata WHERE tx_id = ? AND source_channel IN ('AUTO_SLIP', 'STATEMENT')",
        (tx_id,),
    )
    cursor.execute(
        "DELETE FROM receipt_ocr_metadata WHERE file_hash_sha256 = ?",
        (file_hash,),
    )
    cursor.execute(
        """
        INSERT INTO receipt_ocr_metadata (
            tx_id, source_channel, original_file_path, archived_file_name,
            file_hash_sha256, ocr_biz_reg_no, ocr_total_amount,
            line_items_json, raw_ocr_response, ocr_confidence
        ) VALUES (?, 'STATEMENT', ?, ?, ?, ?, ?, ?, '{"type":"TRANSACTION_STATEMENT"}', 1.0)
        """,
        (
            tx_id,
            orig_path,
            arch_name,
            file_hash,
            updated_tx.get("biz_reg_no"),
            int(updated_tx["total_amount"]),
            json.dumps(final_lines, ensure_ascii=False),
        ),
    )

    return {
        "status": "statement_saved",
        "tx_id": tx_id,
        "item_name": resolved_item,
        "line_items": final_lines,
        "account_code": new_code,
        "account_name": new_name,
        "archived_file_name": arch_name,
    }


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

    head_sample = (raw_content or "")[:1500]
    is_card_statement = any(
        k in head_sample for k in ("승인번호", "이용카드", "이용하신곳", "전표매입", "국내이용금액")
    )
    if portal_info["type"] == "ECOMMERCE_ORDER" and not is_card_statement:
        ecom_res = reconcile_scraped_ecommerce_orders(
            cursor, raw_content, archive_dir=archive_dir
        )
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
            "generated_statements_count": ecom_res["matched_count"],
            "generated_slips_count": slip_res["generated_slips_count"],
        }

    items = parse_scraped_card_statement(raw_content)
    results = [reconcile_single_item(cursor, item) for item in items]
    stage_counts: Dict[str, int] = {}
    for r in results:
        st = r["match_stage"]
        stage_counts[st] = stage_counts.get(st, 0) + 1
        if r.get("action") == "updated" and r.get("tx_id"):
            cursor.execute(
                "DELETE FROM receipt_ocr_metadata WHERE tx_id = ? AND source_channel = 'AUTO_SLIP'",
                (r["tx_id"],),
            )

    cursor.execute(
        """
        UPDATE card_transactions
        SET reconcile_status = 'CANCELED', updated_at = CURRENT_TIMESTAMP
        WHERE card_last4 = 'KB하이'
          AND reconcile_status = 'SMS_ONLY'
          AND raw_merchant_name = '후불하이패스'
          AND EXISTS (
              SELECT 1 FROM card_transactions h
              WHERE h.card_last4 = 'KB하이'
                AND h.reconcile_status = 'HOMETAX_VERIFIED'
                AND ABS(
                    julianday(SUBSTR(card_transactions.transacted_at, 1, 10))
                    - julianday(SUBSTR(h.transacted_at, 1, 10))
                ) <= 5.0
          )
        """
    )
    cursor.execute(
        """
        UPDATE card_transactions
        SET reconcile_status = 'CANCELED', updated_at = CURRENT_TIMESTAMP
        WHERE total_amount < 0
          AND reconcile_status = 'SMS_ONLY'
          AND EXISTS (
              SELECT 1 FROM card_transactions c
              WHERE c.card_last4 = card_transactions.card_last4
                AND c.total_amount = card_transactions.total_amount
                AND c.reconcile_status = 'CANCELED'
                AND c.tx_id != card_transactions.tx_id
                AND ABS(
                    julianday(SUBSTR(card_transactions.transacted_at, 1, 10))
                    - julianday(SUBSTR(c.transacted_at, 1, 10))
                ) <= 5.0
          )
        """
    )

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
