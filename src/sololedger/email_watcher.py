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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger.config import settings
from sololedger.importers import _normalize_datetime_str, _parse_int_amount
from sololedger.nts_client import validate_biz_reg_no_checksum
from sololedger.scraper_service import (
    extract_coupang_orders_from_content,
    extract_rows_from_html_table,
    format_biz_reg_no_display,
    generate_electronic_slips_for_ledger,
    generate_electronic_statement_for_tx,
    reconcile_scraped_ecommerce_orders,
)
from sololedger.sms_parser import (
    format_card_label,
    is_blocked_personal_card_code,
    load_corp_card_policy,
    match_registered_corp_card_code,
    register_corp_card,
)
from sololedger.tax_engine import calculate_supply_and_vat, evaluate_tax_rules

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
    r"(부분취소|결제취소|주문취소|승인취소|취소하신\s*내역|취소한\s*내역|취소되었|취소\s*내역|취소\s*안내)",
    re.IGNORECASE,
)

_IGNORE_EMAIL_SUBJECT_RE = re.compile(
    r"(\(광고\)|\[광고\]|광고성\s*정보|수신동의|로그인\s*되었|새로운\s*기기|새로운\s*환경|"
    r"약관\s*개정|회원\s*등급|인증서\s*개인정보|부분취소|결제취소|주문취소|승인취소|"
    r"취소하신\s*내역|취소한\s*내역|취소되었|취소\s*내역|취소\s*안내|혜택이\s*곧\s*종료|비밀번호\s*변경)",
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
_KV_BIZ_NO_KEYS = (
    "가맹점사업자번호",
    "공급자사업자번호",
    "판매자사업자번호",
    "사업자등록번호",
    "사업자번호",
)

# Known PG & open-market intermediary business numbers (never assign as direct item supplier)
_INTERMEDIARY_OR_PG_BIZ_NOS = frozenset(
    {
        "1058765162",  # 쿠팡페이(주) (Coupay PG / Coupang Eats intermediary)
        "2208115770",  # 나이스정보통신(주)
        "2208155597",  # KG이니시스
        "4118601799",  # 토스페이먼츠
        "1138621083",  # NHN KCP
        "1048150311",  # 한국정보통신 (KICC / 이지페이)
        "1168119948",  # 나이스페이먼츠
        "1168119977",  # 나이스정보통신
        "5248601528",  # 네이버파이낸셜
        "2208162517",  # 네이버(주)
        "5278800696",  # 카카오페이
        "1208147521",  # 카카오
        "2158700829",  # 십일번가(11번가)
        "2208183676",  # 지마켓/옥션
        "1208765763",  # 우아한형제들(배달의민족 중개)
        "1148628575",  # 다날
        "1448115549",  # 엔에이치엔페이코
    }
)

_PG_OR_INTERMEDIARY_NAME_RE = re.compile(
    r"(이니시스|토스페이먼츠|케이씨피|kcp|나이스페이|나이스정보통신|nicepay|한국정보통신|이지페이|easypay|"
    r"다날|페이코|네이버파이낸셜|네이버페이|카카오페이|십일번가|11번가|지마켓|옥션|인터파크|"
    r"쿠페이|쿠팡페이|쿠팡이츠|배달의민족|우아한형제들|요기요|위메프|티몬|"
    r"스마트로|다우데이타|갤럭시아|세틀뱅크|헥토파이낸셜|모빌리언스)",
    re.IGNORECASE,
)

# Platforms whose seller identity must come from the order table (not a generic PG receipt table)
_PLATFORM_SHOP_IN_PG_RE = re.compile(
    r"(쿠팡|coupang|쿠페이|쿠팡이츠|네이버|카카오|11번가|십일번가|지마켓|옥션|인터파크|"
    r"배달의민족|우아한형제들|요기요|무신사|오늘의집)",
    re.IGNORECASE,
)


def _clean_valid_biz_no(raw_val: str) -> str:
    """Extract and validate a 10-digit Korean business registration number via checksum."""
    if not raw_val:
        return ""
    for m in re.finditer(r"(\d{3})\s*-\s*(\d{2})\s*-\s*(\d{5})", str(raw_val)):
        cand = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        if validate_biz_reg_no_checksum(cand):
            return cand
    for m in re.finditer(r"(?<!\d)(\d{10})(?!\d)", str(raw_val)):
        cand = m.group(1)
        if validate_biz_reg_no_checksum(cand):
            return cand
    return ""


def _is_coupang_direct_seller(seller_name: str) -> bool:
    """Return True only when the line-item seller is Coupang Corp. itself (Rocket Direct)."""
    norm = re.sub(r"[\s()（）\[\]주식회사㈜유한회사]+", "", str(seller_name or "")).lower()
    return norm in ("쿠팡", "coupang")


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
        "lookback_days": 365,
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
    limit: int = 500,
) -> List[Dict[str, Any]]:
    by_tx: Dict[int, Dict[str, Any]] = {}
    for item in existing or []:
        if isinstance(item, dict) and item.get("tx_id") is not None:
            by_tx[int(item["tx_id"])] = dict(item)
    for item in incoming or []:
        if isinstance(item, dict) and item.get("tx_id") is not None:
            tid = int(item["tx_id"])
            prev = by_tx.get(tid)
            merged_item = dict(item)
            if prev and prev.get("is_created"):
                merged_item["is_created"] = True
            by_tx[tid] = merged_item
    merged = sorted(
        by_tx.values(),
        key=lambda x: (str(x.get("transacted_at") or ""), int(x.get("tx_id") or 0)),
        reverse=True,
    )
    return merged[:limit]


