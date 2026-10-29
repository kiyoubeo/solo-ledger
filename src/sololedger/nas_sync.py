"""NAS (ipDISK Drive Z:\\ / SMB / FTP Mounted Drive) Safe Database Backup & Evidence Mirroring."""

import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger.config import settings
from sololedger.db import get_connection, init_db


def sync_to_nas(
    target_nas_dir: Optional[Path] = None,
    db_path: Optional[str] = None,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Safely back up the active SQLite database and mirror receipt archives to NAS storage.

    Why two-step DB backup?
    Virtual FTP drives like ipDISK Drive (`Z:\\`) do not support POSIX/Win32 byte-range locks
    required by SQLite's online backup target. Therefore, we first create an atomic hot backup
    in a local temporary file using SQLite's Online Backup API (`conn.backup()`), and then
    stream-copy the snapshot file to the NAS `backups/` directory.
    """
    nas_root = Path(target_nas_dir) if target_nas_dir else settings.nas_backup_dir
    source_db = Path(db_path or settings.db_path)
    source_archive = Path(archive_dir) if archive_dir else settings.archive_dir

    init_db(str(source_db))

    backups_dir = nas_root / "backups"
    nas_archive_dir = nas_root / "archive"
    backups_dir.mkdir(parents=True, exist_ok=True)
    nas_archive_dir.mkdir(parents=True, exist_ok=True)

    date_tag = datetime.now().strftime("%Y%m%d")
    latest_backup_path = backups_dir / "sololedger_prod_latest.db"
    dated_backup_path = backups_dir / f"sololedger_prod_{date_tag}.db"

    # Step 1: Create local consistent SQLite hot snapshot, then copy to NAS
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        tmp_snapshot_path = Path(tmp_file.name)

    try:
        src_conn = get_connection(str(source_db))
        dst_conn = sqlite3.connect(str(tmp_snapshot_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        shutil.copyfile(tmp_snapshot_path, latest_backup_path)
        shutil.copyfile(tmp_snapshot_path, dated_backup_path)
    finally:
        if tmp_snapshot_path.exists():
            tmp_snapshot_path.unlink()

    # Step 2: Incrementally mirror archive subdirectories (original, normalized, exports)
    synced_files: List[str] = []
    skipped_files_count = 0

    for subdir_name in ("original", "normalized", "exports"):
        src_sub = source_archive / subdir_name
        if not src_sub.exists():
            continue
        dst_sub = nas_archive_dir / subdir_name
        dst_sub.mkdir(parents=True, exist_ok=True)

        for src_file in sorted(src_sub.iterdir()):
            if not src_file.is_file():
                continue
            dst_file = dst_sub / src_file.name
            if dst_file.exists() and dst_file.stat().st_size == src_file.stat().st_size:
                skipped_files_count += 1
                continue
            shutil.copyfile(src_file, dst_file)
            synced_files.append(f"{subdir_name}/{src_file.name}")

    return {
        "status": "synced",
        "nas_root": str(nas_root),
        "latest_db_backup": str(latest_backup_path),
        "dated_db_backup": str(dated_backup_path),
        "synced_files_count": len(synced_files),
        "skipped_existing_count": skipped_files_count,
        "synced_files": synced_files,
    }
