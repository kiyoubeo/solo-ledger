"""Unit and integration tests for Phase 9 Storage Abstraction, Cloud Sync, and Scheduler Jobs."""

import shutil
import zipfile
from typing import Dict
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from sololedger.api import app
from sololedger.archiver import DuplicateReceiptError
from sololedger.cloud_sync import (
    LATEST_DB_OBJECT_KEY,
    backup_sqlite_snapshot_to_storage,
    build_nas_pull_sync_command,
    restore_sqlite_snapshot_from_storage,
)
from sololedger.db import get_connection, init_db
from sololedger.exporters import generate_tax_accountant_pack
from sololedger.models import OcrExtractionResult, OcrLineItem
from sololedger.ocr_engine import process_and_link_receipt
from sololedger.storage_backend import GcsStorageBackend, LocalStorageBackend


def _build_mock_gcs_transport(objects: Dict[str, bytes], expected_bucket: str = "test-bucket"):
    """Build an in-memory httpx.MockTransport emulating Metadata Server + GCS JSON API."""

    def _handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        raw_path_no_q = request.url.raw_path.decode("ascii").split("?", 1)[0]
        decoded_path = unquote(raw_path_no_q)
        query_params = parse_qs(request.url.query.decode("utf-8"))

        if host == "metadata.google.internal":
            if request.headers.get("metadata-flavor") != "Google":
                return httpx.Response(403, json={"error": "Missing Metadata-Flavor header"})
            return httpx.Response(200, json={"access_token": "mock-meta-token", "expires_in": 3600})

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:].strip():
            return httpx.Response(401, json={"error": "Unauthorized"})

        upload_prefix = f"/upload/storage/v1/b/{expected_bucket}/o"
        api_prefix = f"/storage/v1/b/{expected_bucket}/o"

        if request.method == "POST" and decoded_path == upload_prefix:
            obj_name = query_params.get("name", [""])[0]
            if not obj_name:
                return httpx.Response(400, json={"error": "Missing object name"})
            objects[obj_name] = request.content
            return httpx.Response(200, json={"name": obj_name, "bucket": expected_bucket})

        if request.method == "GET" and decoded_path == api_prefix:
            prefix = query_params.get("prefix", [""])[0]
            matched = [
                {"name": k, "size": str(len(v))}
                for k, v in sorted(objects.items())
                if not prefix or k.startswith(prefix)
            ]
            return httpx.Response(200, json={"items": matched})

        if request.method == "GET" and decoded_path.startswith(api_prefix + "/"):
            obj_name = decoded_path[len(api_prefix) + 1 :]
            if obj_name not in objects:
                return httpx.Response(404, json={"error": "Not found"})
            if query_params.get("alt", [""])[0] == "media":
                return httpx.Response(200, content=objects[obj_name])
            return httpx.Response(
                200, json={"name": obj_name, "size": str(len(objects[obj_name]))}
            )

        return httpx.Response(404, json={"error": f"Unhandled mock route: {decoded_path}"})

    return httpx.MockTransport(_handler)


def test_local_and_mock_gcs_storage_backends(tmp_path):
    local_dir = tmp_path / "local_archive"
    local_store = LocalStorageBackend(archive_dir=local_dir)

    path_str = local_store.put_bytes("normalized/sample.jpg", b"local-image-bytes", "image/jpeg")
    assert "sample.jpg" in path_str
    assert local_store.exists("normalized/sample.jpg") is True
    assert local_store.get_bytes("normalized/sample.jpg") == b"local-image-bytes"
    assert local_store.list_keys("normalized") == ["normalized/sample.jpg"]
    assert local_store.build_download_url("normalized/sample.jpg") == "/archive/normalized/sample.jpg"

    with pytest.raises(ValueError):
        local_store.put_bytes("../escape.txt", b"bad")

    gcs_objects: Dict[str, bytes] = {}
    gcs_store = GcsStorageBackend(
        bucket="test-bucket",
        prefix="sololedger",
        local_cache_dir=tmp_path / "gcs_cache",
        write_through_local=False,
        transport=_build_mock_gcs_transport(gcs_objects),
    )

    uri = gcs_store.put_bytes("normalized/receipt_1.jpg", b"gcs-jpeg-bytes", "image/jpeg")
    assert uri == "gs://test-bucket/sololedger/normalized/receipt_1.jpg"
    assert gcs_store.exists("normalized/receipt_1.jpg") is True
    assert gcs_store.exists("normalized/missing.jpg") is False
    assert gcs_store.get_bytes("normalized/receipt_1.jpg") == b"gcs-jpeg-bytes"
    assert gcs_store.list_keys("normalized") == ["normalized/receipt_1.jpg"]

    signed_url = gcs_store.build_download_url("normalized/receipt_1.jpg", expires_in_sec=600)
    assert signed_url.startswith(
        "https://storage.googleapis.com/test-bucket/sololedger/normalized/receipt_1.jpg?"
    )
    assert "expires=" in signed_url and "sig=" in signed_url

    with pytest.raises(FileNotFoundError):
        gcs_store.get_bytes("normalized/does_not_exist.jpg")


