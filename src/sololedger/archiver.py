"""Evidence file SHA-256 deduplication and normalized tax archiving."""

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Optional, Tuple

from sololedger.config import settings
from sololedger.sms_parser import sanitize_card_folder_name


class DuplicateReceiptError(ValueError):
    """Raised when an uploaded receipt image matches an existing SHA-256 hash."""

    def __init__(self, file_hash: str, existing_receipt_id: int):
        super().__init__(
            f"Duplicate receipt upload blocked (SHA-256={file_hash[:12]}..., receipt_id={existing_receipt_id})"
        )
        self.file_hash = file_hash
        self.existing_receipt_id = existing_receipt_id


def compute_sha256_hex(content: bytes) -> str:
    """Compute lowercase 64-character SHA-256 hex digest of raw file bytes."""
    return hashlib.sha256(content).hexdigest()


def sanitize_filename_token(token: str) -> str:
    """Clean corporate prefixes and illegal filesystem characters from a filename segment."""
    cleaned = re.sub(r"\(주\)|주식회사|\(유\)", "", token or "")
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned).strip("_.-")
    return cleaned or "미상"


def build_archived_filename(
    transacted_at: str,
    merchant_name: str,
    account_name: str,
    total_amount: int,
    item_summary: Optional[str] = None,
    original_file_name: str = "receipt.jpg",
) -> str:
    """Build normalized filename for tax accountant submission (5-year archive rule).

    Format:
    - With item: `{YYYYMMDD}_{가맹점}_{계정과목}_{품목}_{금액}.{ext}`
      e.g. `20261020_쿠팡_소모품비_모니터암_45000.jpg`
    - Without item: `{YYYYMMDD}_{가맹점}_{계정과목}_{금액}.{ext}`
      e.g. `20261020_스타벅스_복리후생비_11000.jpg`
    """
    digits_only = re.sub(r"\D", "", transacted_at or "")
    date_prefix = digits_only[:8] if len(digits_only) >= 8 else "00000000"

    ext = Path(original_file_name or "receipt.jpg").suffix.lower()
    if not ext or not re.fullmatch(r"\.[a-z0-9]{2,5}", ext):
        ext = ".jpg"

    merchant_tok = sanitize_filename_token(merchant_name)
    account_tok = sanitize_filename_token(account_name)
    amount_tok = str(abs(int(total_amount)))

    if item_summary:
        item_tok = sanitize_filename_token(item_summary)[:30]
        return f"{date_prefix}_{merchant_tok}_{account_tok}_{item_tok}_{amount_tok}{ext}"

    return f"{date_prefix}_{merchant_tok}_{account_tok}_{amount_tok}{ext}"


def build_receipt_print_title(
    tx_id: int,
    transacted_at: str,
    card_last4: str,
    merchant_name: str,
    account_name: str,
    total_amount: int,
) -> str:
    """Build a distinguishing PDF print filename/title for a single credit card receipt slip.

    Format:
    `매출전표_{YYYYMMDD}_{HHMM}_{카드명}_{가맹점}_{계정과목}_{금액}원_TX{tx_id}`
    e.g. `매출전표_20261004_0945_우리법인카드_6534_카카오페이_소모품비_23050원_TX4271`
    """
    digits = re.sub(r"\D", "", transacted_at or "")
    ymd = digits[:8] if len(digits) >= 8 else "00000000"
    hm = digits[8:12] if len(digits) >= 12 else "0000"
    card_tok = sanitize_card_folder_name(card_last4 or "0000")
    merchant_tok = sanitize_filename_token(merchant_name)
    account_tok = sanitize_filename_token(account_name)
    amount_tok = f"{abs(int(total_amount))}원"
    return f"매출전표_{ymd}_{hm}_{card_tok}_{merchant_tok}_{account_tok}_{amount_tok}_TX{int(tx_id)}"


def check_duplicate_receipt_hash(
    cursor: sqlite3.Cursor, file_hash_sha256: str
) -> Optional[int]:
    """Return existing receipt_id if file_hash_sha256 already exists in receipt_ocr_metadata."""
    cursor.execute(
        "SELECT receipt_id FROM receipt_ocr_metadata WHERE file_hash_sha256 = ?",
        (file_hash_sha256,),
    )
    row = cursor.fetchone()
    return int(row[0]) if row else None


def save_receipt_files(
    content: bytes,
    original_file_name: str,
    archived_file_name: str,
    archive_dir: Optional[Path] = None,
    card_last4: Optional[str] = None,
) -> Tuple[str, str]:
    """Persist original, normalized, and per-card categorized receipt files under archive directory."""
    base_dir = archive_dir or settings.archive_dir
    orig_dir = base_dir / "original"
    norm_dir = base_dir / "normalized"
    orig_dir.mkdir(parents=True, exist_ok=True)
    norm_dir.mkdir(parents=True, exist_ok=True)

    safe_orig_name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", Path(original_file_name).name) or "receipt.jpg"
    orig_path = orig_dir / safe_orig_name
    norm_path = norm_dir / archived_file_name

    orig_path.write_bytes(content)
    norm_path.write_bytes(content)

    if card_last4:
        card_folder = sanitize_card_folder_name(card_last4)
        card_dir = norm_dir / "by_card" / card_folder
        card_dir.mkdir(parents=True, exist_ok=True)
        (card_dir / archived_file_name).write_bytes(content)

    return str(orig_path), str(norm_path)
