"""Unit and integration tests for Phase 4: Hometax CSV Importer, Vehicle Limit Tracker, and Tax Pack Exporter."""

import base64
import zipfile
from pathlib import Path

from sololedger.importers import decode_csv_bytes, import_and_reconcile_hometax_csv
from sololedger.vehicle_tracker import get_vehicle_expense_summary


def test_decode_cp949_and_utf8_csv_bytes():
    korean_csv = "거래일시,가맹점명,합계금액\n2026-10-20 12:00:00,스타벅스,11000\n"
    cp949_bytes = korean_csv.encode("cp949")
    utf8_bom_bytes = korean_csv.encode("utf-8-sig")

    assert decode_csv_bytes(cp949_bytes) == korean_csv
    assert decode_csv_bytes(utf8_bom_bytes) == korean_csv


def test_import_hometax_csv_3stage_reconciliation(db_conn):
    cur = db_conn.cursor()
    # Pre-insert two SMS_ONLY drafts:
    # 1) Approval number match with provisional FX amount (28000 -> settled 29500)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '1234', '77889900', '2026-10-20 10:00:00', 'OPENAI',
            28000, 25455, 2545, '831', '지급수수료', 'DEDUCTIBLE', 'SMS_ONLY'
        )
        """
    )
    tx1_id = cur.lastrowid

    # 2) Time window match (±10 min) without approval_no
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '9876', NULL, '2026-10-20 13:20:00', '(주)쿠팡',
            45000, 40909, 4091, '830', '소모품비', 'DEDUCTIBLE', 'SMS_ONLY'
        )
        """
    )
    tx2_id = cur.lastrowid

    sample_csv = (
        "거래일시,카드번호,승인번호,가맹점명,사업자등록번호,공급가액,부가세,합계금액,과세유형,거래구분\n"
        "2026-10-20 10:01:00,1234-5678-9012-1234,77889900,"
        "OPENAI CHATGPT,101-86-00001,26818,2682,29500,일반과세자,승인\n"
        "2026-10-20 13:23:00,9999-8888-7777-9876,55443322,(주)쿠팡,120-88-00767,40909,4091,45000,일반과세자,승인\n"
        "2026-10-21 19:00:00,9999-8888-7777-9876,66778899,동네간이식당,201-81-21515,20000,2000,22000,간이과세자,승인\n"
    )

    outcome = import_and_reconcile_hometax_csv(cur, sample_csv)
    assert outcome["total_rows"] == 3
    assert outcome["stage_breakdown"]["STAGE_1_APPROVAL_NO"] == 1
    assert outcome["stage_breakdown"]["STAGE_2_AMOUNT_TIME_WINDOW"] == 1
    assert outcome["stage_breakdown"]["STAGE_3_FORCE_INSERT"] == 1

    # Verify FX settled amount updated on tx1
    cur.execute("SELECT total_amount, reconcile_status FROM card_transactions WHERE tx_id = ?", (tx1_id,))
    r1 = cur.fetchone()
    assert r1["total_amount"] == 29500
    assert r1["reconcile_status"] == "HOMETAX_VERIFIED"

    # Verify approval_no and biz_reg_no filled on tx2
    cur.execute("SELECT approval_no, biz_reg_no FROM card_transactions WHERE tx_id = ?", (tx2_id,))
    r2 = cur.fetchone()
    assert r2["approval_no"] == "55443322"
    assert r2["biz_reg_no"] == "1208800767"


