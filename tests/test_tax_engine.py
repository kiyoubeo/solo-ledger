"""Unit tests for the automated tax classification rule engine."""

from sololedger.dashboard import reapply_tax_rules_to_ledger
from sololedger.scraper_service import generate_electronic_statement_for_tx
from sololedger.tax_engine import (
    calculate_supply_and_vat,
    evaluate_tax_rules,
    generate_default_memo,
    is_uncertain_item_merchant,
)


def test_rule_pg_ecommerce_unclassified_until_item_confirmed(db_conn):
    cur = db_conn.cursor()
    res = evaluate_tax_rules(cur, "(주)쿠팡")
    assert res["account_code"] == "999"
    assert res["account_name"] == "미분류"
    assert res["vat_deduction_type"] == "PENDING"
    assert res["is_vehicle_expense"] is False
    assert res["require_memo_prompt"] is True
    assert generate_default_memo("999", "(주)쿠팡", True) is None


def test_rule_vehicle_expense_non_deductible(db_conn):
    cur = db_conn.cursor()
    for m_name in (
        "스피드메이트 대전용산점",
        "(주)지오에너지 명장주유소",
        "에이치디현대오일뱅크(주)직영 양화교셀프주유소",
        "아마노코리아(주)",
    ):
        res = evaluate_tax_rules(cur, m_name)
        assert res["account_code"] == "822"
        assert res["account_name"] == "차량유지비"
        assert res["vat_deduction_type"] == "NON_DEDUCTIBLE"
        assert res["non_deduct_reason"] == "PASSENGER_CAR"
        assert res["is_vehicle_expense"] is True
        assert res["require_memo_prompt"] is False

    assert generate_default_memo("822", "(주)지오에너지 명장주유소") == "업무용승용차 주유비"
    assert generate_default_memo("822", "아마노코리아(주)") == "차량 주차료"


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

    # Convenience store '이마트24' stays 811, whereas hypermarket '(주) 이마트' is 999 미분류
    res_cvs = evaluate_tax_rules(cur, "이마트24 역삼점")
    assert res_cvs["account_code"] == "811"
    res_mart = evaluate_tax_rules(cur, "(주) 이마트")
    assert res_mart["account_code"] == "999"


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


def test_specific_businesses_vs_uncertain_item_merchants(db_conn):
    """Verify single-purpose businesses classify while uncertain-item marts/malls/PG stay 999."""
    cur = db_conn.cursor()

    # 1. Specific site material & hardware stores -> 501 원재료비
    for m_name in (
        "성심철물건재",
        "던에드워드페인트강서점",
        "주식회사 유진홈센터 ACE Hardware 용산점",
        "크린대장(주)",
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
        generate_default_memo("501", "크린대장(주)")
        == "인테리어 현장 준공·마감 청소자재 구입"
    )

    # 2. Specific subcontracting, heavy equipment & freight -> 511 외주용역비
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

    # 3. Uncertain-item merchants (marts, dept stores, multi-purpose stores, PG, ambiguous) -> 999 미분류
    uncertain_merchants = [
        "（주）이마트",
        "(주)농협유통 하나로마트 부전점",
        "대원할인마트 주식회사",
        "주식회사 아성다이소",
        "(주)신세계",
        "롯데쇼핑（주）부산점",
        "주식회사 현대백화점 THE HYUNDAI SEOUL(더현대서울)",
        "이케아코리아 유한회사",
        "(주)휴앤디 낙민점",
        "스마트홈",
        "삼성전자판매 주식회사",
        "씨제이올리브영 주식회사",
        "네이버파이낸셜 주식회사",
        "엔에이치엔케이씨피 주식회사",
        "한국정보통신（주）",
        "주식회사스마트로",
        "(주)케이지이니시스",
        "토스페이먼츠 주식회사",
        "(주) 다날",
        "주식회사 우리카드",
        "서울특별시",
        "대한예수교장로회 예봄교회",
        "우리동네 착한정육점",
        "청과 오보소",
    ]
    for u_name in uncertain_merchants:
        assert is_uncertain_item_merchant(u_name) is True
        res = evaluate_tax_rules(cur, u_name, biz_category="소매", biz_item="대형할인점")
        assert res["account_code"] == "999", f"{u_name} should be 999 미분류"
        assert res["account_name"] == "미분류"
        assert res["vat_deduction_type"] == "PENDING"
        assert res["require_memo_prompt"] is True
        assert generate_default_memo("830", u_name) is None


