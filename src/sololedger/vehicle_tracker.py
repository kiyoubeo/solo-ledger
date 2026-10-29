"""Corporate Vehicle Expense Annual 15,000,000 KRW Tax Special Limit Tracker."""

import sqlite3
from typing import Any, Dict

ANNUAL_VEHICLE_LIMIT_KRW = 15_000_000


def get_vehicle_expense_summary(cursor: sqlite3.Cursor, year: int) -> Dict[str, Any]:
    """Calculate cumulative corporate vehicle expense against the 15M KRW annual limit.

    Under Korean Corporate Tax Law, up to 15,000,000 KRW of corporate passenger car
    expenses (depreciation + maintenance/fuel/tolls) are recognized without a driving log.
    """
    year_prefix = f"{int(year):04d}-%"
    cursor.execute(
        """
        SELECT COUNT(*) AS tx_count,
               COALESCE(SUM(total_amount), 0) AS total_used
        FROM card_transactions
        WHERE is_vehicle_expense = 1
          AND reconcile_status != 'CANCELED'
          AND transacted_at LIKE ?
        """,
        (year_prefix,),
    )
    row = cursor.fetchone()
    tx_count = int(row["tx_count"] or 0)
    total_used = int(row["total_used"] or 0)
    remaining_limit = max(0, ANNUAL_VEHICLE_LIMIT_KRW - total_used)
    usage_ratio_pct = round((total_used / ANNUAL_VEHICLE_LIMIT_KRW) * 100.0, 2)

    if total_used > ANNUAL_VEHICLE_LIMIT_KRW:
        warning_level = "LIMIT_EXCEEDED"
        advice = (
            "연간 1,500만 원 특례 한도를 초과했습니다. "
            "초과분 손금 산입을 위해 업무용승용차 운행기록부 작성이 필수입니다."
        )
    elif usage_ratio_pct >= 80.0:
        warning_level = "WARNING_80PCT"
        advice = "연간 1,500만 원 특례 한도의 80% 이상을 사용했습니다."
    else:
        warning_level = "NORMAL"
        advice = "연간 1,500만 원 무운행기록부 특례 한도 내에서 정상 관리 중입니다."

    return {
        "year": int(year),
        "annual_limit": ANNUAL_VEHICLE_LIMIT_KRW,
        "total_used": total_used,
        "remaining_limit": remaining_limit,
        "usage_ratio_pct": usage_ratio_pct,
        "tx_count": tx_count,
        "warning_level": warning_level,
        "advice": advice,
    }
