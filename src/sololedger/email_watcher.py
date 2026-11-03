"""Real-time IMAP Email Watcher & Order/PG Payment Email Parser for SoloLedger.

Automatically receives and parses order confirmation & PG payment emails (Coupang, NaverPay,
KG Inicis, Toss Payments, NHN KCP, NicePay, KakaoPay, 11st, Gmarket, etc.), matches them
against card transactions, and generates itemized Electronic Transaction Statements (거래명세서).
"""

import email
import email.message
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger.config import settings
from sololedger.importers import _normalize_datetime_str, _parse_int_amount
from sololedger.scraper_service import (
    extract_coupang_orders_from_content,
    extract_rows_from_html_table,
    reconcile_scraped_ecommerce_orders,
)

IMAP_PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "NAVER": {
        "name": "네이버 메일 (imap.naver.com)",
        "host": "imap.naver.com",
        "port": 993,
    },
    "GMAIL": {
        "name": "Google Gmail (imap.gmail.com)",
        "host": "imap.gmail.com",
        "port": 993,
    },
    "DAUM_KAKAO": {
        "name": "다음 / 카카오 메일 (imap.daum.net)",
        "host": "imap.daum.net",
        "port": 993,
    },
    "WORKSMOBILE": {
        "name": "네이버웍스 (imap.worksmobile.com)",
        "host": "imap.worksmobile.com",
        "port": 993,
    },
    "OUTLOOK": {
        "name": "Outlook / Microsoft 365 (outlook.office365.com)",
        "host": "outlook.office365.com",
        "port": 993,
    },
    "CUSTOM": {
        "name": "직접 입력 (사내 웹메일 / 기타 IMAP)",
        "host": "",
        "port": 993,
    },
}

_ORDER_EMAIL_SUBJECT_RE = re.compile(
    r"(주문|결제|승인|영수증|거래명세|명세서|구매|배송|발주|이용내역|매출전표|"
    r"쿠팡|네이버|이니시스|KCP|케이씨피|토스|나이스|다날|페이코|카카오|"
    r"Order|Receipt|Invoice|Payment)",
    re.IGNORECASE,
)

_KV_PRODUCT_KEYS = (
    "상품명",
    "주문상품",
    "구매상품",
    "품목명",
    "품목",
    "제품명",
    "주문내역",
    "결제상품",
    "거래상품",
    "상품/서비스명",
    "서비스명",
    "내역",
    "item",
    "product",
    "description",
)
_KV_AMOUNT_KEYS = (
    "총결제금액",
    "총 결제금액",
    "결제금액",
    "승인금액",
    "실결제금액",
    "주문금액",
    "합계금액",
    "거래금액",
    "구매금액",
    "청구금액",
    "합계",
    "금액",
    "total",
    "amount",
)
_KV_DATE_KEYS = (
    "결제일시",
    "승인일시",
    "거래일시",
    "주문일시",
    "주문일자",
    "결제일자",
    "승인일자",
    "거래일자",
    "구매일시",
    "일자",
    "일시",
    "date",
)
_KV_SHOP_KEYS = (
    "상점명",
    "가맹점명",
    "가맹점",
    "이용상점",
    "판매자",
    "판매처",
    "공급자",
    "쇼핑몰",
    "이용기관",
    "merchant",
    "seller",
)
_KV_APPROVAL_KEYS = (
    "승인번호",
    "카드승인번호",
    "approval",
)


def _config_file_path(archive_dir: Optional[Path] = None) -> Path:
    base = archive_dir or settings.archive_dir
    base.mkdir(parents=True, exist_ok=True)
    return base / "email_watcher_config.json"