def test_reapply_tax_rules_reverts_unconfirmed_auto_memos_and_keeps_statements(
    db_conn, tmp_path
):
    """Verify reapply_tax_rules_to_ledger reverts over-classified marts while keeping STATEMENT rows."""
    cur = db_conn.cursor()

    cur.execute(
        """
        INSERT INTO merchants (biz_reg_no, merchant_name, tax_type_code, biz_status, is_pg_provider)
        VALUES
            ('2068650913', '(주) 이마트', 'GENERAL', 'ACTIVE', 0),
            ('4118601799', '토스페이먼츠 주식회사', 'GENERAL', 'ACTIVE', 1),
            ('1348739278', '주식회사 카카오모빌리티', 'GENERAL', 'ACTIVE', 1)
        """
    )

    # Row 1: Over-classified Emart row with fabricated auto-memo and no confirmed statement
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, counterparty_info, reconcile_status
        ) VALUES (
            '7014', '10000001', '2026-03-29 12:00:00', '(주) 이마트', '2068650913',
            76280, 69345, 6935, '830', '소모품비',
            'DEDUCTIBLE', '현장 시공 부자재·마감용품 및 사무실 소모품 구입', 'HOMETAX_VERIFIED'
        )
        """
    )
    emart_tx_id = int(cur.lastrowid)

    # Row 2: Confirmed e-commerce lighting purchase with STATEMENT evidence
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, counterparty_info, reconcile_status
        ) VALUES (
            '7014', '10000002', '2026-03-20 15:00:00', '토스페이먼츠 주식회사', '4118601799',
            159000, 144545, 14455, '999', '미분류',
            'PENDING', NULL, 'HOMETAX_VERIFIED'
        )
        """
    )
    stmt_tx_id = int(cur.lastrowid)
    generate_electronic_statement_for_tx(
        cur,
        tx_id=stmt_tx_id,
        item_name="머쉬룸 장스탠드 조명",
        archive_dir=tmp_path,
    )

    # Row 3: Confirmed Kakao Mobility parking statement -> 822 차량유지비 (NON_DEDUCTIBLE)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, counterparty_info, reconcile_status
        ) VALUES (
            '7014', '10000003', '2026-03-21 16:00:00', '주식회사 카카오모빌리티', '1348739278',
            9900, 9000, 900, '999', '미분류',
            'PENDING', NULL, 'SMS_ONLY'
        )
        """
    )
    parking_tx_id = int(cur.lastrowid)
    generate_electronic_statement_for_tx(
        cur,
        tx_id=parking_tx_id,
        item_name="T 주차 운임",
        archive_dir=tmp_path,
    )

    reapply_res = reapply_tax_rules_to_ledger(cur)
    assert reapply_res["reclassified_count"] >= 1

    cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (emart_tx_id,))
    emart_row = dict(cur.fetchone())
    assert emart_row["account_code"] == "999"
    assert emart_row["account_name"] == "미분류"
    assert emart_row["vat_deduction_type"] == "PENDING"
    assert emart_row["counterparty_info"] is None

    cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (stmt_tx_id,))
    stmt_row = dict(cur.fetchone())
    assert stmt_row["account_code"] == "501"
    assert stmt_row["account_name"] == "원재료비"
    assert stmt_row["counterparty_info"] == "머쉬룸 장스탠드 조명"

    cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (parking_tx_id,))
    parking_row = dict(cur.fetchone())
    assert parking_row["account_code"] == "822"
    assert parking_row["account_name"] == "차량유지비"
    assert parking_row["vat_deduction_type"] == "NON_DEDUCTIBLE"
    assert bool(parking_row["is_vehicle_expense"]) is True


def test_branch_name_false_positives_pg_propagation_guard_and_exempt_sync(db_conn):
    """Verify park-city/stationery fixes, PG propagation guard, 900 exclusion, and EXEMPT sync."""
    from sololedger.dashboard import update_transaction_classification

    cur = db_conn.cursor()

    # 1. Branch names like '연산파크시티점' on cafes -> 811 복리후생비 (NOT 822 차량유지비)
    for cafe_name in ("메가엠지씨커피 연산파크시티점", "컴포즈커피 연산파크시티점"):
        res = evaluate_tax_rules(cur, cafe_name)
        assert res["account_code"] == "811"
        assert res["is_vehicle_expense"] is False

    # 2. '문구야놀자' -> 830 소모품비 (NOT 812 여비교통비)
    res_stat = evaluate_tax_rules(cur, "문구야놀자(연산파크시티점)")
    assert res_stat["account_code"] == "830"

    # 3. Court registry, public parking facility, and plumbing materials
    assert evaluate_tax_rules(cur, "법원행정처")["account_code"] == "831"
    assert evaluate_tax_rules(cur, "서울특별시영등포구시설관리공단")["account_code"] == "822"
    assert evaluate_tax_rules(cur, "주식회사 신호종합배관")["account_code"] == "501"

    # 4. PG / Mart propagation guard: saving 1 Coupang row must NOT overwrite sibling Coupang row
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES
            ('2949', '2026-10-10 12:00:00', '쿠팡페이 주식회사', 33000, 30000, 3000,
             '999', '미분류', 'PENDING', 'HOMETAX_VERIFIED'),
            ('2949', '2026-10-12 12:00:00', '쿠팡페이 주식회사', 55000, 50000, 5000,
             '999', '미분류', 'PENDING', 'HOMETAX_VERIFIED')
        """
    )
    cp_rows = cur.execute(
        "SELECT tx_id FROM card_transactions WHERE raw_merchant_name = '쿠팡페이 주식회사' ORDER BY tx_id"
    ).fetchall()
    cp1, cp2 = int(cp_rows[0][0]), int(cp_rows[1][0])

    upd_cp = update_transaction_classification(
        cur, tx_id=cp1, account_code="501", counterparty_info="현장 타카핀", apply_to_same_merchant=True
    )
    assert upd_cp["propagated_merchant_count"] == 0
    cp2_row = dict(cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (cp2,)).fetchone())
    assert cp2_row["account_code"] == "999"
    assert cp2_row["counterparty_info"] is None

    # 5. EXEMPT merchant consistency sync + 900 경비제외 protection across reapply
    cur.execute(
        """
        INSERT INTO merchants (biz_reg_no, merchant_name, tax_type_code, biz_status)
        VALUES ('2529202318', '문경청과', 'EXEMPT', 'ACTIVE')
        """
    )
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES
            ('2949', '2026-10-19 12:00:00', '문경청과', '2529202318',
             10000, 9091, 909, '999', '미분류', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'),
            ('0445', '2026-05-28 12:00:00', '미&끼 헤어퍼밍', NULL,
             15000, 15000, 0, '999', '미분류', 'EXEMPT', 'HOMETAX_VERIFIED')
        """
    )
    mk_id = int(
        cur.execute("SELECT tx_id FROM card_transactions WHERE raw_merchant_name = '문경청과'").fetchone()[0]
    )
    hair_id = int(
        cur.execute("SELECT tx_id FROM card_transactions WHERE raw_merchant_name = '미&끼 헤어퍼밍'").fetchone()[0]
    )

    excl_res = update_transaction_classification(cur, tx_id=hair_id, account_code="900")
    assert excl_res["account_code"] == "900"
    assert excl_res["vat_deduction_type"] == "NON_DEDUCTIBLE"
    assert excl_res["non_deduct_reason"] == "PERSONAL_USE"
    assert excl_res["supply_value"] == 15000
    assert excl_res["vat_amount"] == 0
    assert "사적사용" in (excl_res["counterparty_info"] or "")

    reapply_out = reapply_tax_rules_to_ledger(cur)
    assert reapply_out["exempt_synced_count"] >= 1

    mk_after = dict(cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (mk_id,)).fetchone())
    assert mk_after["vat_deduction_type"] == "EXEMPT"
    assert mk_after["supply_value"] == 10000
    assert mk_after["vat_amount"] == 0

    hair_after = dict(cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (hair_id,)).fetchone())
    assert hair_after["account_code"] == "900"
    assert hair_after["non_deduct_reason"] == "PERSONAL_USE"



