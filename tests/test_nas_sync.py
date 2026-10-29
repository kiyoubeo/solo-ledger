"""Unit and integration tests for NAS (ipDISK Drive Z:\\) safe backup and archive mirroring."""

import base64
import sqlite3
from pathlib import Path

from sololedger.cli import run_cli


def test_nas_backup_api_and_cli_incremental_sync(client, test_db_path, tmp_path):
    """Verify safe SQLite hot snapshot backup and incremental archive mirroring to NAS."""
    headers = {"X-API-Key": "test-secret-key"}
    mock_nas_dir = tmp_path / "mock_khomeNAS" / "SoloLedger"

    # 1. Seed a transaction and an archived receipt
    sms_res = client.post(
        "/api/v1/webhooks/sms",
        headers=headers,
        json={
            "sender": "15447200",
            "raw_message": "[Web발신] 신한법인(9876) 승인 45,000원 일시불 10/29 10:30 쿠팡(주) 승인번호:99887766",
            "received_at": "2026-10-29T10:30:00",
        },
    )
    assert sms_res.status_code == 200
    tx_id = sms_res.json()["tx_id"]

    up_res = client.post(
        "/api/v1/receipts/upload",
        headers=headers,
        json={
            "file_name": "nas_test_receipt.jpg",
            "file_content_b64": base64.b64encode(b"NAS-SYNC-TEST-RECEIPT").decode("ascii"),
            "tx_id": tx_id,
            "ocr_override": {
                "biz_reg_no": "120-88-00767",
                "merchant_name": "쿠팡(주)",
                "approval_no": "99887766",
                "transacted_at": "2026-10-29 10:30:00",
                "total_amount": 45000,
                "supply_value": 40909,
                "vat_amount": 4091,
                "line_items": [{"item": "외장SSD", "qty": 1, "price": 45000}],
            },
        },
    )
    assert up_res.status_code == 200

    # 2. Trigger NAS backup via API endpoint
    backup_res = client.post(
        "/api/v1/system/backup-nas",
        headers=headers,
        json={"nas_dir": str(mock_nas_dir)},
    )
    assert backup_res.status_code == 200
    body = backup_res.json()
    assert body["status"] == "synced"
    assert body["synced_files_count"] >= 2  # original + normalized receipt

    latest_db = Path(body["latest_db_backup"])
    dated_db = Path(body["dated_db_backup"])
    assert latest_db.exists()
    assert dated_db.exists()

    # Verify backed-up SQLite DB is uncorrupted and contains the transaction
    conn = sqlite3.connect(str(latest_db))
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM card_transactions")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()

    # 3. Second sync via CLI should skip already-synced identical files
    cli_res = run_cli(["backup-nas", "--nas-dir", str(mock_nas_dir)])
    assert cli_res["command"] == "backup-nas"
    assert cli_res["status"] == "synced"
    assert cli_res["synced_files_count"] == 0
    assert cli_res["skipped_existing_count"] >= 2
