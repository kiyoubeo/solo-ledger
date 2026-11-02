"""Hometax & Corporate Card Purchase History CSV Batch Parser and Reconciler."""

import csv
import io
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from sololedger.models import ReconcileItem
from sololedger.reconciler import reconcile_single_item


def decode_csv_bytes(raw_bytes: bytes) -> str:
    """Auto-detect UTF-8 (with or without BOM) vs CP949/EUC-KR encoding."""
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _parse_int_amount(val: str) -> int:
    cleaned = re.sub(r"[^\d\-]", "", str(val or "").strip())
    return int(cleaned) if cleaned and cleaned != "-" else 0


def _extract_year_hint(text: str) -> Optional[str]:
    """Extract 4-digit year hint from preamble text like `(2026.10.01 ~ 2026.10.31)`."""
    m = re.search(r"(20\d{2})[.\-/]\d{2}[.\-/]\d{2}", str(text or ""))
    return m.group(1) if m else None


def _normalize_datetime_str(raw_dt: str, default_year: Optional[str] = None) -> str:
    """Normalize various date/time formats into `YYYY-MM-DD HH:MM:SS`."""
    raw_str = str(raw_dt or "").strip()
    m_kor = re.search(
        r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        raw_str,
    )
    if m_kor:
        y, mo, d, h, mi, sec = m_kor.groups()
        if h and mi:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {int(h):02d}:{mi}:{sec or '00'}"
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} 12:00:00"

    s = raw_str.replace(".", "-").replace("/", "-")
    # YYYY-MM-DD HH:MM:SS
    m_full = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?", s)
    if m_full:
        y, mo, d, h, mi, sec = m_full.groups()
        return f"{y}-{mo}-{d} {h}:{mi}:{sec or '00'}"
    # YYYY-MM-DD only
    m_date = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m_date:
        y, mo, d = m_date.groups()
        return f"{y}-{mo}-{d} 12:00:00"
    # MM-DD HH:MM(:SS) without year (e.g. Woori Card `10.24 10:12:43`)
    yr = default_year or str(datetime.now().year)
    m_short_full = re.search(r"(?<!\d)(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?", s)
    if m_short_full:
        mo, d, h, mi, sec = m_short_full.groups()
        return f"{yr}-{mo}-{d} {h}:{mi}:{sec or '00'}"
    # MM-DD only without year
    m_short_date = re.search(r"^(?:0?[1-9]|1[0-2])-(\d{2})$", s)
    if m_short_date:
        parts = s.split("-")
        return f"{yr}-{int(parts[0]):02d}-{int(parts[1]):02d} 12:00:00"
    return s


def _extract_card_last4(raw_card: str) -> str:
    txt = str(raw_card or "").strip()
    if "네이버" in txt and "현대" in txt:
        return "현대NV"
    if "현대백화" in txt:
        return "현대백화"
    if "하이패스" in txt and ("KB" in txt.upper() or "국민" in txt):
        return "KB하이"
    m_last4 = re.findall(r"\d[\d*]{2}[\d*]", txt)
    if m_last4:
        return m_last4[-1]
    cleaned = re.sub(r"[\s\-]", "", txt)
    if len(cleaned) >= 4:
        return cleaned[-4:]
    return "0000"


def _map_tax_type_and_status(raw_tax_type: str) -> tuple[str, str]:
    txt = str(raw_tax_type or "").strip()
    upper = txt.upper()
    if upper in ("GENERAL", "SIMPLIFIED", "EXEMPT", "UNREGISTERED"):
        return upper, "ACTIVE"
    if "폐업" in txt:
        return "SIMPLIFIED", "CLOSED"
    if "휴업" in txt:
        return "SIMPLIFIED", "SUSPENDED"
    if "간이" in txt or "특례" in txt:
        return "SIMPLIFIED", "ACTIVE"
    if "면세" in txt or "비영리" in txt:
        return "EXEMPT", "ACTIVE"
    if "미등록" in txt:
        return "UNREGISTERED", "CLOSED"
    return "GENERAL", "ACTIVE"


def _pick_col(row: Dict[str, Any], candidates: List[str], default: str = "") -> str:
    for key, val in row.items():
        if key is None:
            continue
        clean_key = re.sub(r"\s+", "", str(key)).lower()
        for cand in candidates:
            if cand.lower() in clean_key:
                return str(val or "").strip()
    return default