def _write_config(cfg: Dict[str, Any], archive_dir: Optional[Path] = None) -> Dict[str, Any]:
    cfg["poll_interval_sec"] = max(30, int(cfg.get("poll_interval_sec") or 120))
    cfg["lookback_days"] = max(1, min(1095, int(cfg.get("lookback_days") or 365)))
    if isinstance(cfg.get("processed_ids"), list):
        dedup_ids = list(dict.fromkeys(str(x) for x in cfg["processed_ids"] if x))
        if len(dedup_ids) > 50000:
            dedup_ids = dedup_ids[-50000:]
        cfg["processed_ids"] = dedup_ids
    if isinstance(cfg.get("recent_matched_items"), list):
        cfg["recent_matched_items"] = cfg["recent_matched_items"][:500]

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
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Query live ledger transactions that have generated Transaction Statements (거래명세서)."""
    cursor.execute(
        """
        SELECT t.tx_id, t.transacted_at, t.card_last4, t.raw_merchant_name, t.biz_reg_no,
               t.total_amount, t.account_code, t.account_name, t.counterparty_info,
               t.reconcile_status, r.archived_file_name, r.line_items_json, r.raw_ocr_response
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
        mp_seller = ""
        raw_ocr = r["raw_ocr_response"]
        if raw_ocr:
            try:
                ocr_obj = json.loads(str(raw_ocr))
                if isinstance(ocr_obj, dict):
                    mp_seller = str(ocr_obj.get("marketplace_seller") or "").strip()
            except Exception:
                pass
        raw_biz = str(r["biz_reg_no"] or "").strip()
        fmt_biz = format_biz_reg_no_display(raw_biz) if raw_biz else ""
        items.append(
            {
                "tx_id": int(r["tx_id"]),
                "transacted_at": str(r["transacted_at"]),
                "card_label": format_card_label(str(r["card_last4"] or "0000")),
                "raw_merchant_name": str(r["raw_merchant_name"]),
                "biz_reg_no": fmt_biz,
                "marketplace_seller": mp_seller,
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
        live_items = list_statement_matched_transactions(cursor, limit=500)
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
                seller_s = ""
                step = 4
                if j + 4 < n:
                    cand_seller = tokens[j + 4].strip()
                    next_is_section = cand_seller in (
                        "결제 정보",
                        "결제정보",
                        "받으시는 분",
                    ) or cand_seller.startswith(("로켓배송 상품", "판매자배송 상품", "새벽배송 상품"))
                    next_is_item_row = (
                        j + 7 < n
                        and re.match(r"^[\d,]+\s*원$", tokens[j + 5])
                        and tokens[j + 6].isdigit()
                        and re.match(r"^[\d,]+\s*원$", tokens[j + 7])
                    )
                    if not next_is_section and not next_is_item_row:
                        seller_s = cand_seller
                        step = 5
                l_amt = _parse_int_amount(l_price_s)
                qty_val = max(1, int(qty_s))
                if not _is_noise_product_name(p_name) and l_amt > 0:
                    entry: Dict[str, Any] = {"item": p_name, "qty": qty_val, "price": l_amt}
                    if seller_s:
                        entry["seller"] = seller_s
                    raw_items.append(entry)
                j += step
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
            p_entry: Dict[str, Any] = {"item": it["item"], "qty": it["qty"], "price": net_p}
            if it.get("seller"):
                p_entry["seller"] = it["seller"]
            prorated_items.append(p_entry)
    else:
        prorated_items = [dict(it) for it in raw_items]

    order_dt = fallback_dt[:10] if len(fallback_dt) >= 10 else ""
    if not order_dt:
        m_dt = re.search(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", combined_text)
        if m_dt:
            order_dt = f"{int(m_dt.group(1)):04d}-{int(m_dt.group(2)):02d}-{int(m_dt.group(3)):02d}"
    if not order_dt:
        return []

    # Separate Coupang Direct (쿠팡(주) -> 120-88-00767) from 3rd-party Marketplace/Rocket-Growth sellers
    sellers = [
        str(it.get("seller") or "").strip()
        for it in prorated_items
        if str(it.get("seller") or "").strip()
    ]
    third_party_sellers: List[str] = []
    for s_name in sellers:
        if not _is_coupang_direct_seller(s_name) and s_name not in third_party_sellers:
            third_party_sellers.append(s_name)
    has_direct_cp = any(_is_coupang_direct_seller(s_name) for s_name in sellers)

    m_cp_biz = re.search(r"사업자등록번호\s*[:：]?\s*(\d{3}\s*-\s*\d{2}\s*-\s*\d{5})", combined_text)
    cp_biz_no = _clean_valid_biz_no(m_cp_biz.group(1)) if m_cp_biz else "1208800767"
    if not cp_biz_no:
        cp_biz_no = "1208800767"

    order_biz_no = ""
    mp_seller = ""
    if third_party_sellers:
        if len(third_party_sellers) == 1 and not has_direct_cp:
            mp_seller = third_party_sellers[0]
        elif has_direct_cp:
            mp_seller = f"{third_party_sellers[0]} 외 (쿠팡 복합주문)"
        else:
            mp_seller = f"{third_party_sellers[0]} 외 {len(third_party_sellers) - 1}곳"
    elif has_direct_cp or ("로켓배송" in combined_text and "판매자배송" not in combined_text):
        order_biz_no = cp_biz_no

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
            "사업자번호": order_biz_no,
            "입점판매자": mp_seller,
            "품목내역": prorated_items,
        }
    ]


def _infer_sender_portal(sender: str, subject: str) -> str:
    comb = f"{sender} {subject}".lower()
    for token, canonical in (
        ("토스페이먼츠", "토스페이먼츠"),
        ("tosspayments", "토스페이먼츠"),
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


_TOSS_RECEIPT_URL_RE = re.compile(
    r"https://dashboard\.tosspayments\.com/receipt/(?:redirection|sales-slip)\?[^\"'\s<>]+",
    re.IGNORECASE,
)

_TOSS_SUBJECT_BUYER_SELLER_RE = re.compile(
    r"^\s*(?P<buyer>.+?)님,\s*(?P<seller>.+?)에서\s*"
    r"(?P<action>부분취소한|취소한|결제한)\s*내역이\s*도착했어요",
    re.IGNORECASE,
)

_CORP_BUYER_OR_ITEM_RE = re.compile(
    r"(주식회사|\(주\)|㈜|\(유\)|유한회사|사단법인|재단법인|협동조합|사업자범용)",
    re.IGNORECASE,
)

# Korean Corporate Card BIN prefixes vs Personal Card BIN prefixes on Toss sales slips
_TOSS_CORP_CARD_BIN6_PREFIXES = (
    "910020",  # 우리비씨 기업/법인카드
    "9430",    # BC 기업/법인카드 계열
    "9411",    # 기업카드 계열
)

_TOSS_PERSONAL_CARD_BIN6_PREFIXES = (
    "940915",  # 롯데 개인카드
    "950001",  # 우리 개인카드
    "379183",  # 삼성 개인 AMEX
)


def is_toss_payments_email(
    sender: str,
    subject: str = "",
    body_html: str = "",
    body_text: str = "",
) -> bool:
    """Return True if an email originates from or contains a Toss Payments receipt notice."""
    hdr_comb = f"{sender} {subject}".lower()
    if "tosspayments" in hdr_comb or "토스페이먼츠" in hdr_comb:
        return True
    if body_html and "dashboard.tosspayments.com/receipt/" in body_html.lower():
        return True
    body_probe = f"{body_text[:4000]} {body_html[:6000]}"
    return "토스페이먼츠 주식회사" in body_probe and "411-86-01799" in body_probe


def parse_toss_payments_email(
    subject: str,
    sender: str = "",
    date_str: str = "",
    body_text: str = "",
    body_html: str = "",
) -> Optional[Dict[str, Any]]:
    """Parse a Toss Payments payment / partial-cancel / cancel email into structured fields."""
    if not is_toss_payments_email(sender, subject, body_html, body_text):
        return None

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
            norm_fb = _normalize_datetime_str(date_str)
            if len(norm_fb) >= 10 and norm_fb[:4].isdigit():
                fallback_dt = norm_fb

    buyer_greeting = ""
    subj_seller = ""
    event_type = "PAYMENT"
    m_subj = _TOSS_SUBJECT_BUYER_SELLER_RE.search(subject or "")
    if m_subj:
        buyer_greeting = m_subj.group("buyer").strip()
        subj_seller = m_subj.group("seller").strip()
        act = m_subj.group("action")
        if "부분취소" in act:
            event_type = "PARTIAL_CANCEL"
        elif "취소" in act:
            event_type = "CANCEL"
    elif subject:
        if "부분취소" in subject:
            event_type = "PARTIAL_CANCEL"
        elif re.search(r"(결제\s*취소|승인\s*취소|취소한\s*내역|취소하신\s*내역)", subject):
            event_type = "CANCEL"

    def _find_toss_field(labels: tuple[str, ...]) -> str:
        lbl_pat = "|".join(re.escape(lb) for lb in labels)
        if body_html:
            # Match <th>/<td> label cell followed by value cell in Toss HTML email table
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body_html, flags=re.I | re.S):
                cells = [
                    re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                    for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.I | re.S)
                ]
                cells = [c for c in cells if c]
                for idx in range(len(cells) - 1):
                    key_clean = re.sub(r"[\s:：·\-]+", "", cells[idx])
                    if any(key_clean == re.sub(r"\s+", "", lb) for lb in labels):
                        val_cand = cells[idx + 1].strip()
                        if val_cand:
                            return val_cand
        # Text line fallback ("구매상품 : ..." or "구매상품\n...")
        m_inline = re.search(
            rf"(?:^|\n)\s*(?:{lbl_pat})\s*[:：]\s*([^\n\r]+)",
            combined_text,
        )
        if m_inline:
            return m_inline.group(1).strip()
        m_next = re.search(
            rf"(?:^|\n)\s*(?:{lbl_pat})\s*\n+\s*([^\n\r]+)",
            combined_text,
        )
        if m_next:
            return m_next.group(1).strip()
        return ""

    product_name = _find_toss_field(("구매상품", "주문상품", "상품명", "품목명"))
    raw_paid_amt = _find_toss_field(("결제금액", "총결제금액", "승인금액"))
    raw_cancel_amt = _find_toss_field(("부분취소금액", "취소금액", "환불금액"))
    raw_remain_amt = _find_toss_field(("남은결제금액", "취소후잔액", "잔여금액"))
    pay_method = _find_toss_field(("결제수단", "결제방식"))
    card_issuer = _find_toss_field(("결제카드", "카드사", "카드종류"))
    installment = _find_toss_field(("할부기간", "할부개월"))
    raw_paid_dt = _find_toss_field(("결제일시", "승인일시", "거래일시"))
    raw_cancel_dt = _find_toss_field(("부분취소일시", "취소일시"))
    raw_app_no = _find_toss_field(("승인번호", "카드승인번호"))
    order_no = _find_toss_field(("주문번호", "거래번호"))
    seller_name = _find_toss_field(("판매자", "가맹점", "상점명", "이용상점")) or subj_seller

    # Ensure PG footer ("토스페이먼츠 주식회사") never overrides the actual seller
    if seller_name and "토스페이먼츠" in seller_name and subj_seller:
        seller_name = subj_seller

    paid_amt = _parse_int_amount(raw_paid_amt)
    cancel_amt = _parse_int_amount(raw_cancel_amt)
    remain_amt = _parse_int_amount(raw_remain_amt)

    if paid_amt <= 0 and event_type == "PAYMENT":
        amt_cands = [
            _parse_int_amount(x)
            for x in re.findall(r"(\d{1,3}(?:,\d{3})+)\s*원", combined_text)
        ]
        valid_cands = [a for a in amt_cands if a >= 100]
        if valid_cands:
            paid_amt = valid_cands[0]

    norm_dt = _normalize_datetime_str(raw_paid_dt) if raw_paid_dt else ""
    if not (len(norm_dt) >= 10 and norm_dt[:4].isdigit()):
        norm_dt = fallback_dt

    m_app = re.search(r"(\d{6,12})", raw_app_no or "")
    approval_no = m_app.group(1) if m_app else ""

    receipt_url = ""
    for src_txt in (body_html, combined_text):
        if not src_txt:
            continue
        m_url = _TOSS_RECEIPT_URL_RE.search(src_txt)
        if m_url:
            receipt_url = html.unescape(m_url.group(0)).strip()
            break

    effective_amt = paid_amt
    if event_type == "PARTIAL_CANCEL":
        if remain_amt > 0:
            effective_amt = remain_amt
        elif paid_amt > cancel_amt > 0:
            effective_amt = paid_amt - cancel_amt

    if not norm_dt and not product_name and paid_amt <= 0 and cancel_amt <= 0:
        return None

    return {
        "event_type": event_type,
        "buyer_greeting": buyer_greeting,
        "merchant_name": seller_name,
        "item_name": product_name,
        "paid_amount": paid_amt,
        "cancel_amount": cancel_amt,
        "effective_amount": effective_amt,
        "payment_method": pay_method,
        "card_issuer": card_issuer,
        "installment": installment,
        "transacted_at": norm_dt[:19] if len(norm_dt) >= 19 else norm_dt,
        "canceled_at": _normalize_datetime_str(raw_cancel_dt) if raw_cancel_dt else "",
        "approval_no": approval_no,
        "order_no": order_no,
        "receipt_url": receipt_url,
        "subject": subject,
    }


