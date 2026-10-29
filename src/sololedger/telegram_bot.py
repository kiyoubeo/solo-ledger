"""Bidirectional Telegram Bot (@SoloLedgerBot) callback, reply memo, and photo handler."""

import re
import sqlite3
from typing import Any, Dict, Optional, Tuple

from sololedger.archiver import build_archived_filename

_MEMO_ACCOUNT_KEYWORDS = [
    (re.compile(r"(비품|모니터|노트북|키보드|책상|의자)"), ("212", "비품", False, None)),
    (re.compile(r"(도서|책|인쇄|출판)"), ("826", "도서인쇄비", False, None)),
    (re.compile(r"(소프트웨어|구독|클라우드|서버|수수료)"), ("831", "지급수수료", False, None)),
    (re.compile(r"(미팅|접대|거래처|고객사|식사대접)"), ("813", "기업업무추진비", True, "ENTERTAINMENT")),
    (re.compile(r"(소모품|사무용품|케이블|마우스패드)"), ("830", "소모품비", False, None)),
]


def _extract_tx_id_from_text(text: str) -> Optional[int]:
    """Extract transaction ID like `#12` or `전표번호: #12` from message text."""
    if not text:
        return None
    m = re.search(r"#(\d+)", text)
    return int(m.group(1)) if m else None


def _detect_memo_account_override(
    memo_text: str,
) -> Optional[Tuple[str, str, bool, Optional[str]]]:
    """Infer accounting code override from user memo keywords if present."""
    for pattern, acc_tuple in _MEMO_ACCOUNT_KEYWORDS:
        if pattern.search(memo_text):
            return acc_tuple
    return None


def _refresh_receipt_archived_filename(cursor: sqlite3.Cursor, tx_id: int) -> Optional[str]:
    """Update archived_file_name in receipt_ocr_metadata when transaction account/memo changes."""
    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    tx = cursor.fetchone()
    if not tx:
        return None

    cursor.execute(
        "SELECT receipt_id, original_file_path FROM receipt_ocr_metadata "
        "WHERE tx_id = ? ORDER BY receipt_id DESC LIMIT 1",
        (tx_id,),
    )
    rec = cursor.fetchone()
    if not rec:
        return None

    new_name = build_archived_filename(
        transacted_at=tx["transacted_at"],
        merchant_name=tx["raw_merchant_name"],
        account_name=tx["account_name"],
        total_amount=tx["total_amount"],
        item_summary=tx["counterparty_info"],
        original_file_name=rec["original_file_path"],
    )
    cursor.execute(
        "UPDATE receipt_ocr_metadata SET archived_file_name = ? WHERE receipt_id = ?",
        (new_name, rec["receipt_id"]),
    )
    return new_name


def handle_telegram_callback_query(
    cursor: sqlite3.Cursor, callback_query: Dict[str, Any]
) -> Dict[str, Any]:
    """Process inline keyboard button presses (`meal_solo:{tx_id}` or `meal_meeting:{tx_id}`)."""
    data = str(callback_query.get("data", "")).strip()
    m = re.fullmatch(r"(meal_solo|meal_meeting):(\d+)", data)
    if not m:
        return {"status": "ignored", "reason": f"Unsupported callback_data: {data}"}

    action, tx_id_str = m.groups()
    tx_id = int(tx_id_str)

    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    row = cursor.fetchone()
    if row is None:
        return {"status": "error", "reason": f"Transaction #{tx_id} not found."}

    if action == "meal_solo":
        cursor.execute(
            """
            UPDATE card_transactions
            SET account_code = '811',
                account_name = '복리후생비',
                vat_deduction_type = CASE
                    WHEN non_deduct_reason = 'SIMPLIFIED_TAX' THEN vat_deduction_type
                    ELSE 'DEDUCTIBLE'
                END,
                non_deduct_reason = CASE
                    WHEN non_deduct_reason = 'ENTERTAINMENT' THEN NULL
                    ELSE non_deduct_reason
                END,
                counterparty_info = COALESCE(counterparty_info, '대표자 단독 식사'),
                updated_at = CURRENT_TIMESTAMP
            WHERE tx_id = ?
            """,
            (tx_id,),
        )
        _refresh_receipt_archived_filename(cursor, tx_id)
        return {
            "status": "updated",
            "tx_id": tx_id,
            "classification": "811_WELFARE",
            "account_code": "811",
            "account_name": "복리후생비",
            "vat_deduction_type": "DEDUCTIBLE",
            "reply_text": f"전표 #{tx_id} 건이 [811 복리후생비 (단독 식사)]로 확정되었습니다.",
        }

    # action == "meal_meeting" -> Corporate Business Promotion Expense (Entertainment, Non-deductible)
    cursor.execute(
        """
        UPDATE card_transactions
        SET account_code = '813',
            account_name = '기업업무추진비',
            vat_deduction_type = 'NON_DEDUCTIBLE',
            non_deduct_reason = 'ENTERTAINMENT',
            updated_at = CURRENT_TIMESTAMP
        WHERE tx_id = ?
        """,
        (tx_id,),
    )
    _refresh_receipt_archived_filename(cursor, tx_id)
    return {
        "status": "updated",
        "tx_id": tx_id,
        "classification": "813_ENTERTAINMENT",
        "account_code": "813",
        "account_name": "기업업무추진비",
        "vat_deduction_type": "NON_DEDUCTIBLE",
        "non_deduct_reason": "ENTERTAINMENT",
        "reply_text": (
            f"전표 #{tx_id} 건이 [813 기업업무추진비 (불공제-접대비)]로 변경되었습니다. "
            f"참석자 소속 및 성명을 답장으로 입력해 주십시오."
        ),
    }


