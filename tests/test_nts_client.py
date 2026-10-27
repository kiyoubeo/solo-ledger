"""Unit and integration tests for Phase 2: NTS Business Status API & 30-day TTL Cache Engine."""

from datetime import datetime, timedelta

import httpx
import pytest

from sololedger.merchant_service import (
    apply_merchant_tax_status_to_ledger,
    get_or_verify_merchant,
)
from sololedger.nts_client import (
    fetch_nts_biz_status,
    normalize_biz_reg_no,
    validate_biz_reg_no_checksum,
)
from sololedger.tax_engine import evaluate_tax_rules


def test_normalize_and_checksum_validation():
    assert normalize_biz_reg_no("201-81-21515") == "2018121515"
    assert validate_biz_reg_no_checksum("201-81-21515") is True
    assert validate_biz_reg_no_checksum("120-88-00767") is True
    assert validate_biz_reg_no_checksum("220-81-62517") is True

    # Invalid check digit
    assert validate_biz_reg_no_checksum("201-81-21519") is False
    assert validate_biz_reg_no_checksum("12345") is False

    with pytest.raises(ValueError, match="Invalid business registration number"):
        normalize_biz_reg_no("123-45")


@pytest.mark.asyncio
async def test_fetch_nts_biz_status_mock_api():
    mock_payload = {
        "status_code": "OK",
        "match_cnt": 4,
        "request_cnt": 4,
        "data": [
            {
                "b_no": "2018121515",
                "b_stt": "계속사업자",
                "b_stt_cd": "01",
                "tax_type": "부가가치세 일반과세자",
                "tax_type_cd": "01",
            },
            {
                "b_no": "1234567891",
                "b_stt": "계속사업자",
                "b_stt_cd": "01",
                "tax_type": "부가가치세 간이과세자",
                "tax_type_cd": "02",
            },
            {
                "b_no": "2208162517",
                "b_stt": "휴업자",
                "b_stt_cd": "02",
                "tax_type": "부가가치세 면세사업자",
                "tax_type_cd": "04",
            },
            {
                "b_no": "9999999999",
                "b_stt": "",
                "b_stt_cd": "",
                "tax_type": "국세청에 등록되지 않은 사업자등록번호입니다.",
                "tax_type_cd": "",
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "serviceKey=test-nts-key" in str(request.url)
        return httpx.Response(200, json=mock_payload)

    transport = httpx.MockTransport(handler)
    results = await fetch_nts_biz_status(
        ["201-81-21515", "123-45-67891", "220-81-62517", "999-99-99999"],
        service_key="test-nts-key",
        transport=transport,
    )

    assert results["2018121515"].tax_type_code == "GENERAL"
    assert results["2018121515"].biz_status == "ACTIVE"
    assert results["2018121515"].is_valid_checksum is True

    assert results["1234567891"].tax_type_code == "SIMPLIFIED"
    assert results["1234567891"].biz_status == "ACTIVE"

    assert results["2208162517"].tax_type_code == "EXEMPT"
    assert results["2208162517"].biz_status == "SUSPENDED"

    assert results["9999999999"].tax_type_code == "UNREGISTERED"
    assert results["9999999999"].biz_status == "CLOSED"


@pytest.mark.asyncio
async def test_merchant_30day_ttl_cache_hit_and_expiry(db_conn):
    cur = db_conn.cursor()
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b_no": "1208800767",
                        "b_stt": "계속사업자",
                        "b_stt_cd": "01",
                        "tax_type": "부가가치세 일반과세자",
                        "tax_type_cd": "01",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    t0 = datetime(2026, 10, 1, 10, 0, 0)

    # 1. First lookup at T0 -> Cache miss, hits NTS API
    res1 = await get_or_verify_merchant(
        cur,
        biz_reg_no="120-88-00767",
        merchant_name="(주)쿠팡",
        reference_now=t0,
        service_key="test-key",
        transport=transport,
    )
    assert res1.from_cache is False
    assert res1.tax_type_code == "GENERAL"
    assert res1.is_pg_provider is True
    assert call_count == 1

    # 2. Lookup 15 days later -> Cache hit (within 30-day TTL), no API call
    res2 = await get_or_verify_merchant(
        cur,
        biz_reg_no="120-88-00767",
        reference_now=t0 + timedelta(days=15),
        service_key="test-key",
        transport=transport,
    )
    assert res2.from_cache is True
    assert res2.merchant_name == "(주)쿠팡"
    assert call_count == 1

    # 3. Lookup 31 days later -> Cache expired (>= 30 days), hits NTS API again
    res3 = await get_or_verify_merchant(
        cur,
        biz_reg_no="120-88-00767",
        reference_now=t0 + timedelta(days=31),
        service_key="test-key",
        transport=transport,
    )
    assert res3.from_cache is False
    assert call_count == 2


@pytest.mark.asyncio
async def test_apply_merchant_status_to_ledger_and_recurring_default_account(db_conn):
    cur = db_conn.cursor()
    # Insert an unclassified transaction for a local simplified taxpayer restaurant
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount,
            account_code, account_name, vat_deduction_type, reconcile_status
        ) VALUES (
            '1234', '2026-10-25 12:30:00', '행복한간이식당',
            22000, 20000, 2000, '999', '미분류', 'PENDING', 'SMS_ONLY'
        )
        """
    )
    tx_id = cur.lastrowid

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b_no": "2018121515",
                        "b_stt": "계속사업자",
                        "b_stt_cd": "01",
                        "tax_type": "부가가치세 간이과세자",
                        "tax_type_cd": "02",
                    }
                ]
            },
        )

    status = await get_or_verify_merchant(
        cur,
        biz_reg_no="201-81-21515",
        merchant_name="행복한간이식당",
        default_account_code="811",
        service_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    updated_cnt = apply_merchant_tax_status_to_ledger(cur, status)
    assert updated_cnt == 1

    cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    row = cur.fetchone()
    assert row["biz_reg_no"] == "2018121515"
    assert row["account_code"] == "811"
    assert row["account_name"] == "복리후생비"
    assert row["vat_deduction_type"] == "NON_DEDUCTIBLE"
    assert row["non_deduct_reason"] == "SIMPLIFIED_TAX"

    # Next time evaluate_tax_rules runs for '행복한간이식당', it uses cached merchant overrides
    next_eval = evaluate_tax_rules(cur, "행복한간이식당")
    assert next_eval["account_code"] == "811"
    assert next_eval["vat_deduction_type"] == "NON_DEDUCTIBLE"
    assert next_eval["non_deduct_reason"] == "SIMPLIFIED_TAX"


def test_verify_merchant_api_endpoints(client):
    headers = {"X-API-Key": "test-secret-key"}
    payload = {
        "biz_reg_no": "201-81-21515",
        "merchant_name": "스타벅스코리아",
        "default_account_code": "811",
    }
    r1 = client.post("/api/v1/merchants/verify", json=payload, headers=headers)
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["status"] == "verified"
    assert body1["merchant"]["biz_reg_no"] == "2018121515"
    assert body1["merchant"]["is_valid_checksum"] is True
    assert body1["merchant"]["from_cache"] is False

    # Second call hits 30-day cache
    r2 = client.post("/api/v1/merchants/verify", json=payload, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["merchant"]["from_cache"] is True

    # List cached merchants
    r_list = client.get("/api/v1/merchants", headers=headers)
    assert r_list.status_code == 200
    assert r_list.json()["count"] == 1
    assert r_list.json()["items"][0]["merchant_name"] == "스타벅스코리아"
