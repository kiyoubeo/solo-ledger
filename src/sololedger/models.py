"""Pydantic data models for SoloLedger."""

from typing import List, Optional

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

    biz_reg_no: str = Field(..., description="10-digit business registration number (with or without hyphens)")
    merchant_name: str = Field(default="미상가맹점", description="Merchant display name")
    default_account_code: Optional[str] = Field(
        default=None, description="Optional default account code for recurring visits"
    )
    force_refresh: bool = Field(
        default=False, description="Force NTS API lookup even if 30-day cache is valid"
    )
