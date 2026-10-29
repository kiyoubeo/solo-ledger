"""Tax Accountant Submission Package (CSV + Evidence ZIP Archive) Exporter."""

import csv
import io
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger.config import settings

CSV_HEADERS = [
    "전표번호",
    "거래일시",
    "카드뒷자리",
    "승인번호",
    "가맹점명",
    "사업자등록번호",
    "과세유형",
    "공급가액",
    "부가세",
    "합계금액",
    "계정과목코드",
    "계정과목명",
    "공제구분",
    "불공제사유",
    "업무용승용차여부",
    "적요_참석자_품목",
    "대사상태",
    "증빙파일명",
]


def generate_tax_accountant_pack(
    cursor: sqlite3.Cursor,
    year_month: str,
    force_reexport: bool = False,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate a monthly CSV ledger + normalized receipt ZIP bundle for the tax accountant.

    Marks exported rows with `exported_year_month = year_month` to prevent duplicate filing.
    """
    if len(year_month) != 6 or not year_month.isdigit():
        raise ValueError(f"Invalid year_month format (expected YYYYMM): {year_month}")

    ym_prefix = f"{year_month[:4]}-{year_month[4:6]}-%"
    base_archive = archive_dir or settings.archive_dir
    export_dir = base_archive / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    query = """
        SELECT t.*,
               m.tax_type_code AS merchant_tax_type,
               (
                   SELECT r.archived_file_name
                   FROM receipt_ocr_metadata r
                   WHERE r.tx_id = t.tx_id
                   ORDER BY r.receipt_id DESC
                   LIMIT 1
               ) AS archived_file_name
        FROM card_transactions t
        LEFT JOIN merchants m ON t.biz_reg_no = m.biz_reg_no
        WHERE t.transacted_at LIKE ?
          AND t.reconcile_status != 'CANCELED'
    """
    if not force_reexport:
        query += " AND (t.exported_year_month IS NULL OR t.exported_year_month = '')"
    query += " ORDER BY t.transacted_at ASC, t.tx_id ASC"

    cursor.execute(query, (ym_prefix,))
    rows = cursor.fetchall()

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(CSV_HEADERS)

    exported_tx_ids: List[int] = []
    attached_receipt_files: List[str] = []
    total_supply = 0
    total_vat = 0
    total_amount = 0
    deductible_vat = 0

    for row in rows:
        tx_id = int(row["tx_id"])
        exported_tx_ids.append(tx_id)
        supply_val = int(row["supply_value"])
        vat_val = int(row["vat_amount"])
        amt_val = int(row["total_amount"])

        total_supply += supply_val
        total_vat += vat_val
        total_amount += amt_val
        if row["vat_deduction_type"] == "DEDUCTIBLE":
            deductible_vat += vat_val

        arch_name = row["archived_file_name"] or ""
        if arch_name:
            attached_receipt_files.append(arch_name)

        writer.writerow(
            [
                tx_id,
                row["transacted_at"],
                row["card_last4"],
                row["approval_no"] or "",
                row["raw_merchant_name"],
                row["biz_reg_no"] or "",
                row["merchant_tax_type"] or "UNKNOWN",
                supply_val,
                vat_val,
                amt_val,
                row["account_code"],
                row["account_name"],
                row["vat_deduction_type"],
                row["non_deduct_reason"] or "",
                "Y" if row["is_vehicle_expense"] else "N",
                row["counterparty_info"] or "",
                row["reconcile_status"],
                arch_name,
            ]
        )

    csv_filename = f"SoloLedger_기장원장_{year_month}.csv"
    csv_path = export_dir / csv_filename
    csv_bytes = csv_buffer.getvalue().encode("utf-8-sig")
    csv_path.write_bytes(csv_bytes)

    zip_filename = f"SoloLedger_세무증빙_{year_month}.zip"
    zip_path = export_dir / zip_filename
    norm_dir = base_archive / "normalized"

    bundled_receipts_count = 0
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(csv_filename, csv_bytes)
        for arch_name in set(attached_receipt_files):
            candidate = norm_dir / arch_name
            if candidate.exists():
                zf.write(candidate, arcname=f"receipts/{arch_name}")
                bundled_receipts_count += 1

    if exported_tx_ids:
        placeholders = ",".join("?" for _ in exported_tx_ids)
        cursor.execute(
            f"""
            UPDATE card_transactions
            SET exported_year_month = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE tx_id IN ({placeholders})
            """,
            [year_month, *exported_tx_ids],
        )

    return {
        "status": "exported",
        "year_month": year_month,
        "exported_count": len(exported_tx_ids),
        "bundled_receipts_count": bundled_receipts_count,
        "total_supply_value": total_supply,
        "total_vat_amount": total_vat,
        "total_amount": total_amount,
        "deductible_vat_amount": deductible_vat,
        "csv_file_path": str(csv_path),
        "zip_file_path": str(zip_path),
    }
