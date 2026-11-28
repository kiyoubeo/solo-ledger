"""Phase 8 Pre-Export Parallel Tax Audit Fan-out / Join Graph."""

import asyncio
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from sololedger.models import TaxAuditRequest
from sololedger.nts_client import validate_biz_reg_no_checksum
from sololedger.sms_parser import (
    is_allowed_corporate_card,
    is_blocked_personal_card_code,
    load_corp_card_policy,
)
from sololedger.tax_engine import is_uncertain_item_merchant
from sololedger.vehicle_tracker import get_vehicle_expense_summary


class AuditIssue(BaseModel):
    """Single tax compliance error or warning flagged by an audit branch."""

    tx_id: Optional[int] = None
    category: str  # VAT_STATUS, VEHICLE_POLICY, CARD_POLICY, EVIDENCE
    severity: str  # ERROR, WARNING
    code: str
    message: str


class TaxAuditState(BaseModel):
    """Aggregated state returned by the pre-export parallel tax audit graph."""

    year_month: str
    scanned_count: int = 0
    ready_for_export: bool = True
    error_count: int = 0
    warning_count: int = 0
    issues: List[AuditIssue] = Field(default_factory=list)
    flagged_tx_ids: List[int] = Field(default_factory=list)
    vehicle_summary: Dict[str, Any] = Field(default_factory=dict)
    visited_nodes: List[str] = Field(default_factory=list)


def audit_fan_out(
    cursor: sqlite3.Cursor,
    year_month: str,
    include_exported: bool = False,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load the target month's ledger snapshot, vehicle limit summary, and card policy."""
    if len(year_month) != 6 or not year_month.isdigit():
        raise ValueError(f"Invalid year_month format (expected YYYYMM): {year_month}")
    month_num = int(year_month[4:6])
    if month_num < 1 or month_num > 12:
        raise ValueError(f"Invalid month in year_month (expected 01..12): {year_month}")

    ym_prefix = f"{year_month[:4]}-{year_month[4:6]}-%"
    query = """
        SELECT t.*,
               m.tax_type_code AS merchant_tax_type,
               m.biz_status AS merchant_biz_status,
               (
                   SELECT r.archived_file_name
                   FROM receipt_ocr_metadata r
                   WHERE r.tx_id = t.tx_id
                   ORDER BY r.receipt_id DESC
                   LIMIT 1
               ) AS archived_file_name
        FROM card_transactions t
        LEFT JOIN merchants m ON t.biz_reg_no = m.biz_reg_no
        WHERE t.transacted_at LIKE ?
          AND t.reconcile_status != 'CANCELED'
    """
    if not include_exported:
        query += " AND (t.exported_year_month IS NULL OR t.exported_year_month = '')"
    query += " ORDER BY t.transacted_at ASC, t.tx_id ASC"

    cursor.execute(query, (ym_prefix,))
    rows = [dict(r) for r in cursor.fetchall()]

    vehicle_summary = get_vehicle_expense_summary(cursor, int(year_month[:4]))
    corp_policy = load_corp_card_policy(archive_dir)

    return {
        "year_month": year_month,
        "rows": rows,
        "vehicle_summary": vehicle_summary,
        "corp_policy": corp_policy,
    }


async def audit_vat_status(snapshot: Dict[str, Any]) -> List[AuditIssue]:
    """Branch A: Check VAT deduction type against NTS merchant status, arithmetic, and checksum."""
    issues: List[AuditIssue] = []
    for row in snapshot["rows"]:
        tx_id = int(row["tx_id"])
        merchant_name = row.get("raw_merchant_name") or ""
        vat_type = str(row.get("vat_deduction_type") or "PENDING")
        m_tax = str(row.get("merchant_tax_type") or "")
        m_status = str(row.get("merchant_biz_status") or "")

        if vat_type == "DEDUCTIBLE" and (
            m_tax in ("SIMPLIFIED", "EXEMPT", "UNREGISTERED")
            or m_status in ("SUSPENDED", "CLOSED")
        ):
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="VAT_STATUS",
                    severity="ERROR",
                    code="DEDUCTIBLE_ON_NON_GENERAL_MERCHANT",
                    message=(
                        f"전표 #{tx_id}({merchant_name}): 간이·면세·휴폐업 가맹점"
                        f"({m_tax}/{m_status})에 매입세액 공제(DEDUCTIBLE) 적용 불가"
                    ),
                )
            )

        if vat_type in ("PENDING", "PENDING_CHECK"):
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="VAT_STATUS",
                    severity="ERROR",
                    code="UNRESOLVED_VAT_DEDUCTION_STATUS",
                    message=(
                        f"전표 #{tx_id}({merchant_name}): 부가세 공제 구분이 미확정({vat_type}) 상태"
                    ),
                )
            )

        supply_val = int(row.get("supply_value") or 0)
        vat_val = int(row.get("vat_amount") or 0)
        total_val = int(row.get("total_amount") or 0)
        if (supply_val + vat_val) != total_val:
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="VAT_STATUS",
                    severity="ERROR",
                    code="ARITHMETIC_VAT_MISMATCH",
                    message=(
                        f"전표 #{tx_id}({merchant_name}): 공급가액({supply_val})+부가세({vat_val}) != "
                        f"합계({total_val})"
                    ),
                )
            )

        biz_no = row.get("biz_reg_no")
        if biz_no and not validate_biz_reg_no_checksum(str(biz_no)):
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="VAT_STATUS",
                    severity="ERROR",
                    code="INVALID_BIZ_REG_NO_CHECKSUM",
                    message=(
                        f"전표 #{tx_id}({merchant_name}): 사업자등록번호({biz_no}) 체크섬 오류"
                    ),
                )
            )

    return issues


