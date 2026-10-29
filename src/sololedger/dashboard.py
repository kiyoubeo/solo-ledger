"""Lightweight Web Dashboard and Inline Ledger Management for SoloLedger."""

import html
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger import __version__
from sololedger.archiver import compute_sha256_hex, save_receipt_files
from sololedger.config import settings
from sololedger.tax_engine import calculate_supply_and_vat
from sololedger.telegram_bot import _refresh_receipt_archived_filename
from sololedger.vehicle_tracker import get_vehicle_expense_summary

ACCOUNT_CODE_CATALOG: Dict[str, str] = {
    "811": "복리후생비",
    "813": "기업업무추진비",
    "822": "차량유지비",
    "826": "도서인쇄비",
    "830": "소모품비",
    "831": "지급수수료",
    "212": "비품",
    "999": "가계정(미분류)",
}


def update_transaction_classification(
    cursor: sqlite3.Cursor,
    tx_id: int,
    account_code: Optional[str] = None,
    account_name: Optional[str] = None,
    vat_deduction_type: Optional[str] = None,
    non_deduct_reason: Optional[str] = None,
    counterparty_info: Optional[str] = None,
) -> Dict[str, Any]:
    """Update a transaction's account classification, VAT deduction status, or memo."""
    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"Transaction #{tx_id} not found.")

    resolved_code = account_code or row["account_code"]
    resolved_name = account_name or ACCOUNT_CODE_CATALOG.get(resolved_code, row["account_name"])
    resolved_vat_type = vat_deduction_type or row["vat_deduction_type"]
    resolved_reason = non_deduct_reason if non_deduct_reason is not None else row["non_deduct_reason"]

    if resolved_code == "813" and vat_deduction_type is None:
        resolved_vat_type = "NON_DEDUCTIBLE"
        resolved_reason = resolved_reason or "ENTERTAINMENT"
    elif resolved_code == "822" and vat_deduction_type is None:
        resolved_vat_type = "NON_DEDUCTIBLE"
        resolved_reason = resolved_reason or "PASSENGER_CAR"

    is_vehicle = 1 if resolved_code == "822" else int(row["is_vehicle_expense"])
    supply_value, vat_amount = calculate_supply_and_vat(int(row["total_amount"]), resolved_vat_type)
    resolved_memo = counterparty_info if counterparty_info is not None else row["counterparty_info"]

    cursor.execute(
        """
        UPDATE card_transactions
        SET account_code = ?,
            account_name = ?,
            vat_deduction_type = ?,
            non_deduct_reason = ?,
            is_vehicle_expense = ?,
            supply_value = ?,
            vat_amount = ?,
            counterparty_info = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE tx_id = ?
        """,
        (
            resolved_code,
            resolved_name,
            resolved_vat_type,
            resolved_reason,
            is_vehicle,
            supply_value,
            vat_amount,
            resolved_memo,
            tx_id,
        ),
    )

    archived_file_name = _refresh_receipt_archived_filename(cursor, tx_id)
    cursor.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (tx_id,))
    updated = dict(cursor.fetchone())
    updated["archived_file_name"] = archived_file_name
    return updated


