"""Phase 5 End-to-End (E2E) Full Lifecycle, Web Dashboard, CLI, and Deployment Verification."""

import base64
import zipfile
from pathlib import Path

from sololedger.cli import run_cli
from sololedger.config import PROJECT_ROOT


def test_full_sololedger_lifecycle_e2e(client):
    """Verify full lifecycle: SMS -> Telegram Bot/OCR -> NTS -> Hometax CSV -> Tax Pack -> Dashboard."""
    headers = {"X-API-Key": "test-secret-key"}

    # 1. Real-time SMS Webhook Reception (3 transactions: Coupang PG, Starbucks Meal, Speedmate Car)
    sms_coupang = client.post(
        "/api/v1/webhooks/sms",
        headers=headers,
        json={
            "sender": "15447200",
            "raw_message": "[Web발신] 신한법인(9876) 승인 45,000원 일시불 10/20 14:22 쿠팡(주) 승인번호:11223344",
            "received_at": "2026-10-20T14:22:05",
        },
    )
    assert sms_coupang.status_code == 200
    tx_coupang_id = sms_coupang.json()["tx_id"]
    assert sms_coupang.json()["account_code"] == "830"  # 소모품비

    sms_starbucks = client.post(
        "/api/v1/webhooks/sms",
        headers=headers,
        json={
            "sender": "15888900",
            "raw_message": "[Web발신] 현대카드(1234) 승인 22,000원 일시불 10/21 12:30 스타벅스역삼점 승인번호:55667788",
            "received_at": "2026-10-21T12:30:10",
        },
    )
    assert sms_starbucks.status_code == 200
    tx_starbucks_id = sms_starbucks.json()["tx_id"]
    assert sms_starbucks.json()["account_code"] == "811"  # 초기 복리후생비

    sms_vehicle = client.post(
        "/api/v1/webhooks/sms",
        headers=headers,
        json={
            "sender": "15888900",
            "raw_message": (
                "[Web발신] 현대카드(1234) 승인 110,000원 일시불 10/22 18:10 "
                "SK에너지주유소 승인번호:99001122"
            ),
            "received_at": "2026-10-22T18:10:00",
        },
    )
    assert sms_vehicle.status_code == 200
    assert sms_vehicle.json()["account_code"] == "822"
    assert sms_vehicle.json()["is_vehicle_expense"] is True

    # 2. Telegram Bot Callback: Convert Starbucks from Solo Meal (811) -> Client Meeting (813 Non-deductible)
    tg_callback = client.post(
        "/api/v1/webhooks/telegram",
        headers=headers,
        json={
            "callback_query": {
                "id": "cb-e2e-1",
                "data": f"meal_meeting:{tx_starbucks_id}",
            }
        },
    )
    assert tg_callback.status_code == 200
    assert tg_callback.json()["account_code"] == "813"
    assert tg_callback.json()["vat_deduction_type"] == "NON_DEDUCTIBLE"

    # Telegram Bot Text Reply: Record meeting attendees for Starbucks
    tg_reply = client.post(
        "/api/v1/webhooks/telegram",
        headers=headers,
        json={
            "message": {
                "text": "B사 박영희 이사 프로젝트 미팅",
                "reply_to_message": {"text": f"전표번호 #{tx_starbucks_id} 참석자 입력"},
            }
        },
    )
    assert tg_reply.status_code == 200
    assert tg_reply.json()["counterparty_info"] == "B사 박영희 이사 프로젝트 미팅"

    # 3. Vision OCR Receipt Upload for Coupang transaction + SHA-256 Deduplication
    receipt_bytes = b"E2E-COUPANG-RECEIPT-IMAGE-20261020"
    upload_res = client.post(
        "/api/v1/receipts/upload",
        headers=headers,
        json={
            "file_name": "coupang_order.png",
            "file_content_b64": base64.b64encode(receipt_bytes).decode("ascii"),
            "source_channel": "TELEGRAM_BOT",
            "tx_id": tx_coupang_id,
            "ocr_override": {
                "biz_reg_no": "120-88-00767",
                "merchant_name": "쿠팡(주)",
                "approval_no": "11223344",
                "transacted_at": "2026-10-20 14:22:00",
                "total_amount": 45000,
                "supply_value": 40909,
                "vat_amount": 4091,
                "line_items": [{"item": "모니터암", "qty": 1, "price": 45000}],
                "confidence": 0.99,
            },
        },
    )
    assert upload_res.status_code == 200
    assert upload_res.json()["archived_file_name"] == "20261020_쿠팡_소모품비_모니터암_45000.png"

    # 4. NTS Merchant Verification & 30-day Cache Check
    nts_res = client.post(
        "/api/v1/merchants/verify",
        headers=headers,
        json={
            "biz_reg_no": "120-88-00767",
            "merchant_name": "쿠팡(주)",
        },
    )
    assert nts_res.status_code == 200
    assert nts_res.json()["merchant"]["biz_reg_no"] == "1208800767"

    # 5. Hometax CSV Batch Import (reconciles existing & inserts 1 unclassified new row)
    csv_text = (
        "거래일시,카드번호,승인번호,가맹점명,사업자등록번호,공급가액,부가세,합계금액,과세유형\n"
        "2026-10-20 14:22:00,9876,11223344,쿠팡(주),120-88-00767,40909,4091,45000,일반과세자\n"
        "2026-10-25 16:00:00,1234,77889900,알수없는문구점,214-85-39210,9091,909,10000,일반과세자\n"
    )
    imp_res = client.post(
        "/api/v1/imports/hometax-csv",
        headers=headers,
        json={"csv_text": csv_text},
    )
    assert imp_res.status_code == 200
    assert imp_res.json()["total_rows"] == 2
    assert imp_res.json()["stage_breakdown"]["STAGE_1_APPROVAL_NO"] == 1
    assert imp_res.json()["stage_breakdown"]["STAGE_3_FORCE_INSERT"] == 1

    # 6. Dashboard Summary & HTML Page Check (before resolving unclassified item)
    dash_sum = client.get(
        "/api/v1/dashboard/summary?year_month=202610&year=2026",
        headers=headers,
    )
    assert dash_sum.status_code == 200
    sum_data = dash_sum.json()
    assert sum_data["total_tx_count"] == 4
    assert sum_data["total_expenditure"] == 45000 + 22000 + 110000 + 10000
    assert sum_data["pending_action_count"] >= 1
    assert sum_data["vehicle_limit"]["total_used"] == 110000

    dash_html = client.get("/dashboard?year_month=202610&year=2026")
    assert dash_html.status_code == 200
    assert "SoloLedger 경비·세무증빙 대시보드" in dash_html.text
    assert "알수없는문구점" in dash_html.text

    # 7. Inline Patch of Unclassified Transaction via PATCH /api/v1/transactions/{tx_id}
    unclassified_tx = [
        t for t in sum_data["transactions"] if t["raw_merchant_name"] == "알수없는문구점"
    ][0]
    patch_res = client.patch(
        f"/api/v1/transactions/{unclassified_tx['tx_id']}",
        headers=headers,
        json={
            "account_code": "830",
            "vat_deduction_type": "DEDUCTIBLE",
            "counterparty_info": "법인 인감도장 패드 및 복사용지",
        },
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["transaction"]["account_code"] == "830"
    assert patch_res.json()["transaction"]["account_name"] == "소모품비"
    assert patch_res.json()["transaction"]["counterparty_info"] == "법인 인감도장 패드 및 복사용지"

    # 8. Export Tax Accountant Package (CSV + ZIP)
    export_res = client.post(
        "/api/v1/exports/tax-pack",
        headers=headers,
        json={"year_month": "202610"},
    )
    assert export_res.status_code == 200
    exp_body = export_res.json()
    assert exp_body["exported_count"] == 4
    assert exp_body["bundled_receipts_count"] == 1
    zip_path = Path(exp_body["zip_file_path"])
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        assert "SoloLedger_기장원장_202610.csv" in zf.namelist()
        assert "receipts/20261020_쿠팡_소모품비_모니터암_45000.png" in zf.namelist()


def test_operational_cli_commands(test_db_path, tmp_path):
    """Verify CLI subcommands: init-db, import-csv, status, and export-pack."""
    init_res = run_cli(["init-db"])
    assert init_res["status"] == "initialized"

    sample_csv = tmp_path / "sample_hometax.csv"
    sample_csv.write_text(
        "거래일시,카드번호,승인번호,가맹점명,사업자등록번호,공급가액,부가세,합계금액,과세유형\n"
        "2026-10-28 11:00:00,5678,33445566,교보문고광화문점,102-81-11670,18000,0,18000,면세사업자\n",
        encoding="utf-8",
    )

    imp_res = run_cli(["import-csv", str(sample_csv)])
    assert imp_res["command"] == "import-csv"
    assert imp_res["total_rows"] == 1
    assert imp_res["stage_breakdown"]["STAGE_3_FORCE_INSERT"] == 1

    stat_res = run_cli(["status", "--year-month", "202610", "--year", "2026"])
    assert stat_res["command"] == "status"
    assert stat_res["total_ledger_rows"] == 1
    assert stat_res["monthly_summary"]["total_expenditure"] == 18000

    exp_res = run_cli(["export-pack", "202610"])
    assert exp_res["command"] == "export-pack"
    assert exp_res["exported_count"] == 1
    assert Path(exp_res["zip_file_path"]).exists()

    seed_res = run_cli(["seed-demo", "--year-month", "202610"])
    assert seed_res["command"] == "seed-demo"
    assert seed_res["inserted_count"] == 6

    rst_res = run_cli(["reset-db"])
    assert rst_res["command"] == "reset-db"
    assert rst_res["deleted_transactions"] == 7


def test_docker_deployment_manifests_exist():
    """Verify Dockerfile and docker-compose.yml exist and configure SoloLedger properly."""
    dockerfile = PROJECT_ROOT / "Dockerfile"
    compose = PROJECT_ROOT / "docker-compose.yml"
    assert dockerfile.exists()
    assert compose.exists()

    df_text = dockerfile.read_text(encoding="utf-8")
    dc_text = compose.read_text(encoding="utf-8")
    assert "sololedger.api:app" in df_text
    assert "sololedger-api:" in dc_text
    assert "SOLOLEDGER_DB_PATH" in dc_text