def _extract_toss_payments_email_orders(
    subject: str,
    sender: str,
    date_str: str,
    body_text: str,
    body_html: str,
) -> List[Dict[str, Any]]:
    """Convert a Toss Payments payment email into SoloLedger's standard order list format."""
    parsed = parse_toss_payments_email(
        subject=subject,
        sender=sender,
        date_str=date_str,
        body_text=body_text,
        body_html=body_html,
    )
    if not parsed or parsed["event_type"] != "PAYMENT":
        return []
    dt_full = str(parsed.get("transacted_at") or "")
    amt = int(parsed.get("effective_amount") or parsed.get("paid_amount") or 0)
    prod = str(parsed.get("item_name") or "").strip()
    if len(dt_full) < 10 or amt <= 0 or not prod or _is_noise_product_name(prod):
        return []

    return [
        {
            "주문일자": dt_full[:10],
            "주문일시": dt_full if len(dt_full) >= 16 else f"{dt_full[:10]} 12:00:00",
            "상품명": prod,
            "결제금액": amt,
            "상점명": str(parsed.get("merchant_name") or ""),
            "발신처": "토스페이먼츠",
            "승인번호": str(parsed.get("approval_no") or ""),
            "카드사": str(parsed.get("card_issuer") or ""),
            "결제수단": str(parsed.get("payment_method") or "신용카드"),
            "주문번호": str(parsed.get("order_no") or ""),
            "영수증URL": str(parsed.get("receipt_url") or ""),
            "구매자힌트": str(parsed.get("buyer_greeting") or ""),
            "사업자번호": "",
            "품목내역": [{"item": prod, "qty": 1, "price": amt}],
        }
    ]


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
        "table_biz_nos": [],
    }
    priority_amt_keys = ("총결제금액", "총 결제금액", "최종결제금액", "결제금액", "승인금액", "실결제금액")

    def _record_kv(k: str, v: str, *, from_html_table: bool = False) -> None:
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
        elif from_html_table and _match_kv_label(k, _KV_BIZ_NO_KEYS):
            found["table_biz_nos"].append(v)

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
                    _record_kv(clean_cells[idx], clean_cells[idx + 1], from_html_table=True)

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

    # 1b. Dedicated Toss Payments Email parser (separating actual seller from PG footer)
    toss_email_orders = _extract_toss_payments_email_orders(
        subject=subject,
        sender=sender,
        date_str=date_str,
        body_text=body_text,
        body_html=body_html,
    )
    if toss_email_orders:
        return toss_email_orders

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

    # Extract supplier biz_reg_no ONLY for verified direct sellers (never PG/intermediary footers)
    kv_biz_no = ""
    is_pg_sender = bool(_PG_OR_INTERMEDIARY_NAME_RE.search(f"{sender} {portal_hint}"))
    for raw_b in kv["table_biz_nos"]:
        cand_b = _clean_valid_biz_no(raw_b)
        if (
            cand_b
            and cand_b not in _INTERMEDIARY_OR_PG_BIZ_NOS
            and (not kv_shop or not _PG_OR_INTERMEDIARY_NAME_RE.search(kv_shop))
            and not (is_pg_sender and kv_shop and _PLATFORM_SHOP_IN_PG_RE.search(kv_shop))
        ):
            kv_biz_no = cand_b
            break

    if not kv_biz_no and portal_hint == "쿠팡" and (
        "로켓와우클럽 월회비" in combined_text
        or "와우 멤버십 월회비" in subject
        or kv_prod == "와우 멤버십"
    ):
        m_wow_biz = re.search(
            r"사업자등록번호\s*[:：]?\s*(\d{3}\s*-\s*\d{2}\s*-\s*\d{5})", combined_text
        )
        kv_biz_no = _clean_valid_biz_no(m_wow_biz.group(1)) if m_wow_biz else "1208800767"
        if not kv_biz_no:
            kv_biz_no = "1208800767"

    if (
        not kv_biz_no
        and "통신판매중개자" not in combined_text
        and not _PG_OR_INTERMEDIARY_NAME_RE.search(f"{sender} {portal_hint}")
    ):
        for m_fb in re.finditer(
            r"사업자(?:등록)?번호\s*[:：]?\s*(\d{3}\s*-\s*\d{2}\s*-\s*\d{5})", combined_text
        ):
            ctx_win = combined_text[
                max(0, m_fb.start() - 80) : min(len(combined_text), m_fb.end() + 40)
            ]
            if _PG_OR_INTERMEDIARY_NAME_RE.search(ctx_win):
                continue
            cand_b = _clean_valid_biz_no(m_fb.group(1))
            if cand_b and cand_b not in _INTERMEDIARY_OR_PG_BIZ_NOS:
                kv_biz_no = cand_b
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
                "사업자번호": kv_biz_no,
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
        if kv_biz_no and not o.get("사업자번호") and not o.get("입점판매자"):
            o["사업자번호"] = kv_biz_no
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

            for meta_key in (
                "승인번호",
                "카드사",
                "결제수단",
                "상점명",
                "발신처",
                "입점판매자",
                "사업자번호",
            ):
                if meta_key == "사업자번호" and (
                    target_cluster.get("입점판매자") or cand.get("입점판매자")
                ):
                    target_cluster["사업자번호"] = ""
                    continue
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


def _extract_fetch_msg_token(
    fetch_list: List[Any],
    idx: int,
    use_uid: bool,
    default_tok: bytes,
) -> bytes:
    """Extract UID (when use_uid=True) or sequence number from an IMAP FETCH response tuple."""
    item = fetch_list[idx]
    meta_line = item[0] if (isinstance(item, tuple) and isinstance(item[0], bytes)) else b""
    if use_uid:
        m_u = re.search(rb"\bUID\s+(\d+)\b", meta_line, flags=re.I)
        if m_u:
            return m_u.group(1)
        if idx + 1 < len(fetch_list) and isinstance(fetch_list[idx + 1], bytes):
            m_u2 = re.search(rb"\bUID\s+(\d+)\b", fetch_list[idx + 1], flags=re.I)
            if m_u2:
                return m_u2.group(1)
    m_seq = re.match(rb"^(\d+)\b", meta_line.strip())
    return m_seq.group(1) if m_seq else default_tok


