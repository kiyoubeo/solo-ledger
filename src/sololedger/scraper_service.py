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
from sololedger.merchant_service import ACCOUNT_CODE_NAMES
from sololedger.models import ReconcileItem
from sololedger.nts_client import validate_biz_reg_no_checksum
from sololedger.reconciler import reconcile_single_item
from sololedger.sms_parser import (
    format_card_label,
    is_allowed_corporate_card,
    is_blocked_personal_card_code,
    load_corp_card_policy,
    match_registered_corp_card_code,
    register_corp_card,
    sanitize_card_folder_name,
)
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

    supply_raw = _pick_col(row, ["공급가액", "공급가", "supply_value", "splyamt"])
    vat_raw = _pick_col(row, ["부가세", "부가가치세", "세액", "vat_amount", "vatamt"])
    parsed_supply = _parse_int_amount(supply_raw) if supply_raw else None
    parsed_vat = _parse_int_amount(vat_raw) if vat_raw else None

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
        total_amount = (parsed_supply or 0) + (parsed_vat or 0)

    if total_amount == 0:
        return None

    biz_category = _pick_col(row, ["업태", "biz_category"]) or None
    biz_item = _pick_col(row, ["업종", "종목", "biz_item"]) or None
    hometax_decision = _pick_col(row, ["공제여부결정", "공제여부"]) or None
    hometax_remark = _pick_col(row, ["비고", "불공제사유"]) or None

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
        parsed_supply = None
        parsed_vat = None
    else:
        has_explicit_cancel = (
            ("취소" in cancel_hint)
            or (cancel_hint.lower() in ("true", "y", "cancel", "canceled"))
            or (cancel_amt > 0 and cancel_amt >= abs(total_amount))
        )
        if hometax_decision and not has_explicit_cancel:
            is_cancel = False
        else:
            is_cancel = has_explicit_cancel or (total_amount < 0)
        if is_cancel and total_amount > 0:
            total_amount = -total_amount
            if parsed_supply is not None and parsed_supply > 0:
                parsed_supply = -parsed_supply
            if parsed_vat is not None and parsed_vat > 0:
                parsed_vat = -parsed_vat

    tax_type_raw = _pick_col(
        row,
        ["과세유형", "가맹점유형", "사업자유형", "공제여부", "업종", "tax_type_code", "taxtype"],
        "일반과세자",
    )
    tax_type_code, biz_status = _map_tax_type_and_status(tax_type_raw, hometax_remark or "")

    return ReconcileItem(
        card_last4=card_last4,
        approval_no=approval_no,
        transacted_at=transacted_at,
        merchant_name=merchant_name,
        biz_reg_no=biz_reg_no,
        total_amount=total_amount,
        supply_value=parsed_supply,
        vat_amount=parsed_vat,
        tax_type_code=tax_type_code,
        biz_status=biz_status,
        biz_category=biz_category,
        biz_item=biz_item,
        hometax_deduct_decision=hometax_decision,
        hometax_remark=hometax_remark,
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


def _prorate_line_items_to_amount(
    raw_items: List[Dict[str, Any]],
    target_total: int,
) -> List[Dict[str, Any]]:
    """Scale line-item prices proportionally so item prices sum to exact target_total."""
    if not raw_items or target_total <= 0:
        return [dict(it) for it in (raw_items or [])]
    sum_items = sum(max(0, _parse_int_amount(str(x.get("price") or 0))) for x in raw_items)
    if sum_items <= 0 or sum_items == target_total:
        return [dict(it) for it in raw_items]
    prorated: List[Dict[str, Any]] = []
    running = 0
    for idx, it in enumerate(raw_items):
        raw_p = max(0, _parse_int_amount(str(it.get("price") or 0)))
        if idx < len(raw_items) - 1:
            net_p = int(round(raw_p * target_total / sum_items))
            running += net_p
        else:
            net_p = target_total - running
        entry = dict(it)
        entry["price"] = net_p
        prorated.append(entry)
    return prorated


def _is_direct_coupang_seller_label(seller_name: str) -> bool:
    norm = re.sub(r"[\s()（）\[\]주식회사㈜유한회사]+", "", str(seller_name or "")).lower()
    return norm in ("쿠팡", "coupang")


def _walk_coupang_ssr_json(node: Any, collected: List[Dict[str, Any]]) -> None:
    """Recursively extract orders from Coupang SSR API JSON or __NEXT_DATA__ state."""
    if isinstance(node, list):
        for child in node:
            _walk_coupang_ssr_json(child, collected)
        return
    if not isinstance(node, dict):
        return

    raw_dt = node.get("orderedAt") or node.get("orderDate") or node.get("paidAt")
    has_order_groups = any(
        k in node for k in ("deliveryGroupList", "productList", "bundleReceiptList", "items")
    )
    if raw_dt and has_order_groups:
        if isinstance(raw_dt, (int, float)) and raw_dt > 1_000_000_000_000:
            from datetime import datetime as _dt

            dt_str = _dt.fromtimestamp(raw_dt / 1000.0).strftime("%Y-%m-%d")
        else:
            dt_str = str(raw_dt)[:10].replace(".", "-")

        prod_items: List[tuple[str, int, int, str]] = []
        seen_prod_keys: set[str] = set()

        def _collect_prods(sub: Any, inherited_seller: str = "") -> None:
            if isinstance(sub, list):
                for el in sub:
                    _collect_prods(el, inherited_seller)
                return
            if not isinstance(sub, dict):
                return
            cur_seller = str(
                sub.get("vendorName")
                or sub.get("sellerName")
                or ((sub.get("vendor") or {}).get("name") if isinstance(sub.get("vendor"), dict) else "")
                or inherited_seller
                or ""
            ).strip()
            p_name = (
                sub.get("productName")
                or sub.get("itemName")
                or sub.get("title")
                or sub.get("name")
            )
            comb_price = sub.get("combinedPrice") or sub.get("totalPrice")
            unit_price = sub.get("unitPrice") or sub.get("price") or sub.get("amount")
            p_qty_raw = sub.get("quantity") or sub.get("qty") or sub.get("count") or 1
            if p_name and (comb_price is not None or unit_price is not None):
                qty_val = max(1, _parse_int_amount(str(p_qty_raw)) or 1)
                if comb_price is not None and _parse_int_amount(str(comb_price)) > 0:
                    amt_val = _parse_int_amount(str(comb_price))
                else:
                    amt_val = _parse_int_amount(str(unit_price or 0)) * qty_val
                if amt_val > 0:
                    vid = str(
                        sub.get("vendorItemId")
                        or sub.get("orderItemId")
                        or sub.get("productId")
                        or ""
                    ).strip()
                    p_clean = str(p_name).strip()
                    dedup_key = f"{vid}|{p_clean}|{qty_val}|{amt_val}" if vid else f"{p_clean}|{qty_val}|{amt_val}"
                    if dedup_key not in seen_prod_keys:
                        seen_prod_keys.add(dedup_key)
                        prod_items.append((p_clean, qty_val, amt_val, cur_seller))
                    # Stop recursing inside a matched leaf product dict to prevent N-x duplicate items
                    return
            for v in sub.values():
                if isinstance(v, (list, dict)):
                    _collect_prods(v, cur_seller)

        for k in ("deliveryGroupList", "productList", "bundleReceiptList", "items"):
            if k in node:
                _collect_prods(node[k], "")

        if prod_items:
            items_sum = sum(x[2] for x in prod_items)
            prod_tot = _parse_int_amount(str(node.get("totalProductPrice") or 0)) or items_sum
            card_pay = _parse_int_amount(
                str(
                    node.get("cardPayAmount")
                    or node.get("creditCardAmount")
                    or node.get("totalPayedPrice")
                    or node.get("payedAmount")
                    or node.get("paymentAmount")
                    or node.get("actualPayAmount")
                    or 0
                )
            )
            cancel_amt = _parse_int_amount(
                str(
                    node.get("cancelAmount")
                    or node.get("totalCancelPrice")
                    or node.get("refundAmount")
                    or 0
                )
            )
            eff_tot = card_pay if card_pay > 0 else prod_tot
            if node.get("isCanceled") is True or (cancel_amt > 0 and cancel_amt >= eff_tot):
                return
            if 0 < cancel_amt < eff_tot and not node.get("netAmountAlreadyApplied"):
                eff_tot = eff_tot - cancel_amt

            sellers = [x[3] for x in prod_items if x[3]]
            third_sellers = list(
                dict.fromkeys(s for s in sellers if not _is_direct_coupang_seller_label(s))
            )
            has_cp = any(_is_direct_coupang_seller_label(s) for s in sellers)
            grp_biz = "120-88-00767" if (not third_sellers and has_cp) else ""
            grp_mp = (
                third_sellers[0]
                if (len(third_sellers) == 1 and not has_cp)
                else (f"{third_sellers[0]} 외" if third_sellers else "")
            )
            order_id_str = str(node.get("orderId") or node.get("orderNo") or "").strip()
            pay_obj = node.get("payment") if isinstance(node.get("payment"), dict) else {}
            card_hint = str(
                node.get("cardName")
                or node.get("cardCompanyName")
                or pay_obj.get("cardName")
                or pay_obj.get("cardCompanyName")
                or ""
            ).strip()
            masked_no = str(
                node.get("maskedCardNo")
                or node.get("cardNo")
                or pay_obj.get("maskedCardNo")
                or pay_obj.get("cardNo")
                or ""
            ).strip()
            if masked_no and masked_no not in card_hint:
                card_hint = f"{card_hint} {masked_no}".strip()
            pay_method = str(node.get("payMethod") or pay_obj.get("payMethod") or "").strip()
            app_no = str(node.get("approvalNo") or pay_obj.get("approvalNo") or "").strip()

            base_lines: List[Dict[str, Any]] = [
                (
                    {"item": n, "qty": q, "price": a, "seller": sel}
                    if sel
                    else {"item": n, "qty": q, "price": a}
                )
                for n, q, a, sel in prod_items
            ]
            prorated_lines = _prorate_line_items_to_amount(base_lines, eff_tot)
            summary_title = (
                prod_items[0][0]
                if len(prod_items) == 1
                else f"{prod_items[0][0]} 외 {len(prod_items) - 1}건"
            )
            main_row: Dict[str, Any] = {
                "주문일자": dt_str,
                "상품명": summary_title,
                "결제금액": str(eff_tot),
                "상품합계금액": str(prod_tot),
                "발신처": "쿠팡",
                "주문번호": order_id_str,
                "사업자번호": grp_biz,
                "입점판매자": grp_mp,
                "카드사": card_hint,
                "결제수단": pay_method,
                "승인번호": app_no,
                "품목내역": prorated_lines,
            }
            collected.append(main_row)

            if len(prod_items) >= 2:
                for p_name, p_qty, p_amt, p_sel in prod_items:
                    sub_biz = "120-88-00767" if (p_sel and _is_direct_coupang_seller_label(p_sel)) else ""
                    sub_mp = p_sel if (p_sel and not _is_direct_coupang_seller_label(p_sel)) else ""
                    li_obj = (
                        {"item": p_name, "qty": p_qty, "price": p_amt, "seller": p_sel}
                        if p_sel
                        else {"item": p_name, "qty": p_qty, "price": p_amt}
                    )
                    collected.append(
                        {
                            "주문일자": dt_str,
                            "상품명": p_name,
                            "결제금액": str(p_amt),
                            "발신처": "쿠팡",
                            "주문번호": order_id_str,
                            "부분품목여부": True,
                            "사업자번호": sub_biz,
                            "입점판매자": sub_mp,
                            "카드사": card_hint,
                            "결제수단": pay_method,
                            "품목내역": [li_obj],
                        }
                    )
        # Do not recurse further into an already-parsed order node
        return

    for val in node.values():
        if isinstance(val, (dict, list)):
            _walk_coupang_ssr_json(val, collected)


def _normalize_and_dedup_coupang_order_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Clean duplicate line items and tag standalone sub-items emitted alongside multi-item summaries."""
    if not rows:
        return []

    # 1. Deduplicate identical repeated line items caused by legacy recursive crawlers
    cleaned_rows: List[Dict[str, Any]] = []
    for raw_r in rows:
        r = dict(raw_r)
        if not r.get("발신처"):
            r["발신처"] = "쿠팡"
        raw_li = r.get("품목내역")
        tot_amt = abs(_parse_int_amount(str(r.get("결제금액") or r.get("주문금액") or 0)))
        if isinstance(raw_li, list) and len(raw_li) >= 2 and tot_amt > 0:
            raw_sum = sum(_parse_int_amount(str(x.get("price") or 0)) for x in raw_li if isinstance(x, dict))
            if raw_sum > tot_amt:
                dedup_li: List[Dict[str, Any]] = []
                seen_li: set[str] = set()
                for x in raw_li:
                    if not isinstance(x, dict):
                        continue
                    sig = f"{x.get('item')}|{x.get('qty')}|{_parse_int_amount(str(x.get('price') or 0))}"
                    if sig not in seen_li:
                        seen_li.add(sig)
                        dedup_li.append(dict(x))
                dedup_sum = sum(_parse_int_amount(str(x.get("price") or 0)) for x in dedup_li)
                if len(dedup_li) < len(raw_li) and dedup_sum <= tot_amt:
                    # e.g., single multi-qty item repeated N times -> restore actual line total
                    if len(dedup_li) == 1:
                        dedup_li[0]["price"] = tot_amt
                        r["상품명"] = str(dedup_li[0].get("item") or r.get("상품명") or "")
                    else:
                        dedup_li = _prorate_line_items_to_amount(dedup_li, tot_amt)
                    r["품목내역"] = dedup_li
                else:
                    r["품목내역"] = _prorate_line_items_to_amount(
                        [dict(x) for x in raw_li if isinstance(x, dict)], tot_amt
                    )
            elif raw_sum < tot_amt:
                # Legacy unitPrice stored instead of unitPrice * qty inside 품목내역
                qty_scaled: List[Dict[str, Any]] = []
                for x in raw_li:
                    if not isinstance(x, dict):
                        continue
                    xc = dict(x)
                    q = max(1, _parse_int_amount(str(xc.get("qty") or 1)) or 1)
                    p = _parse_int_amount(str(xc.get("price") or 0))
                    xc["price"] = p * q if q > 1 else p
                    qty_scaled.append(xc)
                if sum(int(x["price"]) for x in qty_scaled) == tot_amt:
                    r["품목내역"] = qty_scaled
                else:
                    r["품목내역"] = _prorate_line_items_to_amount(
                        [dict(x) for x in raw_li if isinstance(x, dict)], tot_amt
                    )
        elif (
            isinstance(raw_li, list)
            and len(raw_li) == 1
            and isinstance(raw_li[0], dict)
            and not r.get("상품합계금액")
            and tot_amt > 0
        ):
            q1 = max(1, _parse_int_amount(str(raw_li[0].get("qty") or 1)) or 1)
            p1 = _parse_int_amount(str(raw_li[0].get("price") or 0))
            if q1 > 1 and p1 == tot_amt:
                r["상품합계금액"] = str(p1 * q1)
        cleaned_rows.append(r)

    # 2. Identify sub-items that belong to a multi-item summary order on the same date
    multi_child_sigs_by_date: Dict[str, set[str]] = {}
    for r in cleaned_rows:
        dt_k = _normalize_datetime_str(
            _pick_col(r, ["주문일시", "결제일시", "주문일자", "결제일자", "일자", "order_date", "date"])
        )[:10]
        raw_li = r.get("품목내역")
        if dt_k and isinstance(raw_li, list) and len(raw_li) >= 2:
            bucket = multi_child_sigs_by_date.setdefault(dt_k, set())
            for x in raw_li:
                if isinstance(x, dict) and x.get("item"):
                    bucket.add(str(x["item"]).strip())

    primary_rows: List[Dict[str, Any]] = []
    sub_rows: List[Dict[str, Any]] = []
    for r in cleaned_rows:
        if r.get("부분품목여부"):
            sub_rows.append(r)
            continue
        dt_k = _normalize_datetime_str(
            _pick_col(r, ["주문일시", "결제일시", "주문일자", "결제일자", "일자", "order_date", "date"])
        )[:10]
        raw_li = r.get("품목내역")
        title = _pick_col(r, ["상품명", "품목명", "주문상품", "item_name", "product_name", "title"]).strip()
        is_single = not isinstance(raw_li, list) or len(raw_li) <= 1
        if is_single and dt_k in multi_child_sigs_by_date and title in multi_child_sigs_by_date[dt_k]:
            r["부분품목여부"] = True
            sub_rows.append(r)
        else:
            primary_rows.append(r)

    return primary_rows + sub_rows


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
                return _normalize_and_dedup_coupang_order_rows(direct_rows)
            ssr_rows: List[Dict[str, Any]] = []
            _walk_coupang_ssr_json(data, ssr_rows)
            if ssr_rows:
                return _normalize_and_dedup_coupang_order_rows(ssr_rows)
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
            explicit_paid = 0
            m_paid = re.search(
                r"(?:총\s*결제금액|실결제금액|카드결제|신용카드)\s*[:：]?\s*([\d,]{3,})\s*원",
                block,
            )
            if m_paid:
                explicit_paid = _parse_int_amount(m_paid.group(1))
            lines = [re.sub(r"\s+", " ", ln).strip() for ln in block.splitlines()]
            lines = [ln for ln in lines if ln and not _COUPANG_NOISE_RE.match(ln)]

            block_items: List[tuple[str, int, int]] = []
            last_candidate_title = ""
            for ln in lines:
                if re.match(r"^(총\s*결제금액|실결제금액|상품금액|할인금액|배송비)", ln):
                    continue
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

            if len(block_items) >= 2:
                raw_sum = sum(x[2] for x in block_items)
                eff_sum = explicit_paid if explicit_paid > 0 else raw_sum
                summary_title = f"{block_items[0][0]} 외 {len(block_items) - 1}건"
                base_lines = [{"item": n, "qty": q, "price": a} for n, q, a in block_items]
                extracted.append(
                    {
                        "주문일자": dt_str,
                        "상품명": summary_title,
                        "결제금액": str(eff_sum),
                        "상품합계금액": str(raw_sum),
                        "발신처": "쿠팡",
                        "품목내역": _prorate_line_items_to_amount(base_lines, eff_sum),
                    }
                )
                for p_name, p_qty, p_amt in block_items:
                    extracted.append(
                        {
                            "주문일자": dt_str,
                            "상품명": p_name,
                            "결제금액": str(p_amt),
                            "발신처": "쿠팡",
                            "부분품목여부": True,
                            "품목내역": [{"item": p_name, "qty": p_qty, "price": p_amt}],
                        }
                    )
            elif len(block_items) == 1:
                p_name, p_qty, p_amt = block_items[0]
                eff_amt = explicit_paid if explicit_paid > 0 else p_amt
                extracted.append(
                    {
                        "주문일자": dt_str,
                        "상품명": p_name,
                        "결제금액": str(eff_amt),
                        "상품합계금액": str(p_amt),
                        "발신처": "쿠팡",
                        "품목내역": [{"item": p_name, "qty": p_qty, "price": eff_amt}],
                    }
                )
        if extracted:
            return extracted

    reader = csv.DictReader(io.StringIO(content))
    return list(reader)


_COUPANG_JS_COLLECTOR_HELPER = r"""
  const orders = [];
  const rawOrderRecords = [];
  const seenOrderIds = new Set();
  const seenKeys = new Set();

  const parseKrw = (v) => {
    if (v === null || v === undefined) return 0;
    const digits = String(v).replace(/[^0-9-]/g, '');
    const n = parseInt(digits, 10);
    return Number.isFinite(n) ? n : 0;
  };
  const isDirectCoupang = (s) => {
    const norm = String(s || '').replace(/[\s()（）\[\]주식회사㈜유한회사]+/g, '').toLowerCase();
    return norm === '쿠팡' || norm === 'coupang';
  };
  const prorateLines = (lines, targetAmt) => {
    if (!Array.isArray(lines) || lines.length === 0 || targetAmt <= 0) return lines || [];
    const sum = lines.reduce((acc, x) => acc + (Number(x.price) || 0), 0);
    if (sum <= 0 || sum === targetAmt) return lines;
    let running = 0;
    return lines.map((it, idx) => {
      const p = idx < lines.length - 1
        ? Math.round(((Number(it.price) || 0) * targetAmt) / sum)
        : (targetAmt - running);
      if (idx < lines.length - 1) running += p;
      return Object.assign({}, it, { price: p });
    });
  };
  const fetchWithTimeout = async (url, ms = 6500) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms);
    try {
      return await fetch(url, { credentials: 'include', signal: ctrl.signal });
    } finally {
      clearTimeout(timer);
    }
  };
  const extractDetailPaymentMeta = (detailObj, htmlText) => {
    const meta = {
      cardPaidAmt: 0,
      totalPaidAmt: 0,
      cancelAmt: 0,
      isCanceled: false,
      cardHint: '',
      payMethod: '',
      approvalNo: '',
      bizNo: '',
      sellerName: '',
      coupayMoneyOnly: false
    };
    const scanObj = (obj) => {
      if (!obj || typeof obj !== 'object') return;
      if (Array.isArray(obj)) { obj.forEach(scanObj); return; }
      for (const [k, v] of Object.entries(obj)) {
        const kl = k.toLowerCase();
        if (v !== null && typeof v !== 'object') {
          if (['cardpayamount', 'creditcardamount', 'creditcardpayamount', 'cardamount'].includes(kl)) {
            const a = parseKrw(v);
            if (a > meta.cardPaidAmt) meta.cardPaidAmt = a;
          } else if (
            [
              'totalpayedprice', 'payedprice', 'payedamount',
              'paymentamount', 'actualpayamount', 'totalpaymentamount'
            ].includes(kl)
          ) {
            const a = parseKrw(v);
            if (a > meta.totalPaidAmt) meta.totalPaidAmt = a;
          } else if (
            ['cancelamount', 'totalcancelprice', 'refundamount', 'totalrefundamount', 'canceledamount'].includes(kl)
          ) {
            const a = parseKrw(v);
            if (a > meta.cancelAmt) meta.cancelAmt = a;
          } else if (kl === 'iscanceled' || kl === 'canceled') {
            if (v === true || v === 'true' || v === 'Y') meta.isCanceled = true;
          } else if (['cardname', 'cardcompanyname', 'issuername', 'cardissuer'].includes(kl) && v) {
            if (!meta.cardHint) meta.cardHint = String(v).trim();
          } else if (['maskedcardno', 'cardno', 'cardnumber'].includes(kl) && v) {
            const s = String(v).trim();
            if (s && !meta.cardHint.includes(s)) meta.cardHint = (meta.cardHint + ' ' + s).trim();
          } else if (['paymethod', 'paymentmethod', 'paytype', 'paymethodname'].includes(kl) && v) {
            if (!meta.payMethod) meta.payMethod = String(v).trim();
          } else if (['approvalno', 'authno', 'confirmno', 'cardauthno'].includes(kl) && v) {
            const m = String(v).match(/\d{6,12}/);
            if (m && !meta.approvalNo) meta.approvalNo = m[0];
          } else if (['vendorname', 'sellername', 'shopname'].includes(kl) && v) {
            const sv = String(v).trim();
            if (sv && !meta.sellerName) meta.sellerName = sv;
          }
        } else if (v && typeof v === 'object') {
          scanObj(v);
        }
      }
    };
    if (detailObj) scanObj(detailObj);

    if (htmlText) {
      const nextMatch = htmlText.match(/<script[^>]*id=["']__NEXT_DATA__["'][^>]*>([\s\S]*?)<\/script>/i);
      if (nextMatch) {
        try { scanObj(JSON.parse(nextMatch[1])); } catch (_) {}
      }
      const plain = htmlText
        .replace(/<script[\s\S]*?<\/script>/gi, ' ')
        .replace(/<style[\s\S]*?<\/style>/gi, ' ')
        .replace(/<[^>]+>/g, ' ')
        .replace(/&nbsp;/g, ' ')
        .replace(/\s+/g, ' ');
      if (meta.cardPaidAmt <= 0) {
        const mCardAmt = plain.match(
          /(?:카드결제|신용\/체크카드|신용카드|체크카드|쿠페이\s*\(카드\))\s*[:：]?\s*([\d,]{3,})\s*원/
        );
        if (mCardAmt) meta.cardPaidAmt = parseKrw(mCardAmt[1]);
      }
      if (meta.totalPaidAmt <= 0) {
        const mTot = plain.match(/(?:총\s*결제금액|실결제금액|결제금액|합계금액)\s*[:：]?\s*([\d,]{3,})\s*원/);
        if (mTot) meta.totalPaidAmt = parseKrw(mTot[1]);
      }
      if (!meta.cardHint) {
        const cardRe = new RegExp(
          '(우리비씨|우리|국민|KB|신한|삼성|현대|롯데|하나|농협|NH|기업|IBK|비씨|BC)(?:카드|법인)?'
          + '\\s*(?:\\(?\\s*(\\d[\\d*]{3})\\s*\\)?'
          + '|\\d{4}[-\\s]+[\\d*]{4}[-\\s]+[\\d*]{4}[-\\s]+(\\d[\\d*]{3}))?',
          'i'
        );
        const mCard = plain.match(cardRe);
        if (mCard) {
          const tail = mCard[2] || mCard[3] || '';
          meta.cardHint = tail ? `${mCard[1]}카드 (${tail})` : `${mCard[1]}카드`;
        }
      }
      if (!meta.approvalNo) {
        const mApp = plain.match(/승인번호\s*[:：]?\s*(\d{6,12})/);
        if (mApp) meta.approvalNo = mApp[1];
      }
      if (!meta.payMethod && /쿠페이\s*머니/.test(plain) && meta.cardPaidAmt <= 0 && !meta.cardHint) {
        const mCm = plain.match(/쿠페이\s*머니\s*[:：]?\s*([\d,]{3,})\s*원/);
        if (mCm && meta.totalPaidAmt > 0 && parseKrw(mCm[1]) >= meta.totalPaidAmt) {
          meta.payMethod = '쿠페이머니';
          meta.coupayMoneyOnly = true;
        }
      }
    }
    if (
      meta.payMethod
      && /쿠페이\s*머니|coupay\s*money/i.test(meta.payMethod)
      && meta.cardPaidAmt <= 0
      && !meta.cardHint
    ) {
      meta.coupayMoneyOnly = true;
    }
    return meta;
  };

  // Smooth scroll visible order list first for DOM fallback
  for (let sIdx = 0; sIdx < 8; sIdx++) {
    window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
    await new Promise((r) => setTimeout(r, 250));
  }
  const domTexts = [document.body ? document.body.innerText : ''];

  // Step 1: Paginate /ssr/api/myorders/model/page across dynamic years (up to 200 pages/year)
  const curYr = new Date().getFullYear();
  const yearsToScan = [0, curYr, curYr - 1, curYr - 2];
  for (const yr of yearsToScan) {
    let emptyStreak = 0;
    for (let page = 0; page < 200; page++) {
      let data = null;
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          const yrLbl = yr || '최근6개월';
          banner.textContent =
            `🛒 쿠팡 주문목록 스크롤 수집 중 (${yrLbl} · 페이지 ${page + 1} · 누적 ${rawOrderRecords.length}건)...`;
          const u = `/ssr/api/myorders/model/page?requestYear=${yr}&pageIndex=${page}&size=10`;
          const resp = await fetchWithTimeout(u, 8000);
          if (!resp.ok) { data = null; break; }
          data = await resp.json();
          break;
        } catch (_) {
          await new Promise((r) => setTimeout(r, 300));
        }
      }
      if (!data) {
        emptyStreak++;
        if (emptyStreak >= 2) break;
        continue;
      }
      const str = JSON.stringify(data);
      if (!str || str.length < 40) break;
      const topList = data.orderList || data.orders || (data.data && data.data.orderList);
      if (Array.isArray(topList) && topList.length === 0) break;

      let pageFoundOrders = 0;
      const walkOrders = (node) => {
        if (!node || typeof node !== 'object') return;
        if (Array.isArray(node)) { node.forEach(walkOrders); return; }
        const rawDt = node.orderedAt || node.orderDate || node.paidAt;
        const groupSrc = node.deliveryGroupList || node.productList || node.bundleReceiptList || node.items;
        if (rawDt && groupSrc) {
          pageFoundOrders++;
          const oid = String(node.orderId || node.orderNo || '').trim();
          if (oid && seenOrderIds.has(oid)) return;
          if (oid) seenOrderIds.add(oid);

          const dt = typeof rawDt === 'number'
            ? new Date(rawDt + 9 * 3600 * 1000).toISOString().slice(0, 10)
            : String(rawDt).slice(0, 10).replace(/\./g, '-');
          const items = [];
          const seenItemKeys = new Set();
          const findProds = (s, inheritedSeller) => {
            if (!s || typeof s !== 'object') return;
            if (Array.isArray(s)) { s.forEach((el) => findProds(el, inheritedSeller)); return; }
            const curSeller = String(
              s.vendorName || s.sellerName || (s.vendor && s.vendor.name) || inheritedSeller || ''
            ).trim();
            const pName = s.productName || s.itemName || s.title;
            const combAmt = parseKrw(s.combinedPrice || s.totalPrice);
            const unitAmt = parseKrw(s.unitPrice || s.price || s.amount);
            const pQty = Math.max(1, parseInt(String(s.quantity || s.qty || s.count || 1), 10) || 1);
            if (pName && (combAmt > 0 || unitAmt > 0)) {
              const lineAmt = combAmt > 0 ? combAmt : (unitAmt * pQty);
              const vid = String(s.vendorItemId || s.orderItemId || s.productId || '').trim();
              const cleanName = String(pName).trim();
              const ikey = (vid ? vid + '|' : '') + cleanName + '|' + pQty + '|' + lineAmt;
              if (!seenItemKeys.has(ikey) && lineAmt > 0) {
                seenItemKeys.add(ikey);
                items.push([cleanName, pQty, lineAmt, curSeller]);
              }
              return;
            }
            Object.values(s).forEach((child) => findProds(child, curSeller));
          };
          findProds(groupSrc, '');
          if (items.length > 0) {
            const itemsSum = items.reduce((acc, x) => acc + x[2], 0);
            const prodTot = parseKrw(node.totalProductPrice) || itemsSum;
            const listPaid = parseKrw(node.cardPayAmount || node.totalPayedPrice || node.paymentAmount);
            rawOrderRecords.push({
              orderId: oid,
              dt,
              items,
              prodTot,
              listPaid,
              rawNode: node
            });
          }
          return;
        }
        Object.values(node).forEach(walkOrders);
      };
      walkOrders(data);

      if (pageFoundOrders === 0) {
        emptyStreak++;
        if (emptyStreak >= 2) break;
      } else {
        emptyStreak = 0;
      }
      if (data.hasNext === false || (data.data && data.data.hasNext === false)) break;
    }
  }

  // Step 2: Enrich each order via Order Detail / Receipt API in parallel batches of 6
  const batchSize = 6;
  for (let i = 0; i < rawOrderRecords.length; i += batchSize) {
    const batch = rawOrderRecords.slice(i, i + batchSize);
    const doneCnt = Math.min(i + batchSize, rawOrderRecords.length);
    banner.textContent =
      `🧾 주문상세·카드영수증 실결제금액 확인 중 (${doneCnt}/${rawOrderRecords.length}건)...`;
    await Promise.all(batch.map(async (rec) => {
      if (!rec.orderId) return;
      let detailObj = null;
      let detailHtml = '';
      for (const ep of [
        `/ssr/api/myorders/model/order/${rec.orderId}`,
        `/ssr/desktop/order/detail/${rec.orderId}`
      ]) {
        try {
          const r = await fetchWithTimeout(ep, 5500);
          if (!r.ok) continue;
          const ct = (r.headers.get('content-type') || '').toLowerCase();
          if (ct.includes('json')) {
            detailObj = await r.json();
            break;
          } else {
            detailHtml = await r.text();
            if (detailHtml && detailHtml.length > 200) break;
          }
        } catch (_) {}
      }
      rec.detailMeta = extractDetailPaymentMeta(detailObj || rec.rawNode, detailHtml);
    }));
  }

  // Step 3: Build normalized SoloLedger order list
  const pushUnique = (obj) => {
    const sig = `${obj.주문일자}|${obj.상품명}|${obj.결제금액}|${obj.부분품목여부 ? 'sub' : 'main'}`;
    if (seenKeys.has(sig)) return;
    seenKeys.add(sig);
    orders.push(obj);
  };

  for (const rec of rawOrderRecords) {
    const dm = rec.detailMeta || {};
    if (dm.isCanceled || dm.coupayMoneyOnly) continue;
    let effPaid = dm.cardPaidAmt > 0
      ? dm.cardPaidAmt
      : (dm.totalPaidAmt > 0 ? dm.totalPaidAmt : (rec.listPaid > 0 ? rec.listPaid : rec.prodTot));
    if (dm.cancelAmt > 0) {
      if (dm.cancelAmt >= effPaid) continue;
      if (dm.cardPaidAmt <= 0 && effPaid === rec.prodTot) effPaid = effPaid - dm.cancelAmt;
    }
    if (effPaid <= 0) continue;

    const baseLines = rec.items.map(([n, q, a, sel]) => {
      const effSel = sel || dm.sellerName || '';
      return effSel ? { item: n, qty: q, price: a, seller: effSel } : { item: n, qty: q, price: a };
    });
    const finalLines = prorateLines(baseLines, effPaid);
    const sellers = finalLines.map((x) => x.seller).filter(Boolean);
    const third = [...new Set(sellers.filter((s) => !isDirectCoupang(s)))];
    const hasCp = sellers.some(isDirectCoupang);
    const grpBiz = (third.length === 0 && hasCp) ? '120-88-00767' : (dm.bizNo || '');
    const grpMp = third.length > 0
      ? (third.length === 1 && !hasCp ? third[0] : (third[0] + ' 외'))
      : '';
    const title = rec.items.length === 1
      ? rec.items[0][0]
      : `${rec.items[0][0]} 외 ${rec.items.length - 1}건`;

    pushUnique({
      주문일자: rec.dt,
      상품명: title,
      결제금액: Number(effPaid),
      상품합계금액: Number(rec.prodTot),
      발신처: '쿠팡',
      주문번호: rec.orderId || '',
      사업자번호: grpBiz,
      입점판매자: grpMp,
      카드사: dm.cardHint || '',
      결제수단: dm.payMethod || '',
      승인번호: dm.approvalNo || '',
      품목내역: finalLines
    });

    if (rec.items.length >= 2) {
      rec.items.forEach(([n, q, a, sel]) => {
        const effSel = sel || dm.sellerName || '';
        const bNo = effSel && isDirectCoupang(effSel) ? '120-88-00767' : '';
        const mp = effSel && !isDirectCoupang(effSel) ? effSel : '';
        const li = effSel ? { item: n, qty: q, price: a, seller: effSel } : { item: n, qty: q, price: a };
        pushUnique({
          주문일자: rec.dt,
          상품명: n,
          결제금액: Number(a),
          발신처: '쿠팡',
          주문번호: rec.orderId || '',
          부분품목여부: true,
          사업자번호: bNo,
          입점판매자: mp,
          카드사: dm.cardHint || '',
          결제수단: dm.payMethod || '',
          품목내역: [li]
        });
      });
    }
  }
