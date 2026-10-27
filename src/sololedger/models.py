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
