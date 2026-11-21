"""Phase 8 Core Lightweight State Graph Engine & Transaction Resolution Workflow."""

import inspect
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from sololedger.config import settings
from sololedger.merchant_service import ACCOUNT_CODE_NAMES, get_or_verify_merchant
from sololedger.models import OcrLineItem, WorkflowResumeRequest, WorkflowRunRequest
from sololedger.nts_client import normalize_biz_reg_no, validate_biz_reg_no_checksum
from sololedger.ocr_engine import sanitize_ocr_identifiers, summarize_line_items
from sololedger.tax_engine import (
    calculate_supply_and_vat,
    evaluate_tax_rules,
    generate_default_memo,
    is_uncertain_item_merchant,
    learn_merchant_rule,
)
from sololedger.telegram_bot import _detect_memo_account_override, _refresh_receipt_archived_filename

END_NODE = "__END__"
INTERRUPT_NODE = "__INTERRUPT__"
DEFAULT_GRAPH_NAME = "tx_resolution_graph"


class TxGraphState(BaseModel):
    """Shared state object passed across nodes in the transaction resolution state graph."""

    tx_id: int
    card_last4: str = "0000"
    raw_merchant_name: str = ""
    biz_reg_no: Optional[str] = None
    total_amount: int = 0
    supply_value: int = 0
    vat_amount: int = 0
    account_code: str = "999"
    account_name: str = "미분류"
    vat_deduction_type: str = "PENDING"
    non_deduct_reason: Optional[str] = None
    is_vehicle_expense: bool = False
    counterparty_info: Optional[str] = None
    reconcile_status: str = "SMS_ONLY"

    item_hint: Optional[str] = None
    receipt_line_items: List[OcrLineItem] = Field(default_factory=list)

    deterministic_resolved: bool = False
    force_llm: bool = False
    llm_called: bool = False
    llm_call_count: int = 0
    llm_confidence: float = 1.0
    confidence_threshold: float = 0.80
    retry_count: int = 0
    max_retries: int = 2
    validation_errors: List[str] = Field(default_factory=list)
    validation_passed: bool = False

    next_action: str = "COMMIT"
    require_human_review: bool = False
    interrupt_reason: Optional[str] = None
    status: str = "RUNNING"
    current_node: str = "ingest_parse"
    visited_nodes: List[str] = Field(default_factory=list)
    checkpoint_id: Optional[int] = None


@dataclass
class WorkflowContext:
    """Execution context injected into graph nodes."""

    cursor: sqlite3.Cursor
    llm_infer_fn: Optional[Callable[[TxGraphState], Any]] = None
    llm_override: Optional[Dict[str, Any]] = None
    vision_transport: Optional[httpx.AsyncBaseTransport] = None
    nts_transport: Optional[httpx.AsyncBaseTransport] = None


class StateGraph:
    """Minimal declarative state graph runner supporting cycles and checkpoint interrupts."""

    def __init__(self) -> None:
        self.nodes: Dict[str, Callable[[TxGraphState, WorkflowContext], Any]] = {}
        self.edges: Dict[str, str] = {}
        self.conditional_edges: Dict[str, Callable[[TxGraphState], str]] = {}
        self.entry_point: Optional[str] = None

    def add_node(
        self, name: str, func: Callable[[TxGraphState, WorkflowContext], Any]
    ) -> None:
        """Register a sync or async node function."""
        self.nodes[name] = func

    def add_edge(self, src: str, dst: str) -> None:
        """Register a deterministic directed edge from src to dst."""
        self.edges[src] = dst

    def add_conditional_edges(
        self, src: str, router_fn: Callable[[TxGraphState], str]
    ) -> None:
        """Register a conditional router evaluated after src completes."""
        self.conditional_edges[src] = router_fn

    def set_entry_point(self, name: str) -> None:
        """Set the default entry node name."""
        self.entry_point = name

    async def run(
        self,
        state: TxGraphState,
        ctx: WorkflowContext,
        start_node: Optional[str] = None,
        max_steps: int = 25,
    ) -> TxGraphState:
        """Execute graph nodes from start_node until END_NODE or INTERRUPT_NODE."""
        current = start_node or self.entry_point
        if not current:
            raise ValueError("Graph entry_point is not configured.")

        for _ in range(max_steps):
            if current in (END_NODE, INTERRUPT_NODE):
                return state
            if current not in self.nodes:
                raise KeyError(f"Unknown workflow node: {current}")

            node_fn = self.nodes[current]
            if inspect.iscoroutinefunction(node_fn):
                result = await node_fn(state, ctx)
            else:
                result = node_fn(state, ctx)
                if inspect.isawaitable(result):
                    result = await result

            state = result if isinstance(result, TxGraphState) else state
            state.current_node = current
            state.visited_nodes.append(current)

            if current in self.conditional_edges:
                current = self.conditional_edges[current](state)
            else:
                current = self.edges.get(current, END_NODE)

            if current in (END_NODE, INTERRUPT_NODE):
                return state

        raise RuntimeError(f"Workflow graph exceeded max_steps={max_steps}")


