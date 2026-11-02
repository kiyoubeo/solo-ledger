"""Hometax & Corporate Card Purchase History CSV Batch Parser and Reconciler."""

import csv
import io
import re
import sqlite3
import struct
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional

from sololedger.models import ReconcileItem
from sololedger.reconciler import reconcile_single_item


def _decode_rk_number(rk: int) -> float:
    if rk & 0x02:
        signed_rk = rk if rk < 0x80000000 else rk - 0x100000000
        val = float(signed_rk >> 2)
    else:
        val = struct.unpack("<d", struct.pack("<II", 0, rk & ~3))[0]
    if rk & 0x01:
        val /= 100.0
    return val


def _format_xls_num(num: float) -> str:
    if num == int(num):
        return str(int(num))
    return str(num)


def _convert_biff8_xls_to_csv(data: bytes) -> Optional[str]:
    """Parse binary OLE2 Compound Document / BIFF8 Excel (.xls) into CSV text."""
    try:
        if len(data) < 512 or not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return None
        sec_shift = struct.unpack_from("<H", data, 30)[0]
        sec_size = 1 << sec_shift
        dir_first_sec = struct.unpack_from("<I", data, 48)[0]
        min_std_size = struct.unpack_from("<I", data, 56)[0]
        short_fat_first = struct.unpack_from("<i", data, 60)[0]
        difat_first = struct.unpack_from("<i", data, 68)[0]
        num_difat = struct.unpack_from("<I", data, 72)[0]
        difat = list(struct.unpack_from("<109i", data, 76))
        s = difat_first
        for _ in range(num_difat):
            if s < 0:
                break
            off = (s + 1) * sec_size
            vals = struct.unpack_from(f"<{sec_size // 4}i", data, off)
            difat.extend(vals[:-1])
            s = vals[-1]
        fat: List[int] = []
        for fs in difat:
            if fs < 0:
                continue
            off = (fs + 1) * sec_size
            if off + sec_size <= len(data):
                fat.extend(struct.unpack_from(f"<{sec_size // 4}i", data, off))

        def read_chain(start_sec: int, size: Optional[int] = None) -> bytes:
            out = bytearray()
            cur = start_sec
            visited = set()
            while cur >= 0 and cur not in visited and cur < len(fat):
                visited.add(cur)
                off = (cur + 1) * sec_size
                out.extend(data[off : off + sec_size])
                cur = fat[cur]
            return bytes(out[:size] if size is not None else out)

        dir_bytes = read_chain(dir_first_sec)
        root_start = struct.unpack_from("<i", dir_bytes, 116)[0] if len(dir_bytes) >= 128 else -2
        wb = b""
        for i in range(0, len(dir_bytes), 128):
            entry = dir_bytes[i : i + 128]
            if len(entry) < 128:
                break
            name_len = struct.unpack_from("<H", entry, 64)[0]
            if name_len <= 2:
                continue
            name = entry[: name_len - 2].decode("utf-16le", errors="ignore")
            if name.lower() in ("workbook", "book"):
                start_sec = struct.unpack_from("<i", entry, 116)[0]
                stream_size = struct.unpack_from("<I", entry, 120)[0]
                if stream_size < min_std_size and root_start >= 0 and short_fat_first >= 0:
                    mini_stream = read_chain(root_start)
                    sfat_bytes = read_chain(short_fat_first)
                    sfat = list(struct.unpack_from(f"<{len(sfat_bytes) // 4}i", sfat_bytes, 0))
                    out_m = bytearray()
                    cur_m = start_sec
                    vis_m = set()
                    while cur_m >= 0 and cur_m not in vis_m and cur_m < len(sfat):
                        vis_m.add(cur_m)
                        out_m.extend(mini_stream[cur_m * 64 : (cur_m + 1) * 64])
                        cur_m = sfat[cur_m]
                    wb = bytes(out_m[:stream_size])
                else:
                    wb = read_chain(start_sec, stream_size)
                break
        if not wb:
            return None

        records: List[tuple[int, List[bytes]]] = []
        pos = 0
        while pos + 4 <= len(wb):
            rec_id, rec_len = struct.unpack_from("<HH", wb, pos)
            chunk = wb[pos + 4 : pos + 4 + rec_len]
            pos += 4 + rec_len
            if rec_id == 0x003C and records:
                records[-1][1].append(chunk)
            else:
                records.append((rec_id, [chunk]))

        sst: List[str] = []
        for rec_id, chunks in records:
            if rec_id == 0x00FC:
                c_idx = 0
                c_pos = 8
                num_unique = struct.unpack_from("<I", chunks[0], 4)[0]

                def read_sst_bytes(n: int) -> bytes:
                    nonlocal c_idx, c_pos
                    res = bytearray()
                    while len(res) < n and c_idx < len(chunks):
                        avail = len(chunks[c_idx]) - c_pos
                        if avail <= 0:
                            c_idx += 1
                            c_pos = 0
                            continue
                        take = min(n - len(res), avail)
                        res.extend(chunks[c_idx][c_pos : c_pos + take])
                        c_pos += take
                    return bytes(res)

                for _ in range(num_unique):
                    hdr = read_sst_bytes(3)
                    if len(hdr) < 3:
                        break
                    n_chars, flags = struct.unpack_from("<HB", hdr, 0)
                    rt_runs = (
                        struct.unpack_from("<H", read_sst_bytes(2), 0)[0] if (flags & 0x08) else 0
                    )
                    ext_len = (
                        struct.unpack_from("<I", read_sst_bytes(4), 0)[0] if (flags & 0x04) else 0
                    )
                    is_16 = bool(flags & 0x01)
                    chars_left = n_chars
                    s_parts: List[str] = []
                    while chars_left > 0 and c_idx < len(chunks):
                        if c_pos >= len(chunks[c_idx]):
                            c_idx += 1
                            if c_idx >= len(chunks):
                                break
                            is_16 = bool(chunks[c_idx][0] & 0x01)
                            c_pos = 1
                        avail_bytes = len(chunks[c_idx]) - c_pos
                        avail_chars = (avail_bytes // 2) if is_16 else avail_bytes
                        take_chars = min(chars_left, avail_chars)
                        take_bytes = take_chars * (2 if is_16 else 1)
                        raw_s = chunks[c_idx][c_pos : c_pos + take_bytes]
                        s_parts.append(
                            raw_s.decode("utf-16le", errors="replace")
                            if is_16
                            else raw_s.decode("latin1")
                        )
                        c_pos += take_bytes
                        chars_left -= take_chars
                    if rt_runs:
                        read_sst_bytes(4 * rt_runs)
                    if ext_len:
                        read_sst_bytes(ext_len)
                    sst.append("".join(s_parts))

        cells: Dict[tuple[int, int], str] = {}
        max_r, max_c = 0, 0
        for rec_id, chunks in records:
            buf = b"".join(chunks)
            if rec_id == 0x00FD and len(buf) >= 10:  # LABELSST
                r, c, _, sst_idx = struct.unpack_from("<HHHI", buf, 0)
                cells[(r, c)] = sst[sst_idx] if sst_idx < len(sst) else ""
                max_r, max_c = max(max_r, r), max(max_c, c)
            elif rec_id == 0x0203 and len(buf) >= 14:  # NUMBER
                r, c, _, num = struct.unpack_from("<HHHd", buf, 0)
                cells[(r, c)] = _format_xls_num(num)
                max_r, max_c = max(max_r, r), max(max_c, c)
            elif rec_id == 0x027E and len(buf) >= 10:  # RK
                r, c, _, rk = struct.unpack_from("<HHHI", buf, 0)
                cells[(r, c)] = _format_xls_num(_decode_rk_number(rk))
                max_r, max_c = max(max_r, r), max(max_c, c)
            elif rec_id == 0x00BD and len(buf) >= 6:  # MULRK
                r, first_c = struct.unpack_from("<HH", buf, 0)
                last_c = struct.unpack_from("<H", buf, len(buf) - 2)[0]
                pos_rk = 4
                for c in range(first_c, last_c + 1):
                    if pos_rk + 6 <= len(buf) - 2:
                        rk = struct.unpack_from("<I", buf, pos_rk + 2)[0]
                        cells[(r, c)] = _format_xls_num(_decode_rk_number(rk))
                        max_r, max_c = max(max_r, r), max(max_c, c)
                    pos_rk += 6
            elif rec_id == 0x0204 and len(buf) >= 9:  # LABEL
                r, c, _, slen, sflag = struct.unpack_from("<HHHHB", buf, 0)
                raw_lbl = buf[9:]
                cells[(r, c)] = (
                    raw_lbl[: slen * 2].decode("utf-16le", errors="replace")
                    if (sflag & 0x01)
                    else raw_lbl[:slen].decode("cp949", errors="replace")
                )
                max_r, max_c = max(max_r, r), max(max_c, c)

        if not cells:
            return None
        sio = io.StringIO()
        writer = csv.writer(sio)
        for r in range(max_r + 1):
            writer.writerow([cells.get((r, c), "") for c in range(max_c + 1)])
        return sio.getvalue()
    except Exception:
        return None


def _convert_xlsx_to_csv(data: bytes) -> Optional[str]:
    """Parse OOXML Excel (.xlsx) ZIP archive into CSV text."""
    try:
        if not data.startswith(b"PK\x03\x04"):
            return None
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            shared_strings: List[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in ss_root.iter():
                    if si.tag.endswith("}si") or si.tag == "si":
                        texts = [
                            t.text or ""
                            for t in si.iter()
                            if t.tag.endswith("}t") or t.tag == "t"
                        ]
                        shared_strings.append("".join(texts))
            sheet_names = sorted(
                n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            )
            if not sheet_names:
                return None
            sh_root = ET.fromstring(zf.read(sheet_names[0]))
            sio = io.StringIO()
            writer = csv.writer(sio)
            for row_el in sh_root.iter():
                if not (row_el.tag.endswith("}row") or row_el.tag == "row"):
                    continue
                row_vals: List[str] = []
                for c_el in row_el:
                    if not (c_el.tag.endswith("}c") or c_el.tag == "c"):
                        continue
                    c_type = c_el.attrib.get("t", "")
                    val_str = ""
                    for child in c_el:
                        if child.tag.endswith("}v") or child.tag == "v":
                            raw_v = child.text or ""
                            if c_type == "s" and raw_v.isdigit():
                                idx = int(raw_v)
                                val_str = shared_strings[idx] if idx < len(shared_strings) else ""
                            else:
                                val_str = raw_v
                        elif child.tag.endswith("}is") or child.tag == "is":
                            val_str = "".join(t.text or "" for t in child.iter() if t.text)
                    row_vals.append(val_str)
                if any(row_vals):
                    writer.writerow(row_vals)
            return sio.getvalue()
    except Exception:
        return None


def decode_csv_bytes(raw_bytes: bytes) -> str:
    """Auto-detect binary .xls (BIFF8), .xlsx (OOXML), UTF-8, or CP949/EUC-KR."""
    if raw_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        xls_csv = _convert_biff8_xls_to_csv(raw_bytes)
        if xls_csv:
            return xls_csv
    if raw_bytes.startswith(b"PK\x03\x04"):
        xlsx_csv = _convert_xlsx_to_csv(raw_bytes)
        if xlsx_csv:
            return xlsx_csv
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _parse_int_amount(val: str) -> int:
    s = str(val or "").strip().replace(",", "")
    if not s or s == "-":
        return 0
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return 0
    return int(round(float(m.group(0))))


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
    if "쿠팡" in txt and "와우" in txt:
        return "8691"
    if "위시" in txt or "WE:SH" in txt.upper():
        return "KB위시"
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
    for cand in candidates:
        c_low = cand.lower()
        for key, val in row.items():
            if key is None:
                continue
            clean_key = re.sub(r"\s+", "", str(key)).lower()
            if c_low in clean_key:
                val_str = str(val or "").strip()
                if val_str:
                    return val_str
    return default


def parse_hometax_csv(csv_text: str) -> List[ReconcileItem]:
    """Parse Hometax or corporate card statement CSV into ReconcileItem objects."""
    content = (csv_text or "").strip()
    if not content:
        return []

    default_year = _extract_year_hint(content[:400])
    all_rows = list(csv.reader(io.StringIO(content)))
    header_idx = 0
    hdr_keys = ("일자", "일시", "이용일", "가맹점", "이용하신곳", "상호", "승인번호", "transacted_at")
    for idx, cells in enumerate(all_rows[:30]):
        joined = " ".join(str(c) for c in cells)
        if sum(1 for k in hdr_keys if k in joined) >= 2:
            header_idx = idx
            break
    else:
        for idx, cells in enumerate(all_rows[:30]):
            joined = " ".join(str(c) for c in cells)
            if any(k in joined for k in hdr_keys):
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
        if raw_dt and ":" not in raw_dt:
            t_part = _pick_col(row, ["거래시간", "승인시간", "이용시간", "결제시간", "시간"])
            if t_part:
                raw_dt = f"{raw_dt} {t_part}".strip()
        if not raw_dt:
            d_part = _pick_col(row, ["거래일자", "승인일자", "이용일자"])
            if d_part:
                t_part = _pick_col(row, ["거래시간", "승인시간", "이용시간"], "12:00:00")
                raw_dt = f"{d_part} {t_part}".strip()

        if not raw_dt:
            continue

        row_year = default_year or _extract_year_hint(_pick_col(row, ["결제예정일", "청구일자"]))
        transacted_at = _normalize_datetime_str(raw_dt, default_year=row_year)
        if len(transacted_at) < 10 or not transacted_at[:4].isdigit():
            continue
        card_last4 = _extract_card_last4(
            _pick_col(row, ["카드번호", "카드뒷자리", "이용카드", "card_last4"], "0000")
        )
        approval_no = _pick_col(row, ["승인번호", "approval_no"]) or None
        merchant_name = _pick_col(
            row,
            ["가맹점명", "가맹점상호", "이용가맹점", "이용하신곳", "가맹점", "상호", "merchant_name"],
            "미상가맹점",
        )
        biz_reg_no = (
            re.sub(r"\D", "", _pick_col(row, ["사업자등록번호", "사업자번호", "biz_reg_no"])) or None
        )

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
            ],
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
