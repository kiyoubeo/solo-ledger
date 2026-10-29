"""Korean corporate card SMS parser supporting single-line and multi-line formats."""

import re
from datetime import datetime
from typing import Optional

from sololedger.models import ParsedSmsResult


def parse_korean_card_sms(
    raw_text: str, fallback_time: Optional[str] = None
) -> ParsedSmsResult:
    """Parse multi-line or single-line Korean corporate card SMS into structured fields.

    Supports Shinhan, KB, Hyundai, Samsung, Hana, Woori, IBK/BC, and NH Nonghyup formats.
    """
    clean_text = raw_text.replace("\r\n", "\n").strip()

    # 1. Detect if the message is a cancellation event
    is_cancel = bool(
        re.search(
            r"(승인취소|취소완료|취소승인|결제취소|법인취소|체크취소|해외취소|취소)",
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
        r"(?:\(|신한|KB국민|국민|현대|삼성|하나|우리|IBK기업|기업|BC|NH농협|농협|롯데|법인|카드|체크|승인|취소|\s)"
        r"(\d[\d\*]{3})"
        r"(?:\)|\s|님|체크|법인|카드|해외|승인|취소|$)",
        clean_text,
    )
    card_last4 = card_match.group(1) if card_match else "0000"

    # 5. Extract Approval Number if present in SMS (8 digits)
    app_no_match = re.search(r"승인번호\s*:?\s*(\d{8})", clean_text)
    approval_no = app_no_match.group(1) if app_no_match else None

    # 6. Extract Merchant Name by filtering out known metadata tokens
    lines = [line.strip() for line in clean_text.split("\n") if line.strip()]
    merchant_candidates = []

    for line in lines:
        stripped = re.sub(r"\[Web발신\]|\[웹발신\]", "", line, flags=re.IGNORECASE)
        stripped = re.sub(r"\[[^\]]*카드[^\]]*\]", "", stripped)
        stripped = re.sub(r"승인번호\s*:?\s*\d{8}", "", stripped)
        stripped = re.sub(r"누적.*", "", stripped)
        stripped = re.sub(
            r"(?:네이버\s+)?(신한|KB국민|국민|현대|삼성|하나|우리|IBK기업|기업|BC|NH농협|농협|롯데)"
            r"(?:\s*(?:\(?(\d[\d\*]{3})\)?|법인|카드|체크|해외|승인|취소))+",
            "",
            stripped,
        )
        stripped = re.sub(r"\(?(\d[\d\*]{3})\)?", "", stripped)
        stripped = re.sub(r"\((?:본인|가족|일시불|\d+개월)\)", "", stripped)
        stripped = re.sub(r"[가-힣]\*[가-힣]+님?", "", stripped)
        stripped = re.sub(r"\d{2}/\d{2}\s+\d{2}:\d{2}", "", stripped)
        stripped = re.sub(r"KRW\s*-?[\d,]+", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"-?[\d,]+\s*원", "", stripped)
        stripped = re.sub(
            r"(일시불|\d+개월|할부|승인취소|취소완료|취소승인|결제취소|법인승인|법인취소|"
            r"체크승인|체크취소|해외승인|해외취소|승인|취소|결제|국외|해외)",
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