def save_workflow_checkpoint(
    cursor: sqlite3.Cursor,
    state: TxGraphState,
    graph_name: str = DEFAULT_GRAPH_NAME,
) -> int:
    """Persist TxGraphState to SQLite workflow_checkpoints and return checkpoint_id."""
    payload_json = state.model_dump_json()
    if state.checkpoint_id is not None:
        cursor.execute(
            """
            UPDATE workflow_checkpoints
            SET current_node = ?,
                status = ?,
                interrupt_reason = ?,
                retry_count = ?,
                state_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE checkpoint_id = ?
            """,
            (
                state.current_node,
                state.status,
                state.interrupt_reason,
                state.retry_count,
                payload_json,
                state.checkpoint_id,
            ),
        )
        return state.checkpoint_id

    cursor.execute(
        """
        UPDATE workflow_checkpoints
        SET status = 'SUPERSEDED',
            updated_at = CURRENT_TIMESTAMP
        WHERE tx_id = ? AND graph_name = ? AND status = 'INTERRUPTED'
        """,
        (state.tx_id, graph_name),
    )
    cursor.execute(
        """
        INSERT INTO workflow_checkpoints (
            tx_id, graph_name, current_node, status,
            interrupt_reason, retry_count, state_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            state.tx_id,
            graph_name,
            state.current_node,
            state.status,
            state.interrupt_reason,
            state.retry_count,
            payload_json,
        ),
    )
    ckpt_id = int(cursor.lastrowid)
    state.checkpoint_id = ckpt_id
    # Re-persist state_json with the assigned checkpoint_id
    cursor.execute(
        "UPDATE workflow_checkpoints SET state_json = ? WHERE checkpoint_id = ?",
        (state.model_dump_json(), ckpt_id),
    )
    return ckpt_id


def load_active_checkpoint(
    cursor: sqlite3.Cursor,
    tx_id: int,
    graph_name: str = DEFAULT_GRAPH_NAME,
) -> Optional[TxGraphState]:
    """Load the latest INTERRUPTED checkpoint for tx_id, if present."""
    cursor.execute(
        """
        SELECT checkpoint_id, state_json
        FROM workflow_checkpoints
        WHERE tx_id = ? AND graph_name = ? AND status = 'INTERRUPTED'
        ORDER BY checkpoint_id DESC
        LIMIT 1
        """,
        (tx_id, graph_name),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    loaded = TxGraphState.model_validate_json(row["state_json"])
    loaded.checkpoint_id = int(row["checkpoint_id"])
    return loaded


def _parse_receipt_line_items(raw_json: Optional[str]) -> List[OcrLineItem]:
    if not raw_json:
        return []
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    items: List[OcrLineItem] = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("item"):
            try:
                items.append(OcrLineItem(**entry))
            except Exception:
                continue
    return items


def _apply_item_override_to_state(state: TxGraphState, item_txt: str) -> bool:
    """Apply deterministic item-keyword account mapping when current account is 999."""
    inferred = _detect_memo_account_override(item_txt)
    if not inferred:
        return False

    acc_code, acc_name, force_non_ded, reason = inferred
    if (
        acc_code == "212"
        and abs(state.total_amount) < 300_000
        and "비품" not in item_txt
    ):
        acc_code, acc_name, force_non_ded, reason = ("830", "소모품비", False, None)

    state.account_code = acc_code
    state.account_name = acc_name
    state.is_vehicle_expense = acc_code == "822"
    if not (
        state.vat_deduction_type in ("EXEMPT", "NON_DEDUCTIBLE")
        and state.non_deduct_reason == "SIMPLIFIED_TAX"
    ):
        state.vat_deduction_type = "NON_DEDUCTIBLE" if force_non_ded else "DEDUCTIBLE"
        state.non_deduct_reason = reason
    return True


def ingest_parse_node(state: TxGraphState, ctx: WorkflowContext) -> TxGraphState:
    """Load transaction and receipt metadata from SQLite into TxGraphState."""
    ctx.cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (state.tx_id,))
    tx = ctx.cursor.fetchone()
    if tx is None:
        raise ValueError(f"Transaction tx_id={state.tx_id} not found.")

    ctx.cursor.execute(
        """
        SELECT ocr_biz_reg_no, line_items_json
        FROM receipt_ocr_metadata
        WHERE tx_id = ?
        ORDER BY receipt_id DESC
        LIMIT 1
        """,
        (state.tx_id,),
    )
    rec = ctx.cursor.fetchone()

    raw_biz = tx["biz_reg_no"] or (rec["ocr_biz_reg_no"] if rec else None)
    clean_biz, _ = sanitize_ocr_identifiers(raw_biz, None)
    line_items = _parse_receipt_line_items(rec["line_items_json"] if rec else None)

    state.card_last4 = str(tx["card_last4"] or "0000")
    state.raw_merchant_name = str(tx["raw_merchant_name"] or "")
    state.biz_reg_no = clean_biz
    state.total_amount = int(tx["total_amount"])
    state.supply_value = int(tx["supply_value"])
    state.vat_amount = int(tx["vat_amount"])
    state.account_code = str(tx["account_code"] or "999")
    state.account_name = str(tx["account_name"] or "미분류")
    state.vat_deduction_type = str(tx["vat_deduction_type"] or "PENDING")
    state.non_deduct_reason = tx["non_deduct_reason"]
    state.is_vehicle_expense = bool(tx["is_vehicle_expense"])
    state.counterparty_info = tx["counterparty_info"]
    state.reconcile_status = str(tx["reconcile_status"] or "SMS_ONLY")
    state.receipt_line_items = line_items
    state.status = "RUNNING"
    return state


async def nts_and_rule_eval_node(
    state: TxGraphState, ctx: WorkflowContext
) -> TxGraphState:
    """Verify NTS merchant cache and evaluate deterministic tax rules (0-cost fast path)."""
    if state.biz_reg_no and validate_biz_reg_no_checksum(state.biz_reg_no):
        await get_or_verify_merchant(
            ctx.cursor,
            biz_reg_no=state.biz_reg_no,
            merchant_name=state.raw_merchant_name,
            transport=ctx.nts_transport,
        )

    existing_account = state.account_code
    rule_res = evaluate_tax_rules(
        ctx.cursor,
        merchant_name=state.raw_merchant_name,
        biz_reg_no=state.biz_reg_no,
    )

    if existing_account == "813":
        state.account_code = "813"
        state.account_name = ACCOUNT_CODE_NAMES["813"]
        state.vat_deduction_type = "NON_DEDUCTIBLE"
        state.non_deduct_reason = "ENTERTAINMENT"
        state.is_vehicle_expense = False
    else:
        state.account_code = str(rule_res["account_code"])
        state.account_name = str(rule_res["account_name"])
        state.vat_deduction_type = str(rule_res["vat_deduction_type"])
        state.non_deduct_reason = rule_res["non_deduct_reason"]
        state.is_vehicle_expense = bool(rule_res["is_vehicle_expense"])

    item_txt = (
        state.item_hint
        or state.counterparty_info
        or summarize_line_items(state.receipt_line_items)
        or ""
    ).strip()
    if item_txt and not (state.counterparty_info or "").strip():
        state.counterparty_info = item_txt

    if state.account_code == "999" and item_txt:
        _apply_item_override_to_state(state, item_txt)

    if state.force_llm:
        state.deterministic_resolved = False
        state.next_action = "LLM"
        return state

    if state.account_code == "813" and not (state.counterparty_info or "").strip():
        state.vat_deduction_type = "NON_DEDUCTIBLE"
        state.non_deduct_reason = "ENTERTAINMENT"
        state.supply_value, state.vat_amount = calculate_supply_and_vat(
            state.total_amount, "NON_DEDUCTIBLE"
        )
        state.require_human_review = True
        state.interrupt_reason = "MISSING_ENTERTAINMENT_ATTENDEE"
        state.next_action = "INTERRUPT"
        return state

    uncertain_without_memo = is_uncertain_item_merchant(
        state.raw_merchant_name
    ) and not (state.counterparty_info or state.item_hint or "").strip()

    if state.account_code == "999" or uncertain_without_memo:
        state.deterministic_resolved = False
        state.next_action = "LLM"
        return state

    if state.vat_deduction_type in ("PENDING", "PENDING_CHECK"):
        state.vat_deduction_type = "DEDUCTIBLE"

    if state.vat_deduction_type == "EXEMPT":
        state.supply_value, state.vat_amount = calculate_supply_and_vat(
            state.total_amount, "EXEMPT"
        )
    elif (state.supply_value + state.vat_amount) != state.total_amount:
        state.supply_value, state.vat_amount = calculate_supply_and_vat(
            state.total_amount, state.vat_deduction_type
        )

    if not (state.counterparty_info or "").strip():
        default_memo = generate_default_memo(
            state.account_code, state.raw_merchant_name, require_memo_prompt=False
        )
        if default_memo:
            state.counterparty_info = default_memo

    state.deterministic_resolved = True
    state.validation_passed = True
    state.next_action = "COMMIT"
    return state


async def _call_gemini_inference(
    state: TxGraphState,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Dict[str, Any]:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent"
    )
    retry_note = ""
    if state.validation_errors:
        retry_note = (
            f"\nPrevious output failed validation with errors: {state.validation_errors}. "
            "Fix supply_value + vat_amount == total_amount and biz_reg_no checksum."
        )
    prompt = (
        "Classify this Korean corporate card transaction as strict JSON with keys: "
        "account_code (one of 501, 511, 811, 812, 813, 821, 822, 826, 830, 831, 212, 999), "
        "vat_deduction_type (DEDUCTIBLE, NON_DEDUCTIBLE, EXEMPT), non_deduct_reason, "
        "supply_value (int), vat_amount (int), biz_reg_no (10 digits or null), "
        "counterparty_info (short Korean memo), confidence (0.0~1.0).\n"
        f"Merchant: {state.raw_merchant_name}\n"
        f"Total Amount: {state.total_amount}\n"
        f"Item Hint: {state.item_hint or state.counterparty_info or ''}"
        f"{retry_note}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(transport=transport, timeout=20.0) as client:
        resp = await client.post(
            url,
            params={"key": settings.gemini_api_key or "mock-key"},
            json=payload,
        )
        resp.raise_for_status()
        raw_json = resp.json()

    if "candidates" in raw_json:
        text_part = raw_json["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text_part)
    return raw_json


def _offline_llm_fallback(state: TxGraphState) -> Dict[str, Any]:
    hint = (
        state.item_hint
        or state.counterparty_info
        or summarize_line_items(state.receipt_line_items)
        or ""
    ).strip()
    if hint:
        inferred = _detect_memo_account_override(hint)
        if inferred:
            acc_code, acc_name, force_non_ded, reason = inferred
            if (
                acc_code == "212"
                and abs(state.total_amount) < 300_000
                and "비품" not in hint
            ):
                acc_code, acc_name, force_non_ded, reason = ("830", "소모품비", False, None)
            ded_type = "NON_DEDUCTIBLE" if force_non_ded else "DEDUCTIBLE"
            sv, va = calculate_supply_and_vat(state.total_amount, ded_type)
            return {
                "account_code": acc_code,
                "account_name": acc_name,
                "vat_deduction_type": ded_type,
                "non_deduct_reason": reason,
                "supply_value": sv,
                "vat_amount": va,
                "counterparty_info": hint,
                "confidence": 0.90,
            }
    sv, va = calculate_supply_and_vat(state.total_amount, "PENDING")
    return {
        "account_code": "999",
        "account_name": "미분류",
        "vat_deduction_type": "PENDING",
        "non_deduct_reason": None,
        "supply_value": sv,
        "vat_amount": va,
        "confidence": 0.35,
    }


async def llm_tax_inference_node(
    state: TxGraphState, ctx: WorkflowContext
) -> TxGraphState:
    """Invoke conditional AI inference (callback, override, Gemini API, or offline fallback)."""
    state.llm_call_count += 1
    state.llm_called = True

    candidate: Dict[str, Any]
    if ctx.llm_infer_fn is not None:
        raw = ctx.llm_infer_fn(state)
        candidate = await raw if inspect.isawaitable(raw) else raw
    elif ctx.llm_override is not None:
        candidate = dict(ctx.llm_override)
    elif settings.gemini_api_key or ctx.vision_transport is not None:
        candidate = await _call_gemini_inference(state, transport=ctx.vision_transport)
    else:
        candidate = _offline_llm_fallback(state)

    if "account_code" in candidate and candidate["account_code"]:
        acc_code = str(candidate["account_code"]).strip()
        state.account_code = acc_code
        state.account_name = ACCOUNT_CODE_NAMES.get(
            acc_code, str(candidate.get("account_name") or state.account_name)
        )
        state.is_vehicle_expense = acc_code == "822"

    if "vat_deduction_type" in candidate and candidate["vat_deduction_type"]:
        state.vat_deduction_type = str(candidate["vat_deduction_type"]).strip()
    if "non_deduct_reason" in candidate:
        state.non_deduct_reason = candidate["non_deduct_reason"]
    if "supply_value" in candidate and candidate["supply_value"] is not None:
        state.supply_value = int(candidate["supply_value"])
    if "vat_amount" in candidate and candidate["vat_amount"] is not None:
        state.vat_amount = int(candidate["vat_amount"])
    if "biz_reg_no" in candidate:
        raw_b = candidate["biz_reg_no"]
        state.biz_reg_no = str(raw_b).strip() if raw_b else None
    if candidate.get("counterparty_info") is not None:
        state.counterparty_info = str(candidate["counterparty_info"]).strip() or None
    if "confidence" in candidate and candidate["confidence"] is not None:
        state.llm_confidence = float(candidate["confidence"])

    return state


def arithmetic_tax_validator_node(
    state: TxGraphState, ctx: WorkflowContext
) -> TxGraphState:
    """Validate arithmetic consistency, biz_reg_no checksum, and tax invariants."""
    errors: List[str] = []

    if (state.supply_value + state.vat_amount) != state.total_amount:
        errors.append("ARITHMETIC_MISMATCH: supply_value + vat_amount != total_amount")

    if state.biz_reg_no:
        digits = re.sub(r"\D", "", str(state.biz_reg_no))
        is_phone_like = digits.startswith(("02", "010", "15", "16", "18"))
        if (
            len(digits) != 10
            or is_phone_like
            or not validate_biz_reg_no_checksum(digits)
        ):
            errors.append("INVALID_BIZ_REG_NO_CHECKSUM")
        else:
            state.biz_reg_no = digits

    if state.account_code not in ACCOUNT_CODE_NAMES:
        errors.append(f"UNKNOWN_ACCOUNT_CODE: {state.account_code}")

    state.validation_errors = errors
    if errors:
        state.validation_passed = False
        if state.retry_count < state.max_retries:
            state.retry_count += 1
            state.next_action = "RETRY_LLM"
        else:
            state.require_human_review = True
            state.interrupt_reason = "VALIDATION_RETRIES_EXCEEDED"
            state.next_action = "INTERRUPT"
        return state

    state.validation_passed = True

    if state.account_code == "813":
        state.vat_deduction_type = "NON_DEDUCTIBLE"
        state.non_deduct_reason = "ENTERTAINMENT"
        state.is_vehicle_expense = False
        state.supply_value, state.vat_amount = calculate_supply_and_vat(
            state.total_amount, "NON_DEDUCTIBLE"
        )
    elif state.account_code == "822":
        state.vat_deduction_type = "NON_DEDUCTIBLE"
        state.non_deduct_reason = "PASSENGER_CAR"
        state.is_vehicle_expense = True
        if (state.supply_value + state.vat_amount) != state.total_amount:
            state.supply_value, state.vat_amount = calculate_supply_and_vat(
                state.total_amount, "NON_DEDUCTIBLE"
            )

    if state.account_code == "813" and not (state.counterparty_info or "").strip():
        state.require_human_review = True
        state.interrupt_reason = "MISSING_ENTERTAINMENT_ATTENDEE"
        state.next_action = "INTERRUPT"
    elif state.account_code == "999" or (
        is_uncertain_item_merchant(state.raw_merchant_name)
        and not (state.counterparty_info or "").strip()
    ):
        state.require_human_review = True
        state.interrupt_reason = "MISSING_PG_OR_MART_ITEM_MEMO"
        state.next_action = "INTERRUPT"
    elif state.llm_confidence < state.confidence_threshold:
        state.require_human_review = True
        state.interrupt_reason = "LOW_CONFIDENCE"
        state.next_action = "INTERRUPT"
    else:
        state.require_human_review = False
        state.interrupt_reason = None
        state.next_action = "COMMIT"

    return state


def human_interrupt_node(state: TxGraphState, ctx: WorkflowContext) -> TxGraphState:
    """Persist checkpoint and mark transaction PENDING_CHECK for human review."""
    state.status = "INTERRUPTED"
    state.current_node = "human_interrupt"
    ctx.cursor.execute(
        """
        UPDATE card_transactions
        SET vat_deduction_type = CASE
                WHEN vat_deduction_type = 'PENDING' THEN 'PENDING_CHECK'
                ELSE vat_deduction_type
            END,
            account_code = ?,
            account_name = ?,
            non_deduct_reason = COALESCE(?, non_deduct_reason),
            is_vehicle_expense = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE tx_id = ?
        """,
        (
            state.account_code,
            state.account_name,
            state.non_deduct_reason,
            1 if state.is_vehicle_expense else 0,
            state.tx_id,
        ),
    )
    save_workflow_checkpoint(ctx.cursor, state)
    return state


async def commit_ledger_node(
    state: TxGraphState, ctx: WorkflowContext
) -> TxGraphState:
    """Persist validated ledger state to card_transactions and finalize checkpoint."""
    safe_biz_no: Optional[str] = None
    if state.biz_reg_no:
        try:
            candidate_biz = normalize_biz_reg_no(state.biz_reg_no)
        except ValueError:
            candidate_biz = ""
        if candidate_biz and validate_biz_reg_no_checksum(candidate_biz):
            safe_biz_no = candidate_biz
            ctx.cursor.execute(
                "SELECT * FROM merchants WHERE biz_reg_no = ?", (safe_biz_no,)
            )
            m_row = ctx.cursor.fetchone()
            if m_row is None:
                await get_or_verify_merchant(
                    ctx.cursor,
                    biz_reg_no=safe_biz_no,
                    merchant_name=state.raw_merchant_name or "미상가맹점",
                    transport=ctx.nts_transport,
                )
                ctx.cursor.execute(
                    "SELECT * FROM merchants WHERE biz_reg_no = ?", (safe_biz_no,)
                )
                m_row = ctx.cursor.fetchone()
            if m_row is None:
                ctx.cursor.execute(
                    "INSERT OR IGNORE INTO merchants (biz_reg_no, merchant_name) VALUES (?, ?)",
                    (safe_biz_no, state.raw_merchant_name or "미상가맹점"),
                )
            else:
                tax_type = m_row["tax_type_code"]
                biz_status = m_row["biz_status"]
                if tax_type == "EXEMPT":
                    state.vat_deduction_type = "EXEMPT"
                    state.non_deduct_reason = state.non_deduct_reason or "SIMPLIFIED_TAX"
                    state.supply_value, state.vat_amount = calculate_supply_and_vat(
                        state.total_amount, "EXEMPT"
                    )
                elif tax_type in ("SIMPLIFIED", "UNREGISTERED") or biz_status in (
                    "SUSPENDED",
                    "CLOSED",
                ):
                    state.vat_deduction_type = "NON_DEDUCTIBLE"
                    state.non_deduct_reason = state.non_deduct_reason or "SIMPLIFIED_TAX"

    state.biz_reg_no = safe_biz_no

    if state.vat_deduction_type in ("PENDING", "PENDING_CHECK"):
        state.vat_deduction_type = "DEDUCTIBLE"

    if (state.supply_value + state.vat_amount) != state.total_amount:
        state.supply_value, state.vat_amount = calculate_supply_and_vat(
            state.total_amount, state.vat_deduction_type
        )

    state.account_name = ACCOUNT_CODE_NAMES.get(state.account_code, state.account_name)

    ctx.cursor.execute(
        """
        UPDATE card_transactions
        SET biz_reg_no = COALESCE(?, biz_reg_no),
            supply_value = ?,
            vat_amount = ?,
            account_code = ?,
            account_name = ?,
            vat_deduction_type = ?,
            non_deduct_reason = ?,
            is_vehicle_expense = ?,
            counterparty_info = COALESCE(?, counterparty_info),
            updated_at = CURRENT_TIMESTAMP
        WHERE tx_id = ?
        """,
        (
            safe_biz_no,
            state.supply_value,
            state.vat_amount,
            state.account_code,
            state.account_name,
            state.vat_deduction_type,
            state.non_deduct_reason,
            1 if state.is_vehicle_expense else 0,
            state.counterparty_info,
            state.tx_id,
        ),
    )

    _refresh_receipt_archived_filename(ctx.cursor, state.tx_id)
    state.status = "COMPLETED"
    state.interrupt_reason = None
    state.current_node = "commit_ledger"

    if state.checkpoint_id is not None:
        ctx.cursor.execute(
            """
            UPDATE workflow_checkpoints
            SET status = 'COMPLETED',
                current_node = 'commit_ledger',
                interrupt_reason = NULL,
                state_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE checkpoint_id = ?
            """,
            (state.model_dump_json(), state.checkpoint_id),
        )

    return state


def _route_after_rule_eval(state: TxGraphState) -> str:
    if state.next_action == "LLM":
        return "llm_tax_inference"
    if state.next_action == "INTERRUPT":
        return "human_interrupt"
    return "commit_ledger"


def _route_after_validator(state: TxGraphState) -> str:
    if state.next_action == "RETRY_LLM":
        return "llm_tax_inference"
    if state.next_action == "INTERRUPT":
        return "human_interrupt"
    return "commit_ledger"


def build_tx_resolution_graph() -> StateGraph:
    """Build the Phase 8 transaction classification, validation-cycle, and checkpoint graph."""
    graph = StateGraph()
    graph.add_node("ingest_parse", ingest_parse_node)
    graph.add_node("nts_and_rule_eval", nts_and_rule_eval_node)
    graph.add_node("llm_tax_inference", llm_tax_inference_node)
    graph.add_node("arithmetic_tax_validator", arithmetic_tax_validator_node)
    graph.add_node("human_interrupt", human_interrupt_node)
    graph.add_node("commit_ledger", commit_ledger_node)

    graph.set_entry_point("ingest_parse")
    graph.add_edge("ingest_parse", "nts_and_rule_eval")
    graph.add_conditional_edges("nts_and_rule_eval", _route_after_rule_eval)
    graph.add_edge("llm_tax_inference", "arithmetic_tax_validator")
    graph.add_conditional_edges("arithmetic_tax_validator", _route_after_validator)
    graph.add_edge("human_interrupt", INTERRUPT_NODE)
    graph.add_edge("commit_ledger", END_NODE)
    return graph


async def run_transaction_workflow(
    cursor: sqlite3.Cursor,
    tx_id: int,
    request: Optional[WorkflowRunRequest] = None,
    llm_infer_fn: Optional[Callable[[TxGraphState], Any]] = None,
    vision_transport: Optional[httpx.AsyncBaseTransport] = None,
    nts_transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Dict[str, Any]:
    """Execute the transaction resolution state graph for a specific tx_id."""
    req = request or WorkflowRunRequest()
    state = TxGraphState(
        tx_id=tx_id,
        max_retries=req.max_retries,
        confidence_threshold=req.confidence_threshold,
        force_llm=req.force_llm,
        item_hint=req.item_hint,
    )
    ctx = WorkflowContext(
        cursor=cursor,
        llm_infer_fn=llm_infer_fn,
        llm_override=req.llm_override,
        vision_transport=vision_transport,
        nts_transport=nts_transport,
    )
    graph = build_tx_resolution_graph()
    final_state = await graph.run(state, ctx)
    return final_state.model_dump()


async def resume_transaction_workflow(
    cursor: sqlite3.Cursor,
    tx_id: int,
    request: WorkflowResumeRequest,
    nts_transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Dict[str, Any]:
    """Resume an interrupted workflow checkpoint with human clarification input."""
    state = load_active_checkpoint(cursor, tx_id)
    if state is None:
        raise ValueError(f"No active interrupted checkpoint found for tx_id={tx_id}.")

    if request.counterparty_info is not None:
        state.counterparty_info = request.counterparty_info.strip() or None

    if request.account_code:
        acc_code = request.account_code.strip()
        state.account_code = acc_code
        state.account_name = (
            request.account_name
            or ACCOUNT_CODE_NAMES.get(acc_code, state.account_name)
        )
    elif state.counterparty_info:
        inferred = _detect_memo_account_override(state.counterparty_info)
        if inferred:
            acc_code, acc_name, force_non_ded, reason = inferred
            if (
                acc_code == "212"
                and abs(state.total_amount) < 300_000
                and "비품" not in state.counterparty_info
            ):
                acc_code, acc_name, force_non_ded, reason = ("830", "소모품비", False, None)
            state.account_code = acc_code
            state.account_name = acc_name
            if force_non_ded:
                state.vat_deduction_type = "NON_DEDUCTIBLE"
                state.non_deduct_reason = reason
        elif state.account_code == "999":
            state.account_code = "830"
            state.account_name = ACCOUNT_CODE_NAMES["830"]

    if request.vat_deduction_type:
        state.vat_deduction_type = request.vat_deduction_type.strip()
    elif state.vat_deduction_type in ("PENDING", "PENDING_CHECK"):
        state.vat_deduction_type = "DEDUCTIBLE"

    if request.non_deduct_reason is not None:
        state.non_deduct_reason = request.non_deduct_reason.strip() or None

    if state.account_code == "813":
        state.vat_deduction_type = "NON_DEDUCTIBLE"
        state.non_deduct_reason = "ENTERTAINMENT"
        state.is_vehicle_expense = False
    elif state.account_code == "822":
        state.vat_deduction_type = "NON_DEDUCTIBLE"
        state.non_deduct_reason = "PASSENGER_CAR"
        state.is_vehicle_expense = True
    else:
        state.is_vehicle_expense = False

    state.supply_value, state.vat_amount = calculate_supply_and_vat(
        state.total_amount, state.vat_deduction_type
    )

    if state.account_code == "813" and not (state.counterparty_info or "").strip():
        state.status = "INTERRUPTED"
        state.require_human_review = True
        state.interrupt_reason = "MISSING_ENTERTAINMENT_ATTENDEE"
        save_workflow_checkpoint(cursor, state)
        return state.model_dump()

    ctx = WorkflowContext(cursor=cursor, nts_transport=nts_transport)
    graph = build_tx_resolution_graph()
    final_state = await graph.run(state, ctx, start_node="commit_ledger")

    if request.learn_rule and not is_uncertain_item_merchant(final_state.raw_merchant_name):
        learn_merchant_rule(
            cursor,
            merchant_name=final_state.raw_merchant_name,
            account_code=final_state.account_code,
            account_name=final_state.account_name,
            vat_deduction_type=final_state.vat_deduction_type,
            non_deduct_reason=final_state.non_deduct_reason,
        )

    return final_state.model_dump()