def sync_imap_order_emails(
    cursor: sqlite3.Cursor,
    archive_dir: Optional[Path] = None,
    max_messages: int = 0,
    force_rescan: bool = False,
    account_id: Optional[str] = None,
    recent_rescan_days: int = 0,
    lookback_days: Optional[int] = None,
    include_toss_scroll: bool = True,
    verify_toss_slips: bool = True,
) -> Dict[str, Any]:
    """Paginate-scroll enabled IMAP accounts across lookback_days (default 365d), enrich Toss slips, and reconcile."""
    cfg = load_email_watcher_config(archive_dir)
    accounts: List[Dict[str, Any]] = list(cfg.get("accounts") or [])
    raw_lb = lookback_days if lookback_days is not None else (cfg.get("lookback_days") or 365)
    eff_lookback_days = max(1, min(1095, int(raw_lb)))
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
            "lookback_days": eff_lookback_days,
            "scrolled_pages": 0,
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

    since_ymd = (datetime.now() - timedelta(days=eff_lookback_days)).strftime("%Y-%m-%d")
    since_dt = _format_imap_date_token(since_ymd, 0)
    recent_since_dt = (
        _format_imap_date_token(
            (datetime.now() - timedelta(days=recent_rescan_days)).strftime("%Y-%m-%d"),
            0,
        )
        if recent_rescan_days > 0 and not force_rescan
        else ""
    )
    total_scanned_emails = 0
    total_scrolled_pages = 0
    all_orders: List[Dict[str, Any]] = []
    raw_toss_records: List[Dict[str, Any]] = []
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
        acc_pages = 0
        acc_orders: List[Dict[str, Any]] = []
        acc_toss_records: List[Dict[str, Any]] = []
        acc_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            imap = imaplib.IMAP4_SSL(host, port, timeout=30.0)
            try:
                imap.login(user, password)
                use_uid = hasattr(imap, "uid")
                mailboxes = _discover_imap_scroll_mailboxes(imap, mailbox)
                seen_acc_msg_keys: set[str] = set()

                for mbox in mailboxes:
                    sel_name = f'"{mbox}"' if (" " in mbox and not mbox.startswith('"')) else mbox
                    typ_sel, _ = imap.select(sel_name, readonly=True)
                    if typ_sel != "OK":
                        continue

                    if use_uid:
                        typ, data = imap.uid("SEARCH", None, "SINCE", since_dt)
                    else:
                        typ, data = imap.search(None, f"(SINCE {since_dt})")
                    if typ != "OK" or not data or not data[0]:
                        msg_nums: List[bytes] = []
                    else:
                        msg_nums = data[0].split()
                    if not msg_nums:
                        continue

                    always_rescan_nums: set[bytes] = set()
                    if recent_since_dt:
                        if use_uid:
                            typ_r, data_r = imap.uid("SEARCH", None, "SINCE", recent_since_dt)
                        else:
                            typ_r, data_r = imap.search(None, f"(SINCE {recent_since_dt})")
                        if typ_r == "OK" and data_r and data_r[0]:
                            always_rescan_nums = set(data_r[0].split())

                    recent_nums = (
                        list(reversed(msg_nums[-max_messages:]))
                        if max_messages and max_messages > 0
                        else list(reversed(msg_nums))
                    )
                    acc_checked_count += len(recent_nums)
                    key_prefix = f"{user}:{mbox}:uid:" if use_uid else f"{user}:{mbox}:"
                    pending_nums: List[bytes] = []
                    for num in recent_nums:
                        uid_key = f"{key_prefix}{num.decode('ascii', errors='ignore')}"
                        if (
                            not force_rescan
                            and num not in always_rescan_nums
                            and uid_key in processed_ids
                        ):
                            continue
                        pending_nums.append(num)

                    # Paginate header fetches in chunks of 200 across the entire lookback period
                    candidate_bodies: List[tuple[bytes, str, str, bool]] = []
                    for b_start in range(0, len(pending_nums), 200):
                        chunk = pending_nums[b_start : b_start + 200]
                        seq_set = b",".join(chunk)
                        hdr_query = "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])"
                        acc_pages += 1
                        total_scrolled_pages += 1
                        if use_uid:
                            typ_h, hdr_data = imap.uid("FETCH", seq_set, hdr_query)
                        else:
                            typ_h, hdr_data = imap.fetch(seq_set, hdr_query)
                        if typ_h != "OK" or not hdr_data:
                            continue
                        for idx, item in enumerate(hdr_data):
                            if not (
                                isinstance(item, tuple)
                                and len(item) >= 2
                                and isinstance(item[1], bytes)
                            ):
                                continue
                            num_b = _extract_fetch_msg_token(hdr_data, idx, use_uid, chunk[0])
                            uid_key = f"{key_prefix}{num_b.decode('ascii', errors='ignore')}"
                            hdr_msg = email.message_from_bytes(item[1])
                            subj = _decode_mime_header(hdr_msg.get("Subject"))
                            sender = _decode_mime_header(hdr_msg.get("From"))
                            raw_mid = str(hdr_msg.get("Message-ID") or "").strip()
                            msg_id = f"{user}:{raw_mid}" if raw_mid else uid_key

                            if msg_id in seen_acc_msg_keys:
                                new_processed.append(uid_key)
                                continue

                            is_always = force_rescan or (num_b in always_rescan_nums)
                            if not is_always and msg_id in processed_ids:
                                seen_acc_msg_keys.add(msg_id)
                                new_processed.append(uid_key)
                                continue

                            if include_toss_scroll and is_toss_payments_email(sender, subj):
                                seen_acc_msg_keys.add(msg_id)
                                candidate_bodies.append((num_b, msg_id, uid_key, False))
                                continue
                            if _CANCELED_EMAIL_SUBJECT_RE.search(subj):
                                seen_acc_msg_keys.add(msg_id)
                                candidate_bodies.append((num_b, msg_id, uid_key, True))
                                continue
                            if _IGNORE_EMAIL_SUBJECT_RE.search(subj) or not _ORDER_EMAIL_SUBJECT_RE.search(
                                f"{subj} {sender}"
                            ):
                                seen_acc_msg_keys.add(msg_id)
                                new_processed.append(msg_id)
                                new_processed.append(uid_key)
                                continue
                            seen_acc_msg_keys.add(msg_id)
                            candidate_bodies.append((num_b, msg_id, uid_key, False))

                    # Paginate full-body fetches in chunks of 25
                    for b_start in range(0, len(candidate_bodies), 25):
                        chunk_cands = candidate_bodies[b_start : b_start + 25]
                        by_seq = {num_b: (m_id, u_id, is_c) for num_b, m_id, u_id, is_c in chunk_cands}
                        by_mid = {m_id: (m_id, u_id, is_c) for _, m_id, u_id, is_c in chunk_cands}
                        seq_set = b",".join(num_b for num_b, _, _, _ in chunk_cands)
                        acc_pages += 1
                        total_scrolled_pages += 1
                        if use_uid:
                            typ_b, body_data = imap.uid("FETCH", seq_set, "(BODY.PEEK[])")
                        else:
                            typ_b, body_data = imap.fetch(seq_set, "(BODY.PEEK[])")
                        if typ_b != "OK" or not body_data:
                            continue
                        for idx, item in enumerate(body_data):
                            if not (
                                isinstance(item, tuple)
                                and len(item) >= 2
                                and isinstance(item[1], bytes)
                            ):
                                continue
                            seq_k = _extract_fetch_msg_token(body_data, idx, use_uid, chunk_cands[0][0])
                            raw_eml = item[1]
                            if not raw_eml:
                                continue

                            parts = _extract_eml_parts(raw_eml)
                            cand_meta = by_seq.get(seq_k)
                            if cand_meta is None and parts["message_id"]:
                                cand_meta = by_mid.get(f"{user}:{parts['message_id']}")

                            if include_toss_scroll and is_toss_payments_email(
                                parts["sender"],
                                parts["subject"],
                                parts["body_html"],
                                parts["body_text"],
                            ):
                                parsed_toss = parse_toss_payments_email(
                                    subject=parts["subject"],
                                    sender=parts["sender"],
                                    date_str=parts["date_str"],
                                    body_text=parts["body_text"],
                                    body_html=parts["body_html"],
                                )
                                if parsed_toss:
                                    parsed_toss["recipient_email"] = user
                                    acc_toss_records.append(parsed_toss)
                                    if parsed_toss.get("event_type") == "PAYMENT":
                                        acc_scanned += 1
                            else:
                                is_cancel_msg = cand_meta[2] if cand_meta else False
                                if is_cancel_msg:
                                    canceled_apps.update(_extract_cancel_approval_nos_from_eml(raw_eml))
                                else:
                                    acc_scanned += 1
                                    extracted = parse_raw_email_bytes_or_text(raw_eml)
                                    acc_orders.extend(extracted)

                            if cand_meta is not None:
                                m_id, u_id, _ = cand_meta
                                new_processed.append(m_id)
                                new_processed.append(u_id)
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

            acc_merged = _merge_companion_email_orders(acc_orders)
            acc_toss_pay_cnt = sum(1 for r in acc_toss_records if r.get("event_type") == "PAYMENT")
            acc_total_parsed = len(acc_merged) + acc_toss_pay_cnt
            success_accounts += 1
            total_scanned_emails += acc_scanned
            all_orders.extend(acc_orders)
            raw_toss_records.extend(acc_toss_records)
            acc["last_sync_at"] = acc_now
            acc["last_status"] = "synced"
            toss_note = f" (토스 {len(acc_toss_records)}통 포함)" if acc_toss_records else ""
            acc["last_message"] = (
                f"최근 {eff_lookback_days}일 메일함 {acc_checked_count}통 스크롤 · "
                f"주문메일 {acc_scanned}통 분석{toss_note} · 주문 {acc_total_parsed}건 추출"
            )
            account_results.append(
                {
                    "account_id": aid,
                    "email_address": user,
                    "status": "synced",
                    "scrolled_pages": acc_pages,
                    "checked_mailbox_count": acc_checked_count,
                    "scanned_emails": acc_scanned,
                    "parsed_orders_count": acc_total_parsed,
                    "parsed_toss_count": len(acc_toss_records),
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
                    "scrolled_pages": acc_pages,
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
            "lookback_days": eff_lookback_days,
            "scrolled_pages": total_scrolled_pages,
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

    # 1. Precision-process Toss Payments records first (SalesSlip verification + corp/personal card separation)
    toss_summary: Dict[str, Any] = {
        "parsed_toss_count": 0,
        "verified_slips_count": 0,
        "corp_total_count": 0,
        "corp_matched_existing_count": 0,
        "corp_created_count": 0,
        "skipped_personal_count": 0,
        "skipped_canceled_count": 0,
        "created_tx_ids": [],
        "newly_stated_tx_ids": [],
        "updated_tx_ids": [],
        "items": [],
    }
    if raw_toss_records:
        toss_summary = _process_toss_records_batch(
            cursor,
            raw_toss_records,
            verify_receipt_slip=verify_toss_slips,
            auto_create_corp_tx=True,
            archive_dir=archive_dir,
        )

    # 2. Reconcile general e-commerce & PG order emails (Coupang, NaverPay, Inicis, KCP, EasyPay, etc.)
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

    # 3. Combine unique reconciled transaction counts across Toss + general e-commerce pipelines
    combined_updated_ids = list(
        dict.fromkeys(
            [*(toss_summary.get("updated_tx_ids") or []), *(rec_res.get("updated_tx_ids") or [])]
        )
    )
    combined_created_ids = list(
        dict.fromkeys(
            [*(toss_summary.get("created_tx_ids") or []), *(rec_res.get("created_tx_ids") or [])]
        )
    )
    combined_newly_stated_ids = list(
        dict.fromkeys(
            [
                *(toss_summary.get("newly_stated_tx_ids") or []),
                *(rec_res.get("newly_stated_tx_ids") or []),
            ]
        )
    )
    matched = len(combined_updated_ids)
    created_cnt = len(combined_created_ids)
    newly_stated_cnt = len(combined_newly_stated_ids)
    total_parsed_orders = len(merged_orders) + sum(
        1 for r in raw_toss_records if r.get("event_type") == "PAYMENT"
    )

    matched_items = _merge_recent_matched_items(
        toss_summary.get("items") or [],
        rec_res.get("matched_items") or [],
    )
    merged_history = _merge_recent_matched_items(
        cfg.get("recent_matched_items") or [],
        matched_items,
    )

    if created_cnt > 0:
        summary_msg = (
            f"최근 {eff_lookback_days}일 통합 스크롤 (계정 {success_accounts}/{len(valid_targets)}개) · "
            f"주문메일 {total_scanned_emails}통 분석 · 주문 {total_parsed_orders}건 확인 · "
            f"전표 {matched}건 반영 (신규 전표 {created_cnt}건 자동생성)"
        )
    else:
        summary_msg = (
            f"최근 {eff_lookback_days}일 통합 스크롤 (계정 {success_accounts}/{len(valid_targets)}개) · "
            f"주문메일 {total_scanned_emails}통 분석 · 주문 {total_parsed_orders}건 확인 · "
            f"전표 {matched}건 거래명세서 반영 완료"
        )
    if raw_toss_records:
        summary_msg += (
            f" [토스 법인카드 {toss_summary['corp_total_count']}건 반영 · "
            f"개인카드 {toss_summary['skipped_personal_count']}건 제외]"
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
        "lookback_days": eff_lookback_days,
        "scrolled_pages": total_scrolled_pages,
        "scanned_accounts": success_accounts,
        "scanned_emails": total_scanned_emails,
        "parsed_orders_count": total_parsed_orders,
        "matched_count": matched,
        "created_count": created_cnt,
        "newly_stated_count": newly_stated_cnt,
        "created_tx_ids": combined_created_ids,
        "newly_stated_tx_ids": combined_newly_stated_ids,
        "updated_tx_ids": combined_updated_ids,
        "matched_items": matched_items,
        "generated_statements_count": matched,
        "toss_scroll_summary": {
            "parsed_toss_count": toss_summary["parsed_toss_count"],
            "verified_slips_count": toss_summary["verified_slips_count"],
            "corp_total_count": toss_summary["corp_total_count"],
            "corp_matched_existing_count": toss_summary["corp_matched_existing_count"],
            "corp_created_count": toss_summary["corp_created_count"],
            "skipped_personal_count": toss_summary["skipped_personal_count"],
            "skipped_canceled_count": toss_summary["skipped_canceled_count"],
        },
        "account_results": account_results,
    }


def _extract_card_tail4_and_bin6(card_number_masked: str) -> tuple[str, str]:
    """Extract 4-char tail (e.g. '607*' or '1175') and 6-digit BIN from a masked card number."""
    compact = re.sub(r"[\s\-]+", "", str(card_number_masked or "").strip())
    if len(compact) < 4:
        return "", ""
    tail4 = compact[-4:]
    bin_digits = re.sub(r"\D", "", compact[:8])
    bin6 = bin_digits[:6] if len(bin_digits) >= 6 else ""
    return tail4, bin6


def _infer_corp_issuer_code(card_name: str) -> str:
    txt = str(card_name or "").strip()
    if "우리" in txt:
        return "WOORI"
    if "국민" in txt or "KB" in txt.upper():
        return "KB"
    if "신한" in txt:
        return "SHINHAN"
    if "하나" in txt:
        return "HANA"
    if "삼성" in txt:
        return "SAMSUNG"
    if "현대" in txt:
        return "HYUNDAI"
    if "롯데" in txt:
        return "LOTTE"
    if "농협" in txt or "NH" in txt.upper():
        return "NH"
    if "기업" in txt or "IBK" in txt.upper():
        return "IBK"
    if "비씨" in txt or "BC" in txt.upper():
        return "BC"
    return "CORP"


def fetch_toss_sales_slip_metadata(
    receipt_url: str,
    recipient_email: str = "",
    approval_no: str = "",
    approved_date: str = "",
    amount: int = 0,
    timeout: float = 8.0,
) -> Optional[Dict[str, Any]]:
    """Resolve a Toss Payments email receipt link into official SalesSlip JSON metadata."""
    clean_url = html.unescape(str(receipt_url or "").strip())
    if not clean_url or "tosspayments.com" not in clean_url.lower():
        return None

    parsed_u = urllib.parse.urlparse(clean_url)
    qs = urllib.parse.parse_qs(parsed_u.query)
    raw_tid = (qs.get("transactionId") or [""])[0]
    if not raw_tid:
        return None

    slip_tid = ""
    sales_slip_url = clean_url
    try:
        if "/receipt/sales-slip" in parsed_u.path:
            slip_tid = raw_tid
        else:
            redir_api = (
                "https://homepage-api-gateway.tosspayments.com"
                "/v3/dashboard/receipts/open/redirection-url?"
                + urllib.parse.urlencode({"transactionId": raw_tid, "ref": "email"})
            )
            req_r = urllib.request.Request(
                redir_api,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "X-TossPayments-Referrer": json.dumps({"url": clean_url, "referrer": ""}),
                },
            )
            with urllib.request.urlopen(req_r, timeout=timeout) as resp_r:
                j_r = json.loads(resp_r.read().decode("utf-8", errors="replace"))
            next_url = str(((j_r.get("data") or {}).get("url")) or "").strip()
            if not next_url or "sales-slip" not in next_url:
                return None
            sales_slip_url = next_url
            qs2 = urllib.parse.parse_qs(urllib.parse.urlparse(next_url).query)
            slip_tid = (qs2.get("transactionId") or [""])[0]
    except Exception:
        return None

    if not slip_tid:
        return None

    candidate_bodies: List[Dict[str, Any]] = []
    if recipient_email:
        candidate_bodies.append(
            {
                "transactionId": slip_tid,
                "method": "EMAIL",
                "email": recipient_email.strip(),
                "lang": "ko",
            }
        )
    if approval_no and len(approved_date) >= 10 and amount > 0:
        candidate_bodies.append(
            {
                "transactionId": slip_tid,
                "method": "AUTH_NUMBER_AND_DATE_AND_AMOUNT",
                "authNumber": approval_no.strip(),
                "approvedDate": approved_date[:10],
                "amount": int(amount),
                "lang": "ko",
            }
        )
    candidate_bodies.append({"transactionId": slip_tid, "lang": "ko"})

    slip_data: Optional[Dict[str, Any]] = None
    slip_api = "https://homepage-api-gateway.tosspayments.com/v1/homepage/receipts/sales-slip"
    ref_hdr = json.dumps({"url": sales_slip_url, "referrer": ""})

    for payload_obj in candidate_bodies:
        try:
            req_s = urllib.request.Request(
                slip_api,
                data=json.dumps(payload_obj).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                    "X-TossPayments-Referrer": ref_hdr,
                },
            )
            with urllib.request.urlopen(req_s, timeout=timeout) as resp_s:
                j_s = json.loads(resp_s.read().decode("utf-8", errors="replace"))
            if j_s.get("result") == "SUCCESS" and isinstance(j_s.get("data"), dict):
                slip_data = j_s["data"]
                break
        except Exception:
            continue

    if not slip_data:
        return None

    paid_obj = slip_data.get("paid") or {}
    buyer_obj = slip_data.get("buyer") or {}
    summary_flags = slip_data.get("summary") or {}
    amt_summary = (slip_data.get("amount") or {}).get("summary") or {}
    main_merch = (slip_data.get("merchants") or {}).get("main") or {}

    card_num = str(paid_obj.get("cardNumber") or "").strip()
    tail4, bin6 = _extract_card_tail4_and_bin6(card_num)
    raw_biz = re.sub(r"\D", "", str(main_merch.get("businessNumber") or ""))
    clean_biz = (
        raw_biz
        if (len(raw_biz) == 10 and validate_biz_reg_no_checksum(raw_biz) and raw_biz != "4118601799")
        else ""
    )

    billed_amt = int(round(float(amt_summary.get("billed") or 0)))
    paid_amt = int(round(float(amt_summary.get("paid") or 0)))
    taxable_amt = int(round(float(amt_summary.get("taxable") or 0)))
    tax_free_amt = int(
        round(float(amt_summary.get("taxFree") or 0) + float(amt_summary.get("taxExemption") or 0))
    )
    vat_amt = int(round(float(amt_summary.get("vat") or 0)))
    supply_val = taxable_amt + tax_free_amt
    if billed_amt > 0 and supply_val + vat_amt != billed_amt:
        supply_val = max(0, billed_amt - vat_amt)

    paid_at_raw = str(paid_obj.get("paidAt") or "").replace("T", " ").strip()
    paid_at_norm = _normalize_datetime_str(paid_at_raw) if paid_at_raw else ""

    return {
        "card_name": str(paid_obj.get("cardName") or "").strip(),
        "card_number_masked": card_num,
        "card_tail4": tail4,
        "card_bin6": bin6,
        "installment": str(paid_obj.get("installment") or "").strip(),
        "slip_status": str(paid_obj.get("status") or "").strip(),
        "is_cancel": bool(summary_flags.get("isCancel")),
        "has_partial_cancel": bool(summary_flags.get("hasPartialCancel")),
        "auth_number": str(paid_obj.get("authNumber") or "").strip(),
        "paid_at": paid_at_norm[:19] if len(paid_at_norm) >= 19 else paid_at_norm,
        "order_id": str(buyer_obj.get("orderId") or "").strip(),
        "buyer_masked": str(buyer_obj.get("buyer") or "").strip(),
        "product_name": str(buyer_obj.get("product") or "").strip(),
        "paid_amount": paid_amt,
        "billed_amount": billed_amt,
        "supply_value": supply_val,
        "vat_amount": vat_amt,
        "tax_free_amount": tax_free_amt,
        "merchant_name": str(main_merch.get("companyName") or "").strip(),
        "biz_reg_no": clean_biz,
        "representative_name": str(main_merch.get("representativeName") or "").strip(),
        "merchant_address": str(main_merch.get("companyAddress") or "").strip(),
        "telephone_number": str(main_merch.get("telephoneNumber") or "").strip(),
        "sales_slip_url": sales_slip_url,
    }