def test_vehicle_expense_annual_15m_limit_tracker(db_conn):
    cur = db_conn.cursor()
    # Insert 12,500,000 KRW of vehicle expenses -> 83.33% (WARNING_80PCT)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, is_vehicle_expense, reconcile_status
        ) VALUES (
            '1234', '2026-05-10 10:00:00', '스피드메이트 정비',
            12500000, 11363636, 1136364, '822', '차량유지비', 'NON_DEDUCTIBLE', 1, 'HOMETAX_VERIFIED'
        )
        """
    )
    summary_warn = get_vehicle_expense_summary(cur, 2026)
    assert summary_warn["total_used"] == 12500000
    assert summary_warn["remaining_limit"] == 2500000
    assert summary_warn["warning_level"] == "WARNING_80PCT"

    # Add another 3,000,000 KRW -> 15,500,000 KRW (LIMIT_EXCEEDED)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, is_vehicle_expense, reconcile_status
        ) VALUES (
            '1234', '2026-10-10 10:00:00', 'SK에너지 주유소',
            3000000, 2727273, 272727, '822', '차량유지비', 'NON_DEDUCTIBLE', 1, 'HOMETAX_VERIFIED'
        )
        """
    )
    summary_exceeded = get_vehicle_expense_summary(cur, 2026)
    assert summary_exceeded["total_used"] == 15500000
    assert summary_exceeded["remaining_limit"] == 0
    assert summary_exceeded["warning_level"] == "LIMIT_EXCEEDED"


def test_export_tax_pack_csv_and_zip_and_prevent_duplicate_export(client):
    headers = {"X-API-Key": "test-secret-key"}

    # 1. Import Hometax CSV via API (CP949 encoded)
    csv_text = (
        "거래일시,카드번호,승인번호,가맹점명,사업자등록번호,공급가액,부가세,합계금액,과세유형\n"
        "2026-10-20 15:00:00,1234,88990011,(주)쿠팡,120-88-00767,40909,4091,45000,일반과세자\n"
        "2026-10-22 11:00:00,9876,99001122,스피드메이트,104-81-12345,50000,5000,55000,일반과세자\n"
    )
    b64_csv = base64.b64encode(csv_text.encode("cp949")).decode("ascii")
    r_imp = client.post(
        "/api/v1/imports/hometax-csv",
        json={"csv_content_b64": b64_csv},
        headers=headers,
    )
    assert r_imp.status_code == 200
    assert r_imp.json()["total_rows"] == 2
    tx1_id = r_imp.json()["results"][0]["tx_id"]

    # 2. Attach receipt to tx1
    r_up = client.post(
        "/api/v1/receipts/upload",
        json={
            "file_name": "coupang.jpg",
            "file_content_b64": base64.b64encode(b"receipt-bytes-202610").decode("ascii"),
            "tx_id": tx1_id,
            "ocr_override": {
                "biz_reg_no": "120-88-00767",
                "merchant_name": "(주)쿠팡",
                "transacted_at": "2026-10-20 15:00:00",
                "total_amount": 45000,
                "line_items": [{"item": "모니터암", "qty": 1, "price": 45000}],
            },
        },
        headers=headers,
    )
    assert r_up.status_code == 200

    # 3. Check vehicle limit analytics endpoint
    r_veh = client.get("/api/v1/analytics/vehicle-limit?year=2026", headers=headers)
    assert r_veh.status_code == 200
    assert r_veh.json()["total_used"] == 55000

    # 4. Generate Tax Pack for 202610
    r_exp1 = client.post(
        "/api/v1/exports/tax-pack",
        json={"year_month": "202610"},
        headers=headers,
    )
    assert r_exp1.status_code == 200
    exp_body = r_exp1.json()
    assert exp_body["exported_count"] == 2
    assert exp_body["bundled_receipts_count"] == 1
    assert exp_body["total_amount"] == 100000
    assert exp_body["deductible_vat_amount"] == 4091  # Only Coupang is deductible; Speedmate is NON_DEDUCTIBLE

    zip_path = Path(exp_body["zip_file_path"])
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        assert "SoloLedger_기장원장_202610.csv" in names
        assert "receipts/20261020_쿠팡_소모품비_모니터암_45000.jpg" in names

    # 5. Second export without force_reexport exports 0 rows (duplicate filing protection)
    r_exp2 = client.post(
        "/api/v1/exports/tax-pack",
        json={"year_month": "202610", "force_reexport": False},
        headers=headers,
    )
    assert r_exp2.status_code == 200
    assert r_exp2.json()["exported_count"] == 0
