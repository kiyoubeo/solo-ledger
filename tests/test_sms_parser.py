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
