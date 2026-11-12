"""Automated tax classification rule engine and VAT calculation logic."""

import re
import sqlite3
from typing import Any, Dict, Optional, Tuple

from sololedger.merchant_service import ACCOUNT_CODE_NAMES


def calculate_supply_and_vat(
    total_amount: int, vat_deduction_type: str = "DEDUCTIBLE"
) -> Tuple[int, int]:
    """Calculate supply_value and vat_amount from total KRW amount.

    For EXEMPT transactions, supply_value equals total_amount and vat_amount is 0.
    Otherwise, supply_value is round(total_amount / 1.1) and vat_amount is the remainder.
    """
    if vat_deduction_type == "EXEMPT":
        return total_amount, 0
    supply_value = int(round(total_amount / 1.1))
    vat_amount = total_amount - supply_value
    return supply_value, vat_amount


def _apply_cached_merchant_overrides(
    cursor: sqlite3.Cursor,
    base_result: Dict[str, Any],
    merchant_name: str,
    biz_reg_no: Optional[str] = None,
) -> Dict[str, Any]:
    """Enrich rule evaluation result with cached merchant tax status and default account."""
    row = None
    if biz_reg_no:
        clean_no = biz_reg_no.replace("-", "").strip()
        cursor.execute("SELECT * FROM merchants WHERE biz_reg_no = ?", (clean_no,))
        row = cursor.fetchone()
    if row is None and merchant_name:
        cursor.execute(
            "SELECT * FROM merchants WHERE merchant_name = ? ORDER BY nts_verified_at DESC LIMIT 1",
            (merchant_name,),
        )
        row = cursor.fetchone()

    if row is None:
        return base_result

    result = dict(base_result)
    if row["default_account_code"] and result["account_code"] == "999":
        code = row["default_account_code"]
        result["account_code"] = code
        result["account_name"] = ACCOUNT_CODE_NAMES.get(code, "지정계정")

    if row["is_pg_provider"]:
        result["require_memo_prompt"] = True

    tax_type = row["tax_type_code"]
    biz_status = row["biz_status"]
    if tax_type == "EXEMPT":
        result["vat_deduction_type"] = "EXEMPT"
        result["non_deduct_reason"] = result["non_deduct_reason"] or "SIMPLIFIED_TAX"
    elif tax_type in ("SIMPLIFIED", "UNREGISTERED") or biz_status in ("SUSPENDED", "CLOSED"):
        result["vat_deduction_type"] = "NON_DEDUCTIBLE"
        result["non_deduct_reason"] = result["non_deduct_reason"] or "SIMPLIFIED_TAX"
    elif (
        result["account_code"] != "999"
        and tax_type == "GENERAL"
        and biz_status == "ACTIVE"
        and result["vat_deduction_type"] == "PENDING"
    ):
        result["vat_deduction_type"] = "DEDUCTIBLE"

    return result


