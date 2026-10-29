"""Unit and integration tests for Phase 3: Telegram Bot, Vision OCR, and SHA-256 Evidence Archiving."""

import base64
import json

import httpx
import pytest

from sololedger.archiver import (
    DuplicateReceiptError,
    build_archived_filename,
)
from sololedger.models import OcrExtractionResult, OcrLineItem, ParsedSmsResult
from sololedger.notifier import build_memo_prompt_payload
from sololedger.ocr_engine import (
    parse_receipt_image_with_vision,
    process_and_link_receipt,
)


def test_build_normalized_archived_filename():
    fname = build_archived_filename(
        transacted_at="2026-10-20 13:20:00",
        merchant_name="(주)쿠팡",
        account_name="소모품비",
        total_amount=45000,
        item_summary="모니터암",
        original_file_name="order_capture.jpg",
    )
    assert fname == "20261020_쿠팡_소모품비_모니터암_45000.jpg"

    fname_no_item = build_archived_filename(
        transacted_at="2026-10-20 12:10:00",
        merchant_name="스타벅스 역삼점",
        account_name="복리후생비",
        total_amount=11000,
        item_summary=None,
        original_file_name="receipt.png",
    )
    assert fname_no_item == "20261020_스타벅스역삼점_복리후생비_11000.png"


def test_build_memo_prompt_inline_keyboard_for_dining():
    parsed = ParsedSmsResult(
        card_last4="1234",
        transacted_at="2026-10-20 12:30:00",
        total_amount=15000,
        raw_merchant_name="스타벅스 강남점",
        is_cancel=False,
    )
    rule_res = {
        "account_code": "811",
        "account_name": "복리후생비",
        "vat_deduction_type": "DEDUCTIBLE",
    }
    payload = build_memo_prompt_payload("99999", 42, parsed, rule_res)
    kb = payload["reply_markup"]["inline_keyboard"][0]
    assert len(kb) == 2
    assert kb[0]["callback_data"] == "meal_solo:42"
    assert kb[1]["callback_data"] == "meal_meeting:42"


@pytest.mark.asyncio
async def test_parse_receipt_image_with_vision_mock():
    gemini_mock_response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "biz_reg_no": "120-88-00767",
                                    "merchant_name": "(주)쿠팡",
                                    "approval_no": "11223344",
                                    "transacted_at": "2026-10-20 13:21:00",
                                    "total_amount": 45000,
                                    "supply_value": 40909,
                                    "vat_amount": 4091,
                                    "line_items": [{"item": "모니터암", "qty": 1, "price": 45000}],
                                    "confidence": 0.98,
                                },
                                ensure_ascii=False,
                            )
                        }
                    ]
                }
            }
        ]
    }

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=gemini_mock_response))
    res = await parse_receipt_image_with_vision(
        b"fake-jpeg-bytes", api_key="test-gemini-key", transport=transport
    )
    assert res.biz_reg_no == "120-88-00767"
    assert res.total_amount == 45000
    assert res.line_items[0].item == "모니터암"
    assert res.confidence == 0.98