def build_dashboard_summary(
    cursor: sqlite3.Cursor,
    year_month: Optional[str] = None,
    year: Optional[int] = None,
) -> Dict[str, Any]:
    """Build aggregated dashboard metrics, pending action items, and vehicle limit gauge."""
    now = datetime.now()
    target_ym = year_month or now.strftime("%Y%m")
    target_year = year or int(target_ym[:4])
    ym_prefix = f"{target_ym[:4]}-{target_ym[4:6]}-%"

    cursor.execute(
        """
        SELECT t.*,
               (
                   SELECT r.archived_file_name
                   FROM receipt_ocr_metadata r
                   WHERE r.tx_id = t.tx_id
                   ORDER BY r.receipt_id DESC
                   LIMIT 1
               ) AS archived_file_name
        FROM card_transactions t
        WHERE t.transacted_at LIKE ?
          AND t.reconcile_status != 'CANCELED'
        ORDER BY t.transacted_at DESC, t.tx_id DESC
        """,
        (ym_prefix,),
    )
    rows = [dict(r) for r in cursor.fetchall()]

    total_expenditure = 0
    deductible_vat = 0
    non_deductible_vat = 0
    non_deductible_total = 0
    pending_items: List[Dict[str, Any]] = []

    for r in rows:
        amt = int(r["total_amount"])
        vat = int(r["vat_amount"])
        total_expenditure += amt
        if r["vat_deduction_type"] == "DEDUCTIBLE":
            deductible_vat += vat
        elif r["vat_deduction_type"] == "NON_DEDUCTIBLE":
            non_deductible_vat += vat
            non_deductible_total += amt

        memo = (r.get("counterparty_info") or "").strip()
        if r["account_code"] == "999" or not memo:
            pending_items.append(r)

    vehicle_summary = get_vehicle_expense_summary(cursor, year=target_year)

    return {
        "app_env": settings.app_env,
        "db_path": str(settings.db_path),
        "nas_backup_dir": str(settings.nas_backup_dir),
        "year_month": target_ym,
        "year": target_year,
        "total_tx_count": len(rows),
        "total_expenditure": total_expenditure,
        "deductible_vat": deductible_vat,
        "non_deductible_vat": non_deductible_vat,
        "non_deductible_total": non_deductible_total,
        "pending_action_count": len(pending_items),
        "pending_transactions": pending_items,
        "transactions": rows,
        "vehicle_limit": vehicle_summary,
    }