async def audit_vehicle_and_card_policy(
    snapshot: Dict[str, Any],
    archive_dir: Optional[Path] = None,
) -> List[AuditIssue]:
    """Branch B: Check 15M KRW vehicle expense limit, non-deductible VAT, and corp card policy."""
    issues: List[AuditIssue] = []
    veh = snapshot["vehicle_summary"]
    warning_level = veh.get("warning_level")
    if warning_level == "LIMIT_EXCEEDED":
        issues.append(
            AuditIssue(
                tx_id=None,
                category="VEHICLE_POLICY",
                severity="WARNING",
                code="VEHICLE_LIMIT_EXCEEDED",
                message=str(veh.get("advice") or "업무용승용차 연간 1,500만 원 한도 초과"),
            )
        )
    elif warning_level == "WARNING_80PCT":
        issues.append(
            AuditIssue(
                tx_id=None,
                category="VEHICLE_POLICY",
                severity="WARNING",
                code="VEHICLE_LIMIT_WARNING_80PCT",
                message=str(veh.get("advice") or "업무용승용차 연간 한도 80% 이상 도달"),
            )
        )

    corp_only = bool(snapshot["corp_policy"].get("corp_only_mode", False))
    for row in snapshot["rows"]:
        tx_id = int(row["tx_id"])
        merchant_name = str(row.get("raw_merchant_name") or "")
        is_veh = bool(row.get("is_vehicle_expense")) or str(row.get("account_code")) == "822"
        vat_type = str(row.get("vat_deduction_type") or "")

        if is_veh and vat_type == "DEDUCTIBLE":
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="VEHICLE_POLICY",
                    severity="ERROR",
                    code="VEHICLE_VAT_DEDUCTIBLE_ERROR",
                    message=(
                        f"전표 #{tx_id}({merchant_name}): 차량유지비(822)는 비영업용 소형승용차 "
                        "매입세액 불공제(NON_DEDUCTIBLE) 필수"
                    ),
                )
            )

        card_last4 = str(row.get("card_last4") or "0000")
        if is_blocked_personal_card_code(card_last4, archive_dir=archive_dir):
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="CARD_POLICY",
                    severity="ERROR",
                    code="PERSONAL_CARD_MIXED",
                    message=(
                        f"전표 #{tx_id}({merchant_name}): 차단된 개인카드({card_last4}) 내역 혼입"
                    ),
                )
            )
        elif corp_only and not is_allowed_corporate_card(
            card_last4, raw_text=merchant_name, archive_dir=archive_dir
        ):
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="CARD_POLICY",
                    severity="ERROR",
                    code="UNREGISTERED_CORP_CARD",
                    message=(
                        f"전표 #{tx_id}({merchant_name}): 법인카드 전용 모드에서 미등록 카드({card_last4})"
                    ),
                )
            )

    return issues