@pytest.mark.asyncio
async def test_gcs_receipt_upload_dedup_and_ephemeral_tax_pack_zip(db_conn, tmp_path):
    cur = db_conn.cursor()
    archive_dir = tmp_path / "ephemeral_archive"
    gcs_objects: Dict[str, bytes] = {}
    gcs_store = GcsStorageBackend(
        bucket="test-bucket",
        prefix="sololedger",
        access_token="explicit-test-token",
        local_cache_dir=archive_dir,
        write_through_local=True,
        transport=_build_mock_gcs_transport(gcs_objects),
    )

    receipt_bytes = b"\xff\xd8\xff\xe0MOCK-RECEIPT-CONTENT-FOR-GCS"
    ocr_res = OcrExtractionResult(
        biz_reg_no="1208147521",
        merchant_name="쿠팡",
        approval_no="44556677",
        transacted_at="2026-10-15 11:20:00",
        total_amount=45_000,
        supply_value=40_909,
        vat_amount=4_091,
        line_items=[OcrLineItem(item="모니터암", qty=1, price=45_000)],
        confidence=0.98,
    )

    outcome = await process_and_link_receipt(
        cur,
        file_bytes=receipt_bytes,
        original_file_name="coupang_order.jpg",
        source_channel="WEB_UPLOAD",
        ocr_override=ocr_res,
        archive_dir=archive_dir,
        storage=gcs_store,
    )
    db_conn.commit()

    arch_name = outcome["archived_file_name"]
    assert f"sololedger/normalized/{arch_name}" in gcs_objects
    assert "sololedger/original/coupang_order.jpg" in gcs_objects

    # Duplicate upload with identical SHA-256 must be blocked
    with pytest.raises(DuplicateReceiptError):
        await process_and_link_receipt(
            cur,
            file_bytes=receipt_bytes,
            original_file_name="coupang_order_dup.jpg",
            ocr_override=ocr_res,
            archive_dir=archive_dir,
            storage=gcs_store,
        )

    # Simulate ephemeral Cloud Run container restart wiping local /tmp/archive/normalized
    shutil.rmtree(archive_dir / "normalized")
    assert not (archive_dir / "normalized" / arch_name).exists()

    pack = generate_tax_accountant_pack(
        cur,
        year_month="202610",
        archive_dir=archive_dir,
        storage=gcs_store,
    )
    db_conn.commit()

    assert pack["exported_count"] == 1
    assert pack["bundled_receipts_count"] == 1
    assert pack["remote_zip_uri"] == "gs://test-bucket/sololedger/exports/SoloLedger_세무증빙_202610.zip"
    assert pack["remote_csv_uri"] == "gs://test-bucket/sololedger/exports/SoloLedger_기장원장_202610.csv"

    with zipfile.ZipFile(pack["zip_file_path"], "r") as zf:
        names = zf.namelist()
        assert f"receipts/{arch_name}" in names
        assert zf.read(f"receipts/{arch_name}") == receipt_bytes