"""


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
{_COUPANG_JS_COLLECTOR_HELPER}
  const pageText = domTexts.join('\\n') || document.body.innerText || '';
  const mainCount = orders.filter((o) => !o.부분품목여부).length;
  const payload = orders.length > 0 ? JSON.stringify(orders, null, 2) : pageText;

  const blob = new Blob([payload], {{ type: 'application/json;charset=utf-8' }});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'coupang_orders_' + new Date().toISOString().slice(0, 10).replace(/-/g, '') + '.json';
  document.body.appendChild(a);
  a.click();
  a.remove();

  try {{
    const res = await fetch(API_URL + '/api/v1/scraper/ingest', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'X-API-Key': API_KEY }},
      body: JSON.stringify({{ portal: 'COUPANG', raw_content: payload, auto_generate_slips: true }})
    }});
    const resJson = await res.json();
    banner.style.background = '#15803d';
    banner.textContent = `✅ SoloLedger 쿠팡 매칭 완료! (${{resJson.matched_orders || 0}}건 반영)`;
    alert(`✅ 쿠팡 주문내역 수집 및 SoloLedger 자동 매칭 완료!\\n`
      + `- 수집 주문 건수: ${{mainCount || orders.length}}건\\n`
      + `- 원장 전표 반영: ${{resJson.matched_orders || 0}}건 (신규 생성: ${{resJson.created_orders || 0}}건)\\n`
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

_CDP_COLLECT_JS = f"""(async () => {{
  const banner = document.createElement('div');
  banner.style.cssText = 'position:fixed;top:16px;right:16px;z-index:999999;background:#1e40af;color:#fff;'
    + 'padding:14px 20px;border-radius:10px;font-size:14px;font-weight:700;box-shadow:0 8px 24px rgba(0,0,0,0.25);';
  banner.textContent = '🛒 SoloLedger 쿠팡 주문내역 자동 수집 중...';
  if (document.body) document.body.appendChild(banner);
{_COUPANG_JS_COLLECTOR_HELPER}
  const mainOrdersCount = orders.filter((o) => !o.부분품목여부).length;
  banner.style.background = '#15803d';
  banner.textContent = `✅ 수집 완료 (${{mainOrdersCount || orders.length}}건)! SoloLedger에 반영 중...`;
  return {{
    ordersCount: mainOrdersCount || orders.length,
    rawEntriesCount: orders.length,
    jsonPayload: JSON.stringify(orders),
    domText: domTexts.join('\\n')
  }};
}})()"""


def _fetch_cdp_page_ws_url(debug_port: int) -> Optional[str]:
    """Return the active Coupang page WebSocket debugger URL from local CDP port."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{debug_port}/json/list", timeout=2.5) as resp:
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
    max_wait_sec: int = 240,
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

    eff_wait = max(15, int(max_wait_sec))
    deadline = time.time() + eff_wait
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
                ws = _CdpWebSocket(ws_url, timeout=15.0)
                try:
                    st_msg = ws.call(
                        "Runtime.evaluate",
                        {"expression": _CDP_STATUS_JS, "returnByValue": True},
                        msg_id=1,
                    )
                    st_val = st_msg.get("result", {}).get("result", {}).get("value") or {}
                    last_state = str(st_val.get("state") or "LOADING")
                    if last_state == "READY":
                        # Extend socket timeout so multi-year pagination + detail fetch never aborts at 30s
                        if getattr(ws, "sock", None) is not None:
                            ws.sock.settimeout(max(180.0, float(eff_wait)))
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
    raw_to_ingest = json_payload if json_payload.strip() not in ("", "[]") else dom_text

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
    if json_payload.strip() not in ("", "[]") and dom_text.strip():
        dom_outcome = reconcile_scraped_ecommerce_orders(
            cursor, dom_text, archive_dir=base_archive
        )
        extra_ids = [
            tid
            for tid in (dom_outcome.get("updated_tx_ids") or [])
            if tid not in (outcome.get("updated_tx_ids") or [])
        ]
        if extra_ids:
            outcome["updated_tx_ids"] = list(outcome.get("updated_tx_ids") or []) + extra_ids
            outcome["matched_orders"] = len(outcome["updated_tx_ids"])
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
    "n페이",
    "엔페이",
    "naver",
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
    "케이에스넷",
    "ksnet",
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


