"""Unit tests for the automated tax classification rule engine."""

from sololedger.tax_engine import (
    calculate_supply_and_vat,
    evaluate_tax_rules,
    generate_default_memo,
)


def test_rule_pg_ecommerce(db_conn):
    cur = db_conn.cursor()
    res = evaluate_tax_rules(cur, "(주)쿠팡")
    assert res["account_code"] == "830"
    assert res["account_name"] == "소모품비"
    assert res["vat_deduction_type"] == "DEDUCTIBLE"
    assert res["is_vehicle_expense"] is False
    assert res["require_memo_prompt"] is True


def test_rule_vehicle_expense_non_deductible(db_conn):
    cur = db_conn.cursor()
    res = evaluate_tax_rules(cur, "스피드메이트 대전용산점")
    assert res["account_code"] == "822"
    assert res["account_name"] == "차량유지비"
    assert res["vat_deduction_type"] == "NON_DEDUCTIBLE"
    assert res["non_deduct_reason"] == "PASSENGER_CAR"
    assert res["is_vehicle_expense"] is True
    assert res["require_memo_prompt"] is False


def test_rule_saas_subscription(db_conn):
    cur = db_conn.cursor()
    res = evaluate_tax_rules(cur, "OPENAI CHATGPT")
    assert res["account_code"] == "831"
    assert res["account_name"] == "지급수수료"
    assert res["vat_deduction_type"] == "DEDUCTIBLE"
    assert res["require_memo_prompt"] is False


def test_rule_cafe_welfare_prompt(db_conn):
    cur = db_conn.cursor()
    res = evaluate_tax_rules(cur, "스타벅스 강남점")
    assert res["account_code"] == "811"
    assert res["account_name"] == "복리후생비"
    assert res["require_memo_prompt"] is True


def test_rule_unclassified_fallback(db_conn):
    cur = db_conn.cursor()
    res = evaluate_tax_rules(cur, "알수없는특수가맹점")
    assert res["account_code"] == "999"
    assert res["account_name"] == "미분류"
    assert res["vat_deduction_type"] == "PENDING"
    assert res["require_memo_prompt"] is True


def test_vat_calculation():
    supply, vat = calculate_supply_and_vat(45000, "DEDUCTIBLE")
    assert supply == 40909
    assert vat == 4091
    assert supply + vat == 45000

    supply_ex, vat_ex = calculate_supply_and_vat(45000, "EXEMPT")
    assert supply_ex == 45000
    assert vat_ex == 0


def test_interior_woodwork_and_design_custom_rules_and_memos(db_conn):
    """Verify 501 (원재료비), 511 (외주용역비), and tailored memos for 주식회사 디자인도하다."""
    cur = db_conn.cursor()

    # 1. Site materials (501 원재료비)
    for m_name in (
        "성심철물건재",
        "던에드워드페인트강서점",
        "주식회사 유진홈센터 ACE Hardware 용산점",
        "이케아코리아 유한회사",
    ):
        res = evaluate_tax_rules(cur, m_name)
        assert res["account_code"] == "501"
        assert res["account_name"] == "원재료비"
        assert res["vat_deduction_type"] == "DEDUCTIBLE"

    assert (
        generate_default_memo("501", "성심철물건재")
        == "내장목공사·인테리어 현장 자재 및 공구 구입"
    )
    assert (
        generate_default_memo("501", "이케아코리아 유한회사")
        == "인테리어·공간디자인 현장 가구·조명·스타일링 자재 구입"
    )
    assert (
        generate_default_memo("501", "크린대장(주)")
        == "인테리어 현장 준공·마감 청소자재 구입"
    )

    # 2. Site subcontracting & equipment / freight (511 외주용역비)
    for s_name in ("개미중기", "지에스건설（주）", "대신택배 강서가양 영업소", "럭키도배건축기술학원"):
        res = evaluate_tax_rules(cur, s_name)
        assert res["account_code"] == "511"
        assert res["account_name"] == "외주용역비"

    assert (
        generate_default_memo("511", "개미중기")
        == "인테리어·목공사 현장 중기·장비 임대료"
    )
    assert (
        generate_default_memo("511", "대신택배 강서가양 영업소")
        == "현장 시공자재 및 디자인 샘플 화물 운송료"
    )

    # 3. Department stores & marts (830 소모품비 with tailored interior/design explanation memo)
    assert (
        generate_default_memo("830", "주식회사 현대백화점 THE HYUNDAI SEOUL(더현대서울)")
        == "공간디자인·인테리어 현장 스타일링 소품 및 샘플 구입"
    )
    assert (
        generate_default_memo("830", "주식회사 아성다이소")
        == "현장 시공 부자재·마감용품 및 사무실 소모품 구입"
    )

