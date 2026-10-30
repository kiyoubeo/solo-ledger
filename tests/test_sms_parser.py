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

    # 6. Woori Card RCS (Chat+ open_rich_card JSON) format from 10/04
    s6_rcs = (
        '{"messageHeader":"[Web발신]","card":"open_rich_card","layout":{"children":['
        '{"text":"● 우리카드 이용안내"},{"text":"우리(6534)승인\\n김*범님\\n23,050원 일시불\\n'
        '10/04 09:45\\n주식회사 카카오페이\\n누적1,680,410원"}]}}'
    )
    r6 = parse_korean_card_sms(s6_rcs, fallback_time="2026-10-04T09:45:27")
    assert r6.card_last4 == "6534"
    assert r6.transacted_at == "2026-10-04 09:45:00"
    assert r6.raw_merchant_name == "주식회사 카카오페이"
    assert r6.total_amount == 23050
    assert r6.is_cancel is False

    # 7. KB Kookmin Card RCS partial cancellation format
    s7 = "[Web발신]\n취소\n[KB국민카드] 8691 김*범님 쿠팡(쿠페이)-쿠 10/08 이용건 10/12 부분취소(-4,340원)"
    r7 = parse_korean_card_sms(s7, fallback_time="2026-10-12T09:02:32")
    assert r7.card_last4 == "8691"
    assert r7.raw_merchant_name == "쿠팡(쿠페이)-쿠"
    assert r7.total_amount == -4340
    assert r7.is_cancel is True


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

    # Verify ALL period dashboard summary includes both imported rows and per-card breakdown
    dash_res = client.get("/dashboard?year_month=ALL")
    assert dash_res.status_code == 200
    assert "씨유영등포신성점" in dash_res.text
    assert "에이치디현대오일" in dash_res.text
    assert "삼성카드(8508)" in dash_res.text
    assert "네이버현대카드" in dash_res.text

    # Verify filtering by a single card (8508)
    card_res = client.get("/dashboard?year_month=ALL&card_last4=8508")
    assert card_res.status_code == 200
    assert "에이치디현대오일" in card_res.text
    assert "씨유영등포신성점" not in card_res.text

    # Verify start_date ~ end_date range filtering ("언제부터 ~ 언제까지")
    range_res = client.get("/dashboard?start_date=2024-04-01&end_date=2024-04-30")
    assert range_res.status_code == 200
    assert "에이치디현대오일" in range_res.text
    assert "씨유영등포신성점" not in range_res.text
    assert 'value="2024-04-01"' in range_res.text
    assert 'value="2024-04-30"' in range_res.text

    range_sum = client.get(
        "/api/v1/dashboard/summary?start_date=2026-07-01&end_date=2026-07-31",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert range_sum.status_code == 200
    r_data = range_sum.json()
    assert r_data["start_date"] == "2026-07-01"
    assert r_data["end_date"] == "2026-07-31"
    assert r_data["total_tx_count"] == 1
    assert r_data["transactions"][0]["raw_merchant_name"] == "씨유영등포신성점"
    assert len(r_data["account_breakdown"]) >= 1
    assert r_data["account_breakdown"][0]["account_code"] == "811"

    # Verify Firefly III-style auto-learning & same-merchant propagation + reapply-rules
    re_res = client.post(
        "/api/v1/system/reapply-rules",
        headers={"X-API-Key": "test-secret-key"},
        json={},
    )
    assert re_res.status_code == 200
    assert re_res.json()["status"] == "reapplied"

    # Verify clickable transaction date opens receipt modal & /api/v1/transactions/{tx_id}/receipt works
    tx_id = r_data["transactions"][0]["tx_id"]
    slip_res = client.get(f"/api/v1/transactions/{tx_id}/receipt")
    assert slip_res.status_code == 200
    slip_data = slip_res.json()
    assert slip_data["tx_id"] == tx_id
    assert slip_data["raw_merchant_name"] == "씨유영등포신성점"
    assert slip_data["card_label"] == "네이버현대카드"
    assert f"openReceiptModal({tx_id})" in client.get("/dashboard?year_month=ALL").text

    # Verify Scraper Engine: targets list, HTML table (.xls) ingestion, and electronic slip generation
    targets_res = client.get("/api/v1/scraper/targets")
    assert targets_res.status_code == 200
    assert "HOMETAX" in targets_res.json()["targets"]
    assert "WOORI" in targets_res.json()["targets"]
    assert "COUPANG" in targets_res.json()["targets"]

    html_xls_payload = (
        "<table><tr><th>승인일자</th><th>승인시간</th><th>카드번호</th>"
        "<th>승인번호</th><th>이용가맹점</th><th>사업자번호</th><th>승인금액</th></tr>"
        "<tr><td>2026-10-04</td><td>09:45:00</td><td>6534</td>"
        "<td>88776655</td><td>주식회사 카카오페이</td><td>120-88-00767</td><td>23,050</td></tr></table>"
    )
    ingest_res = client.post(
        "/api/v1/scraper/ingest",
        headers={"X-API-Key": "test-secret-key"},
        json={"portal": "WOORI", "raw_content": html_xls_payload, "auto_generate_slips": True},
    )
    assert ingest_res.status_code == 200
    ing_body = ingest_res.json()
    assert ing_body["status"] == "ingested"
    assert ing_body["scraped_rows_count"] == 1
    assert ing_body["generated_slips_count"] >= 1

    # Verify E-Commerce order item matching for the 23,050 KRW KakaoPay transaction
    ecom_json = '[{"주문일자":"2026-10-04","상품명":"무선 마우스 및 패드 세트","결제금액":"23050"}]'
    ecom_res = client.post(
        "/api/v1/scraper/ingest",
        headers={"X-API-Key": "test-secret-key"},
        json={"portal": "COUPANG", "raw_content": ecom_json},
    )
    assert ecom_res.status_code == 200
    assert ecom_res.json()["matched_orders"] == 1

    # Verify generated electronic slip file is served via /api/v1/transactions/{tx_id}/receipt-file
    file_res = client.get(f"/api/v1/transactions/{tx_id}/receipt-file")
    assert file_res.status_code == 200
    assert "신용카드 매출전표" in file_res.text