@pytest.mark.asyncio
async def test_process_receipt_links_to_sms_and_blocks_sha256_duplicate(db_conn, tmp_path):
    cur = db_conn.cursor()
    # 1. Insert SMS_ONLY draft transaction
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount,
            account_code, account_name, vat_deduction_type, reconcile_status
        ) VALUES (
            '12*4', '2026-10-20 13:20:00', '(주)쿠팡',
            45000, 40909, 4091, '830', '소모품비', 'DEDUCTIBLE', 'SMS_ONLY'
        )
        """
    )
    sms_tx_id = cur.lastrowid

    ocr_data = OcrExtractionResult(
        biz_reg_no="120-88-00767",
        merchant_name="(주)쿠팡",
        approval_no="55667788",
        transacted_at="2026-10-20 13:22:00",
        total_amount=45000,
        line_items=[OcrLineItem(item="모니터암", qty=1, price=45000)],
        confidence=0.99,
    )
    image_bytes = b"unique-coupang-receipt-image-bytes-001"

    outcome = await process_and_link_receipt(
        cur,
        file_bytes=image_bytes,
        original_file_name="coupang_order.jpg",
        ocr_override=ocr_data,
        archive_dir=tmp_path / "archive",
    )
    assert outcome["tx_id"] == sms_tx_id
    assert outcome["match_stage"] == "STAGE_2_AMOUNT_TIME_WINDOW"
    assert outcome["archived_file_name"] == "20261020_쿠팡_소모품비_모니터암_45000.jpg"

    # Verify duplicate upload of identical image bytes raises DuplicateReceiptError
    with pytest.raises(DuplicateReceiptError) as exc_info:
        await process_and_link_receipt(
            cur,
            file_bytes=image_bytes,
            original_file_name="coupang_order_copy.jpg",
            ocr_override=ocr_data,
            archive_dir=tmp_path / "archive",
        )
    assert exc_info.value.existing_receipt_id == outcome["receipt_id"]


def test_telegram_webhook_callbacks_and_memo_reply_flow(client):
    headers = {"X-API-Key": "test-secret-key"}

    # 1. Create a cafe expense via SMS webhook (classified as 811 복리후생비 initially)
    sms_resp = client.post(
        "/api/v1/webhooks/sms",
        json={
            "raw_message": "[Web발신] 신한법인승인 12*4 10/20 12:15 28,000원 일시불 스타벅스강남점",
            "received_at": "2026-10-20T12:15:00",
        },
        headers=headers,
    )
    assert sms_resp.status_code == 200
    tx_id = sms_resp.json()["tx_id"]
    assert sms_resp.json()["account_code"] == "811"

    # 2. Press [거래처 미팅 (813 기업업무추진비·불공제)] inline button in Telegram
    cb_resp = client.post(
        "/api/v1/webhooks/telegram",
        json={"callback_query": {"id": "cb1", "data": f"meal_meeting:{tx_id}"}},
        headers=headers,
    )
    assert cb_resp.status_code == 200
    cb_data = cb_resp.json()
    assert cb_data["account_code"] == "813"
    assert cb_data["account_name"] == "기업업무추진비"
    assert cb_data["vat_deduction_type"] == "NON_DEDUCTIBLE"
    assert cb_data["non_deduct_reason"] == "ENTERTAINMENT"

    # 3. Reply with attendee names
    reply_resp = client.post(
        "/api/v1/webhooks/telegram",
        json={
            "message": {
                "reply_to_message": {"text": f"[법인카드 실시간 전표 생성 알림]\n- 전표번호: #{tx_id}"},
                "text": "A사 김철수 대표 기술협업 미팅",
            }
        },
        headers=headers,
    )
    assert reply_resp.status_code == 200
    assert reply_resp.json()["counterparty_info"] == "A사 김철수 대표 기술협업 미팅"


def test_receipt_upload_api_and_memo_filename_refresh(client):
    headers = {"X-API-Key": "test-secret-key"}

    # 1. Create Coupang SMS transaction
    r_sms = client.post(
        "/api/v1/webhooks/sms",
        json={
            "raw_message": "[Web발신] 신한법인승인 12*4 10/20 15:00 89,000원 일시불 (주)쿠팡",
            "received_at": "2026-10-20T15:00:00",
        },
        headers=headers,
    )
    tx_id = r_sms.json()["tx_id"]

    # 2. Upload receipt via POST /api/v1/receipts/upload
    b64_img = base64.b64encode(b"sample-receipt-binary-content-89000").decode("ascii")
    upload_payload = {
        "file_name": "coupang_89000.jpg",
        "file_content_b64": b64_img,
        "source_channel": "TELEGRAM_BOT",
        "ocr_override": {
            "biz_reg_no": "120-88-00767",
            "merchant_name": "(주)쿠팡",
            "approval_no": "88990011",
            "transacted_at": "2026-10-20 15:02:00",
            "total_amount": 89000,
            "line_items": [{"item": "무선키보드", "qty": 1, "price": 89000}],
            "confidence": 0.97,
        },
    }
    r_up = client.post("/api/v1/receipts/upload", json=upload_payload, headers=headers)
    assert r_up.status_code == 200
    up_body = r_up.json()
    assert up_body["tx_id"] == tx_id
    assert up_body["archived_file_name"] == "20261020_쿠팡_소모품비_무선키보드_89000.jpg"

    # 3. Duplicate upload returns HTTP 409 Conflict
    r_dup = client.post("/api/v1/receipts/upload", json=upload_payload, headers=headers)
    assert r_dup.status_code == 409

    # 4. Send Telegram memo reclassifying item as '비품' -> refreshes archived_file_name
    r_memo = client.post(
        "/api/v1/webhooks/telegram",
        json={"message": {"text": f"#{tx_id} 연구소용 무선키보드 비품"}},
        headers=headers,
    )
    assert r_memo.status_code == 200
    memo_body = r_memo.json()
    assert memo_body["account_code"] == "212"
    assert memo_body["account_name"] == "비품"
    assert memo_body["archived_file_name"] == "20261020_쿠팡_비품_연구소용무선키보드비품_89000.jpg"

    # 5. Verify GET /api/v1/receipts
    r_list = client.get("/api/v1/receipts", headers=headers)
    assert r_list.status_code == 200
    assert r_list.json()["count"] == 1
