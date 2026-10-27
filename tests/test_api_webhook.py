"""End-to-end integration tests for FastAPI SMS Webhook and Reconciliation endpoints."""


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "sololedger-api"
    assert data["active_tax_rules"] == 4


def test_webhook_unauthorized_rejected(client):
    resp = client.post(
        "/api/v1/webhooks/sms",
        json={"raw_message": "[Web발신] 신한법인승인 12*4 10/20 13:20 45,000원 일시불 (주)쿠팡"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_webhook_records_and_deduplicates_sms(client):
    headers = {"X-API-Key": "test-secret-key"}
    payload = {
        "sender": "15447200",
        "raw_message": "[Web발신] 신한법인승인 12*4 10/20 13:20 45,000원 일시불 (주)쿠팡",
        "received_at": "2026-10-20T13:20:02",
    }

    # First transmission -> recorded
    r1 = client.post("/api/v1/webhooks/sms", json=payload, headers=headers)
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["status"] == "recorded"
    assert body1["merchant"] == "(주)쿠팡"
    assert body1["amount"] == 45000
    assert body1["account_code"] == "830"
    assert body1["account_name"] == "소모품비"

    # Second identical transmission -> duplicate_skipped
    r2 = client.post("/api/v1/webhooks/sms", json=payload, headers=headers)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["status"] == "duplicate_skipped"
    assert body2["tx_id"] == body1["tx_id"]

    # Verify via GET /api/v1/transactions
    r_list = client.get("/api/v1/transactions", headers=headers)
    assert r_list.status_code == 200
    items = r_list.json()["items"]
    assert len(items) == 1
    assert items[0]["raw_merchant_name"] == "(주)쿠팡"


def test_webhook_reconcile_pipeline(client):
    headers = {"SOLOLEDGER_API_KEY": "test-secret-key"}
    sms_payload = {
        "raw_message": (
            "[Web발신]\nKB국민법인(9876)\n김*범님 승인\n"
            "55,000원 일시불\n10/20 14:10\n스피드메이트 대전용산점"
        ),
        "received_at": "2026-10-20T14:10:00",
    }
    r_sms = client.post("/api/v1/webhooks/sms", json=sms_payload, headers=headers)
    assert r_sms.status_code == 200
    tx_id = r_sms.json()["tx_id"]
    assert r_sms.json()["is_vehicle_expense"] is True
    assert r_sms.json()["vat_deduction_type"] == "NON_DEDUCTIBLE"

    # Reconcile with Hometax row within 10-minute window
    reconcile_payload = {
        "items": [
            {
                "card_last4": "9876",
                "approval_no": "44556677",
                "transacted_at": "2026-10-20 14:12:00",
                "merchant_name": "스피드메이트 대전용산점",
                "biz_reg_no": "104-81-12345",
                "total_amount": 55000,
                "tax_type_code": "GENERAL",
                "biz_status": "ACTIVE",
            }
        ]
    }
    r_rec = client.post("/api/v1/reconcile", json=reconcile_payload, headers=headers)
    assert r_rec.status_code == 200
    rec_res = r_rec.json()["results"][0]
    assert rec_res["tx_id"] == tx_id
    assert rec_res["match_stage"] == "STAGE_2_AMOUNT_TIME_WINDOW"