_CARD_ISSUER_TOKEN_MAP = (
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
)

_CARD_ISSUER_CONFLICT_SET = {"국민", "현대", "롯데", "삼성", "신한", "하나", "농협", "우리"}


def _extract_card_issuer_canonical(text: str) -> str:
    """Return canonical Korean card issuer name from an issuer hint or card label."""
    low = str(text or "").strip().lower()
    if not low:
        return ""
    for token, canonical in _CARD_ISSUER_TOKEN_MAP:
        if token in low:
            return canonical
    return ""


_STALE_POLLUTED_MEMO_MARKERS = (
    "구매를 확정",
    "자동구매확정",
    "발송 안내",
    "KSNET을 통하여 결제",
    "SmartroPay 결제",
)


_CORP_CARD_HINT_KEYWORD_RE = re.compile(
    r"(법인카드|기업카드|사업자카드|법인승인|기업승인|"
    r"(?:우리비씨|우리|국민|KB|신한|삼성|현대|롯데|하나|농협|NH|기업|IBK|비씨|BC)\s*법인)",
    re.IGNORECASE,
)

_CORP_CARD_BIN6_PREFIXES = (
    "910020",  # 우리비씨 기업/법인카드
    "9430",    # BC 기업/법인카드 계열
    "9411",    # 기업카드 계열
)

