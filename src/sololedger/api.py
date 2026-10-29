"""FastAPI application for SoloLedger SMS Webhook, NTS Cache, Vision OCR, Telegram Bot, and Tax Export."""

import base64
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from sololedger import __version__
from sololedger.archiver import DuplicateReceiptError
from sololedger.config import settings
from sololedger.dashboard import (
    build_dashboard_summary,
    render_dashboard_html,
    reset_test_ledger,
    seed_demo_transactions,
    update_transaction_classification,
)
from sololedger.db import get_connection, init_db
from sololedger.exporters import generate_tax_accountant_pack
from sololedger.importers import decode_csv_bytes, import_and_reconcile_hometax_csv
from sololedger.merchant_service import (
    apply_merchant_tax_status_to_ledger,
    get_or_verify_merchant,
)
from sololedger.models import (
    HometaxCsvImportRequest,
    MerchantVerifyRequest,
    NasBackupRequest,
    ReceiptUploadRequest,
    ReconcileBatchRequest,
    SmsWebhookPayload,
    TaxPackExportRequest,
    TransactionUpdateRequest,
)
from sololedger.nas_sync import sync_to_nas
from sololedger.notifier import send_telegram_memo_prompt
from sololedger.ocr_engine import process_and_link_receipt
from sololedger.reconciler import reconcile_single_item
from sololedger.sms_parser import parse_korean_card_sms
from sololedger.tax_engine import calculate_supply_and_vat, evaluate_tax_rules
from sololedger.telegram_bot import handle_telegram_update
from sololedger.vehicle_tracker import get_vehicle_expense_summary


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize SQLite schema and seed rules on startup."""
    init_db()
    yield


app = FastAPI(
    title="SoloLedger API — 1-Person Corp Expense & Tax Engine",
    version=__version__,
    lifespan=lifespan,
)


def _verify_api_key(x_api_key: Optional[str], sololedger_api_key: Optional[str]) -> None:
    provided = x_api_key or sololedger_api_key
    if provided != settings.api_key:
        raise HTTPException(status_code=401, detail="Unauthorized webhook API key.")


@app.get("/health")
async def health_check():
    """Health check endpoint verifying service and DB readiness."""
    init_db()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tax_classification_rules WHERE is_active = 1")
        active_rules = cur.fetchone()[0]
        return {
            "status": "ok",
            "service": "sololedger-api",
            "version": __version__,
            "active_tax_rules": active_rules,
        }
    finally:
        conn.close()


@app.post("/api/v1/webhooks/sms")
async def receive_card_sms_webhook(
    payload: SmsWebhookPayload,
    background_tasks: BackgroundTasks,
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Webhook endpoint receiving real-time corporate card SMS from Android/iOS."""
    _verify_api_key(x_api_key, sololedger_api_key)

    try:
        parsed = parse_korean_card_sms(payload.raw_message, payload.received_at)
    except ValueError as e:
        return {"status": "ignored", "reason": str(e)}

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Idempotency check: prevent duplicate SMS insertions within the same timestamp
        cursor.execute(
            """
            SELECT tx_id FROM card_transactions
            WHERE raw_merchant_name = ? AND total_amount = ? AND transacted_at = ?
            """,
            (parsed.raw_merchant_name, parsed.total_amount, parsed.transacted_at),
        )
        existing = cursor.fetchone()
        if existing:
            return {"status": "duplicate_skipped", "tx_id": existing[0]}

        rule_result = evaluate_tax_rules(cursor, parsed.raw_merchant_name)
        supply_value, vat_amount = calculate_supply_and_vat(
            parsed.total_amount, rule_result["vat_deduction_type"]
        )
        status_code = "CANCELED" if parsed.is_cancel else "SMS_ONLY"

        cursor.execute(
            """
            INSERT INTO card_transactions (
                card_last4, approval_no, transacted_at, raw_merchant_name,
                total_amount, supply_value, vat_amount,
                account_code, account_name, vat_deduction_type,
                non_deduct_reason, is_vehicle_expense, reconcile_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parsed.card_last4,
                parsed.approval_no,
                parsed.transacted_at,
                parsed.raw_merchant_name,
                parsed.total_amount,
                supply_value,
                vat_amount,
                rule_result["account_code"],
                rule_result["account_name"],
                rule_result["vat_deduction_type"],
                rule_result["non_deduct_reason"],
                rule_result["is_vehicle_expense"],
                status_code,
            ),
        )
        tx_id = cursor.lastrowid
        conn.commit()

        if rule_result["require_memo_prompt"] and not parsed.is_cancel:
            background_tasks.add_task(send_telegram_memo_prompt, tx_id, parsed, rule_result)

        return {
            "status": "recorded",
            "tx_id": tx_id,
            "merchant": parsed.raw_merchant_name,
            "amount": parsed.total_amount,
            "account_code": rule_result["account_code"],
            "account_name": rule_result["account_name"],
            "vat_deduction_type": rule_result["vat_deduction_type"],
            "is_vehicle_expense": rule_result["is_vehicle_expense"],
            "reconcile_status": status_code,
        }
    finally:
        conn.close()


@app.post("/api/v1/webhooks/telegram")
async def receive_telegram_webhook(
    update: Dict[str, Any] = Body(...),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Webhook endpoint handling Telegram Bot (@SoloLedgerBot) callbacks and memo replies."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        result = handle_telegram_update(cursor, update)
        conn.commit()
        return result
    finally:
        conn.close()


@app.post("/api/v1/receipts/upload")
async def upload_receipt_endpoint(
    request: ReceiptUploadRequest,
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Upload a receipt image, deduplicate by SHA-256, parse via Vision OCR, and link to ledger."""
    _verify_api_key(x_api_key, sololedger_api_key)
    try:
        file_bytes = base64.b64decode(request.file_content_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64 file content: {e}") from e

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        try:
            result = await process_and_link_receipt(
                cursor,
                file_bytes=file_bytes,
                original_file_name=request.file_name,
                source_channel=request.source_channel,
                explicit_tx_id=request.tx_id,
                ocr_override=request.ocr_override,
            )
            conn.commit()
            return {"status": "archived", **result}
        except DuplicateReceiptError as dup_err:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_receipt",
                    "message": str(dup_err),
                    "file_hash_sha256": dup_err.file_hash,
                    "existing_receipt_id": dup_err.existing_receipt_id,
                },
            ) from dup_err
    finally:
        conn.close()


