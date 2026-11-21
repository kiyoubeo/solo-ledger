"""Unit and integration tests for Phase 8 State Graph Engine & Pre-Export Tax Audit Graph."""

import json

import httpx
import pytest

from sololedger.audit_graph import run_pre_export_tax_audit
from sololedger.models import TaxAuditRequest, WorkflowResumeRequest, WorkflowRunRequest
from sololedger.workflow_graph import (
    load_active_checkpoint,
    resume_transaction_workflow,
    run_transaction_workflow,
)

VALID_BIZ_NO_GENERAL = "1208147521"
VALID_BIZ_NO_SIMPLIFIED = "3018123453"


def _insert_tx(
    cursor,
    *,
    card_last4: str = "3081",
    raw_merchant_name: str = "테스트가맹점",
    biz_reg_no: str | None = None,
    total_amount: int = 55_000,
    supply_value: int = 50_000,
    vat_amount: int = 5_000,
    account_code: str = "999",
    account_name: str = "미분류",
    vat_deduction_type: str = "PENDING",
    non_deduct_reason: str | None = None,
    is_vehicle_expense: int = 0,
    counterparty_info: str | None = None,
    reconcile_status: str = "SMS_ONLY",
    transacted_at: str = "2026-10-20 12:30:00",
) -> int:
    cursor.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, non_deduct_reason, is_vehicle_expense,
            counterparty_info, reconcile_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card_last4,
            transacted_at,
            raw_merchant_name,
            biz_reg_no,
            total_amount,
            supply_value,
            vat_amount,
            account_code,
            account_name,
            vat_deduction_type,
            non_deduct_reason,
            is_vehicle_expense,
            counterparty_info,
            reconcile_status,
        ),
    )
    return int(cursor.lastrowid)


@pytest.mark.asyncio
async def test_deterministic_zero_cost_fast_path(db_conn):
    cur = db_conn.cursor()
    gas_tx_id = _insert_tx(
        cur,
        raw_merchant_name="GS칼텍스 역삼주유소",
        total_amount=66_000,
        supply_value=60_000,
        vat_amount=6_000,
    )
    saas_tx_id = _insert_tx(
        cur,
        raw_merchant_name="OPENAI CHATGPT",
        total_amount=29_700,
        supply_value=27_000,
        vat_amount=2_700,
    )
    db_conn.commit()

    def _fail_if_llm_called(_state):
        raise AssertionError("LLM node must not be invoked on deterministic fast-path")

    gas_res = await run_transaction_workflow(cur, gas_tx_id, llm_infer_fn=_fail_if_llm_called)
    saas_res = await run_transaction_workflow(cur, saas_tx_id, llm_infer_fn=_fail_if_llm_called)
    db_conn.commit()

    assert gas_res["status"] == "COMPLETED"
    assert gas_res["llm_called"] is False
    assert gas_res["llm_call_count"] == 0
    assert gas_res["account_code"] == "822"
    assert gas_res["vat_deduction_type"] == "NON_DEDUCTIBLE"
    assert gas_res["non_deduct_reason"] == "PASSENGER_CAR"
    assert gas_res["is_vehicle_expense"] is True
    assert gas_res["visited_nodes"] == ["ingest_parse", "nts_and_rule_eval", "commit_ledger"]

    assert saas_res["status"] == "COMPLETED"
    assert saas_res["llm_called"] is False
    assert saas_res["account_code"] == "831"
    assert saas_res["vat_deduction_type"] == "DEDUCTIBLE"