def _make_account_id(email_address: str) -> str:
    digest = hashlib.sha1(email_address.strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"acc_{digest}"


def _normalize_account_entry(
    raw: Dict[str, Any],
    existing: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    base = dict(existing or {})
    email_addr = str(raw.get("email_address") or base.get("email_address") or "").strip()
    if not email_addr:
        return None

    provider = str(raw.get("provider") or base.get("provider") or "NAVER").upper()
    if provider not in IMAP_PROVIDER_PRESETS:
        provider = "CUSTOM"
    preset = IMAP_PROVIDER_PRESETS[provider]

    host = str(raw.get("imap_host") or "").strip()
    if not host:
        if preset["host"] and (not base.get("imap_host") or raw.get("provider")):
            host = str(preset["host"])
        else:
            host = str(base.get("imap_host") or preset["host"] or "")

    port = int(raw.get("imap_port") or base.get("imap_port") or preset["port"] or 993)
    raw_pw = raw.get("app_password")
    if raw_pw is not None and str(raw_pw).strip() and str(raw_pw).strip() != "********":
        password = str(raw_pw).strip()
        if "gmail.com" in host.lower() or provider == "GMAIL":
            no_space = re.sub(r"\s+", "", password)
            if len(no_space) == 16 and no_space.isalpha():
                password = no_space
    else:
        password = str(base.get("app_password") or "")

    default_label = preset["name"].split("(")[0].strip()
    label = str(raw.get("label") or base.get("label") or default_label).strip()
    enabled = (
        bool(raw["enabled"])
        if "enabled" in raw and raw["enabled"] is not None
        else bool(base.get("enabled", True))
    )
    acc_id = str(raw.get("account_id") or base.get("account_id") or _make_account_id(email_addr))

    return {
        "account_id": acc_id,
        "label": label,
        "provider": provider,
        "imap_host": host,
        "imap_port": port,
        "email_address": email_addr,
        "app_password": password,
        "mailbox": str(raw.get("mailbox") or base.get("mailbox") or "INBOX").strip() or "INBOX",
        "enabled": enabled,
        "last_sync_at": raw.get("last_sync_at", base.get("last_sync_at")),
        "last_status": raw.get("last_status", base.get("last_status", "idle")),
        "last_message": raw.get("last_message", base.get("last_message", "")),
    }


def load_email_watcher_config(archive_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load persisted multi-account IMAP email watcher settings from archive_dir."""
    cfg_path = _config_file_path(archive_dir)
    default_cfg: Dict[str, Any] = {
        "provider": os.getenv("SOLOLEDGER_IMAP_PROVIDER", "NAVER"),
        "imap_host": os.getenv("SOLOLEDGER_IMAP_HOST", "imap.naver.com"),
        "imap_port": int(os.getenv("SOLOLEDGER_IMAP_PORT", "993")),
        "email_address": os.getenv("SOLOLEDGER_IMAP_USER", ""),
        "app_password": os.getenv("SOLOLEDGER_IMAP_PASSWORD", ""),
        "mailbox": "INBOX",
        "accounts": [],
        "auto_poll_enabled": False,
        "poll_interval_sec": 120,
        "lookback_days": 60,
        "last_sync_at": None,
        "last_sync_status": "idle",
        "last_sync_message": "아직 이메일 동기화가 실행되지 않았습니다.",
        "last_matched_count": 0,
        "processed_ids": [],
    }
    if cfg_path.exists():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                default_cfg.update(loaded)
        except Exception:
            pass

    raw_accounts = default_cfg.get("accounts")
    norm_accounts: List[Dict[str, Any]] = []
    if isinstance(raw_accounts, list):
        for item in raw_accounts:
            if isinstance(item, dict):
                norm = _normalize_account_entry(item)
                if norm:
                    norm_accounts.append(norm)

    if not norm_accounts and str(default_cfg.get("email_address") or "").strip():
        migrated = _normalize_account_entry(default_cfg)
        if migrated:
            norm_accounts.append(migrated)

    default_cfg["accounts"] = norm_accounts
    return default_cfg


def _write_config(cfg: Dict[str, Any], archive_dir: Optional[Path] = None) -> Dict[str, Any]:
    cfg["poll_interval_sec"] = max(30, int(cfg.get("poll_interval_sec") or 120))
    cfg["lookback_days"] = max(1, min(365, int(cfg.get("lookback_days") or 60)))
    if isinstance(cfg.get("processed_ids"), list) and len(cfg["processed_ids"]) > 1500:
        cfg["processed_ids"] = cfg["processed_ids"][-1500:]

    accounts = cfg.get("accounts") or []
    if accounts:
        first = accounts[0]
        cfg["provider"] = first["provider"]
        cfg["imap_host"] = first["imap_host"]
        cfg["imap_port"] = first["imap_port"]
        cfg["email_address"] = first["email_address"]
        cfg["app_password"] = first["app_password"]
    else:
        cfg["email_address"] = ""
        cfg["app_password"] = ""

    cfg_path = _config_file_path(archive_dir)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def save_email_watcher_config(
    updates: Dict[str, Any],
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Update global watcher settings and optionally upsert an email account."""
    cfg = load_email_watcher_config(archive_dir)

    for key in (
        "auto_poll_enabled",
        "poll_interval_sec",
        "lookback_days",
        "last_sync_at",
        "last_sync_status",
        "last_sync_message",
        "last_matched_count",
        "processed_ids",
    ):
        if key in updates and updates[key] is not None:
            cfg[key] = updates[key]

    if isinstance(updates.get("accounts"), list):
        existing_by_id = {a["account_id"]: a for a in cfg.get("accounts") or []}
        new_list: List[Dict[str, Any]] = []
        for item in updates["accounts"]:
            if isinstance(item, dict):
                aid = str(item.get("account_id") or "")
                norm = _normalize_account_entry(item, existing=existing_by_id.get(aid))
                if norm:
                    new_list.append(norm)
        cfg["accounts"] = new_list

    if str(updates.get("email_address") or "").strip():
        accounts: List[Dict[str, Any]] = list(cfg.get("accounts") or [])
        target_id = str(updates.get("account_id") or "")
        target_email = str(updates["email_address"]).strip().lower()
        matched_idx = -1
        for idx, acc in enumerate(accounts):
            if (target_id and acc["account_id"] == target_id) or (
                acc["email_address"].strip().lower() == target_email
            ):
                matched_idx = idx
                break
        if matched_idx >= 0:
            updated_acc = _normalize_account_entry(updates, existing=accounts[matched_idx])
            if updated_acc:
                accounts[matched_idx] = updated_acc
        else:
            created_acc = _normalize_account_entry(updates)
            if created_acc:
                accounts.append(created_acc)
        cfg["accounts"] = accounts

    return _write_config(cfg, archive_dir)


def update_email_account_entry(
    account_id: str,
    updates: Dict[str, Any],
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Modify fields (enabled, label, password, host, etc.) on a specific email account."""
    cfg = load_email_watcher_config(archive_dir)
    accounts: List[Dict[str, Any]] = list(cfg.get("accounts") or [])
    found = False
    for idx, acc in enumerate(accounts):
        if acc["account_id"] == account_id:
            merged = _normalize_account_entry(updates, existing=acc)
            if merged:
                merged["account_id"] = account_id
                accounts[idx] = merged
                found = True
            break
    if not found:
        raise KeyError(f"Email account '{account_id}' not found.")
    cfg["accounts"] = accounts
    return _write_config(cfg, archive_dir)


def delete_email_account_entry(
    account_id: str,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Remove an email account by account_id."""
    cfg = load_email_watcher_config(archive_dir)
    accounts = [a for a in (cfg.get("accounts") or []) if a.get("account_id") != account_id]
    cfg["accounts"] = accounts
    return _write_config(cfg, archive_dir)


def get_public_email_watcher_config(archive_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Return multi-account email watcher config with passwords masked for API/Dashboard."""
    cfg = load_email_watcher_config(archive_dir)
    pub = dict(cfg)
    masked_accounts: List[Dict[str, Any]] = []
    any_pw = False
    enabled_cnt = 0
    for acc in cfg.get("accounts") or []:
        c_acc = dict(acc)
        has_pw = bool(str(c_acc.get("app_password") or "").strip())
        if has_pw:
            any_pw = True
        if c_acc.get("enabled", True):
            enabled_cnt += 1
        c_acc["has_password"] = has_pw
        c_acc["app_password"] = "********" if has_pw else ""
        masked_accounts.append(c_acc)

    pub["accounts"] = masked_accounts
    pub["total_accounts_count"] = len(masked_accounts)
    pub["enabled_accounts_count"] = enabled_cnt
    pub["has_password"] = any_pw
    pub["app_password"] = "********" if any_pw else ""
    pub["presets"] = IMAP_PROVIDER_PRESETS
    pub.pop("processed_ids", None)
    return pub


def _decode_mime_header(raw_val: Optional[str]) -> str:
    if not raw_val:
        return ""
    parts = decode_header(raw_val)
    out: List[str] = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            charset = enc or "utf-8"
            try:
                out.append(chunk.decode(charset, errors="replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(str(chunk))
    return "".join(out).strip()


def _html_to_structured_text(raw_html: str) -> str:
    """Convert HTML email body into clean newline-delimited text while preserving table cells."""
    if not raw_html:
        return ""
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.I | re.S)
    cleaned = re.sub(r"</(tr|p|div|li|h[1-6])>", "\n", cleaned, flags=re.I)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.I)
    cleaned = re.sub(r"</(td|th)>", " : ", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    unescaped = html.unescape(cleaned)
    lines: List[str] = []
    for ln in unescaped.splitlines():
        norm = re.sub(r"[ \t]+", " ", ln).strip(" :")
        if norm:
            lines.append(norm)
    return "\n".join(lines)


def _match_kv_label(label: str, candidates: tuple[str, ...]) -> bool:
    clean = re.sub(r"[\s:·•\-_()\[\]]+", "", label).lower()
    if not clean or len(clean) > 18:
        return False
    for cand in candidates:
        c_clean = re.sub(r"[\s:·•\-_()\[\]]+", "", cand).lower()
        if clean == c_clean or clean.endswith(c_clean):
            return True
    return False


def _extract_kv_pairs_from_html_and_text(
    body_html: str,
    body_text: str,
) -> Dict[str, List[str]]:
    """Extract key-value fields (product, amount, date, shop, approval_no) from HTML/text."""
    found: Dict[str, List[str]] = {
        "products": [],
        "amounts": [],
        "dates": [],
        "shops": [],
        "approvals": [],
    }
    # 1. Scan HTML <tr><th>/<td> key-value pairs directly
    if body_html:
        row_matches = re.findall(r"<tr[^>]*>(.*?)</tr>", body_html, flags=re.I | re.S)
        for tr in row_matches:
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.I | re.S)
            clean_cells = [
                re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                for c in cells
            ]
            clean_cells = [c for c in clean_cells if c]
            for idx in range(len(clean_cells) - 1):
                k = clean_cells[idx]
                v = clean_cells[idx + 1]
                if not v or k == v:
                    continue
                if _match_kv_label(k, _KV_PRODUCT_KEYS):
                    found["products"].append(v)
                elif _match_kv_label(k, _KV_AMOUNT_KEYS):
                    found["amounts"].append(v)
                elif _match_kv_label(k, _KV_DATE_KEYS):
                    found["dates"].append(v)
                elif _match_kv_label(k, _KV_SHOP_KEYS):
                    found["shops"].append(v)
                elif _match_kv_label(k, _KV_APPROVAL_KEYS):
                    found["approvals"].append(v)

    # 2. Scan structured text lines for "Key : Value" or "Key\nValue"
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    for idx, ln in enumerate(lines):
        parts = [p.strip() for p in re.split(r"\s*[:：|]\s*", ln, maxsplit=1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            k, v = parts[0], parts[1]
            if _match_kv_label(k, _KV_PRODUCT_KEYS):
                found["products"].append(v)
            elif _match_kv_label(k, _KV_AMOUNT_KEYS):
                found["amounts"].append(v)
            elif _match_kv_label(k, _KV_DATE_KEYS):
                found["dates"].append(v)
            elif _match_kv_label(k, _KV_SHOP_KEYS):
                found["shops"].append(v)
            elif _match_kv_label(k, _KV_APPROVAL_KEYS):
                found["approvals"].append(v)
        elif idx + 1 < len(lines):
            k = ln
            v = lines[idx + 1]
            if _match_kv_label(k, _KV_PRODUCT_KEYS):
                found["products"].append(v)
            elif _match_kv_label(k, _KV_AMOUNT_KEYS):
                found["amounts"].append(v)
            elif _match_kv_label(k, _KV_DATE_KEYS):
                found["dates"].append(v)
            elif _match_kv_label(k, _KV_SHOP_KEYS):
                found["shops"].append(v)
            elif _match_kv_label(k, _KV_APPROVAL_KEYS):
                found["approvals"].append(v)

    return found


def extract_orders_from_email_content(
    subject: str = "",
    sender: str = "",
    date_str: str = "",
    body_text: str = "",
    body_html: str = "",
) -> List[Dict[str, Any]]:
    """Extract normalized e-commerce / PG order records from an email message."""
    combined_text = body_text or _html_to_structured_text(body_html)
    if body_html and body_text:
        html_txt = _html_to_structured_text(body_html)
        if html_txt not in combined_text:
            combined_text = f"{combined_text}\n{html_txt}"

    fallback_dt = ""
    if date_str:
        try:
            parsed_dt = email.utils.parsedate_to_datetime(date_str)
            fallback_dt = parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            norm = _normalize_datetime_str(date_str)
            if len(norm) >= 10 and norm[:4].isdigit():
                fallback_dt = norm

    # 1. Try existing Coupang / HTML table / JSON order extractor first
    orders: List[Dict[str, Any]] = []
    if body_html and "<table" in body_html.lower():
        table_rows = extract_rows_from_html_table(body_html)
        for r in table_rows:
            has_prod = any(k in r for k in ("상품명", "품목명", "주문상품", "상품", "내역"))
            has_amt = any(k in r for k in ("결제금액", "주문금액", "합계금액", "금액", "상품금액"))
            if has_prod and has_amt:
                if not any(k in r for k in ("주문일자", "주문일시", "결제일시", "일자")) and fallback_dt:
                    r["주문일자"] = fallback_dt[:10]
                orders.append(r)

    cp_orders = extract_coupang_orders_from_content(combined_text)
    for co in cp_orders:
        orders.append(co)

    # 2. Extract Key-Value PG / Shopping Mall payment confirmation fields
    kv = _extract_kv_pairs_from_html_and_text(body_html, combined_text)
    kv_dt = ""
    for raw_d in kv["dates"]:
        norm_d = _normalize_datetime_str(raw_d)
        if len(norm_d) >= 10 and norm_d[:4].isdigit():
            kv_dt = norm_d
            break
    if not kv_dt:
        date_m = re.search(
            r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})",
            combined_text,
        )
        if date_m:
            y, m, d = int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3))
            kv_dt = f"{y:04d}-{m:02d}-{d:02d}"
    if not kv_dt and fallback_dt:
        kv_dt = fallback_dt

    kv_amt = 0
    for raw_a in kv["amounts"]:
        val = _parse_int_amount(raw_a)
        if val >= 100:
            kv_amt = val
            break
    if kv_amt <= 0:
        amt_matches = re.findall(r"(\d{1,3}(?:,\d{3})+)\s*원", combined_text)
        if amt_matches:
            parsed_amts = [_parse_int_amount(x) for x in amt_matches]
            valid_amts = [a for a in parsed_amts if a >= 100]
            if valid_amts:
                kv_amt = max(valid_amts)

    kv_prod = ""
    for raw_p in kv["products"]:
        clean_p = re.sub(r"\s+", " ", raw_p).strip()
        if clean_p and len(clean_p) >= 2 and not clean_p.isdigit():
            kv_prod = clean_p
            break
    if not kv_prod and subject:
        subj_clean = re.sub(
            r"^(\[[^\]]+\]|\([^\)]+\)|RE:|FW:|FWD:)\s*",
            "",
            subject.strip(),
            flags=re.I,
        ).strip()
        if subj_clean:
            kv_prod = subj_clean

    kv_shop = ""
    for raw_s in kv["shops"]:
        clean_s = re.sub(r"\s+", " ", raw_s).strip()
        if clean_s and len(clean_s) >= 2:
            kv_shop = clean_s
            break

    kv_app = ""
    for raw_ap in kv["approvals"]:
        m_ap = re.search(r"(\d{6,12})", raw_ap)
        if m_ap:
            kv_app = m_ap.group(1)
            break

    if kv_dt and kv_amt > 0 and kv_prod:
        line_items: List[Dict[str, Any]] = []
        for p_str in kv["products"]:
            c_p = re.sub(r"\s+", " ", p_str).strip()
            if c_p and len(c_p) >= 2 and all(x["item"] != c_p for x in line_items):
                line_items.append({"item": c_p, "qty": 1, "price": kv_amt})
        if not line_items:
            line_items = [{"item": kv_prod, "qty": 1, "price": kv_amt}]
        orders.append(
            {
                "주문일자": kv_dt[:10],
                "상품명": kv_prod,
                "결제금액": kv_amt,
                "상점명": kv_shop,
                "승인번호": kv_app,
                "품목내역": line_items,
            }
        )

    # Deduplicate by (date, amount, product)
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for o in orders:
        dt_k = str(o.get("주문일자") or o.get("주문일시") or fallback_dt)[:10]
        amt_k = _parse_int_amount(str(o.get("결제금액") or o.get("주문금액") or 0))
        prod_k = str(o.get("상품명") or o.get("품목명") or "").strip()
        if not dt_k or amt_k <= 0 or not prod_k:
            continue
        o["주문일자"] = dt_k
        o["결제금액"] = amt_k
        o["상품명"] = prod_k
        sig = f"{dt_k}|{amt_k}|{prod_k}"
        if sig not in seen:
            seen.add(sig)
            deduped.append(o)
    return deduped


def parse_raw_email_bytes_or_text(
    raw_data: bytes | str,
    default_subject: str = "",
    default_date: str = "",
) -> List[Dict[str, Any]]:
    """Parse either a full RFC822 .eml message or pasted email HTML/text into order dicts."""
    if isinstance(raw_data, bytes):
        text_preview = raw_data[:2000].decode("utf-8", errors="ignore")
    else:
        text_preview = raw_data[:2000]

    is_mime = any(
        hdr in text_preview
        for hdr in ("MIME-Version:", "Content-Type:", "From:", "Subject:", "Received:")
    )
    if is_mime:
        msg: email.message.Message
        if isinstance(raw_data, bytes):
            msg = email.message_from_bytes(raw_data)
        else:
            msg = email.message_from_string(raw_data)

        subj = _decode_mime_header(msg.get("Subject")) or default_subject
        sender = _decode_mime_header(msg.get("From"))
        dt_hdr = str(msg.get("Date") or default_date)

        text_parts: List[str] = []
        html_parts: List[str] = []

        if msg.is_multipart():
            for part in msg.walk():
                c_type = (part.get_content_type() or "").lower()
                c_disp = str(part.get("Content-Disposition") or "").lower()
                fname = _decode_mime_header(part.get_filename())
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(charset, errors="replace")
                except LookupError:
                    decoded = payload.decode("utf-8", errors="replace")

                if c_type == "text/plain" and "attachment" not in c_disp:
                    text_parts.append(decoded)
                elif c_type == "text/html" or fname.lower().endswith((".html", ".htm")):
                    html_parts.append(decoded)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(charset, errors="replace")
                except LookupError:
                    decoded = payload.decode("utf-8", errors="replace")
                if (msg.get_content_type() or "").lower() == "text/html":
                    html_parts.append(decoded)
                else:
                    text_parts.append(decoded)

        return extract_orders_from_email_content(
            subject=subj,
            sender=sender,
            date_str=dt_hdr,
            body_text="\n".join(text_parts),
            body_html="\n".join(html_parts),
        )

    raw_str = (
        raw_data.decode("utf-8", errors="replace")
        if isinstance(raw_data, bytes)
        else str(raw_data)
    )
    has_html = "<table" in raw_str.lower() or "<tr" in raw_str.lower() or "<div" in raw_str.lower()
    return extract_orders_from_email_content(
        subject=default_subject,
        date_str=default_date,
        body_text="" if has_html else raw_str,
        body_html=raw_str if has_html else "",
    )


def ingest_email_order_payload(
    cursor: sqlite3.Cursor,
    raw_content: bytes | str,
    subject: str = "",
    date_str: str = "",
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Parse an email payload (.eml, HTML, or text) and reconcile matched card transactions."""
    orders = parse_raw_email_bytes_or_text(
        raw_content,
        default_subject=subject,
        default_date=date_str,
    )
    if not orders:
        return {
            "status": "no_orders_found",
            "parsed_orders_count": 0,
            "matched_count": 0,
            "updated_tx_ids": [],
            "generated_statements_count": 0,
        }

    res = reconcile_scraped_ecommerce_orders(
        cursor,
        raw_content=json.dumps(orders, ensure_ascii=False),
        archive_dir=archive_dir,
        orders_list=orders,
    )
    return {
        "status": "ingested",
        "parsed_orders_count": len(orders),
        "matched_count": res["matched_count"],
        "updated_tx_ids": res["updated_tx_ids"],
        "generated_statements_count": res["matched_count"],
        "orders_preview": orders[:10],
    }


def sync_imap_order_emails(
    cursor: sqlite3.Cursor,
    archive_dir: Optional[Path] = None,
    max_messages: int = 80,
    force_rescan: bool = False,
    account_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Connect to all enabled IMAP accounts (or a single account_id), fetch orders, and reconcile."""
    cfg = load_email_watcher_config(archive_dir)
    accounts: List[Dict[str, Any]] = list(cfg.get("accounts") or [])
    lookback_days = int(cfg.get("lookback_days") or 60)
    processed_ids: set[str] = set() if force_rescan else set(cfg.get("processed_ids") or [])

    if account_id:
        targets = [a for a in accounts if a.get("account_id") == account_id]
    else:
        targets = [a for a in accounts if a.get("enabled", True)]

    valid_targets = [
        a
        for a in targets
        if str(a.get("imap_host") or "").strip()
        and str(a.get("email_address") or "").strip()
        and str(a.get("app_password") or "").strip()
    ]
    if not valid_targets:
        return {
            "status": "not_configured",
            "message": "활성화된 이메일 계정(주소 및 IMAP 앱 비밀번호)을 먼저 등록해 주세요.",
            "scanned_accounts": 0,
            "scanned_emails": 0,
            "parsed_orders_count": 0,
            "matched_count": 0,
            "updated_tx_ids": [],
            "account_results": [],
        }

    since_dt = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    total_scanned_emails = 0
    all_orders: List[Dict[str, Any]] = []
    new_processed: List[str] = list(processed_ids)
    account_results: List[Dict[str, Any]] = []
    success_accounts = 0
    error_messages: List[str] = []

    for acc in valid_targets:
        aid = str(acc["account_id"])
        host = str(acc["imap_host"]).strip()
        port = int(acc.get("imap_port") or 993)
        user = str(acc["email_address"]).strip()
        password = str(acc["app_password"]).strip()
        mailbox = str(acc.get("mailbox") or "INBOX").strip() or "INBOX"
        acc_scanned = 0
        acc_orders: List[Dict[str, Any]] = []
        acc_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            imap = imaplib.IMAP4_SSL(host, port, timeout=20.0)
            try:
                imap.login(user, password)
                imap.select(mailbox, readonly=True)
                typ, data = imap.search(None, f"(SINCE {since_dt})")
                if typ != "OK" or not data or not data[0]:
                    msg_nums: List[bytes] = []
                else:
                    msg_nums = data[0].split()

                recent_nums = list(reversed(msg_nums[-max_messages:]))
                for num in recent_nums:
                    uid_key = f"{user}:{mailbox}:{num.decode('ascii', errors='ignore')}"
                    if not force_rescan and uid_key in processed_ids:
                        continue

                    typ_h, hdr_data = imap.fetch(
                        num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])"
                    )
                    if typ_h != "OK" or not hdr_data:
                        continue
                    raw_hdr = b""
                    for item in hdr_data:
                        if isinstance(item, tuple) and len(item) >= 2:
                            raw_hdr += item[1]
                    hdr_msg = email.message_from_bytes(raw_hdr)
                    subj = _decode_mime_header(hdr_msg.get("Subject"))
                    sender = _decode_mime_header(hdr_msg.get("From"))
                    raw_mid = str(hdr_msg.get("Message-ID") or "").strip()
                    msg_id = f"{user}:{raw_mid}" if raw_mid else uid_key
                    if not force_rescan and msg_id in processed_ids:
                        continue

                    if not _ORDER_EMAIL_SUBJECT_RE.search(f"{subj} {sender}"):
                        new_processed.append(msg_id)
                        new_processed.append(uid_key)
                        continue

                    typ_b, body_data = imap.fetch(num, "(BODY.PEEK[])")
                    if typ_b != "OK" or not body_data:
                        continue
                    raw_eml = b""
                    for item in body_data:
                        if isinstance(item, tuple) and len(item) >= 2:
                            raw_eml += item[1]
                    if not raw_eml:
                        continue

                    acc_scanned += 1
                    extracted = parse_raw_email_bytes_or_text(raw_eml)
                    acc_orders.extend(extracted)
                    new_processed.append(msg_id)
                    new_processed.append(uid_key)
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

            success_accounts += 1
            total_scanned_emails += acc_scanned
            all_orders.extend(acc_orders)
            acc["last_sync_at"] = acc_now
            acc["last_status"] = "synced"
            acc["last_message"] = f"스캔 {acc_scanned}통 · 주문 {len(acc_orders)}건 추출"
            account_results.append(
                {
                    "account_id": aid,
                    "email_address": user,
                    "status": "synced",
                    "scanned_emails": acc_scanned,
                    "parsed_orders_count": len(acc_orders),
                    "message": acc["last_message"],
                }
            )
        except Exception as exc:
            raw_err = str(exc)
            if "Application-specific password required" in raw_err:
                friendly_err = (
                    "일반 비밀번호 대신 Google 16자리 '앱 비밀번호'"
                    "(myaccount.google.com/apppasswords)를 발급받아 입력해 주세요."
                )
            elif "Invalid credentials" in raw_err or "AUTHENTICATIONFAILED" in raw_err.upper():
                friendly_err = (
                    "아이디/앱 비밀번호가 일치하지 않거나 메일함 설정에서 'IMAP 사용함'이 꺼져 있습니다."
                )
            else:
                friendly_err = raw_err
            err_txt = f"{user} ({host}:{port}) 실패: {friendly_err}"
            error_messages.append(err_txt)
            acc["last_sync_at"] = acc_now
            acc["last_status"] = "error"
            acc["last_message"] = friendly_err
            account_results.append(
                {
                    "account_id": aid,
                    "email_address": user,
                    "status": "error",
                    "scanned_emails": acc_scanned,
                    "parsed_orders_count": 0,
                    "message": err_txt,
                }
            )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if success_accounts == 0 and error_messages:
        err_summary = " / ".join(error_messages)
        save_email_watcher_config(
            {
                "accounts": accounts,
                "last_sync_at": now_str,
                "last_sync_status": "error",
                "last_sync_message": f"IMAP 연결 실패: {err_summary}",
            },
            archive_dir=archive_dir,
        )
        return {
            "status": "error",
            "message": f"IMAP 연결 실패: {err_summary}",
            "scanned_accounts": len(valid_targets),
            "scanned_emails": total_scanned_emails,
            "parsed_orders_count": 0,
            "matched_count": 0,
            "updated_tx_ids": [],
            "account_results": account_results,
        }

    rec_res = reconcile_scraped_ecommerce_orders(
        cursor,
        raw_content=json.dumps(all_orders, ensure_ascii=False),
        archive_dir=archive_dir,
        orders_list=all_orders,
    )
    matched = int(rec_res.get("matched_count") or 0)
    summary_msg = (
        f"계정 {success_accounts}/{len(valid_targets)}개 · 메일 {total_scanned_emails}통 스캔 · "
        f"주문 {len(all_orders)}건 추출 · 전표 {matched}건 매칭 및 거래명세서 작성 완료"
    )
    if error_messages:
        summary_msg += f" (일부 계정 오류 {len(error_messages)}건)"

    save_email_watcher_config(
        {
            "accounts": accounts,
            "last_sync_at": now_str,
            "last_sync_status": "synced",
            "last_sync_message": summary_msg,
            "last_matched_count": matched,
            "processed_ids": new_processed,
        },
        archive_dir=archive_dir,
    )
    return {
        "status": "synced",
        "message": summary_msg,
        "last_sync_at": now_str,
        "scanned_accounts": success_accounts,
        "scanned_emails": total_scanned_emails,
        "parsed_orders_count": len(all_orders),
        "matched_count": matched,
        "updated_tx_ids": rec_res.get("updated_tx_ids") or [],
        "generated_statements_count": matched,
        "account_results": account_results,
    }
