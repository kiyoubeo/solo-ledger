"""Golden Benchmark Test Suite for Korean Corporate Card Receipt Edge-Case Dataset."""

import base64
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from sololedger.config import PROJECT_ROOT
from sololedger.db import get_connection

DATASET_PATH = PROJECT_ROOT / "tests" / "fixtures" / "receipt_dataset.json"


def _load_receipt_dataset() -> List[Dict[str, Any]]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "case",
    _load_receipt_dataset(),
    ids=lambda c: c["case_id"],
)
def test_korean_receipt_edge_case_dataset(case: Dict[str, Any], client, test_db_path):
    """Verify each Korean receipt edge case from receipt_dataset.json end-to-end."""
    headers = {"X-API-Key": "test-secret-key"}
    case_id = case["case_id"]

    explicit_tx_id = None
    if case.get("pre_sms"):
        sms_res = client.post(
            "/api/v1/webhooks/sms",
            headers=headers,
            json={
                "sender": "15880000",
                "raw_message": case["pre_sms"]["raw_message"],
                "received_at": case["pre_sms"]["received_at"],
            },
        )
        assert sms_res.status_code == 200
        if case.get("use_explicit_tx_id"):
            explicit_tx_id = sms_res.json()["tx_id"]

    img_bytes = f"SYNTHETIC-RECEIPT-IMAGE-{case_id}".encode("utf-8")
    upload_payload: Dict[str, Any] = {
        "file_name": f"{case_id.lower()}.jpg",
        "file_content_b64": base64.b64encode(img_bytes).decode("ascii"),
        "source_channel": "TELEGRAM_BOT",
        "ocr_override": case["ocr_payload"],
    }
    if explicit_tx_id is not None:
        upload_payload["tx_id"] = explicit_tx_id

    up_res = client.post(
        "/api/v1/receipts/upload",
        headers=headers,
        json=upload_payload,
    )
    assert up_res.status_code == 200, f"{case_id} failed: {up_res.text}"
    body = up_res.json()
    expected = case["expected"]

    assert body["match_stage"] == expected["match_stage"]
    assert body["account_code"] == expected["account_code"]
    assert body["account_name"] == expected["account_name"]
    assert body["vat_deduction_type"] == expected["vat_deduction_type"]
    assert body["archived_file_name"] == expected["archived_file_name"]
    assert body["amount_mismatch"] is expected["amount_mismatch"]

    if "ocr_biz_reg_no" in expected:
        assert body["ocr_biz_reg_no"] == expected["ocr_biz_reg_no"]

    # Verify DB ledger state (supply_value, vat_amount, counterparty_info)
    conn = get_connection(test_db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (body["tx_id"],))
        tx_row = cur.fetchone()
        assert tx_row is not None
        assert int(tx_row["supply_value"]) == expected["supply_value"]
        assert int(tx_row["vat_amount"]) == expected["vat_amount"]
        assert tx_row["counterparty_info"] == expected["counterparty_info"]
    finally:
        conn.close()


def test_receipt_dataset_batch_tax_pack_integrity(client):
    """Run all 8 receipt dataset cases in a single month and verify Tax Pack export totals."""
    headers = {"X-API-Key": "test-secret-key"}
    cases = _load_receipt_dataset()

    for case in cases:
        case_id = case["case_id"]
        explicit_tx_id = None
        if case.get("pre_sms"):
            sms_res = client.post(
                "/api/v1/webhooks/sms",
                headers=headers,
                json={
                    "sender": "15880000",
                    "raw_message": case["pre_sms"]["raw_message"],
                    "received_at": case["pre_sms"]["received_at"],
                },
            )
            if case.get("use_explicit_tx_id"):
                explicit_tx_id = sms_res.json()["tx_id"]

        img_bytes = f"BATCH-RECEIPT-{case_id}".encode("utf-8")
        payload: Dict[str, Any] = {
            "file_name": f"{case_id.lower()}.jpg",
            "file_content_b64": base64.b64encode(img_bytes).decode("ascii"),
            "source_channel": "WEB_UPLOAD",
            "ocr_override": case["ocr_payload"],
        }
        if explicit_tx_id is not None:
            payload["tx_id"] = explicit_tx_id

        res = client.post("/api/v1/receipts/upload", headers=headers, json=payload)
        assert res.status_code == 200

    export_res = client.post(
        "/api/v1/exports/tax-pack",
        headers=headers,
        json={"year_month": "202610"},
    )
    assert export_res.status_code == 200
    exp_data = export_res.json()
    assert exp_data["exported_count"] == len(cases)
    assert exp_data["bundled_receipts_count"] == len(cases)
    # Deductible VAT sum across the 5 deductible cases:
    # CASE_01 (3500) + CASE_02 (30000) + CASE_04 (3000) + CASE_05 (2500) + CASE_06 (4091) = 43,091 KRW
    assert exp_data["deductible_vat_amount"] == 3500 + 30000 + 3000 + 2500 + 4091
    assert Path(exp_data["zip_file_path"]).exists()
