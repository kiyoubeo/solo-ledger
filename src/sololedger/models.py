"""Pydantic data models for SoloLedger."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SmsWebhookPayload(BaseModel):
    """Payload sent by Android MacroDroid / Tasker or iOS Shortcuts."""

    sender: Optional[str] = Field(default="UNKNOWN", description="SMS sender phone number")
    raw_message: str = Field(..., description="Full raw text of the SMS notification")
    received_at: Optional[str] = Field(
        default=None, description="ISO timestamp from mobile device"
    )


class ParsedSmsResult(BaseModel):
    """Structured result extracted from a Korean corporate card SMS."""

    card_last4: str
    transacted_at: str
    total_amount: int
    raw_merchant_name: str
    is_cancel: bool
    approval_no: Optional[str] = None


class RuleEvaluationResult(BaseModel):
    """Accounting and VAT deduction classification output from the rule engine."""

    account_code: str
    account_name: str
    vat_deduction_type: str
    non_deduct_reason: Optional[str] = None
    is_vehicle_expense: bool = False
    require_memo_prompt: bool = True


class ReconcileItem(BaseModel):
    """Single row from Hometax / card issuer purchase history or OCR result."""

    card_last4: str = "0000"
    approval_no: Optional[str] = None
    transacted_at: str
    merchant_name: str
    biz_reg_no: Optional[str] = None
    total_amount: int
    tax_type_code: str = "GENERAL"
    biz_status: str = "ACTIVE"
    reconcile_source: str = "HOMETAX_VERIFIED"
    is_cancel: bool = False


class ReconcileBatchRequest(BaseModel):
    """Batch request for reconciling transactions."""

    items: List[ReconcileItem]


class NtsStatusResult(BaseModel):
    """Normalized result from NTS business registration status lookup or cache."""

    biz_reg_no: str
    merchant_name: Optional[str] = None
    tax_type_code: str = "UNKNOWN"  # GENERAL, SIMPLIFIED, EXEMPT, UNREGISTERED, UNKNOWN
    biz_status: str = "ACTIVE"  # ACTIVE, SUSPENDED, CLOSED
    is_valid_checksum: bool = False
    is_pg_provider: bool = False
    default_account_code: Optional[str] = None
    raw_tax_type_msg: Optional[str] = None
    closed_dt: Optional[str] = None
    from_cache: bool = False
    nts_verified_at: Optional[str] = None


class MerchantVerifyRequest(BaseModel):
    """Request payload for verifying and caching a merchant's NTS tax status."""

    biz_reg_no: str = Field(
        ..., description="10-digit business registration number (with or without hyphens)"
    )
    merchant_name: str = Field(default="미상가맹점", description="Merchant display name")
    default_account_code: Optional[str] = Field(
        default=None, description="Optional default account code for recurring visits"
    )
    force_refresh: bool = Field(
        default=False, description="Force NTS API lookup even if 30-day cache is valid"
    )


class OcrLineItem(BaseModel):
    """Single purchased item extracted from a receipt or online order capture."""

    item: str
    qty: int = 1
    price: int = 0


class OcrExtractionResult(BaseModel):
    """Structured receipt metadata extracted by Multimodal Vision LLM."""

    biz_reg_no: Optional[str] = None
    merchant_name: str = "미상가맹점"
    approval_no: Optional[str] = None
    transacted_at: str
    total_amount: int
    supply_value: Optional[int] = None
    vat_amount: Optional[int] = None
    line_items: List[OcrLineItem] = Field(default_factory=list)
    confidence: float = 0.95
    raw_response: Dict[str, Any] = Field(default_factory=dict)


class ReceiptUploadRequest(BaseModel):
    """Request payload for uploading a receipt image and linking it to the ledger."""

    file_name: str = Field(default="receipt.jpg", description="Original file name")
    file_content_b64: str = Field(..., description="Base64-encoded image or PDF content")
    source_channel: str = Field(default="WEB_UPLOAD", description="TELEGRAM_BOT or WEB_UPLOAD")
    tx_id: Optional[int] = Field(default=None, description="Optional explicit transaction ID")
    ocr_override: Optional[OcrExtractionResult] = Field(
        default=None, description="Optional pre-parsed OCR payload"
    )


class HometaxCsvImportRequest(BaseModel):
    """Request payload for importing Hometax or card issuer CSV data."""

    csv_content_b64: Optional[str] = Field(
        default=None, description="Base64-encoded CSV file bytes (UTF-8 or CP949)"
    )
    csv_text: Optional[str] = Field(
        default=None, description="Direct plain-text CSV content"
    )


class TaxPackExportRequest(BaseModel):
    """Request payload for generating a monthly tax accountant CSV + ZIP bundle."""

    year_month: str = Field(
        ..., pattern=r"^\d{6}$", description="Target归属 year-month in YYYYMM format (e.g., 202610)"
    )
    force_reexport: bool = Field(
        default=False,
        description="Include transactions that were already exported in a previous run",
    )


class TransactionUpdateRequest(BaseModel):
    """Request payload for manual or inline dashboard update of a transaction."""

    account_code: Optional[str] = Field(default=None, description="Target account code (e.g., 811, 813, 830)")
    account_name: Optional[str] = Field(default=None, description="Target account name")
    vat_deduction_type: Optional[str] = Field(
        default=None, description="DEDUCTIBLE, NON_DEDUCTIBLE, EXEMPT, or PENDING_CHECK"
    )
    non_deduct_reason: Optional[str] = Field(default=None, description="Reason code if non-deductible")
    counterparty_info: Optional[str] = Field(
        default=None, description="Item summary or meeting attendee memo"
    )
    apply_to_same_merchant: bool = Field(
        default=True,
        description="Propagate account classification and learned rule to identical merchants",
    )


class NasBackupRequest(BaseModel):
    """Request payload for backing up SQLite DB and evidence archive to NAS drive."""

    nas_dir: Optional[str] = Field(
        default=None, description="Optional override path for NAS backup root (defaults to Z:\\SoloLedger)"
    )