async def audit_missing_evidence(snapshot: Dict[str, Any]) -> List[AuditIssue]:
    """Branch C: Check unclassified 999, 813 entertainment attendee, PG item memo, and slips."""
    issues: List[AuditIssue] = []
    for row in snapshot["rows"]:
        tx_id = int(row["tx_id"])
        merchant_name = str(row.get("raw_merchant_name") or "")
        acc_code = str(row.get("account_code") or "999")
        vat_type = str(row.get("vat_deduction_type") or "")
        memo = str(row.get("counterparty_info") or "").strip()

        if acc_code == "900":
            if vat_type == "DEDUCTIBLE":
                issues.append(
                    AuditIssue(
                        tx_id=tx_id,
                        category="VAT_STATUS",
                        severity="ERROR",
                        code="EXCLUDED_EXPENSE_VAT_DEDUCTIBLE_ERROR",
                        message=(
                            f"전표 #{tx_id}({merchant_name}): 경비제외(900) 항목은 "
                            "매입세액 공제(DEDUCTIBLE) 적용 불가"
                        ),
                    )
                )
            continue

        if acc_code == "999":
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="EVIDENCE",
                    severity="ERROR",
                    code="UNCLASSIFIED_ACCOUNT_999",
                    message=f"전표 #{tx_id}({merchant_name}): 미분류(999) 계정과목 확정 필요",
                )
            )

        if acc_code == "813":
            if vat_type == "DEDUCTIBLE":
                issues.append(
                    AuditIssue(
                        tx_id=tx_id,
                        category="EVIDENCE",
                        severity="ERROR",
                        code="ENTERTAINMENT_VAT_DEDUCTIBLE_ERROR",
                        message=(
                            f"전표 #{tx_id}({merchant_name}): 기업업무추진비(813)는 "
                            "매입세액 불공제(NON_DEDUCTIBLE) 필수"
                        ),
                    )
                )
            if not memo:
                issues.append(
                    AuditIssue(
                        tx_id=tx_id,
                        category="EVIDENCE",
                        severity="ERROR",
                        code="MISSING_ENTERTAINMENT_ATTENDEE",
                        message=(
                            f"전표 #{tx_id}({merchant_name}): 기업업무추진비(813) "
                            "거래처·참석자 적요 기록 누락"
                        ),
                    )
                )

        if is_uncertain_item_merchant(merchant_name) and not memo:
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="EVIDENCE",
                    severity="ERROR",
                    code="MISSING_PG_ITEM_MEMO",
                    message=(
                        f"전표 #{tx_id}({merchant_name}): PG·쇼핑몰·마트 결제 구매 품목 소명 누락"
                    ),
                )
            )

        total_amt = abs(int(row.get("total_amount") or 0))
        if (
            total_amt >= 30_000
            and str(row.get("reconcile_status") or "") == "SMS_ONLY"
            and not row.get("archived_file_name")
            and not row.get("biz_reg_no")
        ):
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="EVIDENCE",
                    severity="WARNING",
                    code="SMS_ONLY_UNVERIFIED_SLIP",
                    message=(
                        f"전표 #{tx_id}({merchant_name}, {total_amt:,}원): 3만 원 이상 "
                        "SMS_ONLY 미대사 건(홈택스 대사 또는 영수증 첨부 권장)"
                    ),
                )
            )

    return issues


_PERSONAL_HOUSEHOLD_RE = re.compile(
    r"(헤어|미용실|네일|피부과|성형외과|정형외과|한의원|치과의원|내과의원|"
    r"예봄교회|교회|성당|사찰|서윤니트|애플아울렛|에이비씨마트|ABC마트|"
    r"몽키뮤직타운|노래연습장|노래방)",
    re.IGNORECASE,
)
_RAW_GROCERY_RE = re.compile(
    r"(쌀직매장|동화쌀|착한정육점|정육점|참고기|청과|과일쟁이|농산생활|"
    r"고구마왕자|야생과|방아간|방앗간|뻥튀기공작소|수산축산)",
    re.IGNORECASE,
)
_PUB_ALCOHOL_RE = re.compile(
    r"(봉구비어|비어도|주휴소|피맥하우스|청담이상|이자카야|호프|포차|맥주)",
    re.IGNORECASE,
)
_RESORT_LEISURE_RE = re.compile(
    r"(아난티|한화리조트|리조트|해운조합|세이버리\s*뮤지엄)",
    re.IGNORECASE,
)


