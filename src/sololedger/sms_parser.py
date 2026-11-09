"""Korean corporate card SMS parser supporting single-line and multi-line formats."""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger.config import settings
from sololedger.models import ParsedSmsResult

CARD_LABEL_MAP: Dict[str, str] = {
    "8508": "삼성카드(8508)",
    "4195": "삼성카드(4195)",
    "7023": "삼성카드(7023)",
    "1000": "삼성카드(1000)",
    "6*1*": "롯데카드(6*1*)",
    "6*4*": "롯데카드(6*4*)",
    "4*4*": "롯데카드(하이패스)",
    "0*7*": "롯데카드(0*7*)",
    "8*6*": "롯데카드(8*6*)",
    "1266": "신한카드(1266)",
    "6096": "신한카드(6096)",
    "6534": "우리카드(6534)",
    "1595": "우리카드(1595)",
    "4479": "우리체크(4479)",
    "8691": "KB국민카드(8691)",
    "KB하이": "KB국민(하이패스)",
    "KB위시": "KB국민(마이위시)",
    "5*5*": "하나체크(5*5*)",
    "현대NV": "네이버현대카드",
    "현대백화": "현대백화점카드",
    "9876": "신한법인(9876)",
    "5678": "KB국민법인(5678)",
    "12*4": "신한법인(12*4)",
    "3456": "하나법인(3456)",
    "3081": "우리법인(3081)",
    "1175": "우리법인(1175)",
    "7014": "우리법인(7014)",
    "0445": "우리법인(0445)",
    "2088": "우리법인(2088)",
    "7620": "우리법인(7620)",
}

DEFAULT_BLOCKED_PERSONAL_CARDS: tuple[str, ...] = (
    "8691",
    "KB하이",
    "KB위시",
    "6*4*",
    "4*4*",
    "6*1*",
    "0*7*",
    "8*6*",
    "현대NV",
    "현대백화",
    "8508",
    "4195",
    "7023",
    "1000",
    "1266",
    "6096",
    "6534",
    "1595",
    "4479",
    "5*5*",
)

CORP_CARD_ISSUER_PREFIX: Dict[str, str] = {
    "WOORI": "우리법인",
    "SHINHAN": "신한법인",
    "KB": "KB국민법인",
    "HANA": "하나법인",
    "HYUNDAI": "현대법인",
    "SAMSUNG": "삼성법인",
    "LOTTE": "롯데법인",
    "IBK": "IBK기업법인",
    "NH": "NH농협법인",
    "BC": "BC법인",
    "CORP": "법인카드",
}


def get_corp_card_policy_path(archive_dir: Optional[Path] = None) -> Path:
    """Return path to corp_card_policy.json inside the active archive directory."""
    base_dir = archive_dir or settings.archive_dir
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "corp_card_policy.json"