_PERSONAL_CARD_BIN6_PREFIXES = (
    "940915",  # 롯데 개인카드
    "950001",  # 우리 개인카드
    "379183",  # 삼성 개인 AMEX
)

_PG_INTERMEDIARY_BIZ_NOS_SET = frozenset(
    {
        "1058765162",  # 쿠팡페이
        "5248601528",  # 네이버파이낸셜
        "4118601799",  # 토스페이먼츠
        "2208155597",  # KG이니시스
        "1138621083",  # NHN KCP
        "1138521083",  # NHN KCP
        "1048150311",  # 한국정보통신(이지페이)
        "1168119948",  # 나이스페이먼츠
        "2208115770",  # 나이스정보통신
        "5278800696",  # 카카오페이
        "1208197322",  # KSNET
        "2178114493",  # 스마트로
        "1148628575",  # 다날
        "5638100723",  # NHN페이코
    }
)


def _extract_hint_card_last4(hint: str) -> str:
    """Extract masked/last-4 card digits from a payment method hint, preferring 4-group card tails."""
    text = str(hint or "").strip()
    if not text:
        return ""
    m_masked = re.search(
        r"\d{4}[-\s]+[\dXx*]{2,4}[-\s]+[\dXx*]{2,4}[-\s]+((?=\S*\d)[\d*]{4})",
        text,
    )
    if m_masked:
        return m_masked.group(1)
    m_contig = re.search(
        r"(?<!\d)\d{6}[\dXx*]{5,6}((?=\S*\d)[\d*]{4})(?!\d)",
        text,
    )
    if m_contig:
        return m_contig.group(1)
    m_digits = re.search(
        r"(?:[()\[\]\-\s]|카드|법인|체크|^)(\d[\d*]{3})(?:[()\[\]\s]|님|승인|결제|일시불|$)",
        text,
    )
    return m_digits.group(1) if m_digits else ""


def _extract_hint_card_bin6(hint: str) -> str:
    """Extract 6-digit BIN prefix from a masked card number inside a payment hint if present."""
    text = str(hint or "").strip()
    if not text:
        return ""
    m_hyph = re.search(
        r"(?<!\d)(\d{4})[-\s]+(\d{2})[\dXx*]{0,2}[-\s]+[\dXx*]{2,4}[-\s]+[\d*]{4}",
        text,
    )
    if m_hyph:
        return f"{m_hyph.group(1)}{m_hyph.group(2)}"
    m_contig = re.search(r"(?<!\d)(\d{6})[\dXx*]{5,6}[\d*]{4}(?!\d)", text)
    if m_contig:
        return m_contig.group(1)
    return ""


def _has_corporate_card_text_evidence(hint: str) -> bool:
    """Return True if a payment/card string contains corporate BIN or explicit corporate card terms."""
    text = str(hint or "").strip()
    if not text:
        return False
    bin6 = _extract_hint_card_bin6(text)
    if bin6 and any(bin6.startswith(p) for p in _PERSONAL_CARD_BIN6_PREFIXES):
        return False
    if bin6 and any(bin6.startswith(p) for p in _CORP_CARD_BIN6_PREFIXES):
        return True
    return bool(_CORP_CARD_HINT_KEYWORD_RE.search(text))


def _resolve_card_last4_from_issuer(
    cursor: sqlite3.Cursor,
    card_hint: str,
    merchant_name: str,
) -> str:
    """Resolve card_last4 from PG email issuer hint ('국민', '현대', etc.) or merchant history."""
    hint = str(card_hint or "").strip()
    extracted_last4 = _extract_hint_card_last4(hint)
    if extracted_last4:
        return extracted_last4

    issuer_kw = _extract_card_issuer_canonical(hint)

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