def parse_hometax_csv(csv_text: str) -> List[ReconcileItem]:
    """Parse Hometax or corporate card statement CSV into ReconcileItem objects."""
    content = (csv_text or "").strip()
    if not content:
        return []

    default_year = _extract_year_hint(content[:400])
    all_rows = list(csv.reader(io.StringIO(content)))
    header_idx = 0
    for idx, cells in enumerate(all_rows[:15]):
        joined = " ".join(str(c) for c in cells)
        if any(k in joined for k in ("일자", "일시", "이용일", "가맹점", "상호", "승인번호", "transacted_at")):
            header_idx = idx
            break

    if header_idx >= len(all_rows):
        return []

    headers = [re.sub(r"\s+", "", str(h)) for h in all_rows[header_idx]]
    items: List[ReconcileItem] = []

    for cells in all_rows[header_idx + 1 :]:
        if not any(str(c).strip() for c in cells):
            continue
        row = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}
        raw_dt = _pick_col(row, ["거래일시", "승인일시", "이용일시", "이용일", "transacted_at"])
        if not raw_dt:
            d_part = _pick_col(row, ["거래일자", "승인일자", "이용일자"])
            if d_part:
                t_part = _pick_col(row, ["거래시간", "승인시간", "이용시간"], "12:00:00")
                raw_dt = f"{d_part} {t_part}".strip()

        if not raw_dt:
            continue

        row_year = default_year or _extract_year_hint(_pick_col(row, ["결제예정일", "청구일자"]))
        transacted_at = _normalize_datetime_str(raw_dt, default_year=row_year)
        card_last4 = _extract_card_last4(
            _pick_col(row, ["카드번호", "카드뒷자리", "이용카드", "card_last4"], "0000")
        )
        approval_no = _pick_col(row, ["승인번호", "approval_no"]) or None
        merchant_name = _pick_col(
            row, ["가맹점명", "가맹점상호", "이용가맹점", "상호", "merchant_name"], "미상가맹점"
        )
        biz_reg_no = (
            re.sub(r"\D", "", _pick_col(row, ["사업자등록번호", "사업자번호", "biz_reg_no"])) or None
        )

        total_str = _pick_col(
            row, ["합계금액", "합계", "결제금액", "승인금액", "이용금액", "매입금액", "total_amount"]
        )
        if total_str:
            total_amount = _parse_int_amount(total_str)
        else:
            supply = _parse_int_amount(_pick_col(row, ["공급가액", "supply_value"]))
            vat = _parse_int_amount(_pick_col(row, ["부가세", "세액", "vat_amount"]))
            total_amount = supply + vat

        cancel_amt = abs(_parse_int_amount(_pick_col(row, ["취소금액", "cancel_amount"])))
        cancel_hint = _pick_col(row, ["취소여부", "거래구분", "상태", "결제예정일", "is_cancel"])
        is_cancel = (
            ("취소" in cancel_hint)
            or (cancel_hint.lower() == "true")
            or (total_amount < 0)
            or (cancel_amt > 0 and cancel_amt >= abs(total_amount))
        )
        if is_cancel and total_amount > 0:
            total_amount = -total_amount

        tax_type_raw = _pick_col(
            row, ["과세유형", "가맹점유형", "사업자유형", "tax_type_code"], "일반과세자"
        )
        tax_type_code, biz_status = _map_tax_type_and_status(tax_type_raw)

        items.append(
            ReconcileItem(
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
        )

    return items


def import_and_reconcile_hometax_csv(
    cursor: sqlite3.Cursor, csv_text: str
) -> Dict[str, Any]:
    """Parse Hometax CSV and execute 3-stage reconciliation for all rows."""
    items = parse_hometax_csv(csv_text)
    results = [reconcile_single_item(cursor, item) for item in items]

    stage_counts: Dict[str, int] = {}
    for r in results:
        st = r["match_stage"]
        stage_counts[st] = stage_counts.get(st, 0) + 1

    return {
        "status": "imported",
        "total_rows": len(results),
        "stage_breakdown": stage_counts,
        "results": results,
    }