async def audit_tax_disallowance_risk(snapshot: Dict[str, Any]) -> List[AuditIssue]:
    """Branch D: Flag substantive corporate tax disallowance (가사경비·상여처분) risks."""
    issues: List[AuditIssue] = []
    for row in snapshot["rows"]:
        tx_id = int(row["tx_id"])
        acc_code = str(row.get("account_code") or "999")
        if acc_code == "900":
            continue

        merchant_name = str(row.get("raw_merchant_name") or "")
        total_amt = abs(int(row.get("total_amount") or 0))
        memo = str(row.get("counterparty_info") or "").strip()
        dt_str = str(row.get("transacted_at") or "")[:10]
        is_weekend = False
        if len(dt_str) == 10:
            try:
                is_weekend = datetime.strptime(dt_str, "%Y-%m-%d").weekday() >= 5
            except ValueError:
                is_weekend = False

        if _PERSONAL_HOUSEHOLD_RE.search(merchant_name):
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="TAX_DISALLOWANCE_RISK",
                    severity="WARNING",
                    code="PERSONAL_HOUSEHOLD_MERCHANT_RISK",
                    message=(
                        f"전표 #{tx_id}({merchant_name}, {total_amt:,}원): 미용·의료·의류·종교·유흥 "
                        "업종 가사경비 부인 위험 — 900 경비제외(가지급금) 전환 검토 권고"
                    ),
                )
            )
            continue

        if _RAW_GROCERY_RE.search(merchant_name) and acc_code != "813":
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="TAX_DISALLOWANCE_RISK",
                    severity="WARNING",
                    code="RAW_GROCERY_HOUSEHOLD_RISK",
                    message=(
                        f"전표 #{tx_id}({merchant_name}, {total_amt:,}원): 미가공 농축산물·식자재 "
                        "가사경비 오인 주의 — 거래처 선물(813)·현장 간식 소명 또는 900 경비제외 권고"
                    ),
                )
            )
            continue

        if _RESORT_LEISURE_RE.search(merchant_name):
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="TAX_DISALLOWANCE_RISK",
                    severity="WARNING",
                    code="RESORT_OR_LEISURE_EXPENSE_RISK",
                    message=(
                        f"전표 #{tx_id}({merchant_name}, {total_amt:,}원): 리조트·여객선·문화시설 결제 "
                        "— 출장·현장 증빙 확인 또는 사적 일정 시 900 경비제외 권고"
                    ),
                )
            )
            continue

        if acc_code == "811" and (_PUB_ALCOHOL_RE.search(merchant_name) or total_amt >= 100_000):
            weekend_tag = "주말 " if is_weekend else ""
            issues.append(
                AuditIssue(
                    tx_id=tx_id,
                    category="TAX_DISALLOWANCE_RISK",
                    severity="WARNING",
                    code="PUB_OR_HIGH_WELFARE_DINING_RISK",
                    message=(
                        f"전표 #{tx_id}({merchant_name}, {weekend_tag}{total_amt:,}원): "
                        "주점 또는 10만 원 이상 복리후생비(811) — 거래처 미팅이면 "
                        "813 기업업무추진비(참석자 기재), 사적 모임이면 900 경비제외 권고"
                        + (f" [현재 적요: {memo}]" if memo else "")
                    ),
                )
            )

    return issues


def join_audit_report(
    cursor: sqlite3.Cursor,
    year_month: str,
    snapshot: Dict[str, Any],
    branch_results: List[List[AuditIssue]],
    auto_revert_unverified_to_queue: bool = False,
) -> TaxAuditState:
    """Join parallel audit branch findings and optionally revert critical PENDING rows."""
    all_issues: List[AuditIssue] = []
    for branch_list in branch_results:
        all_issues.extend(branch_list)

    error_count = sum(1 for iss in all_issues if iss.severity == "ERROR")
    warning_count = sum(1 for iss in all_issues if iss.severity == "WARNING")
    flagged_tx_ids = sorted(
        {
            iss.tx_id
            for iss in all_issues
            if iss.tx_id is not None and iss.severity == "ERROR"
        }
    )

    if auto_revert_unverified_to_queue and flagged_tx_ids:
        placeholders = ",".join("?" for _ in flagged_tx_ids)
        cursor.execute(
            f"""
            UPDATE card_transactions
            SET vat_deduction_type = 'PENDING_CHECK',
                updated_at = CURRENT_TIMESTAMP
            WHERE tx_id IN ({placeholders})
              AND vat_deduction_type = 'PENDING'
            """,
            flagged_tx_ids,
        )

    return TaxAuditState(
        year_month=year_month,
        scanned_count=len(snapshot["rows"]),
        ready_for_export=(error_count == 0),
        error_count=error_count,
        warning_count=warning_count,
        issues=all_issues,
        flagged_tx_ids=flagged_tx_ids,
        vehicle_summary=snapshot["vehicle_summary"],
        visited_nodes=[
            "audit_fan_out",
            "audit_vat_status",
            "audit_vehicle_and_card_policy",
            "audit_missing_evidence",
            "audit_tax_disallowance_risk",
            "join_audit_report",
        ],
    )


async def run_pre_export_tax_audit(
    cursor: sqlite3.Cursor,
    year_month: str,
    request: Optional[TaxAuditRequest] = None,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute the pre-export parallel tax audit fan-out/join graph for year_month."""
    req = request or TaxAuditRequest()
    snapshot = audit_fan_out(
        cursor,
        year_month=year_month,
        include_exported=req.include_exported,
        archive_dir=archive_dir,
    )
    branch_results = list(
        await asyncio.gather(
            audit_vat_status(snapshot),
            audit_vehicle_and_card_policy(snapshot, archive_dir=archive_dir),
            audit_missing_evidence(snapshot),
            audit_tax_disallowance_risk(snapshot),
        )
    )
    state = join_audit_report(
        cursor,
        year_month=year_month,
        snapshot=snapshot,
        branch_results=branch_results,
        auto_revert_unverified_to_queue=req.auto_revert_unverified_to_queue,
    )
    return state.model_dump()
