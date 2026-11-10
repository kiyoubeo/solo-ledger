"""FastAPI application for SoloLedger SMS Webhook, NTS Cache, Vision OCR, Telegram Bot, and Tax Export."""

import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from sololedger import __version__
from sololedger.archiver import DuplicateReceiptError, build_receipt_print_title
from sololedger.config import PROJECT_ROOT, settings
from sololedger.dashboard import (
    build_dashboard_summary,
    reapply_tax_rules_to_ledger,
    render_dashboard_html,
    reset_test_ledger,
    seed_demo_transactions,
    update_transaction_classification,
)
from sololedger.db import get_connection, init_db
from sololedger.email_watcher import (
    delete_email_account_entry,
    get_public_email_watcher_config,
    ingest_email_order_payload,
    load_email_watcher_config,
    save_email_watcher_config,
    sync_imap_order_emails,
    update_email_account_entry,
)
from sololedger.exporters import generate_tax_accountant_pack
from sololedger.importers import decode_csv_bytes, import_and_reconcile_hometax_csv
from sololedger.merchant_service import (
    apply_merchant_tax_status_to_ledger,
    get_or_verify_merchant,
)
from sololedger.models import (
    AppPushWebhookPayload,
    HometaxCsvImportRequest,
    MerchantVerifyRequest,
    MobileLoginRequest,
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
from sololedger.phone_adb import import_phone_sms_to_ledger
from sololedger.reconciler import reconcile_single_item
from sololedger.scraper_service import (
    SUPPORTED_SCRAPER_TARGETS,
    auto_crawl_coupang_orders,
    build_coupang_browser_extractor_script,
    generate_electronic_slips_for_ledger,
    generate_electronic_statement_for_tx,
    ingest_scraped_payload,
)
from sololedger.sms_parser import (
    CORP_CARD_ISSUER_PREFIX,
    format_card_label,
    get_effective_card_label_map,
    is_allowed_corporate_card,
    load_corp_card_policy,
    parse_korean_card_sms,
    register_corp_card,
    remove_corp_card,
    sanitize_card_folder_name,
    save_corp_card_policy,
)
from sololedger.tax_engine import calculate_supply_and_vat, evaluate_tax_rules
from sololedger.telegram_bot import handle_telegram_update
from sololedger.vehicle_tracker import get_vehicle_expense_summary


async def _background_email_watcher_loop() -> None:
    """Periodically poll IMAP inbox in the background when auto_poll_enabled is active."""
    last_run_ts = 0.0
    while True:
        await asyncio.sleep(5.0)
        try:
            cfg = load_email_watcher_config()
            if not cfg.get("auto_poll_enabled"):
                continue
            interval = max(30, int(cfg.get("poll_interval_sec") or 120))
            now_ts = datetime.now().timestamp()
            if now_ts - last_run_ts < interval:
                continue
            last_run_ts = now_ts

            def _poll_once() -> None:
                init_db()
                conn = get_connection()
                cur = conn.cursor()
                try:
                    sync_imap_order_emails(cur)
                    conn.commit()
                finally:
                    conn.close()

            await asyncio.to_thread(_poll_once)
        except asyncio.CancelledError:
            break
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize SQLite schema, seed rules, and start background email watcher on startup."""
    init_db()
    watcher_task = asyncio.create_task(_background_email_watcher_loop())
    try:
        yield
    finally:
        watcher_task.cancel()


app = FastAPI(
    title="SoloLedger API — 1-Person Corp Expense & Tax Engine",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


_current_request_var: contextvars.ContextVar[Optional[Request]] = contextvars.ContextVar(
    "sololedger_current_request", default=None
)
SESSION_COOKIE_NAME = "sololedger_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


def _create_session_token(ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    exp = int(time.time()) + max(60, ttl_seconds)
    payload = f"sl1.{exp}"
    sig = hmac.new(
        settings.api_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


def _verify_session_token(token: Optional[str]) -> bool:
    if not token or not isinstance(token, str):
        return False
    cleaned = token.strip()
    if hmac.compare_digest(cleaned.encode("utf-8"), settings.api_key.encode("utf-8")):
        return True
    parts = cleaned.split(".")
    if len(parts) != 3 or parts[0] != "sl1" or not parts[1].isdigit():
        return False
    exp = int(parts[1])
    if exp < int(time.time()):
        return False
    expected_sig = hmac.new(
        settings.api_key.encode("utf-8"),
        f"sl1.{exp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(parts[2], expected_sig)


def _is_credential_valid(candidate: str) -> bool:
    if not candidate:
        return False
    cand_bytes = candidate.encode("utf-8")
    if hmac.compare_digest(cand_bytes, settings.api_key.encode("utf-8")):
        return True
    admin_pw = settings.admin_password.strip()
    if admin_pw and hmac.compare_digest(cand_bytes, admin_pw.encode("utf-8")):
        return True
    return False


def _has_valid_auth(
    x_api_key: Optional[str] = None,
    sololedger_api_key: Optional[str] = None,
) -> bool:
    provided = x_api_key or sololedger_api_key
    if provided and _verify_session_token(provided):
        return True
    req = _current_request_var.get()
    if req is not None:
        hdr_key = req.headers.get("x-api-key") or req.headers.get("sololedger_api_key")
        if hdr_key and _verify_session_token(hdr_key):
            return True
        auth_hdr = req.headers.get("authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            bearer_tok = auth_hdr[7:].strip()
            if _verify_session_token(bearer_tok):
                return True
        cookie_tok = req.cookies.get(SESSION_COOKIE_NAME)
        if cookie_tok and _verify_session_token(cookie_tok):
            return True
    return False


@app.middleware("http")
async def add_private_network_access_header(request: Request, call_next):
    """Allow HTTPS pages (e.g., mc.coupang.com) and mobile clients to call SoloLedger API."""
    ctx_token = _current_request_var.set(request)
    try:
        if request.method == "OPTIONS":
            resp = Response(status_code=204)
        else:
            resp = await call_next(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        return resp
    finally:
        _current_request_var.reset(ctx_token)


def _verify_api_key(
    x_api_key: Optional[str] = None,
    sololedger_api_key: Optional[str] = None,
) -> None:
    if not _has_valid_auth(x_api_key, sololedger_api_key):
        raise HTTPException(status_code=401, detail="Unauthorized API key or session token.")


def _verify_read_access() -> None:
    """Enforce authentication on read/UI endpoints when settings.require_auth is enabled."""
    if settings.require_auth and not _has_valid_auth():
        raise HTTPException(status_code=401, detail="Authentication required.")


@app.post("/api/v1/auth/login")
async def mobile_auth_login_endpoint(payload: MobileLoginRequest):
    """Authenticate Android app or remote client and issue a 30-day signed Bearer token."""
    cred = (payload.credential or "").strip()
    if not _is_credential_valid(cred):
        raise HTTPException(status_code=401, detail="비밀번호 또는 API 키가 일치하지 않습니다.")
    token = _create_session_token()
    policy = load_corp_card_policy()
    return {
        "status": "authenticated",
        "token": token,
        "version": __version__,
        "device_name": payload.device_name or "Android",
        "require_auth": settings.require_auth,
        "card_labels": get_effective_card_label_map(),
        "corp_only_mode": bool(policy.get("corp_only_mode", False)),
    }


def _render_login_html(error_msg: str = "") -> str:
    err_block = (
        f'<div style="background:#fee2e2;color:#b91c1c;padding:10px;border-radius:8px;'
        f'font-size:13px;margin-bottom:12px;">{error_msg}</div>'
        if error_msg
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>SoloLedger 로그인</title>
<style>
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Malgun Gothic',sans-serif;
         background:#f1f5f9; display:flex; align-items:center; justify-content:center;
         min-height:100vh; margin:0; }}
  .card {{ background:#fff; padding:28px; border-radius:14px; width:92%; max-width:360px;
          box-shadow:0 10px 25px rgba(15,23,42,0.08); }}
  h1 {{ margin:0 0 6px; font-size:20px; color:#0f172a; }}
  p {{ margin:0 0 18px; font-size:13px; color:#64748b; }}
  input {{ width:100%; box-sizing:border-box; padding:11px 12px; border:1px solid #cbd5e1;
          border-radius:8px; font-size:14px; margin-bottom:12px; }}
  button {{ width:100%; padding:11px; background:#2563eb; color:#fff; border:none;
           border-radius:8px; font-size:14px; font-weight:700; cursor:pointer; }}
</style></head>
<body><div class="card">
  <h1>🔐 SoloLedger 원격 접속</h1>
  <p>관리자 비밀번호 또는 API 키를 입력해 주세요.</p>
  {err_block}
  <form method="post" action="/login">
    <input type="password" name="credential" placeholder="비밀번호 또는 SOLOLEDGER_API_KEY"
           required autofocus />
    <button type="submit">로그인</button>
  </form>
</div></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def render_login_page():
    """Render web login page for remote dashboard access."""
    return HTMLResponse(content=_render_login_html())


@app.post("/login")
async def submit_login_page(request: Request):
    """Validate web login form and set HttpOnly session cookie."""
    body_bytes = await request.body()
    from urllib.parse import parse_qs

    form_data = parse_qs(body_bytes.decode("utf-8", errors="ignore"))
    cred = (form_data.get("credential", [""])[0] or "").strip()
    if not _is_credential_valid(cred):
        return HTMLResponse(
            content=_render_login_html("비밀번호 또는 API 키가 올바르지 않습니다."),
            status_code=401,
        )
    token = _create_session_token()
    resp = RedirectResponse(url="/dashboard", status_code=303)
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return resp


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

    if not is_allowed_corporate_card(parsed.card_last4, raw_text=payload.raw_message):
        return {
            "status": "ignored",
            "reason": "Blocked personal or unregistered card in corporate card mode.",
            "card_last4": parsed.card_last4,
        }

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


@app.post("/api/v1/webhooks/push")
async def receive_card_app_push_webhook(
    payload: AppPushWebhookPayload,
    background_tasks: BackgroundTasks,
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Webhook endpoint receiving Android NotificationListenerService card push alerts (e.g. Woori WON Card)."""
    _verify_api_key(x_api_key, sololedger_api_key)

    combined_text = "\n".join(
        part.strip() for part in (payload.title or "", payload.body or "") if part and part.strip()
    )
    try:
        parsed = parse_korean_card_sms(
            combined_text,
            fallback_time=payload.received_at,
            card_hint=payload.card_hint,
        )
    except ValueError as e:
        return {"status": "ignored", "reason": str(e), "package_name": payload.package_name}

    if not is_allowed_corporate_card(parsed.card_last4, raw_text=combined_text):
        return {
            "status": "ignored",
            "reason": "Blocked personal or unregistered card in corporate card mode.",
            "card_last4": parsed.card_last4,
            "package_name": payload.package_name,
        }

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
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
            "card_last4": parsed.card_last4,
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


@app.get("/api/v1/exports/tax-pack/{year_month}/download")
async def download_tax_pack_zip_endpoint(
    year_month: str,
    force_reexport: bool = Query(default=False),
    auto_generate: bool = Query(default=True),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Stream SoloLedger_세무증빙_{year_month}.zip to mobile/remote clients for direct sharing."""
    _verify_api_key(x_api_key, sololedger_api_key)
    if not re.fullmatch(r"\d{6}", year_month or ""):
        raise HTTPException(status_code=400, detail="Invalid year_month format (expected YYYYMM).")

    zip_filename = f"SoloLedger_세무증빙_{year_month}.zip"
    zip_path = settings.archive_dir / "exports" / zip_filename
    if force_reexport or (auto_generate and not zip_path.exists()):
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        try:
            generate_tax_accountant_pack(
                cursor,
                year_month=year_month,
                force_reexport=force_reexport or (not zip_path.exists()),
            )
            conn.commit()
        finally:
            conn.close()

    if not zip_path.exists() or not zip_path.is_file():
        raise HTTPException(status_code=404, detail=f"Tax pack ZIP not found for {year_month}.")

    encoded_name = quote(zip_filename)
    headers = {
        "Content-Disposition": (
            f'attachment; filename="SoloLedger_tax_pack_{year_month}.zip"; '
            f"filename*=UTF-8''{encoded_name}"
        )
    }
    return Response(
        content=zip_path.read_bytes(),
        media_type="application/zip",
        headers=headers,
    )


@app.get("/api/v1/exports/tax-pack/{year_month}/csv")
async def download_tax_pack_csv_endpoint(
    year_month: str,
    force_reexport: bool = Query(default=False),
    auto_generate: bool = Query(default=True),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Stream SoloLedger_기장원장_{year_month}.csv (UTF-8-BOM) to mobile/remote clients."""
    _verify_api_key(x_api_key, sololedger_api_key)
    if not re.fullmatch(r"\d{6}", year_month or ""):
        raise HTTPException(status_code=400, detail="Invalid year_month format (expected YYYYMM).")

    csv_filename = f"SoloLedger_기장원장_{year_month}.csv"
    csv_path = settings.archive_dir / "exports" / csv_filename
    if force_reexport or (auto_generate and not csv_path.exists()):
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        try:
            generate_tax_accountant_pack(
                cursor,
                year_month=year_month,
                force_reexport=force_reexport or (not csv_path.exists()),
            )
            conn.commit()
        finally:
            conn.close()

    if not csv_path.exists() or not csv_path.is_file():
        raise HTTPException(status_code=404, detail=f"Tax pack CSV not found for {year_month}.")

    encoded_name = quote(csv_filename)
    headers = {
        "Content-Disposition": (
            f'attachment; filename="SoloLedger_ledger_{year_month}.csv"; '
            f"filename*=UTF-8''{encoded_name}"
        )
    }
    return Response(
        content=csv_path.read_bytes(),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


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
                apply_to_same_merchant=request.apply_to_same_merchant,
            )
            conn.commit()
            return {"status": "updated", "transaction": updated}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    finally:
        conn.close()


@app.get("/api/v1/transactions/{tx_id}/receipt")
async def get_transaction_receipt_detail(tx_id: int):
    """Return electronic card sales slip details and attached receipt metadata for a transaction."""
    _verify_read_access()
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
        tx_row = cursor.fetchone()
        if tx_row is None:
            raise HTTPException(status_code=404, detail=f"Transaction #{tx_id} not found.")
        tx_dict = dict(tx_row)

        cursor.execute(
            """
            SELECT * FROM receipt_ocr_metadata
            WHERE tx_id = ?
            ORDER BY receipt_id DESC
            LIMIT 1
            """,
            (tx_id,),
        )
        rec_row = cursor.fetchone()
        rec_dict = dict(rec_row) if rec_row else None

        has_file = False
        is_renderable_image = False
        archived_name = rec_dict["archived_file_name"] if rec_dict else None
        line_items_list = []
        marketplace_seller = None
        if rec_dict:
            raw_li = rec_dict.get("line_items_json")
            if raw_li:
                try:
                    parsed_li = json.loads(str(raw_li))
                    if isinstance(parsed_li, list):
                        line_items_list = [r for r in parsed_li if isinstance(r, dict)]
                except Exception:
                    pass
            raw_ocr_meta = rec_dict.get("raw_ocr_response")
            if raw_ocr_meta:
                try:
                    parsed_meta = json.loads(str(raw_ocr_meta))
                    if isinstance(parsed_meta, dict) and parsed_meta.get("marketplace_seller"):
                        marketplace_seller = str(parsed_meta["marketplace_seller"]).strip() or None
                except Exception:
                    pass
            candidates = [
                Path(rec_dict["original_file_path"]),
                settings.archive_dir / "normalized" / str(archived_name or ""),
            ]
            for p in candidates:
                if p.exists() and p.is_file():
                    has_file = True
                    head = p.read_bytes()[:12]
                    if (
                        head.startswith(b"\x89PNG")
                        or head.startswith(b"\xff\xd8")
                        or head.startswith(b"GIF8")
                        or head.startswith(b"RIFF")
                    ):
                        is_renderable_image = True
                    break

        card_code = str(tx_dict.get("card_last4") or "0000")
        print_title = build_receipt_print_title(
            tx_id=tx_id,
            transacted_at=str(tx_dict["transacted_at"]),
            card_last4=card_code,
            merchant_name=str(tx_dict["raw_merchant_name"]),
            account_name=str(tx_dict["account_name"]),
            total_amount=int(tx_dict["total_amount"]),
        )
        statement_print_title = print_title.replace("매출전표_", "거래명세서_", 1)
        return {
            "tx_id": tx_id,
            "card_last4": card_code,
            "card_label": format_card_label(card_code),
            "card_folder": sanitize_card_folder_name(card_code),
            "print_title": print_title,
            "statement_print_title": statement_print_title,
            "transacted_at": tx_dict["transacted_at"],
            "approval_no": tx_dict.get("approval_no"),
            "raw_merchant_name": tx_dict["raw_merchant_name"],
            "biz_reg_no": tx_dict.get("biz_reg_no"),
            "marketplace_seller": marketplace_seller,
            "total_amount": int(tx_dict["total_amount"]),
            "supply_value": int(tx_dict["supply_value"]),
            "vat_amount": int(tx_dict["vat_amount"]),
            "account_code": tx_dict["account_code"],
            "account_name": tx_dict["account_name"],
            "vat_deduction_type": tx_dict["vat_deduction_type"],
            "non_deduct_reason": tx_dict.get("non_deduct_reason"),
            "counterparty_info": tx_dict.get("counterparty_info"),
            "reconcile_status": tx_dict["reconcile_status"],
            "has_receipt_file": has_file,
            "is_renderable_image": is_renderable_image,
            "archived_file_name": archived_name,
            "line_items": line_items_list,
            "receipt_file_url": f"/api/v1/transactions/{tx_id}/receipt-file" if has_file else None,
        }
    finally:
        conn.close()


@app.post("/api/v1/transactions/{tx_id}/statement")
async def save_transaction_statement_endpoint(
    tx_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Generate an electronic Transaction Statement (거래명세서) or attach a statement file for tx_id."""
    _verify_api_key(x_api_key, sololedger_api_key)
    item_name = str(payload.get("item_name") or "").strip() or None
    account_code = str(payload.get("account_code") or "").strip() or None
    file_name = str(payload.get("file_name") or "").strip() or None
    file_bytes: Optional[bytes] = None
    if payload.get("file_content_b64"):
        try:
            file_bytes = base64.b64decode(str(payload["file_content_b64"]))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 payload: {e}") from e

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        try:
            outcome = generate_electronic_statement_for_tx(
                cursor,
                tx_id=tx_id,
                item_name=item_name,
                account_code=account_code,
                file_bytes=file_bytes,
                file_name=file_name,
            )
            conn.commit()
            return outcome
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    finally:
        conn.close()


@app.get("/api/v1/transactions/{tx_id}/receipt-file")
async def serve_transaction_receipt_file(tx_id: int):
    """Serve the archived receipt file (image, PDF, or text) linked to a transaction."""
    _verify_read_access()
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT original_file_path, archived_file_name
            FROM receipt_ocr_metadata
            WHERE tx_id = ?
            ORDER BY receipt_id DESC
            LIMIT 1
            """,
            (tx_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No receipt attached to #{tx_id}.")

        candidates = [
            Path(row["original_file_path"]),
            settings.archive_dir / "normalized" / str(row["archived_file_name"] or ""),
        ]
        target_path = next((p for p in candidates if p.exists() and p.is_file()), None)
        if target_path is None:
            raise HTTPException(status_code=404, detail="Receipt file not found on disk.")

        raw = target_path.read_bytes()
        headers: Dict[str, str] = {}
        if raw.startswith(b"\x89PNG"):
            media = "image/png"
        elif raw.startswith(b"\xff\xd8"):
            media = "image/jpeg"
        elif raw.startswith(b"GIF8"):
            media = "image/gif"
        elif raw.startswith(b"%PDF"):
            media = "application/pdf"
        elif target_path.suffix.lower() in (".html", ".htm") or b"<!DOCTYPE html" in raw[:120]:
            media = "text/html; charset=utf-8"
            headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
            )
        else:
            media = "text/plain; charset=utf-8"
        return Response(content=raw, media_type=media, headers=headers)
    finally:
        conn.close()


@app.get("/api/v1/dashboard/summary")
async def dashboard_summary_endpoint(
    year_month: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    card_last4: Optional[str] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    account_code: Optional[str] = Query(default=None),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Return aggregated dashboard summary metrics, per-card breakdown, and vehicle limit."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        return build_dashboard_summary(
            cursor,
            year_month=year_month,
            year=year,
            card_last4=card_last4,
            start_date=start_date,
            end_date=end_date,
            account_code=account_code,
        )
    finally:
        conn.close()


@app.post("/api/v1/system/backup-nas")
async def backup_nas_endpoint(
    request: NasBackupRequest = Body(default_factory=NasBackupRequest),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Safely back up the active SQLite database and mirror receipt archives to NAS storage."""
    _verify_api_key(x_api_key, sololedger_api_key)
    target_dir: Optional[Path] = None
    if request.nas_dir:
        if settings.app_env == "production":
            raw_dir = request.nas_dir.strip()
            if raw_dir.startswith("\\\\") or raw_dir.startswith("//"):
                raise HTTPException(
                    status_code=400,
                    detail="Arbitrary UNC path overrides are disabled in production.",
                )
            configured_root = settings.nas_backup_dir.resolve(strict=False)
            requested_root = Path(raw_dir).resolve(strict=False)
            if requested_root != configured_root and configured_root not in requested_root.parents:
                raise HTTPException(
                    status_code=400,
                    detail="NAS backup path override outside SOLOLEDGER_NAS_DIR is blocked in production.",
                )
        target_dir = Path(request.nas_dir)
    return sync_to_nas(target_nas_dir=target_dir)


@app.post("/api/v1/system/seed-demo")
async def seed_demo_endpoint(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Seed realistic test/demo corporate card transactions and sample receipts."""
    _verify_api_key(x_api_key, sololedger_api_key)
    if settings.app_env == "production":
        raise HTTPException(status_code=403, detail="Demo seeding is disabled in production.")
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
    if settings.app_env == "production":
        raise HTTPException(status_code=403, detail="Ledger reset is disabled in production.")
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        outcome = reset_test_ledger(cursor)
        conn.commit()
        return outcome
    finally:
        conn.close()


@app.post("/api/v1/system/import-phone-sms")
async def import_phone_sms_endpoint(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Fetch card SMS messages from connected USB Android phone via ADB and import into ledger."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        limit = int(payload.get("limit", 30)) if isinstance(payload, dict) else 30
        serial = payload.get("device_serial") if isinstance(payload, dict) else None
        raw_adb = payload.get("raw_adb_output") if isinstance(payload, dict) else None
        outcome = import_phone_sms_to_ledger(
            cursor,
            limit=limit,
            device_serial=serial,
            raw_adb_output=raw_adb,
        )
        conn.commit()
        return outcome
    finally:
        conn.close()


@app.post("/api/v1/system/reapply-rules")
async def reapply_rules_endpoint(
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Re-apply expanded Firefly III tax rules and Ledgerline smart default memos to ledger."""
    _verify_api_key(x_api_key, sololedger_api_key)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        outcome = reapply_tax_rules_to_ledger(cursor)
        conn.commit()
        return outcome
    finally:
        conn.close()


@app.get("/api/v1/system/corp-card-policy")
async def get_corp_card_policy_endpoint():
    """Return active corporate card dedicated mode policy and registered corporate cards."""
    _verify_read_access()
    policy = load_corp_card_policy()
    return {
        **policy,
        "issuer_presets": CORP_CARD_ISSUER_PREFIX,
    }


@app.post("/api/v1/system/corp-card-policy")
async def update_corp_card_policy_endpoint(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Update corporate card mode settings and optionally register a corporate card."""
    _verify_api_key(x_api_key, sololedger_api_key)
    updates: Dict[str, Any] = {}
    for bool_key in (
        "corp_only_mode",
        "strict_registered_only",
        "block_unmatched_email_orders",
        "auto_register_on_statement_upload",
    ):
        if bool_key in payload:
            updates[bool_key] = bool(payload[bool_key])
    if "blocked_personal_cards" in payload and isinstance(payload["blocked_personal_cards"], list):
        updates["blocked_personal_cards"] = payload["blocked_personal_cards"]

    policy = save_corp_card_policy(updates) if updates else load_corp_card_policy()
    if payload.get("card_last4"):
        try:
            policy = register_corp_card(
                card_last4=str(payload["card_last4"]),
                issuer=str(payload.get("issuer") or "CORP"),
                label=str(payload.get("label") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "updated",
        **policy,
        "issuer_presets": CORP_CARD_ISSUER_PREFIX,
    }


@app.delete("/api/v1/system/corp-cards/{card_last4}")
async def delete_corp_card_endpoint(
    card_last4: str,
    add_to_blocked: bool = Query(default=False),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Remove a registered corporate card from the allowlist."""
    _verify_api_key(x_api_key, sololedger_api_key)
    policy = remove_corp_card(card_last4, add_to_blocked=bool(add_to_blocked))
    return {
        "status": "removed",
        **policy,
        "issuer_presets": CORP_CARD_ISSUER_PREFIX,
    }


@app.get("/api/v1/scraper/targets")
async def list_scraper_targets_endpoint():
    """List supported card issuer, Hometax, Credit Finance Assoc., and e-commerce scraper targets."""
    return {"count": len(SUPPORTED_SCRAPER_TARGETS), "targets": SUPPORTED_SCRAPER_TARGETS}


@app.get("/api/v1/scraper/coupang-script")
async def get_coupang_extractor_script_endpoint():
    """Return 1-click browser console script for extracting Coupang order list & receipts."""
    _verify_read_access()
    script = build_coupang_browser_extractor_script(
        api_url="http://127.0.0.1:8000",
        api_key=settings.api_key,
    )
    return {
        "portal": "COUPANG",
        "target_url": SUPPORTED_SCRAPER_TARGETS["COUPANG"]["url"],
        "script": script,
    }


@app.post("/api/v1/scraper/coupang-auto-crawl")
async def coupang_auto_crawl_endpoint(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Launch persistent local browser session via CDP, auto-collect Coupang orders, and match."""
    _verify_api_key(x_api_key, sololedger_api_key)
    max_wait = int(payload.get("max_wait_sec", 180)) if isinstance(payload, dict) else 180

    def _run_sync() -> Dict[str, Any]:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        try:
            res = auto_crawl_coupang_orders(cursor, max_wait_sec=max_wait)
            conn.commit()
            return res
        finally:
            conn.close()

    return await asyncio.to_thread(_run_sync)


@app.get("/api/v1/scraper/email-config")
async def get_email_watcher_config_endpoint():
    """Return current IMAP email watcher configuration (with password masked) and sync status."""
    _verify_read_access()
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    try:
        return get_public_email_watcher_config(cursor=cur)
    finally:
        conn.close()


@app.post("/api/v1/scraper/email-config")
async def update_email_watcher_config_endpoint(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Save IMAP email watcher configuration and optionally run an immediate sync."""
    _verify_api_key(x_api_key, sololedger_api_key)
    save_email_watcher_config(payload if isinstance(payload, dict) else {})
    sync_result: Optional[Dict[str, Any]] = None
    if isinstance(payload, dict) and payload.get("sync_now"):
        rescan_days = int(payload.get("recent_rescan_days", 7))

        def _run_sync() -> Dict[str, Any]:
            init_db()
            conn = get_connection()
            cur = conn.cursor()
            try:
                res = sync_imap_order_emails(
                    cur,
                    force_rescan=bool(payload.get("force_rescan", False)),
                    recent_rescan_days=rescan_days,
                )
                conn.commit()
                return res
            finally:
                conn.close()

        sync_result = await asyncio.to_thread(_run_sync)

    init_db()
    conn = get_connection()
    cur = conn.cursor()
    try:
        out = get_public_email_watcher_config(cursor=cur)
    finally:
        conn.close()
    if sync_result is not None:
        out["sync_result"] = sync_result
    return out


@app.patch("/api/v1/scraper/email-accounts/{account_id}")
async def patch_email_account_endpoint(
    account_id: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Toggle enabled state or update settings for a specific registered email account."""
    _verify_api_key(x_api_key, sololedger_api_key)
    try:
        update_email_account_entry(account_id, payload if isinstance(payload, dict) else {})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    try:
        return get_public_email_watcher_config(cursor=cur)
    finally:
        conn.close()


@app.delete("/api/v1/scraper/email-accounts/{account_id}")
async def delete_email_account_endpoint(
    account_id: str,
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Delete a registered IMAP email account by account_id."""
    _verify_api_key(x_api_key, sololedger_api_key)
    delete_email_account_entry(account_id)
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    try:
        return get_public_email_watcher_config(cursor=cur)
    finally:
        conn.close()


@app.post("/api/v1/scraper/email-sync")
async def sync_email_orders_endpoint(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Immediately scan all enabled IMAP mailboxes (or one account_id) and reconcile."""
    _verify_api_key(x_api_key, sololedger_api_key)
    force = bool(payload.get("force_rescan", False)) if isinstance(payload, dict) else False
    max_msgs = int(payload.get("max_messages", 600)) if isinstance(payload, dict) else 600
    acc_id = str(payload.get("account_id") or "").strip() if isinstance(payload, dict) else ""
    rescan_days = int(payload.get("recent_rescan_days", 7)) if isinstance(payload, dict) else 7

    def _run_sync() -> Dict[str, Any]:
        init_db()
        conn = get_connection()
        cur = conn.cursor()
        try:
            res = sync_imap_order_emails(
                cur,
                max_messages=max_msgs,
                force_rescan=force,
                account_id=acc_id or None,
                recent_rescan_days=rescan_days,
            )
            conn.commit()
            return res
        finally:
            conn.close()

    return await asyncio.to_thread(_run_sync)


@app.post("/api/v1/scraper/email-webhook")
async def email_order_webhook_endpoint(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Ingest a forwarded order email (.eml base64 or HTML/text body) and generate statements."""
    _verify_api_key(x_api_key, sololedger_api_key)
    raw_input: bytes | str = ""
    if payload.get("content_b64"):
        raw_input = base64.b64decode(str(payload["content_b64"]))
    elif payload.get("raw_content"):
        raw_input = str(payload["raw_content"])
    if not raw_input:
        raise HTTPException(
            status_code=400,
            detail="Either raw_content or content_b64 must be provided.",
        )

    init_db()
    conn = get_connection()
    cur = conn.cursor()
    try:
        outcome = ingest_email_order_payload(
            cur,
            raw_content=raw_input,
            subject=str(payload.get("subject") or ""),
            date_str=str(payload.get("date_str") or ""),
            auto_create_tx=bool(payload.get("auto_create_tx", False)),
        )
        conn.commit()
        return outcome
    finally:
        conn.close()


@app.post("/api/v1/scraper/ingest")
async def scraper_ingest_endpoint(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Ingest scraped JSON/HTML(.xls)/CSV card statements or e-commerce order lists."""
    _verify_api_key(x_api_key, sololedger_api_key)
    portal = str(payload.get("portal") or "HOMETAX")
    raw_content = payload.get("raw_content") or ""
    if not raw_content and payload.get("content_b64"):
        raw_bytes = base64.b64decode(str(payload["content_b64"]))
        raw_content = decode_csv_bytes(raw_bytes)
    if not raw_content:
        raise HTTPException(
            status_code=400, detail="Either raw_content or content_b64 must be provided."
        )

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        outcome = ingest_scraped_payload(
            cursor,
            target_portal=portal,
            raw_content=str(raw_content),
            auto_generate_slips=bool(payload.get("auto_generate_slips", True)),
        )
        conn.commit()
        return outcome
    finally:
        conn.close()


@app.post("/api/v1/scraper/generate-slips")
async def generate_electronic_slips_endpoint(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_api_key: Optional[str] = Header(default=None),
    sololedger_api_key: Optional[str] = Header(default=None, alias="SOLOLEDGER_API_KEY"),
):
    """Batch-generate per-card electronic sales slip receipts (.html) under archive/by_card/."""
    _verify_api_key(x_api_key, sololedger_api_key)
    force = bool(payload.get("force_regenerate", False)) if isinstance(payload, dict) else False
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        outcome = generate_electronic_slips_for_ledger(cursor, force_regenerate=force)
        conn.commit()
        return outcome
    finally:
        conn.close()


@app.get("/dashboard", response_class=HTMLResponse)
async def render_dashboard_page(
    year_month: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    card_last4: Optional[str] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    account_code: Optional[str] = Query(default=None),
):
    """Render the lightweight HTML5 management dashboard."""
    if settings.require_auth and not _has_valid_auth():
        return RedirectResponse(url="/login", status_code=303)
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        html_doc = render_dashboard_html(
            cursor,
            year_month=year_month,
            year=year,
            card_last4=card_last4,
            start_date=start_date,
            end_date=end_date,
            account_code=account_code,
        )
        return HTMLResponse(content=html_doc)
    finally:
        conn.close()


@app.get("/manual", response_class=HTMLResponse)
async def render_user_manual_page():
    """Serve the standalone HTML5 User Manual page."""
    if settings.require_auth and not _has_valid_auth():
        return RedirectResponse(url="/login", status_code=303)
    manual_path = PROJECT_ROOT / "docs" / "USER_MANUAL.html"
    if not manual_path.exists():
        raise HTTPException(status_code=404, detail="USER_MANUAL.html not found.")
    return HTMLResponse(content=manual_path.read_text(encoding="utf-8"))


@app.get("/download/apk")
async def download_android_apk_endpoint():
    """Serve the compiled SoloLedger Android native APK for 1-click phone installation."""
    _verify_read_access()
    candidates = [
        PROJECT_ROOT / "dist" / "SoloLedger-mobile.apk",
        PROJECT_ROOT
        / "android"
        / "app"
        / "build"
        / "outputs"
        / "apk"
        / "debug"
        / "app-debug.apk",
    ]
    apk_path = next((p for p in candidates if p.exists() and p.is_file()), None)
    if apk_path is None:
        raise HTTPException(
            status_code=404,
            detail="Android APK file not found. Build android:assembleDebug first.",
        )
    return Response(
        content=apk_path.read_bytes(),
        media_type="application/vnd.android.package-archive",
        headers={
            "Content-Disposition": f'attachment; filename="SoloLedger-v{__version__}.apk"'
        },
    )

