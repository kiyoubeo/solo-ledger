"""Hometax & Corporate Card Purchase History CSV Batch Parser and Reconciler."""

import csv
import io
import re
import sqlite3
from typing import Any, Dict, List

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


def _normalize_datetime_str(raw_dt: str) -> str:
    """Normalize various date/time formats into `YYYY-MM-DD HH:MM:SS`."""
    s = str(raw_dt or "").strip().replace(".", "-").replace("/", "-")
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
    return s


def _extract_card_last4(raw_card: str) -> str:
    cleaned = re.sub(r"[\s\-]", "", str(raw_card or "").strip())
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
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    items: List[ReconcileItem] = []

    for row in reader:
        raw_dt = _pick_col(row, ["거래일시", "승인일시", "이용일시", "transacted_at"])
        if not raw_dt:
            d_part = _pick_col(row, ["거래일자", "승인일자", "이용일자"])
            t_part = _pick_col(row, ["거래시간", "승인시간", "이용시간"], "12:00:00")
            raw_dt = f"{d_part} {t_part}".strip()

        if not raw_dt:
            continue

        transacted_at = _normalize_datetime_str(raw_dt)
        card_last4 = _extract_card_last4(_pick_col(row, ["카드번호", "카드뒷자리", "card_last4"], "0000"))
        approval_no = _pick_col(row, ["승인번호", "approval_no"]) or None
        merchant_name = _pick_col(row, ["가맹점명", "가맹점상호", "상호", "merchant_name"], "미상가맹점")
        biz_reg_no = re.sub(r"\D", "", _pick_col(row, ["사업자등록번호", "사업자번호", "biz_reg_no"])) or None

        total_str = _pick_col(row, ["합계금액", "합계", "결제금액", "승인금액", "매입금액", "total_amount"])
        if total_str:
            total_amount = _parse_int_amount(total_str)
        else:
            supply = _parse_int_amount(_pick_col(row, ["공급가액", "supply_value"]))
            vat = _parse_int_amount(_pick_col(row, ["부가세", "세액", "vat_amount"]))
            total_amount = supply + vat

        cancel_hint = _pick_col(row, ["취소여부", "거래구분", "상태", "is_cancel"])
        is_cancel = ("취소" in cancel_hint) or (cancel_hint.lower() == "true") or (total_amount < 0)
        if is_cancel and total_amount > 0:
            total_amount = -total_amount

        tax_type_raw = _pick_col(row, ["과세유형", "가맹점유형", "사업자유형", "tax_type_code"], "일반과세자")
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