def seed_demo_transactions(
    cursor: sqlite3.Cursor,
    year_month: Optional[str] = None,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Insert realistic 1-person corp test transactions and sample receipt files for sandbox testing."""
    now = datetime.now()
    ym = year_month or now.strftime("%Y%m")
    y_str, m_str = ym[:4], ym[4:6]

    demo_rows = [
        (
            "9876",
            "DEMO1001",
            f"{y_str}-{m_str}-05 10:30:00",
            "쿠팡(주)",
            "1208800767",
            45000,
            40909,
            4091,
            "830",
            "소모품비",
            "DEDUCTIBLE",
            None,
            0,
            "모니터암 외 1건",
            "OCR_MATCHED",
        ),
        (
            "1234",
            "DEMO1002",
            f"{y_str}-{m_str}-08 14:20:00",
            "네이버페이",
            "2208162517",
            330000,
            300000,
            30000,
            "212",
            "비품",
            "DEDUCTIBLE",
            None,
            0,
            "LG 27인치 4K 모니터",
            "OCR_MATCHED",
        ),
        (
            "1234",
            "DEMO1003",
            f"{y_str}-{m_str}-12 12:15:00",
            "스타벅스역삼점",
            "2018121515",
            12000,
            10909,
            1091,
            "811",
            "복리후생비",
            "DEDUCTIBLE",
            None,
            0,
            "대표자 단독 식사",
            "HOMETAX_VERIFIED",
        ),
        (
            "1234",
            "DEMO1004",
            f"{y_str}-{m_str}-15 19:10:00",
            "한우명가역삼점",
            "2118754321",
            165000,
            150000,
            15000,
            "813",
            "기업업무추진비",
            "NON_DEDUCTIBLE",
            "ENTERTAINMENT",
            0,
            "A사 김철수 대표 프로젝트 킥오프 미팅",
            "HOMETAX_VERIFIED",
        ),
        (
            "9876",
            "DEMO1005",
            f"{y_str}-{m_str}-18 09:00:00",
            "SK에너지주유소",
            "1168118745",
            110000,
            100000,
            10000,
            "822",
            "차량유지비",
            "NON_DEDUCTIBLE",
            "PASSENGER_CAR",
            1,
            "업무용승용차 주유 60L",
            "HOMETAX_VERIFIED",
        ),
        (
            "5678",
            "DEMO1006",
            f"{y_str}-{m_str}-22 16:40:00",
            "알수없는문구점",
            "2148539210",
            15000,
            13636,
            1364,
            "999",
            "미분류",
            "PENDING_CHECK",
            None,
            0,
            None,
            "SMS_ONLY",
        ),
    ]

    inserted_ids: List[int] = []
    for row in demo_rows:
        cursor.execute(
            "SELECT tx_id FROM card_transactions WHERE approval_no = ?",
            (row[1],),
        )
        if cursor.fetchone():
            continue
        if row[4]:
            cursor.execute(
                """
                INSERT OR IGNORE INTO merchants (
                    biz_reg_no, merchant_name, tax_type_code, biz_status, nts_verified_at
                ) VALUES (?, ?, 'GENERAL', 'ACTIVE', CURRENT_TIMESTAMP)
                """,
                (row[4], row[3]),
            )
        cursor.execute(
            """
            INSERT INTO card_transactions (
                card_last4, approval_no, transacted_at, raw_merchant_name,
                biz_reg_no, total_amount, supply_value, vat_amount,
                account_code, account_name, vat_deduction_type,
                non_deduct_reason, is_vehicle_expense, counterparty_info, reconcile_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        tx_id = int(cursor.lastrowid)
        inserted_ids.append(tx_id)

        # Attach sample receipt for DEMO1001
        if row[1] == "DEMO1001":
            sample_bytes = f"DEMO-RECEIPT-COUPANG-{ym}".encode("utf-8")
            file_hash = compute_sha256_hex(sample_bytes)
            cursor.execute(
                "SELECT receipt_id FROM receipt_ocr_metadata WHERE file_hash_sha256 = ?",
                (file_hash,),
            )
            if not cursor.fetchone():
                arch_name = f"{y_str}{m_str}05_쿠팡_소모품비_모니터암외1건_45000.jpg"
                orig_path, _ = save_receipt_files(
                    sample_bytes,
                    original_file_name="demo_coupang.jpg",
                    archived_file_name=arch_name,
                    archive_dir=archive_dir,
                )
                cursor.execute(
                    """
                    INSERT INTO receipt_ocr_metadata (
                        tx_id, source_channel, original_file_path, archived_file_name,
                        file_hash_sha256, ocr_biz_reg_no, ocr_total_amount,
                        line_items_json, raw_ocr_response, ocr_confidence
                    ) VALUES (?, 'WEB_UPLOAD', ?, ?, ?, '1208800767', 45000, '[]', '{}', 0.99)
                    """,
                    (tx_id, orig_path, arch_name, file_hash),
                )

    return {
        "status": "seeded",
        "year_month": ym,
        "inserted_count": len(inserted_ids),
        "inserted_tx_ids": inserted_ids,
    }


def reset_test_ledger(
    cursor: sqlite3.Cursor,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Clear all transaction, receipt, and merchant rows in the test ledger."""
    cursor.execute("SELECT COUNT(*) FROM card_transactions")
    deleted_tx_count = int(cursor.fetchone()[0])

    cursor.execute("DELETE FROM receipt_ocr_metadata")
    cursor.execute("DELETE FROM card_transactions")
    cursor.execute("DELETE FROM merchants")

    target_archive = archive_dir or settings.archive_dir
    for sub in ("original", "normalized", "exports"):
        sub_path = target_archive / sub
        if sub_path.exists():
            shutil.rmtree(sub_path, ignore_errors=True)
            sub_path.mkdir(parents=True, exist_ok=True)

    return {
        "status": "reset",
        "deleted_transactions": deleted_tx_count,
        "archive_dir_cleaned": str(target_archive),
    }


def render_dashboard_html(
    cursor: sqlite3.Cursor,
    year_month: Optional[str] = None,
    year: Optional[int] = None,
) -> str:
    """Render server-side HTML5 management dashboard for SoloLedger."""
    summary = build_dashboard_summary(cursor, year_month=year_month, year=year)
    veh = summary["vehicle_limit"]
    gauge_pct = min(100.0, float(veh["usage_ratio_pct"]))

    pending_rows_html = []
    for item in summary["pending_transactions"]:
        tx_id = item["tx_id"]
        merchant = html.escape(str(item["raw_merchant_name"]))
        amt_str = f"{int(item['total_amount']):,}원"
        acc_name = html.escape(f"{item['account_code']} {item['account_name']}")
        memo_val = html.escape(str(item.get("counterparty_info") or ""))
        pending_rows_html.append(
            f"<tr data-tx-id='{tx_id}'>"
            f"<td>#{tx_id}</td>"
            f"<td>{html.escape(str(item['transacted_at']))}</td>"
            f"<td><strong>{merchant}</strong></td>"
            f"<td class='num'>{amt_str}</td>"
            f"<td><span class='badge warn'>{acc_name}</span></td>"
            f"<td><input type='text' id='memo-{tx_id}' value='{memo_val}' "
            f"placeholder='품목 또는 참석자 입력' /></td>"
            f"<td><button onclick='saveTxMemo({tx_id})'>저장</button></td>"
            f"</tr>"
        )
    pending_tbody = (
        "\n".join(pending_rows_html)
        if pending_rows_html
        else "<tr><td colspan='7' class='empty'>미분류 또는 소명 누락 전표가 없습니다.</td></tr>"
    )

    tx_rows_html = []
    for tx in summary["transactions"]:
        tx_id = tx["tx_id"]
        merchant = html.escape(str(tx["raw_merchant_name"]))
        amt_str = f"{int(tx['total_amount']):,}원"
        vat_str = f"{int(tx['vat_amount']):,}원"
        acc_str = html.escape(f"{tx['account_code']} {tx['account_name']}")
        ded_str = html.escape(str(tx["vat_deduction_type"]))
        memo_str = html.escape(str(tx.get("counterparty_info") or "-"))
        arch_str = html.escape(str(tx.get("archived_file_name") or "-"))
        tx_rows_html.append(
            f"<tr>"
            f"<td>#{tx_id}</td>"
            f"<td>{html.escape(str(tx['transacted_at']))}</td>"
            f"<td>{merchant}</td>"
            f"<td class='num'>{amt_str}</td>"
            f"<td class='num'>{vat_str}</td>"
            f"<td>{acc_str}</td>"
            f"<td>{ded_str}</td>"
            f"<td>{memo_str}</td>"
            f"<td>{arch_str}</td>"
            f"</tr>"
        )
    tx_tbody = (
        "\n".join(tx_rows_html)
        if tx_rows_html
        else "<tr><td colspan='9' class='empty'>해당 귀속월 거래 내역이 없습니다.</td></tr>"
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>SoloLedger v{__version__} — 1인 법인 경비·세무증빙 대시보드</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           margin: 0; padding: 24px; background: #f8fafc; color: #0f172a; }}
    header {{ display: flex; justify-content: space-between; align-items: center;
              margin-bottom: 24px; border-bottom: 2px solid #e2e8f0; padding-bottom: 12px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
              gap: 16px; margin-bottom: 24px; }}
    .card {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px; }}
    .card h3 {{ margin: 0 0 8px 0; font-size: 13px; color: #64748b; }}
    .card .val {{ font-size: 22px; font-weight: 700; color: #0f172a; }}
    .section {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px;
                padding: 18px; margin-bottom: 24px; }}
    .gauge-track {{ width: 100%; height: 16px; background: #e2e8f0; border-radius: 8px;
                    overflow: hidden; margin: 10px 0; }}
    .gauge-bar {{ height: 100%; background: #2563eb; width: {gauge_pct}%; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e2e8f0; padding: 9px 8px; text-align: left; }}
    th {{ background: #f1f5f9; font-weight: 600; }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    td.empty {{ text-align: center; color: #64748b; padding: 18px; }}
    .badge.warn {{ background: #fef3c7; color: #92400e; padding: 2px 8px; border-radius: 4px; }}
    .badge.env {{ background: #dcfce7; color: #166534; padding: 3px 10px; border-radius: 6px;
                  font-size: 12px; font-weight: 700; margin-left: 8px; }}
    button {{ background: #2563eb; color: #fff; border: none; border-radius: 6px;
              padding: 6px 12px; cursor: pointer; margin-left: 4px; }}
    button.secondary {{ background: #475569; }}
    button.danger {{ background: #dc2626; }}
    input[type="text"] {{ padding: 5px 8px; border: 1px solid #cbd5e1; border-radius: 4px; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>SoloLedger 경비·세무증빙 대시보드 (v{__version__})
        <span class="badge env">{html.escape(summary['app_env'].upper())} MODE</span>
      </h1>
      <p>귀속 연월: <strong>{summary['year_month']}</strong> | 귀속 연도: <strong>{summary['year']}</strong>
         | DB: <code>{html.escape(Path(summary['db_path']).name)}</code></p>
    </div>
    <div>
      <button class="secondary" onclick="seedDemoData()">샘플 데이터 생성</button>
      <button class="danger" onclick="resetTestLedger()">테스트 DB 초기화</button>
      <button onclick="backupToNas()">NAS(Z:\\) 백업 동기화</button>
      <button onclick="exportTaxPack('{summary['year_month']}')">세무사 제출용 ZIP+CSV 생성</button>
    </div>
  </header>

  <section class="cards">
    <div class="card">
      <h3>월 총 지출액 ({summary['total_tx_count']}건)</h3>
      <div class="val">{summary['total_expenditure']:,}원</div>
    </div>
    <div class="card">
      <h3>공제 가능 부가세 합계</h3>
      <div class="val">{summary['deductible_vat']:,}원</div>
    </div>
    <div class="card">
      <h3>불공제 지출 합계 (부가세 {summary['non_deductible_vat']:,}원)</h3>
      <div class="val">{summary['non_deductible_total']:,}원</div>
    </div>
    <div class="card">
      <h3>미분류·소명 필요 건수</h3>
      <div class="val">{summary['pending_action_count']}건</div>
    </div>
  </section>

  <section class="section">
    <h2>업무용승용차 연간 1,500만 원 특례 한도 트래커 ({veh['year']}년)</h2>
    <p>누적 사용액: <strong>{veh['total_used']:,}원</strong> / {veh['annual_limit']:,}원
       ({veh['usage_ratio_pct']}%) | 잔여 한도: <strong>{veh['remaining_limit']:,}원</strong>
       | 상태: <strong>{veh['warning_level']}</strong></p>
    <div class="gauge-track"><div class="gauge-bar"></div></div>
    <p><small>{html.escape(veh['advice'])}</small></p>
  </section>

  <section class="section">
    <h2>미분류(999) 및 품목·참석자 소명 필요 전표 ({summary['pending_action_count']}건)</h2>
    <table>
      <thead>
        <tr>
          <th>전표번호</th><th>거래일시</th><th>가맹점명</th><th>금액</th>
          <th>현재 계정</th><th>품목 / 참석자 소명</th><th>작업</th>
        </tr>
      </thead>
      <tbody>
        {pending_tbody}
      </tbody>
    </table>
  </section>

  <section class="section">
    <h2>귀속월 전체 법인카드 원장 ({summary['total_tx_count']}건)</h2>
    <table>
      <thead>
        <tr>
          <th>전표번호</th><th>거래일시</th><th>가맹점명</th><th>합계금액</th>
          <th>부가세</th><th>계정과목</th><th>공제구분</th><th>적요/참석자</th><th>증빙파일명</th>
        </tr>
      </thead>
      <tbody>
        {tx_tbody}
      </tbody>
    </table>
  </section>

  <script>
    const API_HEADERS = {{
      'Content-Type': 'application/json',
      'X-API-Key': '{html.escape(settings.api_key)}'
    }};
    async function saveTxMemo(txId) {{
      const memoInput = document.getElementById('memo-' + txId);
      if (!memoInput) return;
      await fetch('/api/v1/transactions/' + txId, {{
        method: 'PATCH',
        headers: API_HEADERS,
        body: JSON.stringify({{ counterparty_info: memoInput.value }})
      }});
      window.location.reload();
    }}
    async function exportTaxPack(yearMonth) {{
      await fetch('/api/v1/exports/tax-pack', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{ year_month: yearMonth, force_reexport: true }})
      }});
      window.location.reload();
    }}
    async function backupToNas() {{
      await fetch('/api/v1/system/backup-nas', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{}})
      }});
      window.location.reload();
    }}
    async function seedDemoData() {{
      await fetch('/api/v1/system/seed-demo', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{ year_month: '{summary['year_month']}' }})
      }});
      window.location.reload();
    }}
    async function resetTestLedger() {{
      await fetch('/api/v1/system/reset-test', {{
        method: 'POST',
        headers: API_HEADERS,
        body: JSON.stringify({{}})
      }});
      window.location.reload();
    }}
  </script>
</body>
</html>"""
