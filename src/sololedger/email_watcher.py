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
from sololedger.sms_parser import format_card_label

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
    r"(주문|결제|승인|영수증|거래명세|명세서|구매|발주|매출전표|"
    r"쿠팡|네이버|이니시스|KCP|케이씨피|토스|나이스|다날|페이코|카카오|이지페이|교보|Battle|"
    r"Order|Receipt|Invoice|Payment)",
    re.IGNORECASE,
)

_CANCELED_EMAIL_SUBJECT_RE = re.compile(
    r"(부분취소|결제취소|주문취소|승인취소|취소하신\s*내역|취소되었|취소\s*내역|취소\s*안내)",
    re.IGNORECASE,
)

_IGNORE_EMAIL_SUBJECT_RE = re.compile(
    r"(\(광고\)|\[광고\]|광고성\s*정보|수신동의|로그인\s*되었|새로운\s*기기|새로운\s*환경|"
    r"약관\s*개정|회원\s*등급|인증서\s*개인정보|부분취소|결제취소|주문취소|승인취소|"
    r"취소하신\s*내역|취소되었|취소\s*내역|취소\s*안내|혜택이\s*곧\s*종료|비밀번호\s*변경)",
    re.IGNORECASE,
)

_GENERIC_NOTIFICATION_SUBJECT_RE = re.compile(
    r"(결제하신\s*내역|주문하신\s*내역|결제\s*내역입니다|승인\s*내역|이용내역\s*안내|"
    r"주문이\s*완료|주문\s*안내\s*메일|주문\s*영수증|결제확인\s*안내|결제완료\s*안내|"
    r"주문\s*결제완료|카드승인|결제\s*안내|안내해\s*드립니다|월회비가\s*결제|"
    r"order\s+payment\s+completed|order\s+confirmation|payment\s+receipt)",
    re.IGNORECASE,
)

_NOISE_PRODUCT_RE = re.compile(
    r"^("
    r"이지페이\s*결제내역|결제\s*내역|주문\s*내역|구매\s*내역|구매\s*상세내역|"
    r"결제\s*정보|상점\s*정보|주문\s*정보|배송\s*정보|영수증\s*출력|english\s*receipt|"
    r"쿠팡가|쿠팡|쿠팡\(주\)|쿠팡㈜|로켓배송.*|판매자배송.*|새벽배송.*|무료배송|"
    r"수량|수량\s*:.*|구매금액|판매자|상품\s*가격|총\s*상품\s*금액|총\s*할인\s*금액|"
    r"할인\s*금액|할인금액|배송비|총\s*결제금액|총결제금액|"
    r".*결제하신\s*내역.*|.*주문하신\s*(구매)?내역.*|.*안내해\s*드립니다.*|"
    r".*취소하신\s*내역.*|.*취소되었습니다.*|.*구매\s*품목$"
    r")$",
    re.IGNORECASE,
)

