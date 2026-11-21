"""Phase 9 Serverless SQLite Snapshot Sync, Cloud Scheduler Jobs & NAS Pull Helpers."""

import hashlib
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sololedger.config import settings
from sololedger.db import get_connection, init_db
from sololedger.email_watcher import load_email_watcher_config, sync_imap_order_emails
from sololedger.storage_backend import StorageBackend, get_storage_backend

SQLITE_MAGIC_HEADER = b"SQLite format 3\x00"
LATEST_DB_OBJECT_KEY = "backups/sololedger_prod_latest.db"


def backup_sqlite_snapshot_to_storage(
    db_path: Optional[str] = None,
    storage: Optional[StorageBackend] = None,
) -> Dict[str, Any]:
    """Create a consistent hot SQLite backup and upload it to StorageBackend."""
    target_db = Path(db_path or settings.db_path)
    store = storage or get_storage_backend()
    init_db(str(target_db))

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        tmp_snapshot_path = Path(tmp_file.name)

    try:
        src_conn = get_connection(str(target_db))
        dst_conn = sqlite3.connect(str(tmp_snapshot_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        snapshot_bytes = tmp_snapshot_path.read_bytes()
    finally:
        if tmp_snapshot_path.exists():
            tmp_snapshot_path.unlink()

    sha256_hex = hashlib.sha256(snapshot_bytes).hexdigest()
    date_tag = datetime.now().strftime("%Y%m%d")
    latest_uri = store.put_bytes(
        LATEST_DB_OBJECT_KEY,
        snapshot_bytes,
        content_type="application/x-sqlite3",
    )
    dated_uri = store.put_bytes(
        f"backups/sololedger_prod_{date_tag}.db",
        snapshot_bytes,
        content_type="application/x-sqlite3",
    )

    return {
        "status": "backed_up",
        "backend": store.backend_name,
        "latest_uri": latest_uri,
        "dated_uri": dated_uri,
        "size_bytes": len(snapshot_bytes),
        "sha256": sha256_hex,
    }


def restore_sqlite_snapshot_from_storage(
    db_path: Optional[str] = None,
    storage: Optional[StorageBackend] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Restore SQLite database snapshot from StorageBackend into target_db when needed."""
    target_db = Path(db_path or settings.db_path)
    if not overwrite and target_db.exists() and target_db.stat().st_size > 0:
        return {
            "status": "skipped_existing",
            "db_path": str(target_db),
            "bytes_restored": 0,
        }

    store = storage or get_storage_backend()
    if not store.exists(LATEST_DB_OBJECT_KEY):
        return {
            "status": "no_remote_snapshot",
            "db_path": str(target_db),
            "bytes_restored": 0,
        }

    data = store.get_bytes(LATEST_DB_OBJECT_KEY)
    if len(data) < 100 or data[:16] != SQLITE_MAGIC_HEADER:
        raise ValueError("Invalid or corrupted SQLite snapshot header.")

    target_db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target_db.parent,
        prefix=".restore_",
        suffix=".db",
        delete=False,
    ) as tmp_file:
        tmp_restore_path = Path(tmp_file.name)
        tmp_file.write(data)

    try:
        os.replace(tmp_restore_path, target_db)
    except Exception:
        if tmp_restore_path.exists():
            tmp_restore_path.unlink()
        raise

    init_db(str(target_db))
    return {
        "status": "restored",
        "db_path": str(target_db),
        "bytes_restored": len(data),
    }


def run_cloud_email_sync_job(
    cursor: sqlite3.Cursor,
    lookback_days: int = 14,
    max_messages: int = 200,
    backup_after_sync: bool = True,
    db_path: Optional[str] = None,
    storage: Optional[StorageBackend] = None,
) -> Dict[str, Any]:
    """Execute one-shot IMAP order email sync (replacing while-True loop on Cloud Run)."""
    cfg = load_email_watcher_config()
    accounts = cfg.get("accounts") or []
    if accounts:
        sync_result = sync_imap_order_emails(
            cursor,
            max_messages=max_messages,
            lookback_days=lookback_days,
        )
        cursor.connection.commit()
    else:
        sync_result = {
            "status": "no_accounts_configured",
            "matched_count": 0,
            "created_count": 0,
        }

    result: Dict[str, Any] = {
        "status": "completed",
        "cloud_mode": settings.cloud_mode,
        "email_sync": sync_result,
    }
    if backup_after_sync and (
        storage is not None or settings.storage_backend_type == "gcs"
    ):
        result["snapshot_backup"] = backup_sqlite_snapshot_to_storage(
            db_path=db_path,
            storage=storage,
        )

    return result


def build_nas_pull_sync_command(
    gcs_bucket: str,
    gcs_prefix: str = "sololedger",
    local_nas_dir: Optional[str] = None,
) -> Dict[str, str]:
    """Build non-destructive Pull backup commands from Cloud Storage to local PC / NAS."""
    clean_bucket = (gcs_bucket or "").strip().removeprefix("gs://").strip("/")
    clean_prefix = (gcs_prefix or "").replace("\\", "/").strip().strip("/")
    dest = str(local_nas_dir or settings.nas_backup_dir)

    gs_uri = f"gs://{clean_bucket}/{clean_prefix}" if clean_prefix else f"gs://{clean_bucket}"
    rclone_src = f"gcs:{clean_bucket}/{clean_prefix}" if clean_prefix else f"gcs:{clean_bucket}"

    return {
        "source_uri": gs_uri,
        "destination_dir": dest,
        "gcloud_rsync": f'gcloud storage rsync "{gs_uri}" "{dest}" --recursive',
        "rclone_copy": f'rclone copy "{rclone_src}" "{dest}" --update',
    }