def test_sqlite_snapshot_backup_restore_and_corrupt_guard(test_db_path, tmp_path):
    conn = get_connection(test_db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount,
            account_code, account_name, vat_deduction_type
        ) VALUES ('3081', '2026-10-20 09:00:00', 'GS칼텍스', 55000, 50000, 5000, '822', '차량유지비', 'NON_DEDUCTIBLE')
        """
    )
    conn.commit()
    conn.close()

    gcs_objects: Dict[str, bytes] = {}
    gcs_store = GcsStorageBackend(
        bucket="test-bucket",
        prefix="sololedger",
        access_token="snap-token",
        write_through_local=False,
        transport=_build_mock_gcs_transport(gcs_objects),
    )

    backup_info = backup_sqlite_snapshot_to_storage(db_path=test_db_path, storage=gcs_store)
    assert backup_info["status"] == "backed_up"
    assert backup_info["backend"] == "gcs"
    assert backup_info["size_bytes"] > 0
    assert f"sololedger/{LATEST_DB_OBJECT_KEY}" in gcs_objects

    # Delete local DB and restore into a brand new location
    restored_db_path = tmp_path / "cold_start" / "restored.db"
    restore_info = restore_sqlite_snapshot_from_storage(
        db_path=str(restored_db_path),
        storage=gcs_store,
    )
    assert restore_info["status"] == "restored"
    assert restore_info["bytes_restored"] == backup_info["size_bytes"]

    r_conn = get_connection(str(restored_db_path))
    r_cur = r_conn.cursor()
    r_cur.execute("SELECT raw_merchant_name, total_amount FROM card_transactions")
    rows = r_cur.fetchall()
    r_conn.close()
    assert len(rows) == 1
    assert rows[0]["raw_merchant_name"] == "GS칼텍스"
    assert rows[0]["total_amount"] == 55_000

    # Existing non-empty DB skips restore when overwrite=False
    skip_info = restore_sqlite_snapshot_from_storage(
        db_path=str(restored_db_path),
        storage=gcs_store,
        overwrite=False,
    )
    assert skip_info["status"] == "skipped_existing"

    # Corrupt non-SQLite snapshot payload must be rejected
    gcs_objects[f"sololedger/{LATEST_DB_OBJECT_KEY}"] = b"NOT_A_VALID_SQLITE_HEADER" * 10
    with pytest.raises(ValueError, match="Invalid or corrupted SQLite snapshot header"):
        restore_sqlite_snapshot_from_storage(
            db_path=str(restored_db_path),
            storage=gcs_store,
            overwrite=True,
        )


def test_cloud_mode_lifespan_and_scheduler_job_endpoints(tmp_path, monkeypatch):
    db_file = tmp_path / "cloud_mode.db"
    archive_dir = tmp_path / "cloud_archive"
    monkeypatch.setenv("SOLOLEDGER_DB_PATH", str(db_file))
    monkeypatch.setenv("SOLOLEDGER_ARCHIVE_DIR", str(archive_dir))
    monkeypatch.setenv("SOLOLEDGER_API_KEY", "test-secret-key")
    monkeypatch.setenv("SOLOLEDGER_CLOUD_MODE", "true")
    monkeypatch.setenv("SOLOLEDGER_STORAGE_BACKEND", "local")
    monkeypatch.setenv("SOLOLEDGER_SCHEDULER_SECRET", "my-sched-secret")
    init_db(str(db_file))

    with TestClient(app) as c:
        assert app.state.email_watcher_active is False

        # Unauthorized requests rejected
        assert c.post("/api/v1/cloud/jobs/email-sync").status_code == 401
        assert (
            c.post(
                "/api/v1/cloud/jobs/snapshot-backup",
                headers={"X-CloudScheduler-Secret": "wrong-secret"},
            ).status_code
            == 401
        )

        # Authorized via X-CloudScheduler-Secret
        r_email = c.post(
            "/api/v1/cloud/jobs/email-sync",
            json={"lookback_days": 7, "max_messages": 50, "backup_after_sync": True},
            headers={"X-CloudScheduler-Secret": "my-sched-secret"},
        )
        assert r_email.status_code == 200
        email_body = r_email.json()
        assert email_body["status"] == "completed"
        assert email_body["cloud_mode"] is True
        assert email_body["email_sync"]["status"] == "no_accounts_configured"

        # Authorized via X-API-Key
        r_snap = c.post(
            "/api/v1/cloud/jobs/snapshot-backup",
            json={"verify_archive_keys": True},
            headers={"X-API-Key": "test-secret-key"},
        )
        assert r_snap.status_code == 200
        snap_body = r_snap.json()
        assert snap_body["status"] == "backed_up"
        assert snap_body["backend"] == "local"
        assert snap_body["archive_keys_count"] >= 2

    cmds = build_nas_pull_sync_command(
        gcs_bucket="sololedger-prod-bucket",
        gcs_prefix="sololedger",
        local_nas_dir=r"Z:\HDD1\Data\SoloLedger",
    )
    assert cmds["source_uri"] == "gs://sololedger-prod-bucket/sololedger"
    assert "--delete" not in cmds["gcloud_rsync"]
    assert "rclone copy" in cmds["rclone_copy"]