@pytest.mark.asyncio
async def test_llm_retry_cycle_on_arithmetic_and_checksum_error(db_conn):
    cur = db_conn.cursor()
    tx_id = _insert_tx(
        cur,
        raw_merchant_name="주식회사 쿠팡",
        total_amount=110_000,
        supply_value=100_000,
        vat_amount=10_000,
    )
    db_conn.commit()

    attempts = {"count": 0}
    seen_errors = []

    def _mock_nts_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        b_no = body["b_no"][0]
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b_no": b_no,
                        "b_stt_cd": "01",
                        "b_stt": "계속사업자",
                        "tax_type_cd": "01",
                        "tax_type": "부가가치세 일반과세자",
                    }
                ]
            },
        )

    def _flaky_then_valid_llm(state):
        attempts["count"] += 1
        seen_errors.append(list(state.validation_errors))
        if attempts["count"] == 1:
            return {
                "account_code": "501",
                "vat_deduction_type": "DEDUCTIBLE",
                "supply_value": 99_000,
                "vat_amount": 9_000,  # 99000 + 9000 != 110000
                "biz_reg_no": "0212345678",  # invalid phone-like number
                "counterparty_info": "내장목공사 합판 및 타카핀",
                "confidence": 0.94,
            }
        return {
            "account_code": "501",
            "vat_deduction_type": "DEDUCTIBLE",
            "supply_value": 100_000,
            "vat_amount": 10_000,
            "biz_reg_no": VALID_BIZ_NO_GENERAL,
            "counterparty_info": "내장목공사 합판 및 타카핀",
            "confidence": 0.96,
        }

    nts_transport = httpx.MockTransport(_mock_nts_handler)
    res = await run_transaction_workflow(
        cur,
        tx_id,
        request=WorkflowRunRequest(max_retries=2),
        llm_infer_fn=_flaky_then_valid_llm,
        nts_transport=nts_transport,
    )
    db_conn.commit()

    assert res["status"] == "COMPLETED"
    assert res["llm_called"] is True
    assert res["llm_call_count"] == 2
    assert res["retry_count"] == 1
    assert res["validation_passed"] is True
    assert seen_errors[0] == []
    assert any("ARITHMETIC_MISMATCH" in e for e in seen_errors[1])
    assert any("INVALID_BIZ_REG_NO_CHECKSUM" in e for e in seen_errors[1])
    assert res["visited_nodes"] == [
        "ingest_parse",
        "nts_and_rule_eval",
        "llm_tax_inference",
        "arithmetic_tax_validator",
        "llm_tax_inference",
        "arithmetic_tax_validator",
        "commit_ledger",
    ]

    cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    tx_row = cur.fetchone()
    assert tx_row["account_code"] == "501"
    assert tx_row["account_name"] == "원재료비"
    assert tx_row["biz_reg_no"] == VALID_BIZ_NO_GENERAL


@pytest.mark.asyncio
async def test_llm_max_retries_exceeded_interrupts_safely(db_conn):
    cur = db_conn.cursor()
    tx_id = _insert_tx(
        cur,
        raw_merchant_name="네이버페이",
        total_amount=44_000,
        supply_value=40_000,
        vat_amount=4_000,
    )
    db_conn.commit()

    def _always_broken_llm(_state):
        return {
            "account_code": "830",
            "vat_deduction_type": "DEDUCTIBLE",
            "supply_value": 10_000,
            "vat_amount": 1_000,
            "counterparty_info": "사무용 소모품",
            "confidence": 0.95,
        }

    res = await run_transaction_workflow(
        cur,
        tx_id,
        request=WorkflowRunRequest(max_retries=2),
        llm_infer_fn=_always_broken_llm,
    )
    db_conn.commit()

    assert res["status"] == "INTERRUPTED"
    assert res["interrupt_reason"] == "VALIDATION_RETRIES_EXCEEDED"
    assert res["llm_call_count"] == 3  # 1 initial + 2 retries
    assert res["retry_count"] == 2
    assert res["checkpoint_id"] is not None

    ckpt = load_active_checkpoint(cur, tx_id)
    assert ckpt is not None
    assert ckpt.status == "INTERRUPTED"
    assert ckpt.interrupt_reason == "VALIDATION_RETRIES_EXCEEDED"


@pytest.mark.asyncio
async def test_human_interrupt_and_resume_for_entertainment_and_pg(db_conn):
    cur = db_conn.cursor()
    ent_tx_id = _insert_tx(
        cur,
        raw_merchant_name="청담일식",
        total_amount=165_000,
        supply_value=150_000,
        vat_amount=15_000,
        account_code="813",
        account_name="기업업무추진비",
        vat_deduction_type="NON_DEDUCTIBLE",
        non_deduct_reason="ENTERTAINMENT",
        counterparty_info=None,
    )
    db_conn.commit()

    res_int = await run_transaction_workflow(cur, ent_tx_id)
    db_conn.commit()
    assert res_int["status"] == "INTERRUPTED"
    assert res_int["interrupt_reason"] == "MISSING_ENTERTAINMENT_ATTENDEE"
    assert res_int["llm_called"] is False
    assert res_int["checkpoint_id"] is not None

    # Resuming without attendee memo keeps checkpoint INTERRUPTED
    res_still_blocked = await resume_transaction_workflow(
        cur,
        ent_tx_id,
        WorkflowResumeRequest(account_code="813", counterparty_info="   "),
    )
    db_conn.commit()
    assert res_still_blocked["status"] == "INTERRUPTED"
    assert res_still_blocked["interrupt_reason"] == "MISSING_ENTERTAINMENT_ATTENDEE"

    # Resuming with attendee memo commits ledger and closes checkpoint
    res_done = await resume_transaction_workflow(
        cur,
        ent_tx_id,
        WorkflowResumeRequest(
            account_code="813",
            counterparty_info="A사 김철수 대표 현장 미팅 식사",
        ),
    )
    db_conn.commit()
    assert res_done["status"] == "COMPLETED"
    assert res_done["account_code"] == "813"
    assert res_done["vat_deduction_type"] == "NON_DEDUCTIBLE"
    assert res_done["non_deduct_reason"] == "ENTERTAINMENT"
    assert load_active_checkpoint(cur, ent_tx_id) is None

    cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (ent_tx_id,))
    row = cur.fetchone()
    assert row["counterparty_info"] == "A사 김철수 대표 현장 미팅 식사"
    assert row["vat_deduction_type"] == "NON_DEDUCTIBLE"