def clean_valid_biz_reg_no(raw_biz_no: Optional[str]) -> Optional[str]:
    """Normalize a 10-digit Korean business registration number and validate its checksum."""
    if not raw_biz_no:
        return None
    digits = re.sub(r"\D", "", str(raw_biz_no))
    if len(digits) == 10 and validate_biz_reg_no_checksum(digits):
        return digits
    return None


def format_biz_reg_no_display(
    biz_reg_no: Optional[str],
    marketplace_seller: Optional[str] = None,
    fallback: str = "미등록 (온라인/PG 가맹점)",
) -> str:
    """Format a 10-digit biz_reg_no as XXX-XX-XXXXX or show marketplace seller notice."""
    raw = str(biz_reg_no or "").strip()
    if raw:
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
        return raw
    mp = str(marketplace_seller or "").strip()
    if mp:
        return f"미등록 (입점판매자: {mp} · 홈택스 대사 대기)"
    return fallback


def _ensure_merchant_master(
    cursor: sqlite3.Cursor,
    biz_reg_no: str,
    merchant_name: str,
) -> None:
    """Ensure foreign-key target row exists in `merchants` without overwriting NTS cache."""
    if not biz_reg_no:
        return
    m_name = (merchant_name or "온라인가맹점").strip() or "온라인가맹점"
    is_pg = 1 if any(k in m_name.lower() for k in _ONLINE_OR_PG_MERCHANT_KEYWORDS) else 0
    cursor.execute(
        """
        INSERT INTO merchants (
            biz_reg_no, merchant_name, tax_type_code, biz_status, is_pg_provider
        ) VALUES (?, ?, 'GENERAL', 'ACTIVE', ?)
        ON CONFLICT(biz_reg_no) DO NOTHING
        """,
        (biz_reg_no, m_name, is_pg),
    )


_COUPANG_ELIGIBLE_MERCHANT_KEYWORDS = (
    "쿠팡",
    "쿠페이",
    "coupang",
    "coupay",
    "케이씨피",
    "kcp",
    "한국정보통신",
    "kicc",
    "이지페이",
    "easypay",
    "이니시스",
    "케이지이니시스",
    "토스",
    "나이스",
    "다날",
    "페이코",
    "카카오페이",
    "kakaopay",
    "네이버페이",
    "naverpay",
    "삼성페이",
    "스마트로",
    "케이에스넷",
    "ksnet",
    "갤럭시아",
    "세틀뱅크",
    "모빌리언스",
    "헥토파이낸셜",
    "다우데이타",
)


def _latest_non_email_statement_date(cursor: sqlite3.Cursor) -> str:
    """Return latest YYYY-MM-DD of non-email card transactions (or '0000-00-00' if empty)."""
    cursor.execute(
        """
        SELECT MAX(SUBSTR(transacted_at, 1, 10))
        FROM card_transactions
        WHERE LENGTH(transacted_at) >= 10
          AND reconcile_status NOT IN ('EMAIL_ORDER', 'CANCELED')
        """
    )
    row = cursor.fetchone()
    return str(row[0]) if row and row[0] else "0000-00-00"


def _tx_candidate_passes_filter(
    cand: sqlite3.Row,
    *,
    app_no: str,
    norm_shop: str,
    portal_hint: str,
    is_coupang_order: bool,
    hint_last4: str,
    hint_issuer: str,
    corp_only_mode: bool,
    archive_dir: Optional[Path],
) -> bool:
    """Return True if a ledger transaction row is a valid merchant & card match candidate."""
    cand_merch = str(cand["raw_merchant_name"] or "")
    cand_merch_low = cand_merch.lower()
    norm_cand = _norm_merchant_token(cand_merch)
    cand_app = str(cand["approval_no"] or "").strip()
    is_app_match = bool(app_no and cand_app == app_no)

    if not is_app_match:
        cand_last4 = str(cand["card_last4"] or "").strip()
        if hint_last4 and cand_last4 and cand_last4 != "0000":
            matched_hint = match_registered_corp_card_code(hint_last4, archive_dir=archive_dir) or hint_last4
            if cand_last4 != matched_hint and cand_last4 != hint_last4:
                return False
        if (
            corp_only_mode
            and cand_last4
            and cand_last4 != "0000"
            and hint_issuer in _CARD_ISSUER_CONFLICT_SET
        ):
            cand_issuer = _extract_card_issuer_canonical(
                format_card_label(cand_last4, archive_dir=archive_dir)
            )
            if cand_issuer in _CARD_ISSUER_CONFLICT_SET and cand_issuer != hint_issuer:
                return False

    is_shop_match = bool(
        len(norm_shop) >= 2
        and len(norm_cand) >= 2
        and (norm_shop in norm_cand or norm_cand in norm_shop)
    )
    if is_app_match or is_shop_match:
        return True

    cand_biz = re.sub(r"\D", "", str(cand["biz_reg_no"] or ""))
    if is_coupang_order:
        if cand_biz in ("1208800767", "1058765162"):
            return True
        return any(kw in cand_merch_low for kw in _COUPANG_ELIGIBLE_MERCHANT_KEYWORDS)

    if cand_biz in _PG_INTERMEDIARY_BIZ_NOS_SET:
        return True
    if any(kw in cand_merch_low for kw in _ONLINE_OR_PG_MERCHANT_KEYWORDS):
        return True
    if str(cand["reconcile_status"] or "") == "EMAIL_ORDER":
        return True
    if not portal_hint and not norm_shop and str(cand["account_code"] or "") == "999":
        return True
    return False


def _auto_reconcile_coupang_wow_membership(
    cursor: sqlite3.Cursor,
    used_tx_ids: set[int],
    archive_dir: Optional[Path] = None,
) -> List[int]:
    """Recognize unmatched Coupang/Coupay Wow Membership monthly fee rows (7,890 / 4,990 KRW)."""
    cursor.execute(
        """
        SELECT t.*
        FROM card_transactions t
        LEFT JOIN receipt_ocr_metadata r
          ON r.tx_id = t.tx_id AND r.source_channel = 'STATEMENT'
        WHERE t.reconcile_status != 'CANCELED'
          AND t.total_amount IN (7890, 4990)
          AND (t.raw_merchant_name LIKE '%쿠팡%' OR t.raw_merchant_name LIKE '%쿠페이%')
          AND (t.counterparty_info IS NULL OR TRIM(t.counterparty_info) = '')
          AND r.receipt_id IS NULL
        ORDER BY t.transacted_at DESC, t.tx_id DESC
        """
    )
    wow_rows = cursor.fetchall()
    matched_ids: List[int] = []
    for wr in wow_rows:
        tid = int(wr["tx_id"])
        if tid in used_tx_ids:
            continue
        ex_biz = str(wr["biz_reg_no"] or "").strip()
        if not ex_biz or ex_biz == "1058765162":
            _ensure_merchant_master(cursor, "1208800767", "쿠팡(주)")
            cursor.execute(
                """
                UPDATE card_transactions
                SET biz_reg_no = '1208800767', updated_at = CURRENT_TIMESTAMP
                WHERE tx_id = ?
                """,
                (tid,),
            )
        amt = int(wr["total_amount"])
        generate_electronic_statement_for_tx(
            cursor,
            tx_id=tid,
            item_name="와우 멤버십 월회비",
            line_items=[{"item": "로켓와우클럽 월회비", "qty": 1, "price": amt, "seller": "쿠팡(주)"}],
            archive_dir=archive_dir,
        )
        used_tx_ids.add(tid)
        matched_ids.append(tid)
    return matched_ids