def classify_toss_card_ownership(
    record: Dict[str, Any],
    confirmed_corp_card_numbers: Optional[set[str]] = None,
    existing_corp_approvals: Optional[set[str]] = None,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Determine whether a Toss Payments record belongs to a corporate card or personal card."""
    policy = load_corp_card_policy(archive_dir)
    corp_only = bool(policy.get("corp_only_mode", False))
    corp_cards = confirmed_corp_card_numbers or set()
    corp_apps = existing_corp_approvals or set()

    app_no = str(record.get("approval_no") or "").strip()
    card_num = str(record.get("card_number_masked") or "").strip()
    tail4, bin6 = _extract_card_tail4_and_bin6(card_num)
    card_name = str(record.get("card_name") or record.get("card_issuer") or "").strip()
    issuer_code = _infer_corp_issuer_code(card_name)
    buyer_txt = f"{record.get('buyer_greeting') or ''} {record.get('item_name') or ''}"

    # 1. Match against registered corporate card list (exact or 3-digit + '*' wildcard)
    matched_reg = match_registered_corp_card_code(tail4, archive_dir=archive_dir) if tail4 else None
    if matched_reg:
        return {
            "is_corporate": True,
            "reason": "registered_corp_card",
            "card_last4": matched_reg,
            "issuer_code": issuer_code,
            "should_auto_register": False,
        }

    # 2. Match against existing ledger approval_no
    if app_no and app_no in corp_apps:
        return {
            "is_corporate": True,
            "reason": "existing_ledger_approval_match",
            "card_last4": tail4 or "0000",
            "issuer_code": issuer_code,
            "should_auto_register": False,
        }

    # 3. Block known personal card codes or personal-only card BINs
    if tail4 and is_blocked_personal_card_code(tail4, archive_dir=archive_dir):
        return {
            "is_corporate": False,
            "reason": "blocked_personal_card",
            "card_last4": tail4,
            "issuer_code": issuer_code,
            "should_auto_register": False,
        }
    if bin6 and any(bin6.startswith(p) for p in _TOSS_PERSONAL_CARD_BIN6_PREFIXES):
        return {
            "is_corporate": False,
            "reason": "personal_card_bin",
            "card_last4": tail4 or "0000",
            "issuer_code": issuer_code,
            "should_auto_register": False,
        }

    # 4. Corporate BIN or cohort-verified corporate card number or corporate buyer evidence
    has_corp_bin = bool(bin6 and any(bin6.startswith(p) for p in _TOSS_CORP_CARD_BIN6_PREFIXES))
    in_corp_cohort = bool(card_num and card_num in corp_cards)
    has_corp_buyer = bool(_CORP_BUYER_OR_ITEM_RE.search(buyer_txt))

    if has_corp_bin or in_corp_cohort or (has_corp_buyer and not bin6):
        resolved_c4 = tail4 if len(tail4) == 4 else "0000"
        return {
            "is_corporate": True,
            "reason": (
                "corporate_card_bin"
                if has_corp_bin
                else ("corporate_card_cohort" if in_corp_cohort else "corporate_buyer_match")
            ),
            "card_last4": resolved_c4,
            "issuer_code": issuer_code,
            "should_auto_register": bool(resolved_c4 != "0000"),
        }

    if not corp_only:
        return {
            "is_corporate": True,
            "reason": "corp_only_disabled",
            "card_last4": tail4 if len(tail4) == 4 else "0000",
            "issuer_code": issuer_code,
            "should_auto_register": False,
        }

    return {
        "is_corporate": False,
        "reason": "unverified_or_personal_card",
        "card_last4": tail4 or "0000",
        "issuer_code": issuer_code,
        "should_auto_register": False,
    }


def _extract_eml_parts(raw_eml: bytes) -> Dict[str, str]:
    """Decode RFC822 bytes into subject, sender, date_str, message_id, body_text, and body_html."""
    msg = email.message_from_bytes(raw_eml)
    subj = _decode_mime_header(msg.get("Subject"))
    sender = _decode_mime_header(msg.get("From"))
    dt_hdr = str(msg.get("Date") or "")
    msg_id = str(msg.get("Message-ID") or "").strip()

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

    return {
        "subject": subj,
        "sender": sender,
        "date_str": dt_hdr,
        "message_id": msg_id,
        "body_text": "\n".join(text_parts),
        "body_html": "\n".join(html_parts),
    }


def _discover_imap_scroll_mailboxes(imap: Any, configured_mailbox: str = "INBOX") -> List[str]:
    """Return ordered list of mailboxes to scroll (configured INBOX + any \\All archive folder)."""
    primary = (configured_mailbox or "INBOX").strip() or "INBOX"
    boxes: List[str] = [primary]
    try:
        typ, raw_list = imap.list()
        if typ == "OK" and raw_list:
            for entry in raw_list:
                if not isinstance(entry, bytes):
                    continue
                line = entry.decode("utf-8", errors="replace")
                if "\\All" in line or "[Gmail]/All Mail" in line:
                    m_q = re.search(r'"([^"]+)"\s*$', line)
                    cand = m_q.group(1).strip() if m_q else line.rsplit(" ", 1)[-1].strip()
                    if cand and cand not in boxes:
                        # Put All Mail first when available so archived messages are scanned in one pass
                        boxes.insert(0, cand)
    except Exception:
        pass
    return boxes


def _ensure_toss_merchant_master(
    cursor: sqlite3.Cursor,
    biz_reg_no: str,
    merchant_name: str,
) -> None:
    clean_no = re.sub(r"\D", "", str(biz_reg_no or ""))
    if len(clean_no) != 10 or not merchant_name:
        return
    cursor.execute(
        """
        INSERT INTO merchants (biz_reg_no, merchant_name, tax_type_code, biz_status)
        VALUES (?, ?, 'GENERAL', 'ACTIVE')
        ON CONFLICT(biz_reg_no) DO UPDATE SET
            merchant_name = CASE
                WHEN merchants.merchant_name = '' THEN excluded.merchant_name
                ELSE merchants.merchant_name
            END
        """,
        (clean_no, merchant_name[:100]),
    )


def _format_imap_date_token(ymd: str, add_days: int = 0) -> str:
    """Convert YYYY-MM-DD into RFC3501 DD-Mon-YYYY string (English month names)."""
    norm = _normalize_datetime_str(ymd)[:10]
    dt = datetime.strptime(norm, "%Y-%m-%d") + timedelta(days=add_days)
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{dt.day:02d}-{months[dt.month - 1]}-{dt.year:04d}"


def _reconcile_or_create_corp_toss_record(
    cursor: sqlite3.Cursor,
    record: Dict[str, Any],
    ownership: Dict[str, Any],
    used_tx_ids: set[int],
    auto_create_corp_tx: bool = True,
    archive_dir: Optional[Path] = None,
    run_ledger_slip_gen: bool = True,
) -> Optional[Dict[str, Any]]:
    """Reconcile a verified corporate Toss Payments record with card_transactions and build slips."""
    amount = int(record.get("billed_amount") or record.get("effective_amount") or record.get("paid_amount") or 0)
    full_dt = str(record.get("paid_at") or record.get("transacted_at") or "").strip()
    if amount <= 0 or len(full_dt) < 10:
        return None

    date_prefix = full_dt[:10]
    full_ts = full_dt[:19] if len(full_dt) >= 19 else f"{date_prefix} 12:00:00"
    app_no = str(record.get("approval_no") or "").strip()
    merch_name = str(record.get("merchant_name") or "토스페이먼츠 가맹점").strip()
    clean_biz = re.sub(r"\D", "", str(record.get("biz_reg_no") or ""))
    if len(clean_biz) != 10:
        clean_biz = ""
    item_name = str(record.get("item_name") or "").strip() or f"{merch_name} 결제 품목"
    resolved_c4 = str(ownership.get("card_last4") or "0000").strip() or "0000"

    if clean_biz:
        _ensure_toss_merchant_master(cursor, clean_biz, merch_name)

    policy = load_corp_card_policy(archive_dir)
    if (
        ownership.get("should_auto_register")
        and resolved_c4 != "0000"
        and policy.get("auto_register_on_statement_upload", True)
    ):
        issuer_code = str(ownership.get("issuer_code") or "WOORI")
        register_corp_card(
            card_last4=resolved_c4,
            issuer=issuer_code,
            archive_dir=archive_dir,
        )

    # 1. Search existing card_transactions by approval_no first, then (amount, date ±3d)
    tx_row = None
    if app_no:
        cursor.execute(
            """
            SELECT * FROM card_transactions
            WHERE approval_no = ? AND reconcile_status != 'CANCELED'
            ORDER BY CASE WHEN ABS(total_amount) = ? THEN 0 ELSE 1 END ASC, tx_id DESC
            """,
            (app_no, amount),
        )
        for cand in cursor.fetchall():
            if int(cand["tx_id"]) not in used_tx_ids:
                tx_row = cand
                break

    if tx_row is None:
        cursor.execute(
            """
            SELECT * FROM card_transactions
            WHERE ABS(total_amount) = ?
              AND ABS(julianday(SUBSTR(transacted_at, 1, 10)) - julianday(?)) <= 3.0
              AND reconcile_status != 'CANCELED'
            ORDER BY ABS(julianday(SUBSTR(transacted_at, 1, 10)) - julianday(?)) ASC, tx_id DESC
            """,
            (amount, date_prefix, date_prefix),
        )
        for cand in cursor.fetchall():
            cid = int(cand["tx_id"])
            if cid in used_tx_ids:
                continue
            cand_merch = str(cand["raw_merchant_name"] or "").lower()
            cand_c4 = str(cand["card_last4"] or "")
            is_pg_or_toss = "토스" in cand_merch or str(cand["biz_reg_no"] or "") == "4118601799"
            is_merch_match = bool(
                merch_name
                and (
                    merch_name.lower() in cand_merch
                    or cand_merch in merch_name.lower()
                )
            )
            is_card_match = bool(resolved_c4 != "0000" and cand_c4 == resolved_c4)
            if is_pg_or_toss or is_merch_match or is_card_match:
                tx_row = cand
                break

    action = "MATCHED"
    mp_seller = ""
    biz_changed = False
    if tx_row is None:
        if not auto_create_corp_tx:
            return None
        rule_eval = evaluate_tax_rules(cursor, f"{merch_name} {item_name}", clean_biz or None)
        vat_type = str(rule_eval["vat_deduction_type"])
        slip_vat = record.get("vat_amount")
        slip_tax_free = int(record.get("tax_free_amount") or 0)
        if slip_vat is not None and int(slip_vat) == 0 and slip_tax_free > 0:
            vat_type = "EXEMPT"
        sup_val, vat_val = calculate_supply_and_vat(amount, vat_type)
        if (
            slip_vat is not None
            and record.get("supply_value") is not None
            and int(record["supply_value"]) + int(slip_vat) == amount
            and vat_type == "DEDUCTIBLE"
        ):
            sup_val = int(record["supply_value"])
            vat_val = int(slip_vat)

        cursor.execute(
            """
            INSERT INTO card_transactions (
                card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
                total_amount, supply_value, vat_amount,
                account_code, account_name, vat_deduction_type,
                non_deduct_reason, is_vehicle_expense, counterparty_info, reconcile_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OCR_MATCHED')
            """,
            (
                resolved_c4,
                app_no or None,
                full_ts,
                merch_name[:100],
                clean_biz or None,
                amount,
                sup_val,
                vat_val,
                rule_eval["account_code"],
                rule_eval["account_name"],
                vat_type,
                rule_eval["non_deduct_reason"],
                1 if rule_eval["is_vehicle_expense"] else 0,
                f"[토스페이먼츠] {item_name}"[:200],
            ),
        )
        new_tx_id = int(cursor.lastrowid)
        cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (new_tx_id,))
        tx_row = cursor.fetchone()
        action = "CREATED"
    else:
        ex_id = int(tx_row["tx_id"])
        ex_biz = str(tx_row["biz_reg_no"] or "").strip()
        ex_merch = str(tx_row["raw_merchant_name"] or "")
        ex_c4 = str(tx_row["card_last4"] or "0000")
        next_c4 = resolved_c4 if (ex_c4 == "0000" and resolved_c4 != "0000") else ex_c4
        next_biz = ex_biz
        if clean_biz and (not ex_biz or ex_biz == "4118601799"):
            next_biz = clean_biz
        biz_changed = next_biz != ex_biz
        if "토스" in ex_merch and merch_name and "토스" not in merch_name:
            mp_seller = (
                f"{merch_name} ({format_biz_reg_no_display(clean_biz)})"
                if clean_biz
                else merch_name
            )
        cursor.execute(
            """
            UPDATE card_transactions
            SET approval_no = COALESCE(approval_no, ?),
                card_last4 = ?,
                biz_reg_no = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE tx_id = ?
            """,
            (app_no or None, next_c4, next_biz or None, ex_id),
        )
        cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (ex_id,))
        tx_row = cursor.fetchone()

    if tx_row is None:
        return None

    tx_id = int(tx_row["tx_id"])
    used_tx_ids.add(tx_id)
    tx_merch_now = str(tx_row["raw_merchant_name"] or "")
    memo_item = (
        f"[{merch_name}] {item_name}"
        if ("토스" in tx_merch_now and merch_name and merch_name not in item_name)
        else (f"[토스페이먼츠] {item_name}" if "토스페이먼츠" not in item_name else item_name)
    )

    # Preserve multi-item statement if already populated by a companion shopping-mall email
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

    target_line_items: List[Dict[str, Any]] = [
        {"item": item_name, "qty": 1, "price": amount, "seller": merch_name}
    ]
    target_memo = memo_item[:200]
    if prev_stmt and prev_parsed_lines and len(prev_parsed_lines) >= 2:
        target_line_items = prev_parsed_lines
        if prev_memo:
            target_memo = prev_memo[:200]

    is_newly_stated = bool(
        action == "CREATED"
        or prev_stmt is None
        or prev_memo != target_memo
        or (mp_seller and mp_seller != prev_mp_seller)
        or biz_changed
    )

    stmt_res = generate_electronic_statement_for_tx(
        cursor,
        tx_id=tx_id,
        item_name=target_memo,
        line_items=target_line_items,
        marketplace_seller=(mp_seller or prev_mp_seller) or None,
        archive_dir=archive_dir,
    )

    # Preserve exact Toss SalesSlip supply/VAT breakdown when provided
    slip_vat_f = record.get("vat_amount")
    slip_sup_f = record.get("supply_value")
    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    latest_tx = dict(cursor.fetchone())
    if (
        slip_vat_f is not None
        and slip_sup_f is not None
        and int(slip_sup_f) + int(slip_vat_f) == amount
        and str(latest_tx.get("vat_deduction_type")) in ("DEDUCTIBLE", "EXEMPT")
    ):
        cursor.execute(
            """
            UPDATE card_transactions
            SET supply_value = ?, vat_amount = ?
            WHERE tx_id = ?
            """,
            (int(slip_sup_f), int(slip_vat_f), tx_id),
        )
        cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
        latest_tx = dict(cursor.fetchone())

    if run_ledger_slip_gen:
        generate_electronic_slips_for_ledger(cursor, archive_dir=archive_dir)
    final_biz = str(latest_tx.get("biz_reg_no") or "").strip()
    return {
        "tx_id": tx_id,
        "action": action,
        "is_created": action == "CREATED",
        "is_newly_stated": is_newly_stated,
        "transacted_at": str(latest_tx["transacted_at"]),
        "card_last4": str(latest_tx.get("card_last4") or "0000"),
        "card_label": format_card_label(str(latest_tx.get("card_last4") or "0000")),
        "card_number_masked": str(record.get("card_number_masked") or ""),
        "approval_no": str(latest_tx.get("approval_no") or app_no),
        "order_no": str(record.get("order_no") or ""),
        "raw_merchant_name": str(latest_tx["raw_merchant_name"]),
        "biz_reg_no": format_biz_reg_no_display(final_biz) if final_biz else "",
        "marketplace_seller": mp_seller or prev_mp_seller,
        "item_name": stmt_res["item_name"],
        "line_items_count": len(target_line_items),
        "total_amount": int(latest_tx["total_amount"]),
        "supply_value": int(latest_tx["supply_value"]),
        "vat_amount": int(latest_tx["vat_amount"]),
        "account_code": str(latest_tx["account_code"]),
        "account_name": str(latest_tx["account_name"]),
        "archived_file_name": stmt_res["archived_file_name"],
        "sales_slip_url": str(record.get("sales_slip_url") or record.get("receipt_url") or ""),
        "ownership_reason": str(ownership.get("reason") or ""),
    }


def _process_toss_records_batch(
    cursor: sqlite3.Cursor,
    raw_toss_records: List[Dict[str, Any]],
    *,
    norm_start: str = "",
    norm_end: str = "",
    verify_receipt_slip: bool = True,
    auto_create_corp_tx: bool = True,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Enrich Toss records via SalesSlip API, separate corp vs personal cards, and reconcile."""
    cursor.execute(
        """
        SELECT DISTINCT approval_no FROM card_transactions
        WHERE approval_no IS NOT NULL AND TRIM(approval_no) != ''
          AND reconcile_status != 'CANCELED'
        """
    )
    existing_corp_approvals = {str(r[0]).strip() for r in cursor.fetchall() if r[0]}

    verified_slips_cnt = 0
    slip_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    for rec in raw_toss_records:
        r_url = str(rec.get("receipt_url") or "").strip()
        if not (verify_receipt_slip and r_url):
            continue
        if r_url not in slip_cache:
            slip_cache[r_url] = fetch_toss_sales_slip_metadata(
                receipt_url=r_url,
                recipient_email=str(rec.get("recipient_email") or ""),
                approval_no=str(rec.get("approval_no") or ""),
                approved_date=str(rec.get("transacted_at") or "")[:10],
                amount=int(rec.get("paid_amount") or rec.get("effective_amount") or 0),
            )
        slip_meta = slip_cache[r_url]
        if slip_meta:
            verified_slips_cnt += 1
            rec["card_name"] = slip_meta.get("card_name") or rec.get("card_issuer")
            rec["card_number_masked"] = slip_meta.get("card_number_masked") or ""
            rec["slip_status"] = slip_meta.get("slip_status") or ""
            rec["is_cancel"] = bool(slip_meta.get("is_cancel"))
            rec["has_partial_cancel"] = bool(slip_meta.get("has_partial_cancel"))
            rec["billed_amount"] = slip_meta.get("billed_amount")
            rec["supply_value"] = slip_meta.get("supply_value")
            rec["vat_amount"] = slip_meta.get("vat_amount")
            rec["tax_free_amount"] = slip_meta.get("tax_free_amount")
            if slip_meta.get("merchant_name"):
                rec["merchant_name"] = slip_meta["merchant_name"]
            if slip_meta.get("biz_reg_no"):
                rec["biz_reg_no"] = slip_meta["biz_reg_no"]
            if slip_meta.get("auth_number") and not rec.get("approval_no"):
                rec["approval_no"] = slip_meta["auth_number"]
            if slip_meta.get("paid_at"):
                rec["paid_at"] = slip_meta["paid_at"]
            if slip_meta.get("order_id") and not rec.get("order_no"):
                rec["order_no"] = slip_meta["order_id"]
            if slip_meta.get("product_name") and not rec.get("item_name"):
                rec["item_name"] = slip_meta["product_name"]
            if slip_meta.get("sales_slip_url"):
                rec["sales_slip_url"] = slip_meta["sales_slip_url"]

    # Pass 1: Collect canceled approvals & confirmed corporate card numbers across scanned records
    canceled_approvals: set[str] = set()
    confirmed_corp_cards: set[str] = set()
    for rec in raw_toss_records:
        app_no = str(rec.get("approval_no") or "").strip()
        ev = str(rec.get("event_type") or "")
        s_stat = str(rec.get("slip_status") or "")
        billed = rec.get("billed_amount")
        if (
            ev == "CANCEL"
            or rec.get("is_cancel")
            or s_stat == "취소"
            or (billed is not None and int(billed) == 0)
        ):
            if app_no:
                canceled_approvals.add(app_no)

        c_num = str(rec.get("card_number_masked") or "").strip()
        if not c_num:
            continue
        tail4, bin6 = _extract_card_tail4_and_bin6(c_num)
        if tail4 and is_blocked_personal_card_code(tail4, archive_dir=archive_dir):
            continue
        if bin6 and any(bin6.startswith(p) for p in _TOSS_PERSONAL_CARD_BIN6_PREFIXES):
            continue
        buyer_probe = f"{rec.get('buyer_greeting') or ''} {rec.get('item_name') or ''}"
        if (
            (tail4 and match_registered_corp_card_code(tail4, archive_dir=archive_dir))
            or (app_no and app_no in existing_corp_approvals)
            or (bin6 and any(bin6.startswith(p) for p in _TOSS_CORP_CARD_BIN6_PREFIXES))
            or _CORP_BUYER_OR_ITEM_RE.search(buyer_probe)
        ):
            confirmed_corp_cards.add(c_num)

    # Mark any previously auto-created rows canceled if full cancellation was discovered
    if canceled_approvals:
        ph = ",".join("?" for _ in canceled_approvals)
        cursor.execute(
            f"""
            UPDATE card_transactions
            SET reconcile_status = 'CANCELED', updated_at = CURRENT_TIMESTAMP
            WHERE approval_no IN ({ph})
              AND reconcile_status IN ('EMAIL_ORDER', 'OCR_MATCHED')
            """,
            tuple(canceled_approvals),
        )

    # Pass 2: Deduplicate payment records (oldest-to-newest) and reconcile corporate purchases
    payment_records = [r for r in reversed(raw_toss_records) if r.get("event_type") == "PAYMENT"]
    seen_payment_keys: set[str] = set()
    used_tx_ids: set[int] = set()
    skipped_personal = 0
    skipped_canceled = 0
    matched_existing_cnt = 0
    created_cnt = 0
    created_ids: List[int] = []
    newly_stated_ids: List[int] = []
    result_items: List[Dict[str, Any]] = []

    for rec in payment_records:
        dt_str = str(rec.get("paid_at") or rec.get("transacted_at") or "")[:10]
        if len(norm_start) == 10 and dt_str and dt_str < norm_start:
            continue
        if len(norm_end) == 10 and dt_str and dt_str > norm_end:
            continue

        app_no = str(rec.get("approval_no") or "").strip()
        ord_no = str(rec.get("order_no") or "").strip()
        pay_key = app_no or ord_no or f"{dt_str}|{rec.get('paid_amount')}|{rec.get('item_name')}"
        if pay_key in seen_payment_keys:
            continue
        seen_payment_keys.add(pay_key)

        billed = rec.get("billed_amount")
        if (app_no and app_no in canceled_approvals) or (billed is not None and int(billed) <= 0):
            skipped_canceled += 1
            continue

        ownership = classify_toss_card_ownership(
            rec,
            confirmed_corp_card_numbers=confirmed_corp_cards,
            existing_corp_approvals=existing_corp_approvals,
            archive_dir=archive_dir,
        )
        allow_create = auto_create_corp_tx
        if not ownership["is_corporate"]:
            # If slip metadata was unreachable (no masked card number), still allow matching
            # an existing ledger row by (amount, date ±3d) without auto-creating new rows
            c_masked = str(rec.get("card_number_masked") or "").strip()
            eff_amt = int(
                rec.get("billed_amount") or rec.get("effective_amount") or rec.get("paid_amount") or 0
            )
            if not c_masked and eff_amt > 0 and len(dt_str) == 10:
                ownership = {
                    "is_corporate": True,
                    "reason": "existing_ledger_fallback",
                    "card_last4": "0000",
                    "issuer_code": _infer_corp_issuer_code(
                        str(rec.get("card_name") or rec.get("card_issuer") or "")
                    ),
                    "should_auto_register": False,
                }
                allow_create = False
            else:
                skipped_personal += 1
                continue

        outcome = _reconcile_or_create_corp_toss_record(
            cursor,
            record=rec,
            ownership=ownership,
            used_tx_ids=used_tx_ids,
            auto_create_corp_tx=allow_create,
            archive_dir=archive_dir,
            run_ledger_slip_gen=False,
        )
        if outcome:
            tid = int(outcome["tx_id"])
            if outcome["action"] == "CREATED":
                created_cnt += 1
                created_ids.append(tid)
            else:
                matched_existing_cnt += 1
            if outcome.get("is_newly_stated"):
                newly_stated_ids.append(tid)
            result_items.append(outcome)
        elif ownership.get("reason") == "existing_ledger_fallback":
            skipped_personal += 1

    if result_items:
        generate_electronic_slips_for_ledger(cursor, archive_dir=archive_dir)

    result_items.sort(key=lambda x: str(x.get("transacted_at") or ""), reverse=True)
    return {
        "parsed_toss_count": len(raw_toss_records),
        "verified_slips_count": verified_slips_cnt,
        "corp_total_count": matched_existing_cnt + created_cnt,
        "corp_matched_existing_count": matched_existing_cnt,
        "corp_created_count": created_cnt,
        "skipped_personal_count": skipped_personal,
        "skipped_canceled_count": skipped_canceled,
        "created_tx_ids": created_ids,
        "newly_stated_tx_ids": newly_stated_ids,
        "updated_tx_ids": [int(x["tx_id"]) for x in result_items],
        "items": result_items,
    }


def scroll_imap_toss_payments_emails(
    cursor: sqlite3.Cursor,
    archive_dir: Optional[Path] = None,
    account_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page_size: int = 50,
    max_pages: int = 50,
    verify_receipt_slip: bool = True,
    auto_create_corp_tx: bool = True,
) -> Dict[str, Any]:
    """Deep-scroll IMAP mailboxes page-by-page for corporate card Toss Payments purchase history."""
    cfg = load_email_watcher_config(archive_dir)
    accounts: List[Dict[str, Any]] = list(cfg.get("accounts") or [])
    targets = (
        [a for a in accounts if a.get("account_id") == account_id]
        if account_id
        else [a for a in accounts if a.get("enabled", True)]
    )
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
            "scrolled_pages": 0,
            "scanned_messages": 0,
            "parsed_toss_count": 0,
            "verified_slips_count": 0,
            "corp_matched_existing_count": 0,
            "corp_created_count": 0,
            "skipped_personal_count": 0,
            "skipped_canceled_count": 0,
            "items": [],
            "account_results": [],
        }

    eff_page_size = max(10, min(200, int(page_size or 50)))
    eff_max_pages = max(1, min(200, int(max_pages or 50)))
    norm_start = _normalize_datetime_str(start_date)[:10] if start_date else ""
    norm_end = _normalize_datetime_str(end_date)[:10] if end_date else ""

    total_pages = 0
    total_scanned_msgs = 0
    seen_msg_keys: set[str] = set()
    raw_toss_records: List[Dict[str, Any]] = []
    account_results: List[Dict[str, Any]] = []
    error_messages: List[str] = []
    success_accounts = 0

    for acc in valid_targets:
        aid = str(acc["account_id"])
        host = str(acc["imap_host"]).strip()
        port = int(acc.get("imap_port") or 993)
        user = str(acc["email_address"]).strip()
        password = str(acc["app_password"]).strip()
        conf_mbox = str(acc.get("mailbox") or "INBOX").strip() or "INBOX"
        acc_pages = 0
        acc_msgs = 0
        acc_parsed = 0

        try:
            imap = imaplib.IMAP4_SSL(host, port, timeout=30.0)
            try:
                imap.login(user, password)
                mailboxes = _discover_imap_scroll_mailboxes(imap, conf_mbox)
                for mbox in mailboxes:
                    sel_name = f'"{mbox}"' if (" " in mbox and not mbox.startswith('"')) else mbox
                    typ_sel, _ = imap.select(sel_name, readonly=True)
                    if typ_sel != "OK":
                        continue

                    search_tokens: List[str] = ["FROM", '"tosspayments"']
                    if len(norm_start) == 10:
                        search_tokens.extend(["SINCE", _format_imap_date_token(norm_start, 0)])
                    if len(norm_end) == 10:
                        search_tokens.extend(["BEFORE", _format_imap_date_token(norm_end, 1)])

                    typ_s, data_s = imap.uid("SEARCH", None, *search_tokens)
                    uids: List[bytes] = (
                        data_s[0].split() if (typ_s == "OK" and data_s and data_s[0]) else []
                    )
                    if not uids:
                        continue

                    # Scroll backward newest-to-oldest in pages of eff_page_size
                    rev_uids = list(reversed(uids))
                    for p_idx in range(eff_max_pages):
                        start_i = p_idx * eff_page_size
                        if start_i >= len(rev_uids):
                            break
                        batch_uids = rev_uids[start_i : start_i + eff_page_size]
                        acc_pages += 1
                        total_pages += 1

                        uid_set = b",".join(batch_uids)
                        typ_f, fetch_data = imap.uid("FETCH", uid_set, "(BODY.PEEK[])")
                        if typ_f != "OK" or not fetch_data:
                            continue

                        for item in fetch_data:
                            if not (
                                isinstance(item, tuple)
                                and len(item) >= 2
                                and isinstance(item[1], bytes)
                            ):
                                continue
                            raw_eml = item[1]
                            if not raw_eml:
                                continue
                            acc_msgs += 1
                            total_scanned_msgs += 1
                            parts = _extract_eml_parts(raw_eml)
                            dedup_k = (
                                f"{user}:{parts['message_id']}"
                                if parts["message_id"]
                                else f"{user}:{hashlib.sha1(raw_eml[:2048]).hexdigest()}"
                            )
                            if dedup_k in seen_msg_keys:
                                continue
                            seen_msg_keys.add(dedup_k)

                            parsed = parse_toss_payments_email(
                                subject=parts["subject"],
                                sender=parts["sender"],
                                date_str=parts["date_str"],
                                body_text=parts["body_text"],
                                body_html=parts["body_html"],
                            )
                            if not parsed:
                                continue
                            parsed["recipient_email"] = user
                            acc_parsed += 1
                            raw_toss_records.append(parsed)
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

            success_accounts += 1
            account_results.append(
                {
                    "account_id": aid,
                    "email_address": user,
                    "status": "scrolled",
                    "scrolled_pages": acc_pages,
                    "scanned_messages": acc_msgs,
                    "parsed_toss_count": acc_parsed,
                }
            )
        except Exception as exc:
            err_str = f"{user} ({host}:{port}) 스크롤 실패: {exc}"
            error_messages.append(err_str)
            account_results.append(
                {
                    "account_id": aid,
                    "email_address": user,
                    "status": "error",
                    "scrolled_pages": acc_pages,
                    "scanned_messages": acc_msgs,
                    "parsed_toss_count": acc_parsed,
                    "message": err_str,
                }
            )

    if success_accounts == 0 and error_messages:
        return {
            "status": "error",
            "message": " / ".join(error_messages),
            "scrolled_pages": total_pages,
            "scanned_messages": total_scanned_msgs,
            "parsed_toss_count": 0,
            "verified_slips_count": 0,
            "corp_matched_existing_count": 0,
            "corp_created_count": 0,
            "skipped_personal_count": 0,
            "skipped_canceled_count": 0,
            "items": [],
            "account_results": account_results,
        }

    batch_res = _process_toss_records_batch(
        cursor,
        raw_toss_records,
        norm_start=norm_start,
        norm_end=norm_end,
        verify_receipt_slip=verify_receipt_slip,
        auto_create_corp_tx=auto_create_corp_tx,
        archive_dir=archive_dir,
    )
    result_items = batch_res["items"]
    if result_items:
        merged_hist = _merge_recent_matched_items(
            cfg.get("recent_matched_items") or [],
            result_items,
        )
        save_email_watcher_config({"recent_matched_items": merged_hist}, archive_dir=archive_dir)

    matched_existing_cnt = int(batch_res["corp_matched_existing_count"])
    created_cnt = int(batch_res["corp_created_count"])
    skipped_personal = int(batch_res["skipped_personal_count"])
    skipped_canceled = int(batch_res["skipped_canceled_count"])
    total_corp = int(batch_res["corp_total_count"])
    summary_msg = (
        f"토스페이먼츠 메일 스크롤링 완료 ({total_pages}페이지 · {total_scanned_msgs}통 탐색 · "
        f"결제메일 {len(raw_toss_records)}건 분석): "
        f"법인카드 {total_corp}건 반영 (기존매칭 {matched_existing_cnt}건 · 신규생성 {created_cnt}건), "
        f"개인카드 {skipped_personal}건 제외"
    )
    if skipped_canceled > 0:
        summary_msg += f", 취소 {skipped_canceled}건 제외"

    return {
        "status": "scrolled",
        "message": summary_msg,
        "scrolled_pages": total_pages,
        "scanned_messages": total_scanned_msgs,
        "parsed_toss_count": len(raw_toss_records),
        "verified_slips_count": int(batch_res["verified_slips_count"]),
        "corp_total_count": total_corp,
        "corp_matched_existing_count": matched_existing_cnt,
        "corp_created_count": created_cnt,
        "skipped_personal_count": skipped_personal,
        "skipped_canceled_count": skipped_canceled,
        "items": result_items,
        "account_results": account_results,
    }