_BUILTIN_INSURANCE_RE = re.compile(
    r"(손해보험|생명보험|화재보험|보증보험|해상보험)",
    re.IGNORECASE,
)
_BUILTIN_TRAVEL_RE = re.compile(
    r"(항공|고속버스|시외버스|공항버스|시티투어|카카오T대리|대리운전|운송사업조합|해운조합|여객선|"
    r"한국철도공사|주식회사\s*에스알|개인택시|"
    r"와이페이모어|놀유니버스|야놀자|인터파크|하나투어|모두투어|아고다|트립닷컴|마이리얼트립)",
    re.IGNORECASE,
)
_BUILTIN_UNCERTAIN_ITEM_RE = re.compile(
    r"(엔에이치엔케이씨피|NHNKCP|한국정보통신|스마트로|케이에스넷|KSNET|나이스페이|\(주\)\s*다날|"
    r"쿠팡|네이버페이|네이버파이낸셜|알리익스프레스|이니시스|토스페이먼츠|카카오페이|카카오모빌리티|"
    r"PAYCO|페이코|스마일페이|십일번가|11번가|지마켓|옥션|무신사|"
    r"다이소|아성다이소|이마트(?!24)|홈플러스|롯데마트|롯데쇼핑|신세계|아울렛|애플아울렛|"
    r"코스트코|트레이더스|하나로마트|케이마트|휴마트|할인마트|홈마트|한화커넥트|현대백화점|"
    r"더현대서울|삼성전자판매|삼성닷컴|엠스토아|제로백|씨제이올리브영|올리브영|"
    r"이케아|IKEA|휴앤디|스마트홈|비에스케이코퍼레이션|빅바이트컴퍼니|딥스테이션|코레일유통|"
    r"착한정육점|정육점|청과\s*오보소|동화쌀|쌀직매장|슬기로운\s*농산생활|"
    r"주식회사\s*우리카드|서울특별시|예봄교회|주식회사\s*송핫)",
    re.IGNORECASE,
)
_BUILTIN_VEHICLE_EXTRA_RE = re.compile(
    r"(샤인에너비스|영원석유|삼성에너지|제트라인모터스|아마노코리아)",
    re.IGNORECASE,
)
_BUILTIN_ENTERTAINMENT_RE = re.compile(
    r"(몽키뮤직타운|유흥주점|단란주점)",
    re.IGNORECASE,
)
_BUILTIN_SITE_MATERIAL_RE = re.compile(
    r"(던에드워드페인트|페인트|성심철물건재|철물|건재|유진홈센터|ACE\s*Hardware|하드웨어|"
    r"크린대장|목재|합판|석고보드|타일|벽지|장판|인테리어필름|시트지|타카)",
    re.IGNORECASE,
)
_BUILTIN_SITE_SUBCONTRACT_RE = re.compile(
    r"(개미중기|중기|크레인|사다리차|스카이차|지에스건설|럭키도배건축기술학원|도배.*학원|"
    r"건축기술|대신택배|경동택배|건영택배|화물운송|용달)",
    re.IGNORECASE,
)
_BUILTIN_SERVICE_FEE_RE = re.compile(
    r"(알림서비스|연회비|문자서비스|인증수수료|티머니|이비카드|후불교통|스카이라이프|케이티에이치|"
    r"HCN|우정사업본부|우체국|네이버플러스|와우\s*멤버십|와우멤버십|일상케어|안심케어|나이스_마이홈)",
    re.IGNORECASE,
)
_BUILTIN_RESTAURANT_RE = re.compile(
    r"(대게|횟집|해물|초밥|스시|식당|맛집|이마트24|GS25|지에스\(GS\)25|지에스25|롯데리아|카페인클럽|"
    r"쿠팡이츠|지코바|떡볶이|엘리시안|KFC|꼬막|고칸|감자탕|김가네|바나타이거|소하계|아이스크림|은행골|호코|"
    r"골목집|밀촌손칼국수|칼국수|보쌈|부대찌개|김치찌개|왕갈비탕|곰탕|순대|깍두기|냉면|쌀국수|만두|쭈꾸미|"
    r"수육|낙지|닭갈비|대패|고등어|아웃백|파리바게트|파리바게뜨|르봉뺑|롯데지알에스|비케이알|"
    r"봉구비어|토모리|모리본|백일도|도톰|면곡당|청담이상|히자시|희돈애락|쿠시카츠|레트리|주휴소|"
    r"모던중식|에프지케이|백소정|빠빼루|전주옥|피맥하우스|프렌즈\s*앤\s*야드|페페|토곡팡|삼다진족|"
    r"아모르데이|봉샌드|온정|개미집|스트릿\s*스퀘어)",
    re.IGNORECASE,
)
_BUILTIN_TOLL_RE = re.compile(
    r"(터널/도로|유료도로|자동차세)",
    re.IGNORECASE,
)
_BUILTIN_BOOKSTORE_RE = re.compile(
    r"(교보문고|영풍문고|알라딘|예스24)",
    re.IGNORECASE,
)
_BUILTIN_STATIONERY_RE = re.compile(
    r"(오피스디포|알파문구|드림디포)",
    re.IGNORECASE,
)


def is_uncertain_item_merchant(merchant_name: Optional[str]) -> bool:
    """Return True when a merchant sells mixed/unknown items or is a PG gateway without item info."""
    m_txt = (merchant_name or "").strip()
    if not m_txt:
        return True
    if "쿠팡이츠" in m_txt or "와우 멤버십" in m_txt or "와우멤버십" in m_txt:
        return False
    if "카카오T대리" in m_txt or "알림서비스" in m_txt or "연회비" in m_txt:
        return False
    return bool(_BUILTIN_UNCERTAIN_ITEM_RE.search(m_txt))


