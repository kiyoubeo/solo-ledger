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


def _walk_coupang_ssr_json(node: Any, collected: List[Dict[str, str]]) -> None:
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

        prod_items: List[tuple[str, int]] = []

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
                if p_name and p_price is not None:
                    amt_val = _parse_int_amount(str(p_price))
                    if amt_val > 0:
                        prod_items.append((str(p_name).strip(), amt_val))
                for v in sub.values():
                    if isinstance(v, (list, dict)):
                        _collect_prods(v)

        for k in ("deliveryGroupList", "productList", "bundleReceiptList", "items"):
            if k in node:
                _collect_prods(node[k])

        for p_name, p_amt in prod_items:
            collected.append({"주문일자": dt_str, "상품명": p_name, "결제금액": str(p_amt)})

        tot_pay = (
            node.get("totalPayedPrice")
            or node.get("totalProductPrice")
            or node.get("paymentAmount")
            or (sum(x[1] for x in prod_items) if len(prod_items) >= 2 else 0)
        )
        tot_int = _parse_int_amount(str(tot_pay or 0))
        if prod_items and tot_int > 0 and len(prod_items) >= 2:
            summary_title = f"{prod_items[0][0]} 외 {len(prod_items) - 1}건"
            collected.append({"주문일자": dt_str, "상품명": summary_title, "결제금액": str(tot_int)})

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
            ssr_rows: List[Dict[str, str]] = []
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
        extracted: List[Dict[str, str]] = []
        for idx, m in enumerate(matches):
            y, mo, d = m.groups()
            dt_str = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            start_pos = m.end()
            end_pos = matches[idx + 1].start() if idx + 1 < len(matches) else len(plain)
            block = plain[start_pos:end_pos]
            lines = [re.sub(r"\s+", " ", ln).strip() for ln in block.splitlines()]
            lines = [ln for ln in lines if ln and not _COUPANG_NOISE_RE.match(ln)]

            block_items: List[tuple[str, int]] = []
            last_candidate_title = ""
            for ln in lines:
                price_m = re.search(r"([\d,]{3,})\s*원", ln)
                if price_m:
                    amt = _parse_int_amount(price_m.group(1))
                    before_price = ln[: price_m.start()].strip(" ·-:|")
                    title = before_price if len(before_price) >= 2 else last_candidate_title
                    if title and amt > 0:
                        block_items.append((title, amt))
                        last_candidate_title = ""
                elif len(ln) >= 2 and not ln.isdigit():
                    last_candidate_title = ln

            for p_name, p_amt in block_items:
                extracted.append({"주문일자": dt_str, "상품명": p_name, "결제금액": str(p_amt)})
            if len(block_items) >= 2:
                total_sum = sum(x[1] for x in block_items)
                summary_title = f"{block_items[0][0]} 외 {len(block_items) - 1}건"
                extracted.append(
                    {"주문일자": dt_str, "상품명": summary_title, "결제금액": str(total_sum)}
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
  const addOrder = (dt, name, amt) => {{
    if (!dt || !name || !amt || amt <= 0) return;
    const key = dt + '|' + name + '|' + amt;
    if (seen.has(key)) return;
    seen.add(key);
    orders.push({{ 주문일자: dt, 상품명: String(name).trim(), 결제금액: Number(amt) }});
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
              ? new Date(rawDt).toISOString().slice(0, 10)
              : String(rawDt).slice(0, 10).replace(/\\./g, '-');
            const items = [];
            const findProds = (s) => {{
              if (!s || typeof s !== 'object') return;
              if (Array.isArray(s)) {{ s.forEach(findProds); return; }}
              const pName = s.productName || s.itemName || s.title;
              const pAmt = s.combinedPrice || s.totalPrice || s.unitPrice || s.price;
              if (pName && pAmt) {{
                const nAmt = parseInt(String(pAmt).replace(/[^0-9]/g, ''), 10);
                if (nAmt > 0) items.push([String(pName).trim(), nAmt]);
              }}
              Object.values(s).forEach(findProds);
            }};
            findProds(node.deliveryGroupList || node.productList || node.bundleReceiptList || node.items);
            items.forEach(([n, a]) => addOrder(dt, n, a));
            const tot = parseInt(String(node.totalPayedPrice || node.totalProductPrice || 0).replace(/[^0-9]/g, ''), 10)
              || items.reduce((acc, x) => acc + x[1], 0);
            if (items.length >= 2 && tot > 0) {{
              addOrder(dt, `${{items[0][0]}} 외 ${{items.length - 1}}건`, tot);
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


def reconcile_scraped_ecommerce_orders(
    cursor: sqlite3.Cursor,
    raw_content: str,
) -> Dict[str, Any]:
    """Match scraped Coupang / NaverPay / KakaoPay order items against PG card transactions."""
    content = (raw_content or "").strip()
    if not content:
        return {"status": "empty", "matched_count": 0, "updated_tx_ids": []}

    order_rows = extract_coupang_orders_from_content(content)

    updated_tx_ids: List[int] = []
    used_tx_ids: set[int] = set()
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
            ORDER BY
              CASE
                WHEN raw_merchant_name LIKE '%쿠팡%'
                  OR raw_merchant_name LIKE '%네이버%'
                  OR raw_merchant_name LIKE '%카카오%'
                  OR raw_merchant_name LIKE '%이니시스%' THEN 0
                ELSE 1
              END ASC,
              CASE
                WHEN counterparty_info IS NULL OR TRIM(counterparty_info) = '' THEN 0
                ELSE 1
              END ASC,
              ABS(julianday(SUBSTR(transacted_at, 1, 10)) - julianday(?)) ASC,
              tx_id DESC
            """,
            (amount, date_prefix, date_prefix),
        )
        candidates = cursor.fetchall()
        tx_row = next((c for c in candidates if int(c["tx_id"]) not in used_tx_ids), None)
        if tx_row is None:
            continue

        tx_id = int(tx_row["tx_id"])
        used_tx_ids.add(tx_id)
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
        cursor.execute(
            "DELETE FROM receipt_ocr_metadata WHERE tx_id = ? AND source_channel = 'AUTO_SLIP'",
            (tx_id,),
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

    head_sample = (raw_content or "")[:1500]
    is_card_statement = any(
        k in head_sample for k in ("승인번호", "이용카드", "이용하신곳", "전표매입", "국내이용금액")
    )
    if portal_info["type"] == "ECOMMERCE_ORDER" and not is_card_statement:
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