@pytest.mark.asyncio
async def test_pre_export_tax_audit_parallel_fan_out_join(db_conn, tmp_path):
    cur = db_conn.cursor()
    archive_dir = tmp_path / "archive"

    # Insert simplified merchant
    cur.execute(
        """
        INSERT INTO merchants (biz_reg_no, merchant_name, tax_type_code, biz_status)
        VALUES (?, '간이소매점', 'SIMPLIFIED', 'ACTIVE')
        """,
        (VALID_BIZ_NO_SIMPLIFIED,),
    )
    # 1. Simplified merchant wrongly marked DEDUCTIBLE
    _insert_tx(
        cur,
        card_last4="3081",
        raw_merchant_name="간이소매점",
        biz_reg_no=VALID_BIZ_NO_SIMPLIFIED,
        total_amount=22_000,
        supply_value=20_000,
        vat_amount=2_000,
        account_code="830",
        account_name="소모품비",
        vat_deduction_type="DEDUCTIBLE",
        counterparty_info="현장 문구류",
        reconcile_status="HOMETAX_VERIFIED",
        transacted_at="2026-10-05 11:00:00",
    )
    # 2. Entertainment missing attendee
    _insert_tx(
        cur,
        card_last4="3081",
        raw_merchant_name="강남한우",
        total_amount=220_000,
        supply_value=200_000,
        vat_amount=20_000,
        account_code="813",
        account_name="기업업무추진비",
        vat_deduction_type="NON_DEDUCTIBLE",
        non_deduct_reason="ENTERTAINMENT",
        counterparty_info=None,
        reconcile_status="HOMETAX_VERIFIED",
        transacted_at="2026-10-06 19:30:00",
    )
    # 3. Blocked personal card mixed in
    _insert_tx(
        cur,
        card_last4="8691",
        raw_merchant_name="개인카페",
        total_amount=11_000,
        supply_value=10_000,
        vat_amount=1_000,
        account_code="811",
        account_name="복리후생비",
        vat_deduction_type="DEDUCTIBLE",
        counterparty_info="커피",
        reconcile_status="HOMETAX_VERIFIED",
        transacted_at="2026-10-07 14:00:00",
    )
    # 4. Vehicle expense exceeding 15,000,000 KRW annual limit
    _insert_tx(
        cur,
        card_last4="3081",
        raw_merchant_name="제트라인모터스",
        total_amount=16_000_000,
        supply_value=14_545_455,
        vat_amount=1_454_545,
        account_code="822",
        account_name="차량유지비",
        vat_deduction_type="NON_DEDUCTIBLE",
        non_deduct_reason="PASSENGER_CAR",
        is_vehicle_expense=1,
        counterparty_info="업무용승용차 정비",
        reconcile_status="HOMETAX_VERIFIED",
        transacted_at="2026-10-10 15:00:00",
    )
    # Clean month in a separate tax year (2027-01)
    cur.execute(
        """
        INSERT INTO merchants (biz_reg_no, merchant_name, tax_type_code, biz_status)
        VALUES (?, 'OPENAI', 'GENERAL', 'ACTIVE')
        """,
        (VALID_BIZ_NO_GENERAL,),
    )
    _insert_tx(
        cur,
        card_last4="3081",
        raw_merchant_name="OPENAI",
        biz_reg_no=VALID_BIZ_NO_GENERAL,
        total_amount=33_000,
        supply_value=30_000,
        vat_amount=3_000,
        account_code="831",
        account_name="지급수수료",
        vat_deduction_type="DEDUCTIBLE",
        counterparty_info="업무용 AI 구독",
        reconcile_status="HOMETAX_VERIFIED",
        transacted_at="2027-01-03 10:00:00",
    )
    db_conn.commit()

    dirty_audit = await run_pre_export_tax_audit(
        cur,
        "202610",
        request=TaxAuditRequest(auto_revert_unverified_to_queue=True),
        archive_dir=archive_dir,
    )
    assert dirty_audit["scanned_count"] == 4
    assert dirty_audit["ready_for_export"] is False
    assert dirty_audit["error_count"] >= 3
    assert dirty_audit["warning_count"] >= 1
    codes = {iss["code"] for iss in dirty_audit["issues"]}
    assert "DEDUCTIBLE_ON_NON_GENERAL_MERCHANT" in codes
    assert "MISSING_ENTERTAINMENT_ATTENDEE" in codes
    assert "PERSONAL_CARD_MIXED" in codes
    assert "VEHICLE_LIMIT_EXCEEDED" in codes
    assert dirty_audit["visited_nodes"] == [
        "audit_fan_out",
        "audit_vat_status",
        "audit_vehicle_and_card_policy",
        "audit_missing_evidence",
        "join_audit_report",
    ]

    clean_audit = await run_pre_export_tax_audit(cur, "202701", archive_dir=archive_dir)
    assert clean_audit["scanned_count"] == 1
    assert clean_audit["ready_for_export"] is True
    assert clean_audit["error_count"] == 0
    assert clean_audit["issues"] == []