def handle_telegram_text_reply(
    cursor: sqlite3.Cursor, message: Dict[str, Any]
) -> Dict[str, Any]:
    """Process user text reply containing item description or meeting attendees."""
    raw_text = str(message.get("text", "")).strip()
    if not raw_text:
        return {"status": "ignored", "reason": "Empty message text."}

    reply_to = message.get("reply_to_message") or {}
    reply_source_text = str(reply_to.get("text", ""))

    tx_id = _extract_tx_id_from_text(reply_source_text) or _extract_tx_id_from_text(raw_text)
    if tx_id is None:
        # Fallback to most recent transaction if no explicit #tx_id is found
        cursor.execute("SELECT tx_id FROM card_transactions ORDER BY tx_id DESC LIMIT 1")
        latest = cursor.fetchone()
        if latest is None:
            return {"status": "ignored", "reason": "No target transaction found for memo."}
        tx_id = int(latest[0])

    clean_memo = re.sub(r"^#\d+\s*", "", raw_text).strip()
    override = _detect_memo_account_override(clean_memo)

    if override:
        acc_code, acc_name, force_non_ded, reason = override
        if force_non_ded:
            cursor.execute(
                """
                UPDATE card_transactions
                SET counterparty_info = ?,
                    account_code = ?,
                    account_name = ?,
                    vat_deduction_type = 'NON_DEDUCTIBLE',
                    non_deduct_reason = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tx_id = ?
                """,
                (clean_memo, acc_code, acc_name, reason, tx_id),
            )
        else:
            cursor.execute(
                """
                UPDATE card_transactions
                SET counterparty_info = ?,
                    account_code = ?,
                    account_name = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tx_id = ?
                """,
                (clean_memo, acc_code, acc_name, tx_id),
            )
    else:
        cursor.execute(
            """
            UPDATE card_transactions
            SET counterparty_info = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE tx_id = ?
            """,
            (clean_memo, tx_id),
        )

    new_archived_name = _refresh_receipt_archived_filename(cursor, tx_id)
    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    updated_row = cursor.fetchone()

    return {
        "status": "memo_recorded",
        "tx_id": tx_id,
        "counterparty_info": updated_row["counterparty_info"],
        "account_code": updated_row["account_code"],
        "account_name": updated_row["account_name"],
        "vat_deduction_type": updated_row["vat_deduction_type"],
        "archived_file_name": new_archived_name,
    }


def handle_telegram_update(
    cursor: sqlite3.Cursor, update_payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Top-level dispatcher for Telegram Bot webhook updates."""
    if "callback_query" in update_payload and update_payload["callback_query"]:
        return handle_telegram_callback_query(cursor, update_payload["callback_query"])

    if "message" in update_payload and update_payload["message"]:
        msg = update_payload["message"]
        if "text" in msg and msg["text"]:
            return handle_telegram_text_reply(cursor, msg)

    return {"status": "ignored", "reason": "Unsupported Telegram update payload."}
