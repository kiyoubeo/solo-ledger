"""Merchant 30-day TTL Cache Service & Ledger Tax Status Synchronization."""

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import httpx

from sololedger.config import settings
from sololedger.models import NtsStatusResult
from sololedger.nts_client import (
    fetch_nts_biz_status,
    normalize_biz_reg_no,
    validate_biz_reg_no_checksum,
)

_PG_PROVIDER_PATTERN = re.compile(
    r"(쿠팡|네이버페이|알리익스프레스|이니시스|토스페이먼츠|카카오페이|나이스페이|KCP)",
    re.IGNORECASE,
)

ACCOUNT_CODE_NAMES = {
    "811": "복리후생비",
    "813": "기업업무추진비",
    "822": "차량유지비",
    "830": "소모품비",
    "831": "지급수수료",
    "999": "미분류",
}


def detect_pg_provider(merchant_name: str) -> bool:
    """Return True if merchant_name matches known PG / e-commerce platforms."""
    return bool(_PG_PROVIDER_PATTERN.search(merchant_name or ""))


def is_cache_fresh(
    nts_verified_at: Optional[str],
    ttl_days: Optional[int] = None,
    reference_now: Optional[datetime] = None,
) -> bool:
    """Check whether nts_verified_at timestamp is within the TTL window (default 30 days)."""
    if not nts_verified_at:
        return False
    ttl = ttl_days if ttl_days is not None else settings.nts_cache_ttl_days
    now = reference_now or datetime.now()
    clean_ts = nts_verified_at[:19].replace("T", " ")
    try:
        verified_dt = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (now - verified_dt) < timedelta(days=ttl)


async def get_or_verify_merchant(
    cursor: sqlite3.Cursor,
    biz_reg_no: str,
    merchant_name: str = "미상가맹점",
    default_account_code: Optional[str] = None,
    force_refresh: bool = False,
    ttl_days: Optional[int] = None,
    reference_now: Optional[datetime] = None,
    service_key: Optional[str] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> NtsStatusResult:
    """Retrieve merchant tax status from SQLite 30-day TTL cache or query NTS API on miss/expiry."""
    clean_biz_no = normalize_biz_reg_no(biz_reg_no)
    now = reference_now or datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("SELECT * FROM merchants WHERE biz_reg_no = ?", (clean_biz_no,))
    row = cursor.fetchone()

    if (
        row is not None
        and not force_refresh
        and row["tax_type_code"] != "UNKNOWN"
        and is_cache_fresh(row["nts_verified_at"], ttl_days=ttl_days, reference_now=now)
    ):
        resolved_default_acc = default_account_code or row["default_account_code"]
        if default_account_code and default_account_code != row["default_account_code"]:
            cursor.execute(
                "UPDATE merchants SET default_account_code = ? WHERE biz_reg_no = ?",
                (default_account_code, clean_biz_no),
            )
        return NtsStatusResult(
            biz_reg_no=clean_biz_no,
            merchant_name=row["merchant_name"],
            tax_type_code=row["tax_type_code"],
            biz_status=row["biz_status"],
            is_valid_checksum=validate_biz_reg_no_checksum(clean_biz_no),
            is_pg_provider=bool(row["is_pg_provider"]),
            default_account_code=resolved_default_acc,
            from_cache=True,
            nts_verified_at=row["nts_verified_at"],
        )

    # Cache miss, expired (>= 30 days), UNKNOWN status, or force_refresh=True -> Query NTS API
    nts_map = await fetch_nts_biz_status(
        [clean_biz_no], service_key=service_key, transport=transport
    )
    nts_res = nts_map[clean_biz_no]

    resolved_name = (
        merchant_name
        if merchant_name and merchant_name != "미상가맹점"
        else (row["merchant_name"] if row else "미상가맹점")
    )
    resolved_pg = detect_pg_provider(resolved_name) or (bool(row["is_pg_provider"]) if row else False)
    resolved_acc = default_account_code or (row["default_account_code"] if row else None)

    cursor.execute(
        """
        INSERT INTO merchants (
            biz_reg_no, merchant_name, tax_type_code, biz_status,
            is_pg_provider, default_account_code, nts_verified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(biz_reg_no) DO UPDATE SET
            merchant_name = excluded.merchant_name,
            tax_type_code = excluded.tax_type_code,
            biz_status = excluded.biz_status,
            is_pg_provider = excluded.is_pg_provider,
            default_account_code = COALESCE(excluded.default_account_code, merchants.default_account_code),
            nts_verified_at = excluded.nts_verified_at
        """,
        (
            clean_biz_no,
            resolved_name,
            nts_res.tax_type_code,
            nts_res.biz_status,
            resolved_pg,
            resolved_acc,
            now_str,
        ),
    )

    return NtsStatusResult(
        biz_reg_no=clean_biz_no,
        merchant_name=resolved_name,
        tax_type_code=nts_res.tax_type_code,
        biz_status=nts_res.biz_status,
        is_valid_checksum=nts_res.is_valid_checksum,
        is_pg_provider=resolved_pg,
        default_account_code=resolved_acc,
        raw_tax_type_msg=nts_res.raw_tax_type_msg,
        closed_dt=nts_res.closed_dt,
        from_cache=False,
        nts_verified_at=now_str,
    )


def apply_merchant_tax_status_to_ledger(
    cursor: sqlite3.Cursor, status: NtsStatusResult
) -> int:
    """Apply verified merchant tax status and default account to matching card_transactions."""
    # Link unlinked transactions with identical raw_merchant_name
    if status.merchant_name and status.merchant_name != "미상가맹점":
        cursor.execute(
            """
            UPDATE card_transactions
            SET biz_reg_no = ?
            WHERE biz_reg_no IS NULL AND raw_merchant_name = ?
            """,
            (status.biz_reg_no, status.merchant_name),
        )

    updated_count = 0

    if status.tax_type_code == "EXEMPT":
        cursor.execute(
            """
            UPDATE card_transactions
            SET supply_value = total_amount,
                vat_amount = 0,
                vat_deduction_type = 'EXEMPT',
                non_deduct_reason = COALESCE(non_deduct_reason, 'SIMPLIFIED_TAX'),
                updated_at = CURRENT_TIMESTAMP
            WHERE biz_reg_no = ?
            """,
            (status.biz_reg_no,),
        )
        updated_count = cursor.rowcount
    elif status.tax_type_code in ("SIMPLIFIED", "UNREGISTERED") or status.biz_status in (
        "SUSPENDED",
        "CLOSED",
    ):
        cursor.execute(
            """
            UPDATE card_transactions
            SET vat_deduction_type = 'NON_DEDUCTIBLE',
                non_deduct_reason = COALESCE(non_deduct_reason, 'SIMPLIFIED_TAX'),
                updated_at = CURRENT_TIMESTAMP
            WHERE biz_reg_no = ?
            """,
            (status.biz_reg_no,),
        )
        updated_count = cursor.rowcount
    else:
        cursor.execute(
            "SELECT COUNT(*) FROM card_transactions WHERE biz_reg_no = ?",
            (status.biz_reg_no,),
        )
        updated_count = cursor.fetchone()[0]

    # Apply default_account_code to unclassified ('999') transactions for this merchant
    if status.default_account_code:
        acc_name = ACCOUNT_CODE_NAMES.get(status.default_account_code, "지정계정")
        cursor.execute(
            """
            UPDATE card_transactions
            SET account_code = ?,
                account_name = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE biz_reg_no = ? AND account_code = '999'
            """,
            (status.default_account_code, acc_name, status.biz_reg_no),
        )

    return updated_count
