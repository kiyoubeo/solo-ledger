"""Operational Command-Line Interface (CLI) for SoloLedger."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger import __version__
from sololedger.config import settings
from sololedger.dashboard import (
    build_dashboard_summary,
    reset_test_ledger,
    seed_demo_transactions,
)
from sololedger.db import get_connection, init_db
from sololedger.exporters import generate_tax_accountant_pack
from sololedger.importers import decode_csv_bytes, import_and_reconcile_hometax_csv
from sololedger.nas_sync import sync_to_nas
from sololedger.phone_adb import import_phone_sms_to_ledger


def build_parser() -> argparse.ArgumentParser:
    """Create argparse CLI parser for SoloLedger operational tasks."""
    parser = argparse.ArgumentParser(
        prog="sololedger",
        description=f"SoloLedger v{__version__} — 1-Person Corp Expense & Tax Automation CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. init-db
    subparsers.add_parser("init-db", help="Initialize SQLite schema and default tax rules")

    # 2. status
    status_parser = subparsers.add_parser(
        "status", help="Display ledger summary, counts, and vehicle limit status"
    )
    status_parser.add_argument(
        "--year-month",
        default=None,
        help="Target year-month in YYYYMM format (defaults to current month)",
    )
    status_parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Target year in YYYY format (defaults to current year)",
    )

    # 3. import-csv
    import_parser = subparsers.add_parser(
        "import-csv", help="Import Hometax or card issuer CSV file and run 3-stage reconciliation"
    )
    import_parser.add_argument("file_path", help="Path to CSV file (UTF-8 or CP949)")

    # 4. export-pack
    export_parser = subparsers.add_parser(
        "export-pack",
        help="Generate monthly tax accountant CSV + normalized receipt ZIP bundle",
    )
    export_parser.add_argument("year_month", help="Target year-month in YYYYMM format (e.g. 202610)")
    export_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-export transactions even if already marked exported",
    )

    # 5. backup-nas
    nas_parser = subparsers.add_parser(
        "backup-nas",
        help="Safely back up SQLite DB and mirror receipt archive to NAS drive (default: Z:\\SoloLedger)",
    )
    nas_parser.add_argument(
        "--nas-dir",
        default=None,
        help="Override NAS target directory path (defaults to SOLOLEDGER_NAS_DIR or Z:\\SoloLedger)",
    )

    # 6. seed-demo
    seed_parser = subparsers.add_parser(
        "seed-demo",
        help="Seed realistic sample transactions and receipts into the test ledger",
    )
    seed_parser.add_argument(
        "--year-month",
        default=None,
        help="Target year-month in YYYYMM format (defaults to current month)",
    )

    # 7. reset-db
    subparsers.add_parser(
        "reset-db",
        help="Clear all transactions, receipts, and merchants from the test ledger",
    )

    # 8. import-phone-sms
    phone_parser = subparsers.add_parser(
        "import-phone-sms",
        help="Fetch card SMS messages from connected USB Android phone via ADB and import into ledger",
    )
    phone_parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Maximum number of recent card SMS messages to import (default: 30)",
    )
    phone_parser.add_argument(
        "--serial",
        default=None,
        help="Optional ADB device serial number",
    )

    return parser


def run_cli(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    """Execute CLI command and return structured result dictionary."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init-db":
        db_path = init_db()
        return {
            "command": "init-db",
            "status": "initialized",
            "db_path": str(db_path),
            "version": __version__,
        }

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        if args.command == "status":
            now = datetime.now()
            ym = args.year_month or now.strftime("%Y%m")
            yr = args.year or int(ym[:4])
            dash = build_dashboard_summary(cursor, year_month=ym, year=yr)

            cursor.execute("SELECT COUNT(*) FROM card_transactions")
            total_ledger_rows = int(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM merchants")
            total_merchants = int(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM receipt_ocr_metadata")
            total_receipts = int(cursor.fetchone()[0])

            return {
                "command": "status",
                "version": __version__,
                "db_path": str(settings.db_path),
                "total_ledger_rows": total_ledger_rows,
                "cached_merchants": total_merchants,
                "archived_receipts": total_receipts,
                "monthly_summary": {
                    "year_month": dash["year_month"],
                    "tx_count": dash["total_tx_count"],
                    "total_expenditure": dash["total_expenditure"],
                    "deductible_vat": dash["deductible_vat"],
                    "non_deductible_vat": dash["non_deductible_vat"],
                    "pending_action_count": dash["pending_action_count"],
                },
                "vehicle_limit": dash["vehicle_limit"],
            }

        if args.command == "import-csv":
            csv_path = Path(args.file_path)
            if not csv_path.exists():
                raise FileNotFoundError(f"CSV file not found: {csv_path}")
            csv_text = decode_csv_bytes(csv_path.read_bytes())
            outcome = import_and_reconcile_hometax_csv(cursor, csv_text)
            conn.commit()
            return {"command": "import-csv", "file_path": str(csv_path), **outcome}

        if args.command == "export-pack":
            outcome = generate_tax_accountant_pack(
                cursor,
                year_month=args.year_month,
                force_reexport=bool(args.force),
            )
            conn.commit()
            return {"command": "export-pack", **outcome}

        if args.command == "backup-nas":
            target_dir = Path(args.nas_dir) if args.nas_dir else None
            outcome = sync_to_nas(target_nas_dir=target_dir)
            return {"command": "backup-nas", **outcome}

        if args.command == "seed-demo":
            outcome = seed_demo_transactions(cursor, year_month=args.year_month)
            conn.commit()
            return {"command": "seed-demo", **outcome}

        if args.command == "reset-db":
            outcome = reset_test_ledger(cursor)
            conn.commit()
            return {"command": "reset-db", **outcome}

        if args.command == "import-phone-sms":
            outcome = import_phone_sms_to_ledger(
                cursor,
                limit=int(args.limit),
                device_serial=args.serial,
            )
            conn.commit()
            return {"command": "import-phone-sms", **outcome}

        raise ValueError(f"Unknown CLI command: {args.command}")
    finally:
        conn.close()


def main() -> None:
    """Main entrypoint for `python -m sololedger.cli`."""
    result = run_cli(sys.argv[1:])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
