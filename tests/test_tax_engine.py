"""Unit tests for the automated tax classification rule engine."""

from sololedger.tax_engine import calculate_supply_and_vat, evaluate_tax_rules


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