def test_phase8_workflow_and_audit_api_endpoints(client):
    headers = {"X-API-Key": "test-secret-key"}

    # Auth check
    r_unauth = client.post("/api/v1/workflows/transactions/1/run")
    assert r_unauth.status_code == 401
    r_audit_unauth = client.post("/api/v1/audits/tax-pack/202610")
    assert r_audit_unauth.status_code == 401

    # Not found / Invalid year_month
    assert client.post("/api/v1/workflows/transactions/99999/run", headers=headers).status_code == 404
    assert (
        client.post(
            "/api/v1/workflows/transactions/99999/resume",
            json={"counterparty_info": "메모"},
            headers=headers,
        ).status_code
        == 404
    )
    assert client.post("/api/v1/audits/tax-pack/202613", headers=headers).status_code == 400

    # Ingest a PG transaction (쿠팡 -> 999 미분류)
    r_sms = client.post(
        "/api/v1/webhooks/sms",
        json={
            "raw_message": "[Web발신] 우리법인 3081 승인 10/20 14:15 45,000원 (주)쿠팡",
        },
        headers=headers,
    )
    assert r_sms.status_code == 200
    tx_id = r_sms.json()["tx_id"]

    # 1. Run workflow without item hint -> INTERRUPTED for human review
    r_run = client.post(
        f"/api/v1/workflows/transactions/{tx_id}/run",
        json={},
        headers=headers,
    )
    assert r_run.status_code == 200
    run_body = r_run.json()
    assert run_body["status"] == "INTERRUPTED"
    assert run_body["interrupt_reason"] == "MISSING_PG_OR_MART_ITEM_MEMO"
    assert run_body["checkpoint_id"] is not None

    # 2. Pre-export audit detects unresolved 999/PG item -> ready_for_export False
    r_audit_bad = client.post("/api/v1/audits/tax-pack/202610", headers=headers)
    assert r_audit_bad.status_code == 200
    bad_body = r_audit_bad.json()
    assert bad_body["ready_for_export"] is False
    assert tx_id in bad_body["flagged_tx_ids"]

    # 3. Resume checkpoint with item clarification -> commits 501 원재료비
    r_resume = client.post(
        f"/api/v1/workflows/transactions/{tx_id}/resume",
        json={"counterparty_info": "현장 조명 다운라이트 4개"},
        headers=headers,
    )
    assert r_resume.status_code == 200
    resume_body = r_resume.json()
    assert resume_body["status"] == "COMPLETED"
    assert resume_body["account_code"] == "501"
    assert resume_body["account_name"] == "원재료비"
    assert resume_body["vat_deduction_type"] == "DEDUCTIBLE"

    # 4. Pre-export audit now passes with 0 critical errors
    r_audit_ok = client.post("/api/v1/audits/tax-pack/202610", headers=headers)
    assert r_audit_ok.status_code == 200
    ok_body = r_audit_ok.json()
    assert ok_body["ready_for_export"] is True
    assert ok_body["error_count"] == 0