def reconcile_scraped_ecommerce_orders(
    cursor: sqlite3.Cursor,
    raw_content: str,
    archive_dir: Optional[Path] = None,
    orders_list: Optional[List[Dict[str, Any]]] = None,
    auto_create_after_date: Optional[str] = None,
    auto_create_all: bool = False,
    allow_corp_auto_create: bool = True,
) -> Dict[str, Any]:
    """Match scraped Coupang / NaverPay / KakaoPay / PG order items against card transactions."""
    content = (raw_content or "").strip()
    if not content and not orders_list:
        return {
            "status": "empty",
            "matched_count": 0,
            "created_count": 0,
            "newly_stated_count": 0,
            "skipped_personal_count": 0,
            "created_tx_ids": [],
            "newly_stated_tx_ids": [],
            "updated_tx_ids": [],
            "matched_items": [],
        }

    order_rows = orders_list if orders_list is not None else extract_coupang_orders_from_content(content)

    corp_policy = load_corp_card_policy(archive_dir)
    corp_only = bool(corp_policy.get("corp_only_mode"))
    updated_tx_ids: List[int] = []
    created_tx_ids: List[int] = []
    newly_stated_tx_ids: List[int] = []
    matched_items: List[Dict[str, Any]] = []
    used_tx_ids: set[int] = set()
    skipped_personal_count = 0

    # Pre-parse and partition into primary orders vs split-shipment sub-item fallbacks
    parsed_entries: List[Dict[str, Any]] = []
    sub_item_titles_by_date: Dict[str, set[str]] = {}
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

        pay_method_str = str(row.get("결제수단") or "").strip()
        card_col_str = str(row.get("카드사") or row.get("결제기관") or "").strip()
        card_no_str = str(row.get("카드번호") or "").strip()
        if (
            re.search(
                r"쿠페이\s*머니|coupay\s*money|네이버페이\s*(?:포인트|머니)|카카오페이\s*머니|무통장|계좌이체",
                pay_method_str,
                re.I,
            )
            and not card_col_str
            and not card_no_str
        ):
            skipped_personal_count += 1
            continue

        shop_name = str(row.get("상점명") or row.get("가맹점명") or "").strip()
        portal_hint = str(row.get("발신처") or "").strip()
        app_no = str(row.get("승인번호") or "").strip()
        order_no = str(row.get("주문번호") or "").strip()
        card_hint = f"{card_col_str} {card_no_str} {pay_method_str}".strip()
        explicit_c4 = str(row.get("결제카드코드") or "").strip()
        hint_last4 = explicit_c4 or _extract_hint_card_last4(card_hint)
        hint_bin6 = _extract_hint_card_bin6(card_hint)
        hint_issuer = _extract_card_issuer_canonical(card_hint)
        if corp_only:
            if hint_last4 and is_blocked_personal_card_code(hint_last4, archive_dir=archive_dir):
                skipped_personal_count += 1
                continue
            if hint_bin6 and any(hint_bin6.startswith(p) for p in _PERSONAL_CARD_BIN6_PREFIXES):
                skipped_personal_count += 1
                continue
        matched_corp_c4 = (
            match_registered_corp_card_code(hint_last4, archive_dir=archive_dir)
            if hint_last4
            else None
        )
        is_confirmed_corp = bool(
            row.get("법인카드확정여부")
            or matched_corp_c4
            or _has_corporate_card_text_evidence(card_hint)
        )
        clean_biz_no = clean_valid_biz_reg_no(
            str(row.get("사업자번호") or row.get("biz_reg_no") or "")
        )
        mp_seller = str(row.get("입점판매자") or row.get("marketplace_seller") or "").strip()
        norm_shop = _norm_merchant_token(shop_name)
        prod_sum_amt = abs(_parse_int_amount(str(row.get("상품합계금액") or 0)))
        is_sub_item = bool(row.get("부분품목여부") or row.get("is_sub_item"))
        comb_portal = f"{portal_hint} {shop_name}".lower()
        is_coupang_order = any(k in comb_portal for k in ("쿠팡", "coupang", "쿠페이", "coupay"))

        if is_sub_item:
            sub_item_titles_by_date.setdefault(date_prefix, set()).add(item_name.strip())

        parsed_entries.append(
            {
                "row": row,
                "norm_dt": norm_dt,
                "date_prefix": date_prefix,
                "amount": amount,
                "prod_sum_amt": prod_sum_amt,
                "item_name": item_name,
                "shop_name": shop_name,
                "portal_hint": portal_hint,
                "app_no": app_no,
                "order_no": order_no,
                "card_hint": card_hint,
                "hint_last4": hint_last4,
                "hint_bin6": hint_bin6,
                "hint_issuer": hint_issuer,
                "matched_corp_c4": matched_corp_c4,
                "is_confirmed_corp": is_confirmed_corp,
                "clean_biz_no": clean_biz_no,
                "mp_seller": mp_seller,
                "norm_shop": norm_shop,
                "is_sub_item": is_sub_item,
                "is_coupang_order": is_coupang_order,
            }
        )

    # Self-heal any offline non-PG merchant previously polluted by a Coupang sub-item
    if sub_item_titles_by_date:
        for dt_p, titles in sub_item_titles_by_date.items():
            for t_str in titles:
                cursor.execute(
                    """
                    SELECT t.tx_id, t.raw_merchant_name
                    FROM card_transactions t
                    JOIN receipt_ocr_metadata r
                      ON r.tx_id = t.tx_id AND r.source_channel = 'STATEMENT'
                    WHERE SUBSTR(t.transacted_at, 1, 10) = ?
                      AND t.counterparty_info = ?
                      AND t.reconcile_status = 'HOMETAX_VERIFIED'
                    """,
                    (dt_p, t_str),
                )
                for p_row in cursor.fetchall():
                    m_low = str(p_row["raw_merchant_name"] or "").lower()
                    if not any(kw in m_low for kw in _ONLINE_OR_PG_MERCHANT_KEYWORDS):
                        bad_tid = int(p_row["tx_id"])
                        cursor.execute(
                            "DELETE FROM receipt_ocr_metadata WHERE tx_id = ? AND source_channel = 'STATEMENT'",
                            (bad_tid,),
                        )
                        cursor.execute(
                            """
                            UPDATE card_transactions
                            SET counterparty_info = NULL,
                                account_code = '999',
                                account_name = '미분류',
                                updated_at = CURRENT_TIMESTAMP
                            WHERE tx_id = ?
                            """,
                            (bad_tid,),
                        )

    def _find_exact_candidate(entry: Dict[str, Any], max_days: float = 3.0) -> Optional[sqlite3.Row]:
        cand_amounts = [entry["amount"]]
        if entry["prod_sum_amt"] > 0 and entry["prod_sum_amt"] != entry["amount"]:
            cand_amounts.append(entry["prod_sum_amt"])
        app_no = entry["app_no"]
        date_prefix = entry["date_prefix"]
        for target_amt in cand_amounts:
            cursor.execute(
                """
                SELECT * FROM card_transactions
                WHERE total_amount = ?
                  AND ABS(julianday(SUBSTR(transacted_at, 1, 10)) - julianday(?)) <= ?
                  AND reconcile_status != 'CANCELED'
                ORDER BY
                  CASE WHEN ? != '' AND approval_no = ? THEN 0 ELSE 1 END ASC,
                  CASE
                    WHEN raw_merchant_name LIKE '%쿠팡%'
                      OR raw_merchant_name LIKE '%쿠페이%'
                      OR raw_merchant_name LIKE '%네이버%'
                      OR raw_merchant_name LIKE '%카카오%'
                      OR raw_merchant_name LIKE '%이니시스%'
                      OR raw_merchant_name LIKE '%토스%'
                      OR raw_merchant_name LIKE '%케이씨피%'
                      OR raw_merchant_name LIKE '%KCP%'
                      OR raw_merchant_name LIKE '%나이스%'
                      OR raw_merchant_name LIKE '%다날%'
                      OR raw_merchant_name LIKE '%페이코%'
                      OR raw_merchant_name LIKE '%케이에스넷%'
                      OR raw_merchant_name LIKE '%스마트로%'
                      OR raw_merchant_name LIKE '%한국정보통신%'
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
                (target_amt, date_prefix, max_days, app_no, app_no, date_prefix),
            )
            for cand in cursor.fetchall():
                cid = int(cand["tx_id"])
                if cid in used_tx_ids:
                    continue
                if _tx_candidate_passes_filter(
                    cand,
                    app_no=app_no,
                    norm_shop=entry["norm_shop"],
                    portal_hint=entry["portal_hint"],
                    is_coupang_order=entry["is_coupang_order"],
                    hint_last4=entry["hint_last4"],
                    hint_issuer=entry["hint_issuer"],
                    corp_only_mode=corp_only,
                    archive_dir=archive_dir,
                ):
                    return cand
        return None

    def _apply_match_or_create(
        entry: Dict[str, Any],
        tx_row: Optional[sqlite3.Row],
        *,
        allow_create: bool,
    ) -> bool:
        row = entry["row"]
        norm_dt = entry["norm_dt"]
        date_prefix = entry["date_prefix"]
        amount = entry["amount"]
        item_name = entry["item_name"]
        shop_name = entry["shop_name"]
        app_no = entry["app_no"]
        card_hint = entry["card_hint"]
        hint_last4 = entry["hint_last4"]
        matched_corp_c4 = entry["matched_corp_c4"]
        is_confirmed_corp = entry["is_confirmed_corp"]
        clean_biz_no = entry["clean_biz_no"]
        mp_seller = entry["mp_seller"]
        norm_shop = entry["norm_shop"]

        is_newly_created = False
        biz_backfilled = False
        if tx_row is None:
            if not allow_create:
                return False
            if (
                corp_only
                and corp_policy.get("block_unmatched_email_orders", True)
                and not auto_create_all
                and not is_confirmed_corp
            ):
                nonlocal skipped_personal_count
                skipped_personal_count += 1
                return False
            should_auto_create = (
                auto_create_all
                or (allow_corp_auto_create and is_confirmed_corp)
                or (auto_create_after_date is not None and date_prefix > auto_create_after_date)
            )
            if not should_auto_create:
                return False

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
                return False

            full_ts = (
                norm_dt[:19]
                if len(norm_dt) >= 19 and norm_dt[11:19] != "00:00:00"
                else f"{date_prefix} 12:00:00"
            )
            inferred_merchant = _infer_merchant_for_email_order(cursor, row)
            if clean_biz_no:
                _ensure_merchant_master(cursor, clean_biz_no, shop_name or inferred_merchant)
            resolved_card = (
                matched_corp_c4
                or (hint_last4 if (is_confirmed_corp and len(hint_last4) == 4) else "")
                or _resolve_card_last4_from_issuer(cursor, card_hint, inferred_merchant)
            )
            if (
                is_confirmed_corp
                and not matched_corp_c4
                and len(hint_last4) == 4
                and hint_last4 != "0000"
                and corp_policy.get("auto_register_on_statement_upload", True)
            ):
                issuer_map = {
                    "우리": "WOORI",
                    "국민": "KB",
                    "신한": "SHINHAN",
                    "하나": "HANA",
                    "삼성": "SAMSUNG",
                    "현대": "HYUNDAI",
                    "롯데": "LOTTE",
                    "농협": "NH",
                    "기업": "IBK",
                }
                register_corp_card(
                    card_last4=hint_last4,
                    issuer=issuer_map.get(entry["hint_issuer"], "CORP"),
                    archive_dir=archive_dir,
                )
            elif corp_only and not is_confirmed_corp and not is_allowed_corporate_card(
                resolved_card, raw_text=card_hint, archive_dir=archive_dir
            ):
                skipped_personal_count += 1
                return False
            rule_eval = evaluate_tax_rules(cursor, inferred_merchant, clean_biz_no)
            sup_val, vat_val = calculate_supply_and_vat(amount, rule_eval["vat_deduction_type"])
            cursor.execute(
                """
                INSERT INTO card_transactions (
                    card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
                    total_amount, supply_value, vat_amount,
                    account_code, account_name, vat_deduction_type,
                    non_deduct_reason, is_vehicle_expense, counterparty_info, reconcile_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'EMAIL_ORDER')
                """,
                (
                    resolved_card,
                    app_no or None,
                    full_ts,
                    inferred_merchant,
                    clean_biz_no,
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
                return False
            is_newly_created = True
            created_tx_ids.append(new_tx_id)
        else:
            ex_id = int(tx_row["tx_id"])
            ex_app = str(tx_row["approval_no"] or "").strip()
            ex_card = str(tx_row["card_last4"] or "0000")
            ex_biz = str(tx_row["biz_reg_no"] or "").strip()
            ex_status = str(tx_row["reconcile_status"] or "")
            new_card = (
                (
                    matched_corp_c4
                    or _resolve_card_last4_from_issuer(
                        cursor, card_hint, str(tx_row["raw_merchant_name"])
                    )
                )
                if card_hint and (ex_card == "0000" or ex_status == "EMAIL_ORDER")
                else ex_card
            )
            can_backfill_biz = bool(
                clean_biz_no
                and (not ex_biz or (ex_biz == "1058765162" and clean_biz_no == "1208800767"))
            )
            if can_backfill_biz and clean_biz_no:
                _ensure_merchant_master(
                    cursor, clean_biz_no, shop_name or str(tx_row["raw_merchant_name"])
                )
            if (
                (app_no and not ex_app)
                or (new_card != ex_card and new_card != "0000")
                or can_backfill_biz
            ):
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET approval_no = COALESCE(approval_no, ?),
                        card_last4 = ?,
                        biz_reg_no = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tx_id = ?
                    """,
                    (
                        app_no or None,
                        new_card if new_card != "0000" else ex_card,
                        clean_biz_no if can_backfill_biz else (ex_biz or None),
                        ex_id,
                    ),
                )
                cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (ex_id,))
                tx_row = cursor.fetchone() or tx_row
                biz_backfilled = can_backfill_biz

        tx_id = int(tx_row["tx_id"])
        used_tx_ids.add(tx_id)
        final_item_name = item_name
        if shop_name and shop_name not in final_item_name:
            norm_tx_merch = _norm_merchant_token(str(tx_row["raw_merchant_name"] or ""))
            if norm_shop and norm_shop not in norm_tx_merch and norm_tx_merch not in norm_shop:
                final_item_name = f"[{shop_name}] {final_item_name}"

        raw_lines = row.get("품목내역") or row.get("line_items")
        row_line_items = (
            _prorate_line_items_to_amount(raw_lines, int(tx_row["total_amount"]))
            if isinstance(raw_lines, list) and len(raw_lines) > 0 and int(tx_row["total_amount"]) > 0
            else None
        )

        cursor.execute(
            """
            SELECT archived_file_name, line_items_json, raw_ocr_response
            FROM receipt_ocr_metadata
            WHERE tx_id = ? AND source_channel = 'STATEMENT'
            ORDER BY receipt_id DESC LIMIT 1
            """,
            (tx_id,),
        )
        prev_stmt = cursor.fetchone()
        prev_memo = str(tx_row["counterparty_info"] or "").strip()
        prev_mp_seller = ""
        prev_parsed_lines: Optional[List[Dict[str, Any]]] = None
        if prev_stmt:
            if prev_stmt["line_items_json"]:
                try:
                    p_li = json.loads(str(prev_stmt["line_items_json"]))
                    if isinstance(p_li, list):
                        prev_parsed_lines = p_li
                except Exception:
                    pass
            if prev_stmt["raw_ocr_response"]:
                try:
                    p_ocr = json.loads(str(prev_stmt["raw_ocr_response"]))
                    if isinstance(p_ocr, dict):
                        prev_mp_seller = str(p_ocr.get("marketplace_seller") or "").strip()
                except Exception:
                    pass

        mp_changed = bool(mp_seller and mp_seller != prev_mp_seller)
        prev_is_polluted = any(marker in prev_memo for marker in _STALE_POLLUTED_MEMO_MARKERS)
        if prev_stmt and not prev_is_polluted and (not row_line_items or len(row_line_items) <= 1):
            keep_existing = False
            if (
                re.search(r"포함\s*총\s*\d+", final_item_name)
                and prev_parsed_lines
                and len(prev_parsed_lines) >= 2
            ):
                keep_existing = True
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
                if not biz_backfilled and not mp_changed:
                    updated_tx_ids.append(tx_id)
                    return True
                final_item_name = prev_memo or final_item_name
                if prev_parsed_lines:
                    row_line_items = prev_parsed_lines

        if (
            is_newly_created
            or prev_stmt is None
            or prev_memo != final_item_name
            or biz_backfilled
            or mp_changed
        ):
            newly_stated_tx_ids.append(tx_id)

        effective_mp_seller = mp_seller or prev_mp_seller
        stmt_res = generate_electronic_statement_for_tx(
            cursor,
            tx_id=tx_id,
            item_name=final_item_name,
            line_items=row_line_items,
            marketplace_seller=effective_mp_seller or None,
            archive_dir=archive_dir,
        )
        updated_tx_ids.append(tx_id)
        tx_biz_raw = str(tx_row["biz_reg_no"] or "").strip()
        tx_c4 = str(tx_row["card_last4"] or "0000")
        matched_items.append(
            {
                "tx_id": tx_id,
                "action": "CREATED" if is_newly_created else "MATCHED",
                "transacted_at": str(tx_row["transacted_at"]),
                "card_last4": tx_c4,
                "card_label": format_card_label(tx_c4, archive_dir=archive_dir),
                "approval_no": str(tx_row["approval_no"] or app_no or ""),
                "order_no": entry["order_no"],
                "portal_source": entry["portal_hint"] or "카드결제",
                "raw_merchant_name": str(tx_row["raw_merchant_name"]),
                "biz_reg_no": format_biz_reg_no_display(tx_biz_raw) if tx_biz_raw else "",
                "marketplace_seller": effective_mp_seller,
                "item_name": stmt_res["item_name"],
                "line_items_count": len(stmt_res.get("line_items") or []),
                "total_amount": int(tx_row["total_amount"]),
                "supply_value": int(tx_row["supply_value"]),
                "vat_amount": int(tx_row["vat_amount"]),
                "account_code": stmt_res["account_code"],
                "account_name": stmt_res["account_name"],
                "archived_file_name": stmt_res["archived_file_name"],
                "is_created": is_newly_created,
                "is_newly_stated": tx_id in newly_stated_tx_ids,
            }
        )
        return True

    primary_entries = [e for e in parsed_entries if not e["is_sub_item"]]
    sub_entries = [e for e in parsed_entries if e["is_sub_item"]]
    matched_parent_keys: set[str] = set()
    unmatched_primary: List[Dict[str, Any]] = []

    # Pass 1A: Exact amount matches across all primary orders first
    for entry in primary_entries:
        exact_cand = _find_exact_candidate(entry, max_days=3.0)
        if exact_cand is not None:
            if _apply_match_or_create(entry, exact_cand, allow_create=False):
                if entry["order_no"]:
                    matched_parent_keys.add(f"oid:{entry['order_no']}")
                matched_parent_keys.add(f"dt:{entry['date_prefix']}")
        else:
            unmatched_primary.append(entry)

    # Pass 1B: Coupang discount / Coupang Cash / shipping-fee tolerance best-fit matching
    coupang_fuzzy_pool = [
        (idx, e) for idx, e in enumerate(unmatched_primary) if e["is_coupang_order"]
    ]
    fuzzy_matched_indices: set[int] = set()
    if coupang_fuzzy_pool:
        scored_pairs: List[tuple[tuple[int, float, float, int], int, sqlite3.Row]] = []
        for idx, entry in coupang_fuzzy_pool:
            date_prefix = entry["date_prefix"]
            ref_amounts = [entry["amount"]]
            if entry["prod_sum_amt"] > 0 and entry["prod_sum_amt"] != entry["amount"]:
                ref_amounts.append(entry["prod_sum_amt"])

            cursor.execute(
                """
                SELECT t.*, ABS(julianday(SUBSTR(t.transacted_at, 1, 10)) - julianday(?)) AS day_diff
                FROM card_transactions t
                LEFT JOIN receipt_ocr_metadata r
                  ON r.tx_id = t.tx_id AND r.source_channel = 'STATEMENT'
                WHERE t.reconcile_status != 'CANCELED'
                  AND t.total_amount > 0
                  AND r.receipt_id IS NULL
                  AND ABS(julianday(SUBSTR(t.transacted_at, 1, 10)) - julianday(?)) <= 1.5
                """,
                (date_prefix, date_prefix),
            )
            for cand in cursor.fetchall():
                cid = int(cand["tx_id"])
                if cid in used_tx_ids:
                    continue
                if not _tx_candidate_passes_filter(
                    cand,
                    app_no=entry["app_no"],
                    norm_shop=entry["norm_shop"],
                    portal_hint=entry["portal_hint"],
                    is_coupang_order=True,
                    hint_last4=entry["hint_last4"],
                    hint_issuer=entry["hint_issuer"],
                    corp_only_mode=corp_only,
                    archive_dir=archive_dir,
                ):
                    continue
                cand_amt = int(cand["total_amount"])
                day_diff = float(cand["day_diff"] or 0.0)
                m_low = str(cand["raw_merchant_name"] or "").lower()
                cand_biz = re.sub(r"\D", "", str(cand["biz_reg_no"] or ""))
                is_direct_cp = (
                    "쿠팡" in m_low
                    or "쿠페이" in m_low
                    or cand_biz in ("1208800767", "1058765162")
                )
                if not is_direct_cp and ("카카오" in m_low or "네이버" in m_low):
                    continue
                is_generic_pg = 0 if is_direct_cp else 1
                # Generic PG rows require exact same date to avoid cross-day false matches
                if is_generic_pg == 1 and day_diff > 0.5:
                    continue

                for ref_amt in ref_amounts:
                    is_discount_ok = (
                        0 < (ref_amt - cand_amt) <= max(3000, int(ref_amt * 0.28))
                        and cand_amt >= int(ref_amt * 0.65)
                    )
                    ship_delta = cand_amt - ref_amt
                    is_shipping_ok = ship_delta > 0 and (
                        ship_delta in (2500, 3000, 3500, 4000, 5000)
                        or (ref_amt < 30000 and 2000 <= ship_delta <= 5000)
                    )
                    if is_discount_ok or is_shipping_ok:
                        abs_diff = abs(cand_amt - ref_amt)
                        rel_diff = abs_diff / float(max(1, ref_amt))
                        score = (is_generic_pg, day_diff, rel_diff, abs_diff)
                        scored_pairs.append((score, idx, cand))

        scored_pairs.sort(key=lambda x: x[0])
        for _, idx, cand in scored_pairs:
            if idx in fuzzy_matched_indices:
                continue
            cid = int(cand["tx_id"])
            if cid in used_tx_ids:
                continue
            entry = unmatched_primary[idx]
            if _apply_match_or_create(entry, cand, allow_create=False):
                fuzzy_matched_indices.add(idx)
                if entry["order_no"]:
                    matched_parent_keys.add(f"oid:{entry['order_no']}")
                matched_parent_keys.add(f"dt:{entry['date_prefix']}")

    # Pass 1C: Auto-create post-statement corporate card orders for remaining unmatched primary orders
    for idx, entry in enumerate(unmatched_primary):
        if idx in fuzzy_matched_indices:
            continue
        if _apply_match_or_create(entry, None, allow_create=True):
            if entry["order_no"]:
                matched_parent_keys.add(f"oid:{entry['order_no']}")
            matched_parent_keys.add(f"dt:{entry['date_prefix']}")

    # Pass 2: Split-shipment sub-item exact fallback (only against online/PG rows without existing statement)
    for sub_entry in sub_entries:
        if sub_entry["order_no"] and f"oid:{sub_entry['order_no']}" in matched_parent_keys:
            continue
        sub_cand = _find_exact_candidate(sub_entry, max_days=2.0)
        if sub_cand is not None:
            cursor.execute(
                "SELECT 1 FROM receipt_ocr_metadata WHERE tx_id = ? AND source_channel = 'STATEMENT' LIMIT 1",
                (int(sub_cand["tx_id"]),),
            )
            if cursor.fetchone() is None:
                _apply_match_or_create(sub_entry, sub_cand, allow_create=False)

    # Deduplicate updated_tx_ids preserving order
    dedup_updated = list(dict.fromkeys(updated_tx_ids))
    return {
        "status": "matched",
        "parsed_orders_count": len(primary_entries) or len(order_rows),
        "matched_count": len(dedup_updated),
        "created_count": len(created_tx_ids),
        "newly_stated_count": len(newly_stated_tx_ids),
        "skipped_personal_count": skipped_personal_count,
        "created_tx_ids": created_tx_ids,
        "newly_stated_tx_ids": newly_stated_tx_ids,
        "updated_tx_ids": dedup_updated,
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
    biz_no = html.escape(
        format_biz_reg_no_display(tx_row.get("biz_reg_no"), fallback="문자수집(추후대사)")
    )
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
    <h1>신용카드 매출전표</h1>
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
    marketplace_seller: Optional[str] = None,
) -> str:
    """Render a standalone printable Korean Transaction Statement (거래명세서)."""
    c_code = str(tx_row.get("card_last4") or "0000")
    c_label = html.escape(format_card_label(c_code))
    tx_id = int(tx_row["tx_id"])
    t_dt = html.escape(str(tx_row["transacted_at"]))
    md_str = html.escape(str(tx_row["transacted_at"])[5:10].replace("-", "/"))
    app_no = html.escape(str(tx_row.get("approval_no") or f"STMT-{tx_id:06d}"))
    merchant = html.escape(str(tx_row["raw_merchant_name"]))
    biz_no = html.escape(
        format_biz_reg_no_display(
            tx_row.get("biz_reg_no"),
            marketplace_seller=marketplace_seller,
            fallback="미등록 (온라인/PG 가맹점)",
        )
    )
    total_amt = int(tx_row["total_amount"])
    vat_type = str(tx_row.get("vat_deduction_type") or "DEDUCTIBLE")
    supply_val = int(tx_row["supply_value"])
    vat_amt = int(tx_row["vat_amount"])
    if vat_type != "EXEMPT" and vat_amt == 0 and total_amt != 0:
        supply_val, vat_amt = calculate_supply_and_vat(total_amt, "DEDUCTIBLE")
    acc_str = html.escape(f"{tx_row['account_code']} {tx_row['account_name']}")
    raw_memo = str(tx_row.get("counterparty_info") or "").strip()
    if raw_memo:
        item_title = html.escape(raw_memo)
    elif str(tx_row.get("account_code") or "") == "999":
        item_title = html.escape("품목 미확인 (미분류 — 거래내역 확인 필요)")
    else:
        item_title = html.escape(f"{tx_row['raw_merchant_name']} 구매 품목")
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
            li_seller = str(li.get("seller") or "").strip()
            seller_suffix = (
                f" <span style='color:#64748b; font-size:11px; font-weight:400;'>"
                f"(판매자: {html.escape(li_seller)})</span>"
                if (marketplace_seller and li_seller)
                else ""
            )
            li_qty = max(1, int(li.get("qty") or 1))
            li_price = int(li.get("price") or total_amt)
            li_sup, li_vat = calculate_supply_and_vat(li_price, vat_type)
            tot_qty += li_qty
            tbody_rows.append(
                f"<tr>"
                f"<td>{md_str}</td>"
                f"<td><strong>{li_name}</strong>{seller_suffix}</td>"
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
    <div class="sub">(전표번호 #{tx_id})</div>
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
    marketplace_seller: Optional[str] = None,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate or attach a Transaction Statement (거래명세서) for a specific transaction."""
    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"Transaction #{tx_id} not found.")
    tx = dict(row)

    resolved_mp_seller = (marketplace_seller or "").strip()
    resolved_lines: Optional[List[Dict[str, Any]]] = line_items
    cursor.execute(
        """
        SELECT archived_file_name, line_items_json, raw_ocr_response
        FROM receipt_ocr_metadata
        WHERE tx_id = ? AND source_channel = 'STATEMENT'
        ORDER BY receipt_id DESC LIMIT 1
        """,
        (tx_id,),
    )
    old_stmt_meta = cursor.fetchone()
    if old_stmt_meta:
        if not resolved_mp_seller and old_stmt_meta["raw_ocr_response"]:
            try:
                old_obj = json.loads(str(old_stmt_meta["raw_ocr_response"]))
                if isinstance(old_obj, dict):
                    resolved_mp_seller = str(old_obj.get("marketplace_seller") or "").strip()
            except Exception:
                pass
        if (
            resolved_lines is None
            and not file_bytes
            and old_stmt_meta["line_items_json"]
            and (
                not item_name
                or item_name.strip() == str(tx.get("counterparty_info") or "").strip()
            )
        ):
            try:
                old_li = json.loads(str(old_stmt_meta["line_items_json"]))
                if isinstance(old_li, list) and len(old_li) > 0:
                    resolved_lines = old_li
            except Exception:
                pass

    resolved_item = (item_name or "").strip()
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
    new_name = (
        ACCOUNT_CODE_NAMES.get(account_code, str(tx["account_name"]))
        if account_code
        else str(tx["account_name"])
    )
    new_vat_type = str(tx["vat_deduction_type"])
    new_reason = tx.get("non_deduct_reason")

    inferred = _detect_memo_account_override(resolved_item)
    if inferred and not account_code:
        inf_code, inf_name, force_non_ded, inf_reason = inferred
        if (
            new_code in ("999", "830")
            and inf_code == "212"
            and int(tx["total_amount"]) < 300_000
            and "비품" not in resolved_item
        ):
            inf_code, inf_name, force_non_ded, inf_reason = ("830", "소모품비", False, None)
        new_code = inf_code
        new_name = inf_name
        new_vat_type = "NON_DEDUCTIBLE" if force_non_ded else "DEDUCTIBLE"
        new_reason = inf_reason
    elif not account_code and new_code == "999":
        if "아톡" in str(tx.get("raw_merchant_name") or ""):
            new_code = "831"
            new_name = "지급수수료"
        else:
            new_code = "830"
            new_name = "소모품비"
        new_vat_type = "DEDUCTIBLE"
    elif account_code and new_code != "999" and new_vat_type in ("PENDING", "PENDING_CHECK"):
        new_vat_type = "DEDUCTIBLE"

    is_veh = 1 if new_code == "822" else 0
    sup, vat = calculate_supply_and_vat(int(tx["total_amount"]), new_vat_type)
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
            reconcile_status = CASE
                WHEN reconcile_status = 'SMS_ONLY' THEN 'OCR_MATCHED'
                ELSE reconcile_status
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE tx_id = ?
        """,
        (new_code, new_name, new_vat_type, new_reason, is_veh, sup, vat, resolved_item, tx_id),
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
        stmt_html = render_electronic_statement_html(
            updated_tx,
            line_items=final_lines,
            marketplace_seller=resolved_mp_seller or None,
        )
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

    if old_stmt_meta:
        old_arch_name = str(old_stmt_meta["archived_file_name"] or "").strip()
        if old_arch_name and old_arch_name != arch_name:
            norm_root = (archive_dir or settings.archive_dir) / "normalized"
            if norm_root.exists():
                for stale_p in norm_root.rglob(old_arch_name):
                    if stale_p.is_file():
                        stale_p.unlink(missing_ok=True)

    cursor.execute(
        "DELETE FROM receipt_ocr_metadata WHERE tx_id = ? AND source_channel IN ('AUTO_SLIP', 'STATEMENT')",
        (tx_id,),
    )
    cursor.execute(
        "DELETE FROM receipt_ocr_metadata WHERE file_hash_sha256 = ?",
        (file_hash,),
    )
    meta_json_obj: Dict[str, Any] = {"type": "TRANSACTION_STATEMENT"}
    if resolved_mp_seller:
        meta_json_obj["marketplace_seller"] = resolved_mp_seller
    cursor.execute(
        """
        INSERT INTO receipt_ocr_metadata (
            tx_id, source_channel, original_file_path, archived_file_name,
            file_hash_sha256, ocr_biz_reg_no, ocr_total_amount,
            line_items_json, raw_ocr_response, ocr_confidence
        ) VALUES (?, 'STATEMENT', ?, ?, ?, ?, ?, ?, ?, 1.0)
        """,
        (
            tx_id,
            orig_path,
            arch_name,
            file_hash,
            updated_tx.get("biz_reg_no"),
            int(updated_tx["total_amount"]),
            json.dumps(final_lines, ensure_ascii=False),
            json.dumps(meta_json_obj, ensure_ascii=False),
        ),
    )

    return {
        "status": "statement_saved",
        "tx_id": tx_id,
        "item_name": resolved_item,
        "line_items": final_lines,
        "marketplace_seller": resolved_mp_seller,
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
              AND NOT EXISTS (
                  SELECT 1 FROM receipt_ocr_metadata r
                  WHERE r.tx_id = t.tx_id
                    AND r.source_channel != 'AUTO_SLIP'
              )
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

        if force_regenerate:
            cursor.execute(
                "DELETE FROM receipt_ocr_metadata WHERE tx_id = ? AND source_channel = 'AUTO_SLIP'",
                (tx_id,),
            )

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
        latest_stmt_dt = _latest_non_email_statement_date(cursor)
        ecom_res = reconcile_scraped_ecommerce_orders(
            cursor,
            raw_content,
            archive_dir=archive_dir,
            auto_create_after_date=latest_stmt_dt,
        )
        upd_ids = list(ecom_res["updated_tx_ids"])
        wow_matched_count = 0
        if portal_key == "COUPANG":
            wow_ids = _auto_reconcile_coupang_wow_membership(
                cursor,
                used_tx_ids=set(upd_ids),
                archive_dir=archive_dir,
            )
            wow_matched_count = len(wow_ids)
            for wid in wow_ids:
                if wid not in upd_ids:
                    upd_ids.append(wid)
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
            "matched_orders": len(upd_ids),
            "created_orders": int(ecom_res.get("created_count") or 0),
            "wow_membership_matched": wow_matched_count,
            "updated_tx_ids": upd_ids,
            "generated_statements_count": len(upd_ids),
            "generated_slips_count": slip_res["generated_slips_count"],
        }

    raw_items = parse_scraped_card_statement(raw_content)
    corp_policy = load_corp_card_policy(archive_dir)
    items: List[ReconcileItem] = []
    skipped_personal_count = 0
    for it in raw_items:
        if not is_allowed_corporate_card(
            it.card_last4, archive_dir=archive_dir, is_statement_upload=True
        ):
            skipped_personal_count += 1
            continue
        if (
            corp_policy.get("corp_only_mode")
            and corp_policy.get("auto_register_on_statement_upload", True)
            and it.card_last4 != "0000"
        ):
            issuer_code = (
                portal_key
                if portal_key not in ("HOMETAX", "CARDSALES")
                else (it.card_issuer or "CORP")
            )
            register_corp_card(it.card_last4, issuer=issuer_code, archive_dir=archive_dir)
        items.append(it)

    used_tx_ids: set[int] = set()
    results: List[Dict[str, Any]] = []
    for item in items:
        r_single = reconcile_single_item(cursor, item, exclude_tx_ids=used_tx_ids)
        if r_single.get("tx_id") is not None:
            used_tx_ids.add(int(r_single["tx_id"]))
        results.append(r_single)
    stage_counts: Dict[str, int] = {}
    for r in results:
        st = r["match_stage"]
        stage_counts[st] = stage_counts.get(st, 0) + 1
        if r.get("action") == "updated" and r.get("tx_id"):
            upd_id = int(r["tx_id"])
            cursor.execute(
                "DELETE FROM receipt_ocr_metadata WHERE tx_id = ? AND source_channel = 'AUTO_SLIP'",
                (upd_id,),
            )
            cursor.execute(
                """
                SELECT r.line_items_json, r.ocr_biz_reg_no, t.biz_reg_no
                FROM receipt_ocr_metadata r
                JOIN card_transactions t ON t.tx_id = r.tx_id
                WHERE r.tx_id = ? AND r.source_channel = 'STATEMENT'
                ORDER BY r.receipt_id DESC LIMIT 1
                """,
                (upd_id,),
            )
            st_row = cursor.fetchone()
            if st_row and st_row["biz_reg_no"] and st_row["ocr_biz_reg_no"] != st_row["biz_reg_no"]:
                prev_li = None
                if st_row["line_items_json"]:
                    try:
                        parsed_li = json.loads(str(st_row["line_items_json"]))
                        if isinstance(parsed_li, list):
                            prev_li = parsed_li
                    except Exception:
                        pass
                generate_electronic_statement_for_tx(
                    cursor, tx_id=upd_id, line_items=prev_li, archive_dir=archive_dir
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
        "skipped_personal_count": skipped_personal_count,
        "stage_breakdown": stage_counts,
        "generated_slips_count": slip_res["generated_slips_count"],
    }
