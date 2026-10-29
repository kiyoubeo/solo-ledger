"""Multimodal Vision LLM Receipt OCR Parser & Ledger Linking Engine."""

import base64
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from sololedger.archiver import (
    DuplicateReceiptError,
    build_archived_filename,
    check_duplicate_receipt_hash,
    compute_sha256_hex,
    save_receipt_files,
)
from sololedger.config import settings
from sololedger.merchant_service import (
    apply_merchant_tax_status_to_ledger,
    get_or_verify_merchant,
)
from sololedger.models import OcrExtractionResult, OcrLineItem, ReconcileItem
from sololedger.nts_client import validate_biz_reg_no_checksum
from sololedger.reconciler import reconcile_single_item
from sololedger.tax_engine import calculate_supply_and_vat
from sololedger.telegram_bot import _detect_memo_account_override


def summarize_line_items(line_items: List[OcrLineItem]) -> Optional[str]:
    """Build concise Korean accounting item summary (e.g., '모니터암 외 2건')."""
    if not line_items:
        return None
    first_item = (line_items[0].item or "").strip()
    if not first_item:
        return None
    extra_count = len(line_items) - 1
    if extra_count > 0:
        return f"{first_item} 외 {extra_count}건"
    return first_item


def sanitize_ocr_identifiers(
    raw_biz_reg_no: Optional[str], raw_approval_no: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Filter out phone numbers or long e-commerce order IDs misidentified by OCR."""
    clean_biz_no: Optional[str] = None
    if raw_biz_reg_no:
        digits = re.sub(r"\D", "", raw_biz_reg_no)
        # Reject 10-digit strings starting with Korean phone prefixes (02, 010, 031~064, 15xx, 16xx, 18xx)
        is_phone_like = digits.startswith(("02", "010", "15", "16", "18"))
        if len(digits) == 10 and not is_phone_like:
            clean_biz_no = digits

    clean_approval_no: Optional[str] = None
    if raw_approval_no:
        candidate = re.sub(r"[^A-Za-z0-9]", "", raw_approval_no.strip())
        # Standard Korean card approval numbers are 6~12 alphanumeric characters (usually 8 digits)
        if 6 <= len(candidate) <= 12:
            clean_approval_no = candidate

    return clean_biz_no, clean_approval_no


async def parse_receipt_image_with_vision(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    api_key: Optional[str] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> OcrExtractionResult:
    """Extract structured receipt fields via Gemini Multimodal Vision API.

    Falls back to a deterministic offline response when no API key and no transport are supplied.
    """
    resolved_key = api_key if api_key is not None else settings.gemini_api_key
    if not resolved_key and transport is None:
        return OcrExtractionResult(
            biz_reg_no=None,
            merchant_name="영수증가맹점",
            approval_no=None,
            transacted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            total_amount=0,
            line_items=[],
            confidence=0.0,
            raw_response={"mode": "OFFLINE_FALLBACK"},
        )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent"
    )
    prompt = (
        "Extract Korean receipt or online order details as strict JSON with keys: "
        "biz_reg_no, merchant_name, approval_no, transacted_at (YYYY-MM-DD HH:MM:SS), "
        "total_amount (int), supply_value (int), vat_amount (int), "
        "line_items ([{item, qty, price}]), confidence (0.0~1.0)."
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    async with httpx.AsyncClient(transport=transport, timeout=20.0) as client:
        resp = await client.post(url, params={"key": resolved_key or "mock-key"}, json=payload)
        resp.raise_for_status()
        raw_json = resp.json()

    # Support either Gemini candidate wrapper or direct JSON object from mock transport
    if "candidates" in raw_json:
        text_part = raw_json["candidates"][0]["content"]["parts"][0]["text"]
        parsed_data = json.loads(text_part)
    else:
        parsed_data = raw_json

    parsed_data["raw_response"] = raw_json
    return OcrExtractionResult(**parsed_data)


async def process_and_link_receipt(
    cursor: sqlite3.Cursor,
    file_bytes: bytes,
    original_file_name: str = "receipt.jpg",
    source_channel: str = "TELEGRAM_BOT",
    explicit_tx_id: Optional[int] = None,
    ocr_override: Optional[OcrExtractionResult] = None,
    archive_dir: Optional[Path] = None,
    vision_transport: Optional[httpx.AsyncBaseTransport] = None,
    nts_transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Dict[str, Any]:
    """Full receipt processing pipeline:
    1. Compute SHA-256 hash and block duplicate uploads.
    2. Extract receipt fields via Vision LLM (or use ocr_override).
    3. Verify merchant NTS tax status if biz_reg_no is present.
    4. Link to existing card_transactions row (or create/reconcile).
    5. Generate normalized archive filename and save to disk + receipt_ocr_metadata.
    """
    file_hash = compute_sha256_hex(file_bytes)
    existing_receipt_id = check_duplicate_receipt_hash(cursor, file_hash)
    if existing_receipt_id is not None:
        raise DuplicateReceiptError(file_hash, existing_receipt_id)

    ocr_res = ocr_override or await parse_receipt_image_with_vision(
        file_bytes, transport=vision_transport
    )

    clean_biz_no, clean_approval_no = sanitize_ocr_identifiers(
        ocr_res.biz_reg_no, ocr_res.approval_no
    )
    nts_status = None
    if clean_biz_no:
        nts_status = await get_or_verify_merchant(
            cursor,
            biz_reg_no=clean_biz_no,
            merchant_name=ocr_res.merchant_name,
            transport=nts_transport,
        )

    amount_mismatch = False
    primary_item_name = summarize_line_items(ocr_res.line_items)

    if explicit_tx_id is not None:
        cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (explicit_tx_id,))
        tx_row = cursor.fetchone()
        if tx_row is None:
            raise ValueError(f"Transaction tx_id={explicit_tx_id} not found.")
        matched_tx_id = explicit_tx_id
        match_stage = "EXPLICIT_TX_ID"
        if ocr_res.total_amount > 0 and tx_row["total_amount"] != ocr_res.total_amount:
            amount_mismatch = True
        cursor.execute(
            """
            UPDATE card_transactions
            SET biz_reg_no = COALESCE(?, biz_reg_no),
                approval_no = COALESCE(?, approval_no),
                counterparty_info = COALESCE(counterparty_info, ?),
                reconcile_status = CASE
                    WHEN reconcile_status = 'SMS_ONLY' THEN 'OCR_MATCHED'
                    ELSE reconcile_status
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE tx_id = ?
            """,
            (clean_biz_no, clean_approval_no, primary_item_name, matched_tx_id),
        )
    else:
        rec_item = ReconcileItem(
            card_last4="0000",
            approval_no=clean_approval_no,
            transacted_at=ocr_res.transacted_at,
            merchant_name=ocr_res.merchant_name,
            biz_reg_no=clean_biz_no,
            total_amount=ocr_res.total_amount,
            tax_type_code=nts_status.tax_type_code if nts_status else "GENERAL",
            biz_status=nts_status.biz_status if nts_status else "ACTIVE",
            reconcile_source="OCR_MATCHED",
        )
        rec_outcome = reconcile_single_item(cursor, rec_item)
        matched_tx_id = rec_outcome["tx_id"]
        match_stage = rec_outcome["match_stage"]
        if primary_item_name:
            cursor.execute(
                """
                UPDATE card_transactions
                SET counterparty_info = COALESCE(counterparty_info, ?)
                WHERE tx_id = ?
                """,
                (primary_item_name, matched_tx_id),
            )

    # Apply item-based account inference for PG/unclassified rows if line items indicate specific account
    if primary_item_name:
        cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (matched_tx_id,))
        curr_tx = cursor.fetchone()
        if curr_tx and (
            curr_tx["account_code"] in ("999", "830")
            or curr_tx["vat_deduction_type"] in ("PENDING", "PENDING_CHECK")
        ):
            inferred = _detect_memo_account_override(primary_item_name)
            if inferred:
                acc_code, acc_name, force_non_ded, reason = inferred
                # For PG transactions already classified as 830 (consumables), only upgrade to
                # 212 (equipment) when total_amount >= 300,000 KRW or '비품' is explicitly stated.
                should_apply = True
                if (
                    curr_tx["account_code"] == "830"
                    and acc_code == "212"
                    and int(curr_tx["total_amount"]) < 300_000
                    and "비품" not in primary_item_name
                ):
                    should_apply = False

                if should_apply:
                    new_ded = "NON_DEDUCTIBLE" if force_non_ded else "DEDUCTIBLE"
                    inf_supply, inf_vat = calculate_supply_and_vat(
                        int(curr_tx["total_amount"]), new_ded
                    )
                    cursor.execute(
                        """
                        UPDATE card_transactions
                        SET account_code = ?,
                            account_name = ?,
                            vat_deduction_type = ?,
                            non_deduct_reason = ?,
                            supply_value = ?,
                            vat_amount = ?
                        WHERE tx_id = ?
                        """,
                        (
                            acc_code,
                            acc_name,
                            new_ded,
                            reason,
                            inf_supply,
                            inf_vat,
                            matched_tx_id,
                        ),
                    )
            elif curr_tx["vat_deduction_type"] in ("PENDING", "PENDING_CHECK"):
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET vat_deduction_type = 'DEDUCTIBLE'
                    WHERE tx_id = ?
                    """,
                    (matched_tx_id,),
                )

    if nts_status:
        apply_merchant_tax_status_to_ledger(cursor, nts_status)

    # Respect explicit supply_value and vat_amount from receipt (e.g. mixed tax/exempt or duty-free book)
    if (
        ocr_res.supply_value is not None
        and ocr_res.vat_amount is not None
        and ocr_res.total_amount > 0
        and not amount_mismatch
        and (ocr_res.supply_value + ocr_res.vat_amount == ocr_res.total_amount)
    ):
        cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (matched_tx_id,))
        tx_check = cursor.fetchone()
        if tx_check:
            if ocr_res.vat_amount == 0:
                new_vat_type = (
                    "EXEMPT"
                    if tx_check["vat_deduction_type"] in ("DEDUCTIBLE", "PENDING_CHECK")
                    else tx_check["vat_deduction_type"]
                )
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET supply_value = ?,
                        vat_amount = 0,
                        vat_deduction_type = ?
                    WHERE tx_id = ?
                    """,
                    (ocr_res.supply_value, new_vat_type, matched_tx_id),
                )
            elif tx_check["vat_deduction_type"] in ("DEDUCTIBLE", "PENDING_CHECK"):
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET supply_value = ?,
                        vat_amount = ?
                    WHERE tx_id = ?
                    """,
                    (ocr_res.supply_value, ocr_res.vat_amount, matched_tx_id),
                )

    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (matched_tx_id,))
    final_tx = cursor.fetchone()

    item_summary = final_tx["counterparty_info"] or primary_item_name
    archived_file_name = build_archived_filename(
        transacted_at=final_tx["transacted_at"],
        merchant_name=final_tx["raw_merchant_name"],
        account_name=final_tx["account_name"],
        total_amount=final_tx["total_amount"],
        item_summary=item_summary,
        original_file_name=original_file_name,
    )

    orig_path, _ = save_receipt_files(
        file_bytes,
        original_file_name=original_file_name,
        archived_file_name=archived_file_name,
        archive_dir=archive_dir,
    )

    line_items_str = json.dumps(
        [li.model_dump() for li in ocr_res.line_items], ensure_ascii=False
    )
    raw_ocr_str = json.dumps(ocr_res.raw_response, ensure_ascii=False)

    cursor.execute(
        """
        INSERT INTO receipt_ocr_metadata (
            tx_id, source_channel, original_file_path, archived_file_name,
            file_hash_sha256, ocr_biz_reg_no, ocr_total_amount,
            line_items_json, raw_ocr_response, ocr_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            matched_tx_id,
            source_channel,
            orig_path,
            archived_file_name,
            file_hash,
            clean_biz_no,
            ocr_res.total_amount,
            line_items_str,
            raw_ocr_str,
            ocr_res.confidence,
        ),
    )
    receipt_id = cursor.lastrowid

    return {
        "receipt_id": receipt_id,
        "tx_id": matched_tx_id,
        "match_stage": match_stage,
        "archived_file_name": archived_file_name,
        "file_hash_sha256": file_hash,
        "amount_mismatch": amount_mismatch,
        "ocr_biz_reg_no": clean_biz_no,
        "is_valid_biz_checksum": validate_biz_reg_no_checksum(clean_biz_no) if clean_biz_no else False,
        "account_code": final_tx["account_code"],
        "account_name": final_tx["account_name"],
        "vat_deduction_type": final_tx["vat_deduction_type"],
    }