def _classify_by_hometax_industry(
    biz_category: Optional[str], biz_item: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Fallback classification using Hometax 업태 (biz_category) and 업종 (biz_item) for clear businesses."""
    cat = (biz_category or "").strip()
    item = (biz_item or "").strip()
    combined = f"{cat} {item}".strip()
    if not combined:
        return None

    if re.search(r"(유흥주점|단란주점)", combined):
        return {
            "account_code": "813",
            "account_name": "기업업무추진비",
            "vat_deduction_type": "NON_DEDUCTIBLE",
            "non_deduct_reason": "ENTERTAINMENT",
            "is_vehicle_expense": False,
            "require_memo_prompt": True,
        }
    if re.search(r"(철도운송|여객운송|일반택시|고급택시|항공운송)", combined):
        return {
            "account_code": "812",
            "account_name": "여비교통비",
            "vat_deduction_type": "NON_DEDUCTIBLE",
            "non_deduct_reason": "PASSENGER_TRANSPORT",
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
    if re.search(r"(보증보험|손해보험|생명보험|보험업)", combined):
        return {
            "account_code": "821",
            "account_name": "보험료",
            "vat_deduction_type": "EXEMPT",
            "non_deduct_reason": "SIMPLIFIED_TAX",
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
    if re.search(r"(주유소|석유류|기체연료|전문정비업|자동차정비|주차장)", combined):
        return {
            "account_code": "822",
            "account_name": "차량유지비",
            "vat_deduction_type": "NON_DEDUCTIBLE",
            "non_deduct_reason": "PASSENGER_CAR",
            "is_vehicle_expense": True,
            "require_memo_prompt": False,
        }
    if re.search(
        r"(철물|기계공구|페인트|건재|목재|합판|벽지|장판|타일|건축자재|실내장식자재)",
        combined,
    ):
        return {
            "account_code": "501",
            "account_name": "원재료비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
    if re.search(
        r"(중기|건설|건축공사|도배|실내장식|내장목공|화물운송|용달)",
        combined,
    ):
        return {
            "account_code": "511",
            "account_name": "외주용역비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
    if re.search(
        r"(음식|한식|일식|중식|양식|경양식|분식|치킨|카페|커피|제과|주점|편의점|식품접객|휴게음식|고속도로휴게소)",
        combined,
    ):
        return {
            "account_code": "811",
            "account_name": "복리후생비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
    if re.search(r"(방문소포|소프트웨어|데이터베이스)", combined):
        return {
            "account_code": "831",
            "account_name": "지급수수료",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
    return None


def evaluate_tax_rules(
    cursor: sqlite3.Cursor,
    merchant_name: str,
    biz_reg_no: Optional[str] = None,
    biz_category: Optional[str] = None,
    biz_item: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate merchant_name against active tax_classification_rules and cached merchant data."""
    m_txt = merchant_name or ""

    # 1. User-learned exact rules (priority < 10) always take top precedence
    cursor.execute(
        """
        SELECT match_pattern, target_account_code, target_account_name,
               force_non_deductible, non_deduct_reason, set_vehicle_flag, require_memo_prompt
        FROM tax_classification_rules
        WHERE is_active = 1 AND match_field = 'MERCHANT_NAME' AND priority < 10
        ORDER BY priority ASC
        """
    )
    for (
        pattern,
        acc_code,
        acc_name,
        force_non_ded,
        reason,
        vehicle_flag,
        memo_prompt,
    ) in cursor.fetchall():
        if re.search(pattern, m_txt, re.IGNORECASE):
            learned_vat = (
                "PENDING"
                if acc_code == "999"
                else ("NON_DEDUCTIBLE" if force_non_ded else "DEDUCTIBLE")
            )
            learned_res = {
                "account_code": acc_code,
                "account_name": acc_name,
                "vat_deduction_type": learned_vat,
                "non_deduct_reason": reason,
                "is_vehicle_expense": bool(vehicle_flag),
                "require_memo_prompt": bool(memo_prompt),
            }
            return _apply_cached_merchant_overrides(cursor, learned_res, merchant_name, biz_reg_no)

    # 2. Specific single-purpose sub-services under platform names (before uncertain-item guard)
    if "쿠팡이츠" in m_txt:
        base_eats = {
            "account_code": "811",
            "account_name": "복리후생비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_eats, merchant_name, biz_reg_no)
    if "와우 멤버십" in m_txt or "와우멤버십" in m_txt:
        base_wow = {
            "account_code": "831",
            "account_name": "지급수수료",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_wow, merchant_name, biz_reg_no)
    if (
        "카카오T대리" in m_txt
        or "대리운전" in m_txt
        or "와이페이모어" in m_txt
        or "취급수수료" in m_txt
    ):
        base_daeri = {
            "account_code": "812",
            "account_name": "여비교통비",
            "vat_deduction_type": "NON_DEDUCTIBLE",
            "non_deduct_reason": "PASSENGER_TRANSPORT",
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_daeri, merchant_name, biz_reg_no)

    # 3. Uncertain item list merchants (general marts, department stores, malls, PG gateways) -> 999 미분류
    if is_uncertain_item_merchant(m_txt):
        base_uncertain = {
            "account_code": "999",
            "account_name": "미분류",
            "vat_deduction_type": "PENDING",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": True,
        }
        return _apply_cached_merchant_overrides(
            cursor, base_uncertain, merchant_name, biz_reg_no
        )

    # 4. Specific single-purpose business categories
    if _BUILTIN_INSURANCE_RE.search(m_txt):
        base_ins = {
            "account_code": "821",
            "account_name": "보험료",
            "vat_deduction_type": "EXEMPT",
            "non_deduct_reason": "SIMPLIFIED_TAX",
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_ins, merchant_name, biz_reg_no)
    if _BUILTIN_ENTERTAINMENT_RE.search(m_txt):
        base_ent = {
            "account_code": "813",
            "account_name": "기업업무추진비",
            "vat_deduction_type": "NON_DEDUCTIBLE",
            "non_deduct_reason": "ENTERTAINMENT",
            "is_vehicle_expense": False,
            "require_memo_prompt": True,
        }
        return _apply_cached_merchant_overrides(cursor, base_ent, merchant_name, biz_reg_no)
    if _BUILTIN_TRAVEL_RE.search(m_txt):
        base_trv = {
            "account_code": "812",
            "account_name": "여비교통비",
            "vat_deduction_type": "NON_DEDUCTIBLE",
            "non_deduct_reason": "PASSENGER_TRANSPORT",
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_trv, merchant_name, biz_reg_no)
    if _BUILTIN_VEHICLE_EXTRA_RE.search(m_txt) or _BUILTIN_TOLL_RE.search(m_txt):
        base_veh = {
            "account_code": "822",
            "account_name": "차량유지비",
            "vat_deduction_type": "NON_DEDUCTIBLE",
            "non_deduct_reason": "PASSENGER_CAR",
            "is_vehicle_expense": True,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_veh, merchant_name, biz_reg_no)
    if _BUILTIN_BOOKSTORE_RE.search(m_txt):
        base_book = {
            "account_code": "826",
            "account_name": "도서인쇄비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_book, merchant_name, biz_reg_no)
    if _BUILTIN_STATIONERY_RE.search(m_txt):
        base_stat = {
            "account_code": "830",
            "account_name": "소모품비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_stat, merchant_name, biz_reg_no)
    if _BUILTIN_SITE_MATERIAL_RE.search(m_txt):
        base_mat = {
            "account_code": "501",
            "account_name": "원재료비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_mat, merchant_name, biz_reg_no)
    if _BUILTIN_SITE_SUBCONTRACT_RE.search(m_txt):
        base_sub = {
            "account_code": "511",
            "account_name": "외주용역비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_sub, merchant_name, biz_reg_no)
    if _BUILTIN_SERVICE_FEE_RE.search(m_txt):
        base_fee = {
            "account_code": "831",
            "account_name": "지급수수료",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
        return _apply_cached_merchant_overrides(cursor, base_fee, merchant_name, biz_reg_no)

    # 5. Active seed / custom rules in tax_classification_rules (priority >= 10)
    cursor.execute(
        """
        SELECT match_pattern, target_account_code, target_account_name,
               force_non_deductible, non_deduct_reason, set_vehicle_flag, require_memo_prompt
        FROM tax_classification_rules
        WHERE is_active = 1 AND match_field = 'MERCHANT_NAME' AND priority >= 10
        ORDER BY priority ASC
        """
    )
    matched_result = None
    for (
        pattern,
        acc_code,
        acc_name,
        force_non_ded,
        reason,
        vehicle_flag,
        memo_prompt,
    ) in cursor.fetchall():
        if re.search(pattern, m_txt, re.IGNORECASE):
            rule_vat = (
                "PENDING"
                if acc_code == "999"
                else ("NON_DEDUCTIBLE" if force_non_ded else "DEDUCTIBLE")
            )
            matched_result = {
                "account_code": acc_code,
                "account_name": acc_name,
                "vat_deduction_type": rule_vat,
                "non_deduct_reason": reason,
                "is_vehicle_expense": bool(vehicle_flag),
                "require_memo_prompt": bool(memo_prompt),
            }
            break

    if matched_result is None and _BUILTIN_RESTAURANT_RE.search(m_txt):
        matched_result = {
            "account_code": "811",
            "account_name": "복리후생비",
            "vat_deduction_type": "DEDUCTIBLE",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": False,
        }
    elif matched_result is None:
        matched_result = _classify_by_hometax_industry(biz_category, biz_item)

    if matched_result is None:
        matched_result = {
            "account_code": "999",
            "account_name": "미분류",
            "vat_deduction_type": "PENDING",
            "non_deduct_reason": None,
            "is_vehicle_expense": False,
            "require_memo_prompt": True,
        }

    return _apply_cached_merchant_overrides(cursor, matched_result, merchant_name, biz_reg_no)


def generate_default_memo(
    account_code: str,
    merchant_name: str,
    require_memo_prompt: bool = False,
) -> Optional[str]:
    """Generate a standard accounting memo ONLY for clear single-purpose business categories.

    Returns None for unclassified (999), entertainment (813), or uncertain-item merchants
    so unknown purchase items are never fabricated.
    """
    if account_code in ("999", "813"):
        return None
    if account_code == "830" and require_memo_prompt:
        return None

    m_name = (merchant_name or "").strip()
    if is_uncertain_item_merchant(m_name):
        return None

    if account_code == "501":
        if any(k in m_name for k in ("크린대장", "청소")):
            return "인테리어 현장 준공·마감 청소자재 구입"
        return "내장목공사·인테리어 현장 자재 및 공구 구입"
    if account_code == "511":
        if any(k in m_name for k in ("중기", "크레인", "사다리", "스카이")):
            return "인테리어·목공사 현장 중기·장비 임대료"
        if any(k in m_name for k in ("택배", "화물", "용달")):
            return "현장 시공자재 및 디자인 샘플 화물 운송료"
        if any(k in m_name for k in ("학원", "기술")):
            return "도배·내장목공사 시공 기술연수 및 외주용역"
        return "인테리어·내장목공사 현장 시공 외주용역비"
    if account_code == "822":
        if "하이패스" in m_name or "터널/도로" in m_name or "유료도로" in m_name:
            return "하이패스 통행료"
        if "자동차세" in m_name:
            return "업무용승용차 자동차세 납부"
        if any(k in m_name for k in ("주차", "파크", "아마노")):
            return "차량 주차료"
        if any(k in m_name for k in ("정비", "스피드메이트", "오토큐", "모터스", "타이어", "세차")):
            return "업무용승용차 정비·유지"
        return "업무용승용차 주유비"
    if account_code == "811":
        return "현장 시공·디자인 업무 식대 및 복리후생"
    if account_code == "812":
        if "항공" in m_name:
            return "출장·여비 항공권 발권"
        if "해운" in m_name:
            return "출장·여비 선박권 발권"
        if any(
            k in m_name
            for k in ("놀유니버스", "야놀자", "인터파크", "투어", "아고다", "트립", "와이페이모어", "취급수수료")
        ):
            return "출장·여비 여행플랫폼 수수료"
        return "업무용 대중교통·여비교통비"
    if account_code == "821":
        return "업무용 보험료(여행자·배상책임 등)"
    if account_code == "826":
        return "업무용 도서 구입 및 인쇄"
    if account_code == "830":
        return "사무용 문구 및 소모품 구입"
    if account_code == "831":
        if "티머니" in m_name or "이비카드" in m_name:
            return "대중교통(버스·지하철) 교통카드 이용"
        if "알림서비스" in m_name:
            return "법인카드 알림서비스 수수료"
        if "우체국" in m_name or "우정사업본부" in m_name:
            return "업무용 우편·등기 발송 수수료"
        return "업무용 IT구독·통신 수수료"
    if account_code == "212":
        return "사무용 비품 취득"
    return None


def learn_merchant_rule(
    cursor: sqlite3.Cursor,
    merchant_name: str,
    account_code: str,
    account_name: str,
    vat_deduction_type: str,
    non_deduct_reason: Optional[str] = None,
) -> None:
    """Persist a Firefly III-style learned rule (priority 5) for an exact merchant name."""
    clean_name = (merchant_name or "").strip()
    if not clean_name or account_code == "999":
        return
    pattern = f"^{re.escape(clean_name)}$"
    force_non_ded = 1 if vat_deduction_type == "NON_DEDUCTIBLE" else 0
    is_vehicle = 1 if account_code == "822" else 0
    require_memo = 1 if account_code == "813" else 0
    cursor.execute(
        """
        DELETE FROM tax_classification_rules
        WHERE priority = 5 AND match_field = 'MERCHANT_NAME' AND match_pattern = ?
        """,
        (pattern,),
    )
    cursor.execute(
        """
        INSERT INTO tax_classification_rules (
            priority, match_field, match_pattern, target_account_code,
            target_account_name, force_non_deductible, non_deduct_reason,
            set_vehicle_flag, require_memo_prompt, is_active
        ) VALUES (5, 'MERCHANT_NAME', ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            pattern,
            account_code,
            account_name,
            force_non_ded,
            non_deduct_reason,
            is_vehicle,
            require_memo,
        ),
    )