@app.get("/api/v1/receipts")
async def list_receipts(
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """List archived receipt OCR metadata records."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM receipt_ocr_metadata ORDER BY receipt_id DESC")
        rows = [dict(r) for r in cursor.fetchall()]
        return {"count": len(rows), "items": rows}
    finally:
        conn.close()


@app.post("/api/v1/merchants/verify")
async def verify_merchant_endpoint(
    request: MerchantVerifyRequest,
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Verify merchant business registration status via 30-day TTL cache or NTS API."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        try:
            status = await get_or_verify_merchant(
                cursor,
                biz_reg_no=request.biz_reg_no,
                merchant_name=request.merchant_name,
                default_account_code=request.default_account_code,
                force_refresh=request.force_refresh,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        updated_tx_count = apply_merchant_tax_status_to_ledger(cursor, status)
        conn.commit()
        return {
            "status": "verified",
            "merchant": status.model_dump(),
            "updated_transactions": updated_tx_count,
        }
    finally:
        conn.close()


@app.get("/api/v1/merchants")
async def list_merchants(
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """List cached merchants and their NTS verification status."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM merchants ORDER BY nts_verified_at DESC")
        rows = [dict(r) for r in cursor.fetchall()]
        return {"count": len(rows), "items": rows}
    finally:
        conn.close()


