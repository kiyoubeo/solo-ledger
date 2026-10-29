"""Unit tests for Korean corporate card SMS parser."""

import pytest

from sololedger.sms_parser import parse_korean_card_sms


def test_parse_single_line_shinhan():
    sms = "[Web발신] 신한법인승인 12*4 10/20 13:20 45,000원 일시불 (주)쿠팡"
    res = parse_korean_card_sms(sms, fallback_time="2026-10-20T13:20:05")
    assert res.card_last4 == "12*4"
    assert res.transacted_at == "2026-10-20 13:20:00"
    assert res.total_amount == 45000
    assert res.raw_merchant_name == "(주)쿠팡"
    assert res.is_cancel is False


def test_parse_multi_line_kb():
    sms = (
        "[Web발신]\n"
        "KB국민법인(9876)\n"
        "김*범님 승인\n"
        "45,000원 일시불\n"
        "10/20 13:20\n"
        "스피드메이트 대전용산점\n"
        "누적 1,250,000원"
    )
    res = parse_korean_card_sms(sms, fallback_time="2026-10-20T13:20:10")
    assert res.card_last4 == "9876"
    assert res.transacted_at == "2026-10-20 13:20:00"
    assert res.total_amount == 45000
    assert res.raw_merchant_name == "스피드메이트 대전용산점"
    assert res.is_cancel is False


def test_parse_cancellation_sms():
    sms = "[Web발신] 신한법인취소 12*4 10/20 13:25 45,000원 일시불 (주)쿠팡"
    res = parse_korean_card_sms(sms, fallback_time="2026-10-20T13:25:00")
    assert res.is_cancel is True
    assert res.total_amount == -45000
    assert res.raw_merchant_name == "(주)쿠팡"


def test_parse_sms_with_approval_number():
    sms = "[Web발신] 하나법인(3456) 승인 11,000원 일시불 10/27 09:15 스타벅스역삼점 승인번호:12345678"
    res = parse_korean_card_sms(sms, fallback_time="2026-10-27T09:15:00")
    assert res.card_last4 == "3456"
    assert res.approval_no == "12345678"
    assert res.total_amount == 11000
    assert res.raw_merchant_name == "스타벅스역삼점"


def test_parse_non_payment_sms_raises():
    with pytest.raises(ValueError, match="Failed to extract KRW payment amount"):
        parse_korean_card_sms("[Web발신] 인증번호는 [482910] 입니다.")


def test_parse_real_phone_samsung_lotte_hana_formats():
    # 1. Samsung Card without parentheses + merchant containing '현대'
    s1 = "[Web발신]\n삼성8508승인 김*범\n97,000원 일시불\n04/16 17:23 에이치디현대오일"
    r1 = parse_korean_card_sms(s1, fallback_time="2024-04-16T17:23:00")
    assert r1.card_last4 == "8508"
    assert r1.raw_merchant_name == "에이치디현대오일"
    assert r1.total_amount == 97000
    assert r1.is_cancel is False

    # 2. Samsung Card cancellation with negative amount
    s2 = "[Web발신]\n삼성8508취소 김*범\n-150,000원 일시불\n04/16 00:35 망향주유소"
    r2 = parse_korean_card_sms(s2, fallback_time="2024-04-16T00:35:00")
    assert r2.card_last4 == "8508"
    assert r2.raw_merchant_name == "망향주유소"
    assert r2.total_amount == -150000
    assert r2.is_cancel is True

    # 3. Lotte Card inverted merchant-first format & masked last digit (0*7*)
    s3 = "[Web발신]\n곰수산\n38,000원 승인\n김*범 롯데0*7*\n일시불 10/15 21:21\n누적292,590원"
    r3 = parse_korean_card_sms(s3, fallback_time="2022-10-15T21:21:00")
    assert r3.card_last4 == "0*7*"
    assert r3.raw_merchant_name == "곰수산"
    assert r3.total_amount == 38000

    # 4. Lotte Card overseas KRW format
    s4 = "[Web발신]\n롯데6*1* 해외승인\n김*범\nKRW 2,200 일시불\n06/15 01:05\nSTEAMGAMES.\n누적1,766,034원"
    r4 = parse_korean_card_sms(s4, fallback_time="2020-06-15T01:05:00")
    assert r4.card_last4 == "6*1*"
    assert r4.raw_merchant_name == "STEAMGAMES."
    assert r4.total_amount == 2200

    # 5. Hana Check Card masked format (5*5*)
    s5 = "[Web발신]\n하나5*5*체크승인 김*범 9,000원 12/26 12:16 (주)코리아세븐대전테크노월드"
    r5 = parse_korean_card_sms(s5, fallback_time="2022-12-26T12:16:00")
    assert r5.card_last4 == "5*5*"
    assert r5.raw_merchant_name == "(주)코리아세븐대전테크노월드"
    assert r5.total_amount == 9000


def test_import_phone_sms_api_endpoint(client):
    mock_adb_stdout = (
        "Row: 0 _id=8001, address=15776200, date=1783245240000, body=[Web발신]\n"
        "네이버 현대카드 승인\n김*범\n15,600원 일시불\n07/05 18:54\n씨유영등포신성점\n누적387,250원\n"
        "Row: 1 _id=8002, address=15888900, date=1713255780000, body=[Web발신]\n"
        "삼성8508승인 김*범\n97,000원 일시불\n04/16 17:23 에이치디현대오일\n"
    )
    res = client.post(
        "/api/v1/system/import-phone-sms",
        headers={"X-API-Key": "test-secret-key"},
        json={"limit": 10, "raw_adb_output": mock_adb_stdout},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "imported"
    assert body["inserted_count"] == 2

    # Verify ALL period dashboard summary includes both imported rows
    dash_res = client.get("/dashboard?year_month=ALL")
    assert dash_res.status_code == 200
    assert "씨유영등포신성점" in dash_res.text
    assert "에이치디현대오일" in dash_res.text