_KV_PRODUCT_KEYS = (
    "상품명",
    "주문상품명",
    "주문상품",
    "구매상품명",
    "구매상품",
    "품목명",
    "품목",
    "제품명",
    "결제상품",
    "거래상품",
    "상품/서비스명",
    "서비스명",
    "item",
    "product",
    "productname",
    "itemname",
)
_KV_AMOUNT_KEYS = (
    "총결제금액",
    "총 결제금액",
    "최종결제금액",
    "결제금액",
    "승인금액",
    "실결제금액",
    "주문금액",
    "합계금액",
    "거래금액",
    "구매금액",
    "청구금액",
    "상품금액",
    "총상품금액",
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
    "주문일",
    "결제일",
    "승인일",
    "일자",
    "일시",
    "date",
)
_KV_SHOP_KEYS = (
    "구매상점명",
    "상점명",
    "가맹점명",
    "가맹점",
    "이용상점",
    "상호",
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
_KV_CARD_KEYS = (
    "결제기관",
    "카드사",
    "발급사",
    "결제카드",
    "카드명",
    "카드종류",
)
_KV_PAY_METHOD_KEYS = (
    "결제수단",
    "결제방식",
    "지불수단",
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
        "recent_matched_items": [],
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


def _merge_recent_matched_items(
    existing: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
    limit: int = 150,
) -> List[Dict[str, Any]]:
    by_tx: Dict[int, Dict[str, Any]] = {}
    for item in existing or []:
        if isinstance(item, dict) and item.get("tx_id") is not None:
            by_tx[int(item["tx_id"])] = dict(item)
    for item in incoming or []:
        if isinstance(item, dict) and item.get("tx_id") is not None:
            by_tx[int(item["tx_id"])] = dict(item)
    merged = sorted(
        by_tx.values(),
        key=lambda x: (str(x.get("transacted_at") or ""), int(x.get("tx_id") or 0)),
        reverse=True,
    )
    return merged[:limit]


def _write_config(cfg: Dict[str, Any], archive_dir: Optional[Path] = None) -> Dict[str, Any]:
    cfg["poll_interval_sec"] = max(30, int(cfg.get("poll_interval_sec") or 120))
    cfg["lookback_days"] = max(1, min(365, int(cfg.get("lookback_days") or 60)))
    if isinstance(cfg.get("processed_ids"), list) and len(cfg["processed_ids"]) > 3000:
        cfg["processed_ids"] = cfg["processed_ids"][-3000:]
    if isinstance(cfg.get("recent_matched_items"), list):
        cfg["recent_matched_items"] = cfg["recent_matched_items"][:150]

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
        "recent_matched_items",
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


def list_statement_matched_transactions(
    cursor: sqlite3.Cursor,
    limit: int = 150,
) -> List[Dict[str, Any]]:
    """Query live ledger transactions that have generated Transaction Statements (거래명세서)."""
    cursor.execute(
        """
        SELECT t.tx_id, t.transacted_at, t.card_last4, t.raw_merchant_name,
               t.total_amount, t.account_code, t.account_name, t.counterparty_info,
               t.reconcile_status, r.archived_file_name, r.line_items_json
        FROM card_transactions t
        JOIN receipt_ocr_metadata r ON r.tx_id = t.tx_id
        WHERE t.reconcile_status != 'CANCELED'
          AND r.source_channel = 'STATEMENT'
        ORDER BY t.transacted_at DESC, t.tx_id DESC
        LIMIT ?
        """,
        (limit,),
    )
    items: List[Dict[str, Any]] = []
    for r in cursor.fetchall():
        li_cnt = 1
        raw_li = r["line_items_json"]
        if raw_li:
            try:
                parsed = json.loads(str(raw_li))
                if isinstance(parsed, list) and len(parsed) > 0:
                    li_cnt = len(parsed)
            except Exception:
                pass
        items.append(
            {
                "tx_id": int(r["tx_id"]),
                "transacted_at": str(r["transacted_at"]),
                "card_label": format_card_label(str(r["card_last4"] or "0000")),
                "raw_merchant_name": str(r["raw_merchant_name"]),
                "item_name": str(r["counterparty_info"] or "-"),
                "line_items_count": li_cnt,
                "total_amount": int(r["total_amount"]),
                "account_code": str(r["account_code"]),
                "account_name": str(r["account_name"]),
                "archived_file_name": str(r["archived_file_name"] or ""),
                "is_created": str(r["reconcile_status"] or "") == "EMAIL_ORDER",
            }
        )
    return items


def get_public_email_watcher_config(
    archive_dir: Optional[Path] = None,
    cursor: Optional[sqlite3.Cursor] = None,
) -> Dict[str, Any]:
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
    if cursor is not None:
        live_items = list_statement_matched_transactions(cursor, limit=150)
        pub["recent_matched_items"] = live_items
    else:
        pub["recent_matched_items"] = list(cfg.get("recent_matched_items") or [])
    pub["recent_matched_count"] = len(pub["recent_matched_items"])
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


def _is_noise_product_name(val: str) -> bool:
    s = re.sub(r"\s+", " ", str(val or "")).strip()
    if not s or len(s) < 2 or s.isdigit():
        return True
    if re.match(r"^[-+]?[\d,]+\s*(원|개|건|p|P)?$", s):
        return True
    if _NOISE_PRODUCT_RE.match(s):
        return True
    if "결제하신 내역" in s or "주문하신 내역" in s or "안내해드립니다" in s or "안내드립니다" in s:
        return True
    return False


def _match_kv_label(label: str, candidates: tuple[str, ...]) -> bool:
    clean = re.sub(r"[\s:·•\-_()\[\]ㄴ]+", "", label).lower()
    if not clean or len(clean) > 16:
        return False
    for cand in candidates:
        c_clean = re.sub(r"[\s:·•\-_()\[\]ㄴ]+", "", cand).lower()
        if clean == c_clean:
            return True
    return False


def _extract_coupang_email_orders(
    combined_text: str,
    fallback_dt: str = "",
) -> List[Dict[str, Any]]:
    """Parse Coupang order confirmation email ('구매 상세내역 / 쿠팡가 / 수량 / 구매금액 / 판매자')."""
    if "구매 상세내역" not in combined_text or "쿠팡가" not in combined_text:
        return []

    tokens = [
        tok.strip()
        for ln in combined_text.splitlines()
        for tok in ln.split(" : ")
        if tok.strip()
    ]
    raw_items: List[Dict[str, Any]] = []
    total_paid = 0
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in ("총 결제금액", "총결제금액") and i + 1 < n:
            val = _parse_int_amount(tokens[i + 1])
            if val >= 100:
                total_paid = val
        if (
            tok == "구매 상세내역"
            and i + 4 < n
            and tokens[i + 1] == "쿠팡가"
            and tokens[i + 2] == "수량"
            and tokens[i + 3] == "구매금액"
            and tokens[i + 4] == "판매자"
        ):
            j = i + 5
            while j + 4 <= n:
                p_name = tokens[j]
                u_price_s = tokens[j + 1]
                qty_s = tokens[j + 2]
                l_price_s = tokens[j + 3]
                if (
                    p_name in ("결제 정보", "결제정보", "받으시는 분")
                    or p_name.startswith(("로켓배송 상품", "판매자배송 상품", "새벽배송 상품"))
                    or not re.match(r"^[\d,]+\s*원$", u_price_s)
                    or not qty_s.isdigit()
                    or not re.match(r"^[\d,]+\s*원$", l_price_s)
                ):
                    break
                l_amt = _parse_int_amount(l_price_s)
                qty_val = max(1, int(qty_s))
                if not _is_noise_product_name(p_name) and l_amt > 0:
                    raw_items.append({"item": p_name, "qty": qty_val, "price": l_amt})
                j += 5 if j + 5 <= n else 4
            i = j
            continue
        i += 1

    if not raw_items:
        return []

    sum_items = sum(int(x["price"]) for x in raw_items)
    if total_paid <= 0:
        total_paid = sum_items
    if total_paid <= 0:
        return []

    # Prorate coupon/card discounts across line items so item prices sum to exact total_paid
    prorated_items: List[Dict[str, Any]] = []
    if sum_items > 0 and sum_items != total_paid:
        running = 0
        for idx, it in enumerate(raw_items):
            if idx < len(raw_items) - 1:
                net_p = int(round(int(it["price"]) * total_paid / sum_items))
                running += net_p
            else:
                net_p = total_paid - running
            prorated_items.append({"item": it["item"], "qty": it["qty"], "price": net_p})
    else:
        prorated_items = [dict(it) for it in raw_items]

    order_dt = fallback_dt[:10] if len(fallback_dt) >= 10 else ""
    if not order_dt:
        m_dt = re.search(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", combined_text)
        if m_dt:
            order_dt = f"{int(m_dt.group(1)):04d}-{int(m_dt.group(2)):02d}-{int(m_dt.group(3)):02d}"
    if not order_dt:
        return []

    summary_title = (
        prorated_items[0]["item"]
        if len(prorated_items) == 1
        else f"{prorated_items[0]['item']} 외 {len(prorated_items) - 1}건"
    )
    full_dt = fallback_dt if len(fallback_dt) >= 16 and fallback_dt[:10] == order_dt else order_dt
    return [
        {
            "주문일자": order_dt,
            "주문일시": full_dt,
            "상품명": summary_title,
            "결제금액": total_paid,
            "상점명": "",
            "발신처": "쿠팡",
            "승인번호": "",
            "카드사": "",
            "결제수단": "",
            "품목내역": prorated_items,
        }
    ]


def _infer_sender_portal(sender: str, subject: str) -> str:
    comb = f"{sender} {subject}".lower()
    for token, canonical in (
        ("쿠팡", "쿠팡"),
        ("coupang", "쿠팡"),
        ("네이버", "네이버페이"),
        ("naver", "네이버페이"),
        ("교보문고", "교보문고"),
        ("kyobobook", "교보문고"),
        ("카카오", "카카오페이"),
        ("kakao", "카카오페이"),
        ("11번가", "11번가"),
        ("11st", "11번가"),
        ("지마켓", "지마켓"),
        ("gmarket", "지마켓"),
        ("옥션", "옥션"),
        ("auction", "옥션"),
        ("컬리", "컬리"),
        ("kurly", "컬리"),
        ("무신사", "무신사"),
        ("musinsa", "무신사"),
        ("오늘의집", "오늘의집"),
        ("ohou", "오늘의집"),
    ):
        if token in comb:
            return canonical
    return ""


def _extract_kv_pairs_from_html_and_text(
    body_html: str,
    body_text: str,
) -> Dict[str, List[str]]:
    """Extract key-value fields (product, amount, date, shop, approval_no, card) from HTML/text."""
    found: Dict[str, List[str]] = {
        "products": [],
        "priority_amounts": [],
        "amounts": [],
        "dates": [],
        "shops": [],
        "approvals": [],
        "cards": [],
        "pay_methods": [],
    }
    priority_amt_keys = ("총결제금액", "총 결제금액", "최종결제금액", "결제금액", "승인금액", "실결제금액")

    def _record_kv(k: str, v: str) -> None:
        if not v or k == v:
            return
        if _match_kv_label(k, _KV_PRODUCT_KEYS) and not _is_noise_product_name(v):
            found["products"].append(v)
        elif _match_kv_label(k, priority_amt_keys):
            found["priority_amounts"].append(v)
            found["amounts"].append(v)
        elif _match_kv_label(k, _KV_AMOUNT_KEYS):
            found["amounts"].append(v)
        elif _match_kv_label(k, _KV_DATE_KEYS):
            found["dates"].append(v)
        elif _match_kv_label(k, _KV_SHOP_KEYS):
            found["shops"].append(v)
        elif _match_kv_label(k, _KV_APPROVAL_KEYS):
            found["approvals"].append(v)
        elif _match_kv_label(k, _KV_CARD_KEYS):
            found["cards"].append(v)
        elif _match_kv_label(k, _KV_PAY_METHOD_KEYS):
            found["pay_methods"].append(v)

    # 1. Scan HTML <tr><th>/<td> key-value pairs directly (2-cell or 4-cell KV rows only)
    if body_html:
        row_matches = re.findall(r"<tr[^>]*>(.*?)</tr>", body_html, flags=re.I | re.S)
        for tr in row_matches:
            th_cnt = len(re.findall(r"<th\b", tr, flags=re.I))
            td_cnt = len(re.findall(r"<td\b", tr, flags=re.I))
            if th_cnt >= 2 and td_cnt == 0:
                continue
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.I | re.S)
            clean_cells = [
                re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                for c in cells
            ]
            clean_cells = [c for c in clean_cells if c]
            if len(clean_cells) in (2, 4):
                for idx in range(0, len(clean_cells) - 1, 2):
                    _record_kv(clean_cells[idx], clean_cells[idx + 1])

    # 2. Scan structured text lines for "Key : Value" or "Key\nValue"
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    for idx, ln in enumerate(lines):
        if re.match(r"^주문한\s*상품\s*총\s*\d+\s*개$", ln) and idx + 1 < len(lines):
            cand_p = lines[idx + 1].strip()
            if not _is_noise_product_name(cand_p):
                found["products"].append(cand_p)
            continue

        parts = [p.strip() for p in re.split(r"\s*[:：|]\s*", ln, maxsplit=1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            _record_kv(parts[0], parts[1])
        elif idx + 1 < len(lines):
            _record_kv(ln, lines[idx + 1])

    return found


def extract_orders_from_email_content(
    subject: str = "",
    sender: str = "",
    date_str: str = "",
    body_text: str = "",
    body_html: str = "",
) -> List[Dict[str, Any]]:
    """Extract normalized e-commerce / PG order records from an email message."""
    if subject and _IGNORE_EMAIL_SUBJECT_RE.search(subject):
        return []

    combined_text = body_text or _html_to_structured_text(body_html)
    if body_html and body_text:
        html_txt = _html_to_structured_text(body_html)
        if html_txt not in combined_text:
            combined_text = f"{combined_text}\n{html_txt}"

    # Skip offline NaverPay/card point notifications that contain no product line items
    if "페이홈 > 결제내역 > 오프라인" in combined_text:
        return []

    fallback_dt = ""
    if date_str:
        try:
            parsed_dt = email.utils.parsedate_to_datetime(date_str)
            fallback_dt = parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            norm = _normalize_datetime_str(date_str)
            if len(norm) >= 10 and norm[:4].isdigit():
                fallback_dt = norm

    portal_hint = _infer_sender_portal(sender, subject)

    # 1. Dedicated Coupang Order Confirmation Email parser (multi-item + discount prorating)
    cp_email_orders = _extract_coupang_email_orders(combined_text, fallback_dt=fallback_dt)
    if cp_email_orders:
        return cp_email_orders

    # 2. Try structured HTML table / Coupang web order text / JSON extractor
    orders: List[Dict[str, Any]] = []
    if body_html and "<table" in body_html.lower():
        table_rows = extract_rows_from_html_table(body_html)
        for r in table_rows:
            has_prod = any(k in r for k in ("상품명", "품목명", "주문상품", "주문상품명", "구매상품"))
            has_amt = any(k in r for k in ("결제금액", "주문금액", "합계금액", "금액", "상품금액"))
            if has_prod and has_amt:
                prod_val = str(
                    r.get("상품명")
                    or r.get("품목명")
                    or r.get("주문상품")
                    or r.get("주문상품명")
                    or r.get("구매상품")
                    or ""
                ).strip()
                if _is_noise_product_name(prod_val):
                    continue
                if not any(k in r for k in ("주문일자", "주문일시", "결제일시", "일자")) and fallback_dt:
                    r["주문일자"] = fallback_dt[:10]
                    r["주문일시"] = fallback_dt
                orders.append(r)

    cp_orders = extract_coupang_orders_from_content(combined_text)
    for co in cp_orders:
        if not _is_noise_product_name(str(co.get("상품명") or "")):
            orders.append(co)

    # 3. Extract Key-Value PG / Shopping Mall payment confirmation fields
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
    for raw_a in kv["priority_amounts"] + kv["amounts"]:
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
        if not _is_noise_product_name(clean_p):
            kv_prod = clean_p
            break
    if not kv_prod and (
        "로켓와우클럽 월회비" in combined_text or "와우 멤버십 월회비" in subject
    ):
        kv_prod = "와우 멤버십"
    if not kv_prod and subject:
        subj_clean = re.sub(
            r"^(\[[^\]]+\]|\([^\)]+\)|RE:|FW:|FWD:)\s*",
            "",
            subject.strip(),
            flags=re.I,
        ).strip()
        if (
            subj_clean
            and not _GENERIC_NOTIFICATION_SUBJECT_RE.search(subj_clean)
            and not _is_noise_product_name(subj_clean)
        ):
            kv_prod = subj_clean

    kv_shop = ""
    for raw_s in kv["shops"]:
        clean_s = re.sub(r"\s+", " ", raw_s).strip()
        if clean_s and len(clean_s) >= 2 and len(clean_s) <= 40:
            kv_shop = clean_s
            break

    kv_app = ""
    for raw_ap in kv["approvals"]:
        m_ap = re.search(r"(\d{6,12})", raw_ap)
        if m_ap:
            kv_app = m_ap.group(1)
            break

    kv_card = ""
    for raw_c in kv["cards"]:
        clean_c = re.sub(r"\s+", " ", raw_c).strip()
        if clean_c and len(clean_c) <= 30:
            kv_card = clean_c
            break

    kv_pay = ""
    for raw_pm in kv["pay_methods"]:
        clean_pm = re.sub(r"\s+", " ", raw_pm).strip()
        if clean_pm and len(clean_pm) <= 30:
            kv_pay = clean_pm
            break

    if kv_dt and kv_amt > 0 and kv_prod and not orders:
        orders.append(
            {
                "주문일자": kv_dt[:10],
                "주문일시": kv_dt if len(kv_dt) >= 16 else (fallback_dt or kv_dt),
                "상품명": kv_prod,
                "결제금액": kv_amt,
                "상점명": kv_shop,
                "발신처": portal_hint,
                "승인번호": kv_app,
                "카드사": kv_card,
                "결제수단": kv_pay,
                "품목내역": [{"item": kv_prod, "qty": 1, "price": kv_amt}],
            }
        )

    # Deduplicate by (date, amount, product) and enrich metadata
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for o in orders:
        raw_full_dt = str(o.get("주문일시") or o.get("결제일시") or o.get("주문일자") or fallback_dt)
        norm_full = _normalize_datetime_str(raw_full_dt)
        dt_k = norm_full[:10] if len(norm_full) >= 10 else str(o.get("주문일자") or "")[:10]
        amt_k = _parse_int_amount(str(o.get("결제금액") or o.get("주문금액") or 0))
        prod_k = str(o.get("상품명") or o.get("품목명") or "").strip()
        if not dt_k or amt_k <= 0 or _is_noise_product_name(prod_k):
            continue
        o["주문일자"] = dt_k
        if len(norm_full) >= 19 and norm_full[11:19] != "00:00:00":
            o["주문일시"] = norm_full[:19]
        elif fallback_dt and fallback_dt[:10] == dt_k:
            o["주문일시"] = fallback_dt
        else:
            o["주문일시"] = dt_k
        o["결제금액"] = amt_k
        o["상품명"] = prod_k
        if portal_hint and not o.get("발신처"):
            o["발신처"] = portal_hint
        if kv_shop and not o.get("상점명"):
            o["상점명"] = kv_shop
        if kv_app and not o.get("승인번호"):
            o["승인번호"] = kv_app
        if kv_card and not o.get("카드사"):
            o["카드사"] = kv_card
        if kv_pay and not o.get("결제수단"):
            o["결제수단"] = kv_pay
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


def _order_priority_key(o: Dict[str, Any]) -> tuple[int, int, int]:
    """Prioritize itemized multi-product orders and full titles over truncated PG 1-liners."""
    lines = o.get("품목내역") if isinstance(o.get("품목내역"), list) else []
    li_cnt = len(lines)
    title = str(o.get("상품명") or "")
    is_truncated_pg = 1 if (li_cnt <= 1 and re.search(r"(포함\s*총\s*\d+|외\s*\d+건|\s\d+$)", title)) else 0
    return (-li_cnt, is_truncated_pg, -len(title))


def _parse_order_ts_seconds(ts_str: str) -> Optional[float]:
    s = str(ts_str or "").strip().replace("T", " ")
    if len(s) < 19 or s[11:19] == "00:00:00":
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _are_companion_orders(primary: Dict[str, Any], other: Dict[str, Any]) -> bool:
    """Return True if two same-(date, amount) emails represent Mall + PG emails for one purchase."""
    app_a = str(primary.get("승인번호") or "").strip()
    app_b = str(other.get("승인번호") or "").strip()
    if app_a and app_b and app_a != app_b:
        return False

    norm_a = re.sub(r"[^0-9a-zA-Z가-힣]+", "", str(primary.get("상품명") or "")).lower()
    norm_b = re.sub(r"[^0-9a-zA-Z가-힣]+", "", str(other.get("상품명") or "")).lower()
    prefix_len = min(6, len(norm_a), len(norm_b))
    has_title_overlap = bool(
        prefix_len >= 4 and (norm_a[:prefix_len] == norm_b[:prefix_len] or norm_b in norm_a or norm_a in norm_b)
    )

    ts_a = _parse_order_ts_seconds(str(primary.get("주문일시") or ""))
    ts_b = _parse_order_ts_seconds(str(other.get("주문일시") or ""))
    if ts_a is not None and ts_b is not None and abs(ts_a - ts_b) > 1800 and not has_title_overlap:
        return False

    shop_a = re.sub(r"\(주\)|㈜|주식회사|\s+", "", str(primary.get("상점명") or primary.get("발신처") or "")).lower()
    shop_b = re.sub(r"\(주\)|㈜|주식회사|\s+", "", str(other.get("상점명") or other.get("발신처") or "")).lower()
    if shop_a and shop_b and shop_a not in shop_b and shop_b not in shop_a and not has_title_overlap:
        return False

    return True


def _merge_companion_email_orders(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fuse companion Shopping Mall + PG confirmation emails on the same (date, amount) into one."""
    grouped: Dict[tuple[str, int], List[Dict[str, Any]]] = {}
    for o in orders:
        dt_k = str(o.get("주문일자") or "")[:10]
        amt_k = _parse_int_amount(str(o.get("결제금액") or 0))
        if not dt_k or amt_k <= 0:
            continue
        grouped.setdefault((dt_k, amt_k), []).append(o)

    merged_all: List[Dict[str, Any]] = []
    for _, bucket in grouped.items():
        bucket.sort(key=_order_priority_key)
        clusters: List[Dict[str, Any]] = []
        for cand in bucket:
            target_cluster: Optional[Dict[str, Any]] = None
            for existing in clusters:
                if _are_companion_orders(existing, cand):
                    target_cluster = existing
                    break
            if target_cluster is None:
                clusters.append(dict(cand))
                continue

            for meta_key in ("승인번호", "카드사", "결제수단", "상점명", "발신처"):
                if not target_cluster.get(meta_key) and cand.get(meta_key):
                    target_cluster[meta_key] = cand[meta_key]

            # Prefer PG's exact card approval timestamp when available
            cand_dt = str(cand.get("주문일시") or "")
            cur_dt = str(target_cluster.get("주문일시") or "")
            if cand.get("승인번호") and _parse_order_ts_seconds(cand_dt) is not None:
                target_cluster["주문일시"] = cand_dt
            elif (
                _parse_order_ts_seconds(cur_dt) is None
                and _parse_order_ts_seconds(cand_dt) is not None
            ):
                target_cluster["주문일시"] = cand_dt

        merged_all.extend(clusters)

    merged_all.sort(key=_order_priority_key)
    return merged_all


def _extract_cancel_approval_nos_from_eml(raw_eml: bytes) -> List[str]:
    """Extract card approval numbers from a cancellation notification email."""
    try:
        msg = email.message_from_bytes(raw_eml)
    except Exception:
        return []

    texts: List[str] = []
    htmls: List[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                dec = payload.decode(charset, errors="replace")
            except LookupError:
                dec = payload.decode("utf-8", errors="replace")
            if (part.get_content_type() or "").lower() == "text/html":
                htmls.append(dec)
            else:
                texts.append(dec)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                dec = payload.decode(charset, errors="replace")
            except LookupError:
                dec = payload.decode("utf-8", errors="replace")
            if (msg.get_content_type() or "").lower() == "text/html":
                htmls.append(dec)
            else:
                texts.append(dec)

    body_html = "\n".join(htmls)
    combined = "\n".join(texts)
    if body_html:
        combined = f"{combined}\n{_html_to_structured_text(body_html)}"
    kv = _extract_kv_pairs_from_html_and_text(body_html, combined)
    apps: List[str] = []
    for raw_ap in kv["approvals"]:
        m_ap = re.search(r"(\d{6,12})", raw_ap)
        if m_ap and m_ap.group(1) not in apps:
            apps.append(m_ap.group(1))
    for m_rx in re.finditer(r"승인번호\s*[:：]?\s*(\d{6,12})", combined):
        if m_rx.group(1) not in apps:
            apps.append(m_rx.group(1))
    return apps


def _latest_card_statement_date(cursor: sqlite3.Cursor) -> str:
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


def ingest_email_order_payload(
    cursor: sqlite3.Cursor,
    raw_content: bytes | str,
    subject: str = "",
    date_str: str = "",
    archive_dir: Optional[Path] = None,
    auto_create_tx: bool = False,
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
            "created_count": 0,
            "updated_tx_ids": [],
            "matched_items": [],
            "generated_statements_count": 0,
        }

    merged_orders = _merge_companion_email_orders(orders)
    latest_stmt_dt = _latest_card_statement_date(cursor)
    res = reconcile_scraped_ecommerce_orders(
        cursor,
        raw_content=json.dumps(merged_orders, ensure_ascii=False),
        archive_dir=archive_dir,
        orders_list=merged_orders,
        auto_create_after_date=latest_stmt_dt,
        auto_create_all=auto_create_tx,
    )
    matched_items = res.get("matched_items") or []
    if matched_items:
        cfg = load_email_watcher_config(archive_dir)
        merged = _merge_recent_matched_items(cfg.get("recent_matched_items") or [], matched_items)
        save_email_watcher_config({"recent_matched_items": merged}, archive_dir=archive_dir)

    return {
        "status": "ingested",
        "parsed_orders_count": len(merged_orders),
        "matched_count": res["matched_count"],
        "created_count": int(res.get("created_count") or 0),
        "updated_tx_ids": res["updated_tx_ids"],
        "matched_items": matched_items,
        "generated_statements_count": res["matched_count"],
        "orders_preview": merged_orders[:10],
    }


def sync_imap_order_emails(
    cursor: sqlite3.Cursor,
    archive_dir: Optional[Path] = None,
    max_messages: int = 600,
    force_rescan: bool = False,
    account_id: Optional[str] = None,
    recent_rescan_days: int = 0,
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
            "created_count": 0,
            "newly_stated_count": 0,
            "updated_tx_ids": [],
            "matched_items": [],
            "account_results": [],
        }

    since_dt = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    recent_since_dt = (
        (datetime.now() - timedelta(days=recent_rescan_days)).strftime("%d-%b-%Y")
        if recent_rescan_days > 0 and not force_rescan
        else ""
    )
    total_scanned_emails = 0
    all_orders: List[Dict[str, Any]] = []
    canceled_apps: set[str] = set()
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
        acc_checked_count = 0
        acc_orders: List[Dict[str, Any]] = []
        acc_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            imap = imaplib.IMAP4_SSL(host, port, timeout=25.0)
            try:
                imap.login(user, password)
                imap.select(mailbox, readonly=True)
                typ, data = imap.search(None, f"(SINCE {since_dt})")
                if typ != "OK" or not data or not data[0]:
                    msg_nums: List[bytes] = []
                else:
                    msg_nums = data[0].split()

                always_rescan_nums: set[bytes] = set()
                if recent_since_dt:
                    typ_r, data_r = imap.search(None, f"(SINCE {recent_since_dt})")
                    if typ_r == "OK" and data_r and data_r[0]:
                        always_rescan_nums = set(data_r[0].split())

                recent_nums = list(reversed(msg_nums[-max_messages:]))
                acc_checked_count = len(recent_nums)
                pending_nums: List[bytes] = []
                for num in recent_nums:
                    uid_key = f"{user}:{mailbox}:{num.decode('ascii', errors='ignore')}"
                    if (
                        not force_rescan
                        and num not in always_rescan_nums
                        and uid_key in processed_ids
                    ):
                        continue
                    pending_nums.append(num)

                # Batch-fetch headers in chunks of 100 for fast scanning
                candidate_bodies: List[tuple[bytes, str, str, bool]] = []
                for b_start in range(0, len(pending_nums), 100):
                    chunk = pending_nums[b_start : b_start + 100]
                    seq_set = b",".join(chunk)
                    typ_h, hdr_data = imap.fetch(
                        seq_set,
                        "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])",
                    )
                    if typ_h != "OK" or not hdr_data:
                        continue
                    for item in hdr_data:
                        if not (isinstance(item, tuple) and len(item) >= 2):
                            continue
                        meta_line = item[0] if isinstance(item[0], bytes) else b""
                        m_seq = re.match(rb"^(\d+)\b", meta_line.strip())
                        num_b = m_seq.group(1) if m_seq else chunk[0]
                        uid_key = f"{user}:{mailbox}:{num_b.decode('ascii', errors='ignore')}"
                        hdr_msg = email.message_from_bytes(item[1])
                        subj = _decode_mime_header(hdr_msg.get("Subject"))
                        sender = _decode_mime_header(hdr_msg.get("From"))
                        raw_mid = str(hdr_msg.get("Message-ID") or "").strip()
                        msg_id = f"{user}:{raw_mid}" if raw_mid else uid_key
                        is_always = force_rescan or (num_b in always_rescan_nums)
                        if not is_always and msg_id in processed_ids:
                            continue
                        if _CANCELED_EMAIL_SUBJECT_RE.search(subj):
                            candidate_bodies.append((num_b, msg_id, uid_key, True))
                            continue
                        if _IGNORE_EMAIL_SUBJECT_RE.search(subj) or not _ORDER_EMAIL_SUBJECT_RE.search(
                            f"{subj} {sender}"
                        ):
                            new_processed.append(msg_id)
                            new_processed.append(uid_key)
                            continue
                        candidate_bodies.append((num_b, msg_id, uid_key, False))

                for b_start in range(0, len(candidate_bodies), 25):
                    chunk_cands = candidate_bodies[b_start : b_start + 25]
                    by_seq = {num_b: (m_id, u_id, is_c) for num_b, m_id, u_id, is_c in chunk_cands}
                    seq_set = b",".join(num_b for num_b, _, _, _ in chunk_cands)
                    typ_b, body_data = imap.fetch(seq_set, "(BODY.PEEK[])")
                    if typ_b != "OK" or not body_data:
                        continue
                    for item in body_data:
                        if not (
                            isinstance(item, tuple)
                            and len(item) >= 2
                            and isinstance(item[1], bytes)
                        ):
                            continue
                        meta_b = item[0] if isinstance(item[0], bytes) else b""
                        m_seq = re.match(rb"^(\d+)\b", meta_b.strip())
                        seq_k = m_seq.group(1) if m_seq else chunk_cands[0][0]
                        raw_eml = item[1]
                        if not raw_eml:
                            continue

                        is_cancel_msg = by_seq[seq_k][2] if seq_k in by_seq else False
                        if is_cancel_msg:
                            canceled_apps.update(_extract_cancel_approval_nos_from_eml(raw_eml))
                        else:
                            acc_scanned += 1
                            extracted = parse_raw_email_bytes_or_text(raw_eml)
                            acc_orders.extend(extracted)
                        if seq_k in by_seq:
                            m_id, u_id, _ = by_seq[seq_k]
                            new_processed.append(m_id)
                            new_processed.append(u_id)
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

            acc_merged = _merge_companion_email_orders(acc_orders)
            success_accounts += 1
            total_scanned_emails += acc_scanned
            all_orders.extend(acc_orders)
            acc["last_sync_at"] = acc_now
            acc["last_status"] = "synced"
            acc["last_message"] = (
                f"메일함 {acc_checked_count}통 확인 · 주문메일 {acc_scanned}통 분석 · "
                f"주문 {len(acc_merged)}건 추출"
            )
            account_results.append(
                {
                    "account_id": aid,
                    "email_address": user,
                    "status": "synced",
                    "checked_mailbox_count": acc_checked_count,
                    "scanned_emails": acc_scanned,
                    "parsed_orders_count": len(acc_merged),
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
            "created_count": 0,
            "newly_stated_count": 0,
            "updated_tx_ids": [],
            "matched_items": [],
            "account_results": account_results,
        }

    if canceled_apps:
        placeholders = ",".join("?" for _ in canceled_apps)
        cursor.execute(
            f"""
            UPDATE card_transactions
            SET reconcile_status = 'CANCELED', updated_at = CURRENT_TIMESTAMP
            WHERE reconcile_status = 'EMAIL_ORDER'
              AND approval_no IN ({placeholders})
            """,
            tuple(canceled_apps),
        )

    merged_orders = _merge_companion_email_orders(all_orders)
    if canceled_apps:
        merged_orders = [
            o for o in merged_orders if str(o.get("승인번호") or "").strip() not in canceled_apps
        ]

    latest_stmt_dt = _latest_card_statement_date(cursor)
    rec_res = reconcile_scraped_ecommerce_orders(
        cursor,
        raw_content=json.dumps(merged_orders, ensure_ascii=False),
        archive_dir=archive_dir,
        orders_list=merged_orders,
        auto_create_after_date=latest_stmt_dt,
    )
    matched = int(rec_res.get("matched_count") or 0)
    created_cnt = int(rec_res.get("created_count") or 0)
    newly_stated_cnt = int(rec_res.get("newly_stated_count") or 0)
    matched_items = rec_res.get("matched_items") or []
    merged_history = _merge_recent_matched_items(
        cfg.get("recent_matched_items") or [],
        matched_items,
    )
    if created_cnt > 0:
        summary_msg = (
            f"계정 {success_accounts}/{len(valid_targets)}개 · 주문메일 {total_scanned_emails}통 분석 · "
            f"주문 {len(merged_orders)}건 확인 · 전표 {matched}건 반영 (신규 전표 {created_cnt}건 자동생성)"
        )
    else:
        summary_msg = (
            f"계정 {success_accounts}/{len(valid_targets)}개 · 주문메일 {total_scanned_emails}통 분석 · "
            f"주문 {len(merged_orders)}건 확인 · 전표 {matched}건 거래명세서 반영 완료"
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
            "recent_matched_items": merged_history,
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
        "parsed_orders_count": len(merged_orders),
        "matched_count": matched,
        "created_count": created_cnt,
        "newly_stated_count": newly_stated_cnt,
        "created_tx_ids": rec_res.get("created_tx_ids") or [],
        "newly_stated_tx_ids": rec_res.get("newly_stated_tx_ids") or [],
        "updated_tx_ids": rec_res.get("updated_tx_ids") or [],
        "matched_items": matched_items,
        "generated_statements_count": matched,
        "account_results": account_results,
    }
