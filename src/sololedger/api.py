"""FastAPI application for SoloLedger SMS Webhook, NTS Merchant Cache, and Reconciliation."""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from sololedger import __version__
from sololedger.config import settings
from sololedger.db import get_connection, init_db
from sololedger.merchant_service import (
    apply_merchant_tax_status_to_ledger,
    get_or_verify_merchant,
)
from sololedger.models import (
    MerchantVerifyRequest,
    ReconcileBatchRequest,
    SmsWebhookPayload,
)
from sololedger.notifier import send_telegram_memo_prompt
from sololedger.reconciler import reconcile_single_item
from sololedger.sms_parser import parse_korean_card_sms
from sololedger.tax_engine import calculate_supply_and_vat, evaluate_tax_rules


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