@app.post("/api/v1/reconcile")
async def reconcile_transactions(
    request: ReconcileBatchRequest,
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Batch reconcile Hometax or OCR records against the card_transactions ledger."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        results = [reconcile_single_item(cursor, item) for item in request.items]
        conn.commit()
        return {"status": "reconciled", "count": len(results), "results": results}
    finally:
        conn.close()


@app.post("/api/v1/imports/hometax-csv")
async def import_hometax_csv_endpoint(
    request: HometaxCsvImportRequest,
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Import Hometax or corporate card purchase CSV (UTF-8 / CP949) and run 3-stage reconciliation."""
    _verify_api_key(x_api_key, sololedger_api_key)
    if request.csv_content_b64:
        try:
            raw_bytes = base64.b64decode(request.csv_content_b64)
            csv_text = decode_csv_bytes(raw_bytes)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 CSV payload: {e}") from e
    elif request.csv_text:
        csv_text = request.csv_text
    else:
        raise HTTPException(
            status_code=400, detail="Either csv_content_b64 or csv_text must be provided."
        )

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        outcome = import_and_reconcile_hometax_csv(cursor, csv_text)
        conn.commit()
        return outcome
    finally:
        conn.close()


@app.get("/api/v1/analytics/vehicle-limit")
async def vehicle_limit_endpoint(
    year: int = Query(default_factory=lambda: datetime.now().year),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Query cumulative corporate passenger car expense vs. the 15,000,000 KRW annual limit."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        return get_vehicle_expense_summary(cursor, year=year)
    finally:
        conn.close()


@app.post("/api/v1/exports/tax-pack")
async def export_tax_pack_endpoint(
    request: TaxPackExportRequest,
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Generate tax accountant standard CSV and normalized receipt ZIP archive for YYYYMM."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        outcome = generate_tax_accountant_pack(
            cursor,
            year_month=request.year_month,
            force_reexport=request.force_reexport,
        )
        conn.commit()
        return outcome
    finally:
        conn.close()


@app.get("/api/v1/transactions")
async def list_transactions(
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """List recorded corporate card transactions ordered by transacted_at DESC."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM card_transactions ORDER BY transacted_at DESC, tx_id DESC")
        rows = [dict(r) for r in cursor.fetchall()]
        return {"count": len(rows), "items": rows}
    finally:
        conn.close()


@app.patch("/api/v1/transactions/{tx_id}")
async def patch_transaction_endpoint(
    tx_id: int,
    request: TransactionUpdateRequest,
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Update a transaction's account classification, VAT deduction status, or memo."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        try:
            updated = update_transaction_classification(
                cursor,
                tx_id=tx_id,
                account_code=request.account_code,
                account_name=request.account_name,
                vat_deduction_type=request.vat_deduction_type,
                non_deduct_reason=request.non_deduct_reason,
                counterparty_info=request.counterparty_info,
            )
            conn.commit()
            return {"status": "updated", "transaction": updated}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    finally:
        conn.close()


@app.get("/api/v1/dashboard/summary")
async def dashboard_summary_endpoint(
    year_month: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Return aggregated dashboard summary metrics, pending items, and vehicle limit."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        return build_dashboard_summary(cursor, year_month=year_month, year=year)
    finally:
        conn.close()


@app.post("/api/v1/system/backup-nas")
async def backup_nas_endpoint(
    request: NasBackupRequest = Body(default_factory=NasBackupRequest),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Safely back up the active SQLite database and mirror receipt archives to NAS storage."""
    from pathlib import Path

    _verify_api_key(x_api_key, sololedger_api_key)
    target_dir = Path(request.nas_dir) if request.nas_dir else None
    return sync_to_nas(target_nas_dir=target_dir)


@app.post("/api/v1/system/seed-demo")
async def seed_demo_endpoint(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Seed realistic test/demo corporate card transactions and sample receipts."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        ym = payload.get("year_month") if isinstance(payload, dict) else None
        outcome = seed_demo_transactions(cursor, year_month=ym)
        conn.commit()
        return outcome
    finally:
        conn.close()


@app.post("/api/v1/system/reset-test")
async def reset_test_endpoint(
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Clear all transactions, receipts, and cached merchants from the test ledger."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        outcome = reset_test_ledger(cursor)
        conn.commit()
        return outcome
    finally:
        conn.close()


@app.get("/dashboard", response_class=HTMLResponse)
async def render_dashboard_page(
    year_month: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
):
    """Render the lightweight HTML5 management dashboard."""
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        html_doc = render_dashboard_html(cursor, year_month=year_month, year=year)
        return HTMLResponse(content=html_doc)
    finally:
        conn.close()
