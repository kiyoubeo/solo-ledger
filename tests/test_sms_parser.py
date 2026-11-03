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
    expected_title = f"매출전표_20260705_1854_네이버현대카드_씨유영등포신성점_복리후생비_15600원_TX{tx_id}"
    assert slip_data["print_title"] == expected_title
    dash_html = client.get("/dashboard?year_month=ALL").text
    assert f"openReceiptModal({tx_id})" in dash_html
    assert "@media print" in dash_html
    assert "body.modal-open > section" in dash_html
    assert "printReceiptSlip()" in dash_html
    assert "buildReceiptPrintTitle" in dash_html

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

    # Verify real Woori Card CSV format (preamble banner + multiline header + MM.DD HH:MM:SS + cancel amount)
    woori_real_csv = (
        " ▶ 국내 이용내역,,(2026.10.01 ~ 2026.10.31),,,,,,,\n"
        '이용일,승인번호,이용카드,이용가맹점(은행)명,사업자번호,이용구분,"할부\n개월",'
        "이용금액(원),취소금액(원),결제예정일\n"
        '10.24 10:12:43,80797125,6534,모바일티머니_버스1건,1048183559,일시불,,"1,500",0,2026.11.14\n'
        '10.04 09:45:26,85561585,6534,주식회사 카카오페이손해보험,1138521083,일시불,,"23,050",0,2026.11.14\n'
        '10.04 09:25:08,56550426,6534,세부퍼시픽항공（ＢＳ,5628400022,일시불,,"586,400","586,400",미접수/취소\n'
    )
    woori_csv_res = client.post(
        "/api/v1/scraper/ingest",
        headers={"X-API-Key": "test-secret-key"},
        json={"portal": "WOORI", "raw_content": woori_real_csv, "auto_generate_slips": True},
    )
    assert woori_csv_res.status_code == 200
    assert woori_csv_res.json()["scraped_rows_count"] == 3

    # Verify real Hyundai Card HTML .xls billing statement format (colspan banner + Korean date + </td   >)
    hyundai_xls_html = (
        '<table cellspacing="0" cellpadding="0" border="1">'
        '<tr><th colspan="10">2026년 11월 이용대금명세서</th></tr>'
        '<tr><th colspan="10">결제 상세내역</th></tr>'
        "<tr><th>이용일</th><th>이용카드</th><th>이용가맹점</th><th>이용금액</th></tr>"
        "<tr><td>2026년 10월 01일</td><td>본인 네이버 현대카드</td>"
        "<td>나이스카페인클럽가산먹거리촌점</td   ><td>3,800</td></tr>"
        "<tr><td>2026년 10월 02일</td><td>본인 네이버 현대카드</td>"
        "<td>전국고속버스운송사업조합</td   ><td>43,300</td></tr>"
        "<tr><td>-</td><td></td><td>총 합계 2 건</td><td>0</td></tr>"
        "</table>"
    )
    hyundai_res = client.post(
        "/api/v1/scraper/ingest",
        headers={"X-API-Key": "test-secret-key"},
        json={"portal": "HYUNDAI", "raw_content": hyundai_xls_html, "auto_generate_slips": True},
    )
    assert hyundai_res.status_code == 200
    assert hyundai_res.json()["scraped_rows_count"] == 2

    # Verify real KB Kookmin Card statement format (multi-row preamble, empty 가맹점정보 col, .0 amounts)
    kb_csv = (
        ",,,,,,,,,,,,,\n"
        "조회기간,2025-11-02 ~ 2026-11-02,,,,,,,,,,,,\n"
        "카드번호,전체,,,,,,이용구분,일시불/할부,,,,,\n"
        '이용일,"이용\n시간","이용\n고객명",이용카드명,이용하신곳,"국내이용금액\n(원)",'
        '"해외이용금액\n($)",결제방법,"가맹점\n정보",할인금액,"적립(예상)\n포인트리",상태,결제예정일,승인번호\n'
        "2026-11-01,21:38,김영범,(신) 쿠팡 와우 카드,쿠팡(쿠페이),12170.0,0.0,일시불,,"
        "0.0,0,전표매입,2026-12-14,30001964\n"
        "2026-09-30,06:28,김영범,KB국민 하이패스2.0카드,터널/도로 1 건,1700.0,0.0,일시불,,"
        "0.0,1,전표매입,2026-10-14,45477777\n"
        "2025-12-30,18:51,김영범,(구)KB국민 My WE:SH(마이 위시) 카드,지코바,20500.0,0.0,일시불,,"
        "0.0,0,전표매입,2026-01-14,30000984\n"
    )
    kb_res = client.post(
        "/api/v1/scraper/ingest",
        headers={"X-API-Key": "test-secret-key"},
        json={"portal": "COUPANG", "raw_content": kb_csv, "auto_generate_slips": True},
    )
    assert kb_res.status_code == 200
    kb_body = kb_res.json()
    assert kb_body["mode"] == "CARD_SLIP"
    assert kb_body["scraped_rows_count"] == 3

    # Verify real Lotte Card multi-table HTML .xls format (summary table + detail table + partial cancel)
    lotte_xls_html = (
        "<table><thead><tr><th>조회기간</th><th>건수합계</th><th>금액합계(원)</th></tr></thead>"
        "<tbody><tr><td>2025.11.02~2026.11.02</td><td>2건</td><td>39,400</td></tr></tbody></table>"
        "<table><thead><tr><th>이용일자</th><th>이용시간</th><th>이용카드</th><th>이용가맹점</th>"
        "<th>업종</th><th>이용금액</th><th>승인번호</th><th>취소여부</th><th>취소금액</th></tr></thead>"
        "<tbody>"
        "<tr><td>2026.10.30</td><td>21:07</td><td>본인 LOCA X Special SE(6*4*)</td>"
        "<td>이마트24 가산점</td><td>편의점</td><td>28,400</td><td>64018116</td><td>N</td><td>0</td></tr>"
        "<tr><td>2026.05.09</td><td>15:02</td><td>본인 LOCA X Special SE(6*4*)</td>"
        "<td>(주)트리니티항공</td><td>항공사</td><td>136,000</td><td>66469034</td><td>Y</td><td>-125,000</td></tr>"
        "</tbody></table>"
    )
    lotte_res = client.post(
        "/api/v1/scraper/ingest",
        headers={"X-API-Key": "test-secret-key"},
        json={"portal": "LOTTE", "raw_content": lotte_xls_html, "auto_generate_slips": True},
    )
    assert lotte_res.status_code == 200
    assert lotte_res.json()["scraped_rows_count"] == 2

    # Verify card_breakdown in dashboard summary is sorted by card_label ascending (이름순)
    all_sum = client.get(
        "/api/v1/dashboard/summary?year_month=ALL",
        headers={"X-API-Key": "test-secret-key"},
    ).json()
    c_labels = [c["card_label"] for c in all_sum["card_breakdown"]]
    assert c_labels == sorted(c_labels)
    assert len(all_sum["monthly_periods"]) >= 1
    dash_html = client.get("/dashboard?year_month=ALL").text
    assert 'id="month-filter"' in dash_html
    assert "selectMonthRange(" in dash_html

    # Verify Coupang 1-click browser extractor script endpoint
    cp_script_res = client.get("/api/v1/scraper/coupang-script")
    assert cp_script_res.status_code == 200
    assert "/ssr/api/myorders/model/page" in cp_script_res.json()["script"]

    # Verify Coupang Order List screen copy/DOM text matching (multi-item order -> 12,170 KRW)
    coupang_screen_text = (
        "주문목록\n"
        "2026. 11. 1 주문\n"
        "주문상세보기\n"
        "배송완료 11/2(월) 도착\n"
        "코멧 홈 베이직 미용티슈 250매 6팩\n"
        "7,000원 · 2개\n"
        "탐사수 무라벨 2L 6병\n"
        "5,170원 · 1개\n"
        "장바구니 담기\n"
    )
    cp_match_res = client.post(
        "/api/v1/scraper/ingest",
        headers={"X-API-Key": "test-secret-key"},
        json={
            "portal": "COUPANG",
            "raw_content": coupang_screen_text,
            "auto_generate_slips": True,
        },
    )
    assert cp_match_res.status_code == 200
    cp_match_body = cp_match_res.json()
    assert cp_match_body["mode"] == "ECOMMERCE_ORDER"
    assert cp_match_body["matched_orders"] >= 1
    assert cp_match_body["generated_statements_count"] >= 1
    cp_tx_id = cp_match_body["updated_tx_ids"][0]
    cp_rec = client.get(f"/api/v1/transactions/{cp_tx_id}/receipt").json()
    assert cp_rec["archived_file_name"].startswith("거래명세서_")
    assert len(cp_rec["line_items"]) == 2
    assert cp_rec["line_items"][0]["qty"] == 2

    # Verify Method B: POST /api/v1/scraper/coupang-auto-crawl via mocked CDP session
    from unittest.mock import patch

    from sololedger import scraper_service

    class _DummyCdpWs:
        def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
            self.ws_url = ws_url

        def call(self, method: str, params=None, msg_id: int = 1):
            if msg_id == 1:
                return {"id": 1, "result": {"result": {"value": {"state": "READY"}}}}
            if msg_id == 2:
                payload = '[{"주문일자":"2026-11-01","상품명":"탐사수 무라벨 2L 12병","결제금액":12170}]'
                return {
                    "id": 2,
                    "result": {
                        "result": {
                            "value": {
                                "ordersCount": 1,
                                "jsonPayload": payload,
                                "domText": "",
                            }
                        }
                    },
                }
            return {"id": msg_id, "result": {}}

        def close(self) -> None:
            pass

    with (
        patch.object(scraper_service, "_find_local_chromium_executable", lambda: "chrome.exe"),
        patch.object(
            scraper_service,
            "_fetch_cdp_page_ws_url",
            lambda port: "ws://127.0.0.1:9333/devtools/page/1",
        ),
        patch.object(scraper_service, "_CdpWebSocket", _DummyCdpWs),
    ):
        crawl_res = client.post(
            "/api/v1/scraper/coupang-auto-crawl",
            headers={"X-API-Key": "test-secret-key"},
            json={"max_wait_sec": 10},
        )
    assert crawl_res.status_code == 200
    crawl_body = crawl_res.json()
    assert crawl_body["status"] == "crawled"
    assert crawl_body["collected_orders_count"] == 1
    assert crawl_body["matched_orders"] >= 1

    # Verify Transaction Statement (거래명세서) generation & attachment endpoint
    stmt_tx_id = all_sum["transactions"][0]["tx_id"]
    stmt_res = client.post(
        f"/api/v1/transactions/{stmt_tx_id}/statement",
        headers={"X-API-Key": "test-secret-key"},
        json={"item_name": "사무용 A4 복사용지 80g 5권"},
    )
    assert stmt_res.status_code == 200
    stmt_body = stmt_res.json()
    assert stmt_body["status"] == "statement_saved"
    assert stmt_body["archived_file_name"].startswith("거래명세서_")

    rec_detail = client.get(f"/api/v1/transactions/{stmt_tx_id}/receipt").json()
    assert rec_detail["statement_print_title"].startswith("거래명세서_")
    assert rec_detail["counterparty_info"] == "사무용 A4 복사용지 80g 5권"
    stmt_file_html = client.get(f"/api/v1/transactions/{stmt_tx_id}/receipt-file").text
    assert "VAT 제외 가격" in stmt_file_html
    assert "VAT 가격" in stmt_file_html
    assert "합계 가격" in stmt_file_html
    assert "VAT 제외 가격 (공급가액)" in dash_html
    assert "VAT 가격 (부가세액)" in dash_html
    assert "합계 가격 (총 결제금액)" in dash_html

    # Verify Real-Time Email Order Webhook & IMAP Sync
    email_html_body = """
    <table>
      <tr><th>상점명</th><td>컴퓨존</td></tr>
      <tr><th>상품명</th><td>로지텍 MX Master 3S 무선 마우스</td></tr>
      <tr><th>결제금액</th><td>12,170원</td></tr>
      <tr><th>결제일시</th><td>2026-11-01 18:10:00</td></tr>
    </table>
    """
    em_wh_res = client.post(
        "/api/v1/scraper/email-webhook",
        headers={"X-API-Key": "test-secret-key"},
        json={"raw_content": email_html_body, "subject": "[KG이니시스] 결제확인 안내"},
    )
    assert em_wh_res.status_code == 200
    em_wh_body = em_wh_res.json()
    assert em_wh_body["parsed_orders_count"] == 1
    assert em_wh_body["matched_count"] == 1
    em_tx_id = em_wh_body["updated_tx_ids"][0]
    em_rec = client.get(f"/api/v1/transactions/{em_tx_id}/receipt").json()
    assert "[컴퓨존] 로지텍 MX Master 3S 무선 마우스" in em_rec["counterparty_info"]
    assert em_rec["archived_file_name"].startswith("거래명세서_")

    # Verify IMAP config save & mocked IMAP sync
    import imaplib

    class _DummyImapSsl:
        def __init__(self, host: str, port: int, timeout: float = 20.0) -> None:
            self.host = host

        def login(self, user: str, pw: str):
            return "OK", [b"Logged in"]

        def select(self, mailbox: str, readonly: bool = True):
            return "OK", [b"1"]

        def search(self, charset, criterion: str):
            return "OK", [b"1"]

        def fetch(self, num: bytes, parts: str):
            if "HEADER.FIELDS" in parts:
                hdr = (
                    b"Subject: [Coupang] Order Payment Completed\r\n"
                    b"From: noreply@coupang.com\r\n"
                    b"Date: Sun, 01 Nov 2026 18:10:00 +0900\r\n"
                    b"Message-ID: <test-order-101@coupang.com>\r\n\r\n"
                )
                return "OK", [(b"1 (BODY[HEADER] {120})", hdr)]
            eml = (
                "MIME-Version: 1.0\r\n"
                "Subject: [쿠팡] 주문 결제완료 안내\r\n"
                "From: noreply@coupang.com\r\n"
                "Date: Sun, 01 Nov 2026 18:10:00 +0900\r\n"
                "Content-Type: text/html; charset=utf-8\r\n\r\n"
                "<table><tr><th>주문일자</th><td>2026-11-01</td></tr>"
                "<tr><th>상품명</th><td>쓰리엠 스카치 다목적 가위 3개입</td></tr>"
                "<tr><th>결제금액</th><td>12,170원</td></tr></table>"
            ).encode("utf-8")
            return "OK", [(b"1 (BODY[] {300})", eml)]

        def logout(self):
            return "BYE", [b"Logout"]

    with patch.object(imaplib, "IMAP4_SSL", _DummyImapSsl):
        cfg_res = client.post(
            "/api/v1/scraper/email-config",
            headers={"X-API-Key": "test-secret-key"},
            json={
                "label": "대표 네이버메일",
                "provider": "NAVER",
                "email_address": "ceo@naver.com",
                "app_password": "test-app-password",
                "auto_poll_enabled": True,
                "sync_now": True,
                "force_rescan": True,
            },
        )
        cfg_res2 = client.post(
            "/api/v1/scraper/email-config",
            headers={"X-API-Key": "test-secret-key"},
            json={
                "label": "법인 Gmail",
                "provider": "GMAIL",
                "email_address": "corp@gmail.com",
                "app_password": "abcd efgh ijkl mnop",
                "sync_now": True,
                "force_rescan": True,
            },
        )
    assert cfg_res.status_code == 200
    assert cfg_res2.status_code == 200
    cfg_body2 = cfg_res2.json()
    assert cfg_body2["has_password"] is True
    assert cfg_body2["total_accounts_count"] == 2
    assert cfg_body2["sync_result"]["status"] == "synced"
    assert cfg_body2["sync_result"]["scanned_accounts"] == 2

    sec_acc_id = cfg_body2["accounts"][1]["account_id"]
    toggle_res = client.patch(
        f"/api/v1/scraper/email-accounts/{sec_acc_id}",
        headers={"X-API-Key": "test-secret-key"},
        json={"enabled": False},
    )
    assert toggle_res.status_code == 200
    assert toggle_res.json()["enabled_accounts_count"] == 1

    del_res = client.delete(
        f"/api/v1/scraper/email-accounts/{sec_acc_id}",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert del_res.status_code == 200
    assert del_res.json()["total_accounts_count"] == 1

    # Regression check: ensure dashboard <script> has no broken f-string quote escapes
    dash_js = dash_html.split("<script>")[1].split("</script>")[0]
    assert "async function openEmailOrderModal()" in dash_js
    assert "scanEmailAccountByIdx(" in dash_js
    assert "renderEmailMatchedList(" in dash_js
    assert "openMatchedStatementByIdx(" in dash_js
    assert "toggleStatementRowsFilter(" in dash_js
    assert "'' +" not in dash_js


def test_email_parser_no_duplicate_pg_rows_coupang_table_and_offline_filter(client):
    """Ensure EasyPay 1-item email never emits 3x duplicate rows, Coupang table prorates, and offline skips."""
    from sololedger.email_watcher import extract_orders_from_email_content

    # 1. EasyPay PG email with '이지페이 결제내역' and '결제하신 내역입니다.' header rows
    easypay_html = (
        "<table>"
        "<tr><th>이지페이 결제내역</th><td>김*범님, 쿠팡(주)에서 2026/11/01 21:38에 결제하신 내역입니다.</td></tr>"
        "<tr><th>구매상점명</th><td>쿠팡(주)</td></tr>"
        "<tr><th>상품명</th><td>이롬 황성주 박사의 고단백 두유 플레인 1</td></tr>"
        "<tr><th>결제금액</th><td>12,170원</td></tr>"
        "<tr><th>승인번호</th><td>30001964</td></tr>"
        "</table>"
    )
    ep_orders = extract_orders_from_email_content(
        subject="[한국정보통신] 김*범님의 결제내역입니다.",
        date_str="Sun, 01 Nov 2026 21:38:10 +0900",
        body_html=easypay_html,
    )
    assert len(ep_orders) == 1
    assert ep_orders[0]["상품명"] == "이롬 황성주 박사의 고단백 두유 플레인 1"
    assert len(ep_orders[0]["품목내역"]) == 1
    assert ep_orders[0]["품목내역"][0]["item"] == "이롬 황성주 박사의 고단백 두유 플레인 1"

    # 2. Coupang multi-item order confirmation email with coupon/card discount prorating
    coupang_mail_html = (
        "<table>"
        "<tr><th>구매 상세내역</th><th>쿠팡가</th><th>수량</th>"
        "<th>구매금액</th><th>판매자</th></tr>"
        "<tr><td>나랑드사이다 제로 500ml, 24개</td><td>15,900원</td><td>1</td>"
        "<td>15,900원</td><td>쿠팡(주)</td></tr>"
        "<tr><td>부케가르니 나드 샴푸 1000ml, 2개</td><td>19,800원</td><td>1</td>"
        "<td>19,800원</td><td>쿠팡(주)</td></tr>"
        "</table>"
        "<table><tr><th>총 결제금액</th><td>33,700원</td></tr></table>"
    )
    cp_orders = extract_orders_from_email_content(
        subject="[쿠팡] 김*범님 주문하신 내역을 확인해주세요.",
        date_str="Sun, 01 Nov 2026 03:45:00 +0900",
        body_html=coupang_mail_html,
    )
    assert len(cp_orders) == 1
    assert cp_orders[0]["결제금액"] == 33700
    assert len(cp_orders[0]["품목내역"]) == 2
    assert sum(li["price"] for li in cp_orders[0]["품목내역"]) == 33700
    assert cp_orders[0]["품목내역"][0]["item"] == "나랑드사이다 제로 500ml, 24개"

    # 3. NaverPay offline point/payment notification without product name must return 0 orders
    offline_naver_html = (
        "<div>김*범님, 결제하신 내역을 안내해드립니다.</div>"
        "<div>결제일시 : 2026-10-30 21:07:00</div>"
        "<div>결제금액 : 28,400원</div>"
        "<div>상세 내역은 페이홈 &gt; 결제내역 &gt; 오프라인 에서 확인하실 수 있습니다.</div>"
    )
    off_orders = extract_orders_from_email_content(
        subject="[네이버페이] 결제하신 내역을 안내해드립니다.",
        date_str="Fri, 30 Oct 2026 21:07:05 +0900",
        body_html=offline_naver_html,
    )
    assert off_orders == []

    # 4. Ingest KB card row + EasyPay webhook and verify email-config exposes recent_matched_items
    kb_single_csv = (
        "이용일,이용시간,이용고객명,이용카드명,이용하신곳,국내이용금액(원),결제방법,승인번호\n"
        "2026-11-01,21:38,김영범,(신) 쿠팡 와우 카드,쿠팡(쿠페이),12170,일시불,30001964\n"
    )
    client.post(
        "/api/v1/scraper/ingest",
        headers={"X-API-Key": "test-secret-key"},
        json={"portal": "KB", "raw_content": kb_single_csv, "auto_generate_slips": True},
    )
    wh_res = client.post(
        "/api/v1/scraper/email-webhook",
        headers={"X-API-Key": "test-secret-key"},
        json={"raw_content": easypay_html, "subject": "[한국정보통신] 결제내역"},
    )
    assert wh_res.status_code == 200
    wh_body = wh_res.json()
    assert wh_body["matched_count"] == 1
    matched_tx_id = wh_body["updated_tx_ids"][0]

    rec_json = client.get(f"/api/v1/transactions/{matched_tx_id}/receipt").json()
    assert len(rec_json["line_items"]) == 1
    assert rec_json["counterparty_info"] == "이롬 황성주 박사의 고단백 두유 플레인 1"

    cfg_json = client.get("/api/v1/scraper/email-config").json()
    assert cfg_json["recent_matched_count"] >= 1
    assert any(it["tx_id"] == matched_tx_id for it in cfg_json["recent_matched_items"])