def load_corp_card_policy(archive_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load corporate card dedicated mode policy and registered corporate cards."""
    env_flag = os.getenv("SOLOLEDGER_CORP_ONLY_MODE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    default_policy: Dict[str, Any] = {
        "corp_only_mode": env_flag,
        "strict_registered_only": True,
        "block_unmatched_email_orders": True,
        "auto_register_on_statement_upload": True,
        "registered_corp_cards": [],
        "blocked_personal_cards": list(DEFAULT_BLOCKED_PERSONAL_CARDS),
    }
    cfg_path = get_corp_card_policy_path(archive_dir)
    if not cfg_path.exists():
        return default_policy
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default_policy
        merged = dict(default_policy)
        for k in (
            "corp_only_mode",
            "strict_registered_only",
            "block_unmatched_email_orders",
            "auto_register_on_statement_upload",
        ):
            if k in raw:
                merged[k] = bool(raw[k])
        if isinstance(raw.get("registered_corp_cards"), list):
            merged["registered_corp_cards"] = [
                c for c in raw["registered_corp_cards"] if isinstance(c, dict) and c.get("card_last4")
            ]
        if isinstance(raw.get("blocked_personal_cards"), list):
            merged["blocked_personal_cards"] = [
                str(x).strip() for x in raw["blocked_personal_cards"] if str(x).strip()
            ]
        return merged
    except Exception:
        return default_policy


def save_corp_card_policy(
    updates: Dict[str, Any], archive_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """Update and persist corp_card_policy.json."""
    current = load_corp_card_policy(archive_dir)
    for k in (
        "corp_only_mode",
        "strict_registered_only",
        "block_unmatched_email_orders",
        "auto_register_on_statement_upload",
    ):
        if k in updates and updates[k] is not None:
            current[k] = bool(updates[k])
    if "registered_corp_cards" in updates and isinstance(updates["registered_corp_cards"], list):
        current["registered_corp_cards"] = [
            c
            for c in updates["registered_corp_cards"]
            if isinstance(c, dict) and str(c.get("card_last4") or "").strip()
        ]
    if "blocked_personal_cards" in updates and isinstance(updates["blocked_personal_cards"], list):
        current["blocked_personal_cards"] = [
            str(x).strip() for x in updates["blocked_personal_cards"] if str(x).strip()
        ]

    cfg_path = get_corp_card_policy_path(archive_dir)
    cfg_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def register_corp_card(
    card_last4: str,
    card_label: Optional[str] = None,
    issuer: str = "CORP",
    enabled: bool = True,
    archive_dir: Optional[Path] = None,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    """Register or update a corporate card in corp_card_policy.json and unblock it if needed."""
    code = str(card_last4 or "").strip()
    if not code or code == "0000":
        return load_corp_card_policy(archive_dir)

    issuer_key = re.sub(r"_CARD$", "", str(issuer or "CORP").strip().upper())
    prefix = CORP_CARD_ISSUER_PREFIX.get(issuer_key, "법인카드")
    resolved_label = (card_label or label or "").strip()
    if not resolved_label:
        existing_lbl = CARD_LABEL_MAP.get(code, "")
        if "법인" in existing_lbl:
            resolved_label = existing_lbl
        else:
            resolved_label = f"{prefix}({code})"

    policy = load_corp_card_policy(archive_dir)
    cards: List[Dict[str, Any]] = list(policy.get("registered_corp_cards") or [])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated_flag = False
    for entry in cards:
        if str(entry.get("card_last4") or "").strip() == code:
            entry["card_label"] = resolved_label
            entry["label"] = resolved_label
            entry["issuer"] = issuer_key
            entry["enabled"] = bool(enabled)
            entry["updated_at"] = now_str
            updated_flag = True
            break
    if not updated_flag:
        cards.append(
            {
                "card_last4": code,
                "card_label": resolved_label,
                "label": resolved_label,
                "issuer": issuer_key,
                "enabled": bool(enabled),
                "registered_at": now_str,
            }
        )

    blocked = [
        x for x in (policy.get("blocked_personal_cards") or []) if str(x).strip() != code
    ]
    return save_corp_card_policy(
        {
            "registered_corp_cards": cards,
            "blocked_personal_cards": blocked,
        },
        archive_dir=archive_dir,
    )


def remove_corp_card(
    card_last4: str,
    add_to_blocked: bool = False,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Remove a corporate card from registered_corp_cards."""
    code = str(card_last4 or "").strip()
    policy = load_corp_card_policy(archive_dir)
    cards = [
        c
        for c in (policy.get("registered_corp_cards") or [])
        if str(c.get("card_last4") or "").strip() != code
    ]
    blocked = list(policy.get("blocked_personal_cards") or [])
    if add_to_blocked and code and code not in blocked:
        blocked.append(code)
    return save_corp_card_policy(
        {
            "registered_corp_cards": cards,
            "blocked_personal_cards": blocked,
        },
        archive_dir=archive_dir,
    )


def is_allowed_corporate_card(
    card_last4: str,
    raw_text: str = "",
    archive_dir: Optional[Path] = None,
    is_statement_upload: bool = False,
) -> bool:
    """Return True if card_last4 is allowed under the active corporate card policy."""
    policy = load_corp_card_policy(archive_dir)
    if not policy.get("corp_only_mode", False):
        return True

    key = (card_last4 or "0000").strip()
    reg_list = [
        c
        for c in (policy.get("registered_corp_cards") or [])
        if isinstance(c, dict) and c.get("enabled", True)
    ]
    reg_codes = {str(c.get("card_last4") or "").strip() for c in reg_list if c.get("card_last4")}
    blocked_codes = {str(x).strip() for x in (policy.get("blocked_personal_cards") or []) if x}

    if key in reg_codes:
        return True
    if key in blocked_codes:
        return False

    if is_statement_upload:
        if (
            reg_codes
            and policy.get("strict_registered_only", True)
            and not policy.get("auto_register_on_statement_upload", True)
        ):
            return key in reg_codes
        return True

    if reg_codes and policy.get("strict_registered_only", True):
        return key in reg_codes

    lbl = CARD_LABEL_MAP.get(key, "")
    if "법인" in lbl:
        return True
    if re.search(r"(법인|기업)", raw_text or ""):
        return True
    return False


def get_effective_card_label_map(archive_dir: Optional[Path] = None) -> Dict[str, str]:
    """Return effective card_last4 -> display label mapping respecting corp_card_policy."""
    policy = load_corp_card_policy(archive_dir)
    base = dict(CARD_LABEL_MAP)
    if policy.get("corp_only_mode", False):
        blocked = set(policy.get("blocked_personal_cards") or [])
        for b_code in blocked:
            base.pop(b_code, None)
    for entry in policy.get("registered_corp_cards") or []:
        c_code = str(entry.get("card_last4") or "").strip()
        c_lbl = str(entry.get("card_label") or "").strip()
        if c_code and c_lbl:
            base[c_code] = c_lbl
    return base


def extract_rcs_text(raw_body: str) -> str:
    """Extract visible text lines from an Android RCS open_rich_card / generalPurposeCard JSON payload."""
    body_str = (raw_body or "").strip()
    if not body_str.startswith("{"):
        return body_str
    try:
        data = json.loads(body_str)
    except Exception:
        texts = re.findall(r'"(?:text|description|title)"\s*:\s*"([^"]+)"', body_str)
        return "\n".join(texts) if texts else body_str

    collected: List[str] = []
    if isinstance(data, dict) and data.get("messageHeader"):
        collected.append(str(data["messageHeader"]))

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("text", "description", "title") and isinstance(v, str) and v.strip():
                    collected.append(v.strip())
                else:
                    _walk(v)
        elif isinstance(node, list):
            for elem in node:
                _walk(elem)

    _walk(data)
    return "\n".join(collected) if collected else body_str


def format_card_label(card_last4: str, archive_dir: Optional[Path] = None) -> str:
    """Return a human-readable card display label for a given card_last4 code."""
    key = (card_last4 or "0000").strip()
    policy = load_corp_card_policy(archive_dir)
    for entry in policy.get("registered_corp_cards") or []:
        if str(entry.get("card_last4") or "").strip() == key:
            custom_lbl = str(entry.get("card_label") or "").strip()
            if custom_lbl:
                return custom_lbl
    if key in CARD_LABEL_MAP:
        return CARD_LABEL_MAP[key]
    if key == "0000":
        return "기타카드(0000)"
    return f"법인카드({key})"


def sanitize_card_folder_name(card_last4: str) -> str:
    """Return a filesystem-safe directory name for per-card receipt archiving."""
    label = format_card_label(card_last4)
    cleaned = label.replace("*", "X").replace("(", "_").replace(")", "")
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", cleaned).strip("_")
    return cleaned or "기타카드_0000"


def is_promotional_or_non_tx_notification(raw_text: str) -> bool:
    """Return True if a notification is an advertisement, statement notice, or non-payment alert."""
    text = (raw_text or "").strip()
    if not text:
        return True
    if re.search(r"(\(광고\)|\[광고\]|광고\)|수신거부)", text):
        return True
    has_tx_verb = bool(
        re.search(
            r"(승인|취소|일시불|\d+개월|할부|체크결제|법인결제|해외결제|결제완료)",
            text,
        )
    )
    if re.search(
        r"(이용대금명세서|명세서.*도착|청구서|결제예정금액|결제일\s*안내|"
        r"한도\s*상향|리볼빙|장기카드대출|단기카드대출|카드론|이벤트\s*응모|캐시백\s*혜택)",
        text,
    ) and not re.search(r"(일시불|\d+개월|승인번호)", text):
        return True
    return not has_tx_verb


def parse_korean_card_sms(
    raw_text: str,
    fallback_time: Optional[str] = None,
    card_hint: Optional[str] = None,
) -> ParsedSmsResult:
    """Parse multi-line or single-line Korean corporate card SMS/Push into structured fields.

    Supports Shinhan, KB, Hyundai, Samsung, Hana, Woori (including Woori WON Card Push),
    IBK/BC, NH Nonghyup, and RCS JSON formats.
    """
    unwrapped = extract_rcs_text(raw_text)
    clean_text = unwrapped.replace("\r\n", "\n").strip()

    # 1. Detect if the message is a cancellation event
    is_cancel = bool(
        re.search(
            r"(승인취소|취소완료|취소승인|결제취소|법인취소|체크취소|해외취소|부분취소|취소)",
            clean_text,
        )
    )

    # 2. Extract Total Amount (e.g., '45,000원', '-45,000원', or 'KRW 12,000')
    # Ignore accumulated amount lines by removing '누적' segments first
    text_without_cumulative = re.sub(r"누적\s*:?\s*[\d,]+원?", "", clean_text)
    amount_match = re.search(
        r"(?:KRW\s*(-?[\d,]+)|(-?[\d,]+)\s*원)",
        text_without_cumulative,
        re.IGNORECASE,
    )
    if not amount_match:
        raise ValueError("Failed to extract KRW payment amount from SMS.")

    if is_promotional_or_non_tx_notification(clean_text):
        raise ValueError("Promotional or non-transaction notification ignored.")

    raw_amt_str = amount_match.group(1) or amount_match.group(2)
    raw_amount = int(raw_amt_str.replace(",", ""))
    if raw_amount < 0:
        is_cancel = True
    total_amount = -abs(raw_amount) if is_cancel else abs(raw_amount)

    # 3. Extract Transaction Date & Time (e.g., '10/20 13:20')
    dt_match = re.search(r"(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", clean_text)
    if fallback_time and len(fallback_time) >= 4 and fallback_time[:4].isdigit():
        reference_year = int(fallback_time[:4])
    else:
        reference_year = datetime.now().year

    if dt_match:
        month, day, hour, minute = dt_match.groups()
        transacted_at = f"{reference_year}-{month}-{day} {hour}:{minute}:00"
    elif fallback_time:
        transacted_at = fallback_time[:19].replace("T", " ")
    else:
        transacted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 4. Extract Card Last 4 Digits (handles '1234', '12*4', '0*7*', '(9876)', '삼성8508승인')
    card_match = re.search(
        r"(?:\(|\]\s*|신한|KB국민|국민|현대|삼성|하나|우리|IBK기업|기업|BC|NH농협|농협|롯데|법인|카드|체크|승인|취소|\s)"
        r"(\d[\d\*]{3})"
        r"(?:\)|\s|님|체크|법인|카드|해외|승인|취소|$)",
        clean_text,
    )
    if card_match:
        card_last4 = card_match.group(1)
    elif "네이버 현대카드" in clean_text:
        card_last4 = "현대NV"
    elif "현대백화점카드" in clean_text:
        card_last4 = "현대백화"
    elif "KB국민카드" in clean_text and "후불하이패스" in clean_text:
        card_last4 = "KB하이"
    elif card_hint and card_hint.strip():
        card_last4 = card_hint.strip()
    else:
        card_last4 = "0000"

    # 5. Extract Approval Number if present in SMS (8 digits)
    app_no_match = re.search(r"승인번호\s*:?\s*(\d{8})", clean_text)
    approval_no = app_no_match.group(1) if app_no_match else None

    # 6. Extract Merchant Name by filtering out known metadata tokens
    lines = [line.strip() for line in clean_text.split("\n") if line.strip()]
    merchant_candidates = []

    for line in lines:
        stripped = re.sub(r"\[Web발신\]|\[웹발신\]", "", line, flags=re.IGNORECASE)
        stripped = re.sub(r"^[●◼■▶*]\s*.*이용안내.*", "", stripped)
        stripped = re.sub(r"\[[^\]]*카드[^\]]*\]", "", stripped)
        stripped = re.sub(r"우리WON카드|WON카드", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"(?:승인|결제|취소)\s*알림", "", stripped)
        stripped = re.sub(r"승인번호\s*:?\s*\d{8}", "", stripped)
        stripped = re.sub(r"누적.*", "", stripped)
        stripped = re.sub(
            r"(?:네이버\s+)?(신한|KB국민|국민|현대|삼성|하나|우리|IBK기업|기업|BC|NH농협|농협|롯데)"
            r"(?:\s*(?:\(?(\d[\d\*]{3})\)?|법인|카드|체크|해외|승인|취소))+",
            "",
            stripped,
        )
        stripped = re.sub(r"\(?(\d[\d\*]{3})\)?", "", stripped)
        stripped = re.sub(r"\((?:본인|가족|일시불|\d+개월|-?[\d,]+원)\)", "", stripped)
        stripped = re.sub(r"[가-힣]\*[가-힣]+님?", "", stripped)
        stripped = re.sub(r"\d{2}/\d{2}\s+\d{2}:\d{2}", "", stripped)
        stripped = re.sub(r"\d{2}/\d{2}\s*(?:이용건|결제예정|부분취소)?", "", stripped)
        stripped = re.sub(r"\d+건\s*", "", stripped)
        stripped = re.sub(r"KRW\s*-?[\d,]+", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"-?[\d,]+\s*원", "", stripped)
        stripped = re.sub(
            r"(일시불|\d+개월|할부|승인취소|취소완료|취소승인|결제취소|법인승인|법인취소|"
            r"체크승인|체크취소|해외승인|해외취소|부분취소|승인|취소|결제예정|결제|국외|해외)",
            "",
            stripped,
        )
        stripped = re.sub(r"\s+승$", "", stripped)
        stripped = stripped.strip()
        if len(stripped) >= 2:
            merchant_candidates.append(stripped)

    raw_merchant_name = merchant_candidates[-1] if merchant_candidates else "미확인_가맹점"

    return ParsedSmsResult(
        card_last4=card_last4,
        transacted_at=transacted_at,
        total_amount=total_amount,
        raw_merchant_name=raw_merchant_name,
        is_cancel=is_cancel,
        approval_no=approval_no,
    )
