"""USB Android Phone (ADB) SMS Inbox Extractor and Ledger Importer for SoloLedger."""

import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger.sms_parser import extract_rcs_text, parse_korean_card_sms
from sololedger.tax_engine import calculate_supply_and_vat, evaluate_tax_rules

_AD_EXCLUSION_KEYWORDS = (
    "무료거부",
    "수신거부",
    "거부 080",
    "무료수신거부",
    "(광고)",
    "대출",
    "한도",
    "결제일 안내",
    "결제금액",
    "출금완료",
    "연회비포함",
    "카드대금",
    "명세서",
    "주문취소 안내",
    "이용 안내 메시지",
    "쿠폰 다운로드",
    "이벤트",
)


def resolve_adb_binary() -> str:
    """Locate adb executable on PATH or default Windows Android SDK platform-tools directory."""
    in_path = shutil.which("adb")
    if in_path:
        return in_path
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        sdk_adb = Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
        if sdk_adb.exists():
            return str(sdk_adb)
    raise FileNotFoundError("adb executable not found. Please install Android SDK platform-tools.")


def fetch_card_sms_from_adb(
    limit: int = 1500,
    device_serial: Optional[str] = None,
    raw_adb_output: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query both SMS inbox and RCS (Chat+) messages from connected Android phone via ADB."""
    raw_texts: List[str] = []
    if raw_adb_output is None:
        adb_bin = resolve_adb_binary()
        for uri in ("content://sms/inbox", "content://im/chat"):
            cmd = [adb_bin]
            if device_serial:
                cmd.extend(["-s", device_serial])
            cmd.extend(
                [
                    "shell",
                    f"content query --uri {uri} --projection _id:address:date:body",
                ]
            )
            proc = subprocess.run(cmd, capture_output=True, check=False, timeout=25)
            if proc.returncode != 0 and uri == "content://sms/inbox":
                err_msg = proc.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"ADB query failed: {err_msg or 'No authorized Android device connected.'}"
                )
            out_str = proc.stdout.decode("utf-8", errors="replace")
            if out_str.strip():
                raw_texts.append(out_str)
    else:
        raw_texts.append(raw_adb_output)

    all_parsed_rows: List[Dict[str, Any]] = []
    for text in raw_texts:
        raw_rows = re.split(r"(?m)^Row:\s+\d+\s+", text)
        for chunk in raw_rows[1:]:
            match = re.match(
                r"_id=(\d+),\s*address=([^,]*),\s*date=(\d+),\s*body=(.*)",
                chunk,
                re.DOTALL,
            )
            if not match:
                continue
            sms_id = int(match.group(1))
            address = match.group(2).strip()
            date_ms = int(match.group(3))
            body = extract_rcs_text(match.group(4).strip())

            if any(ad in body for ad in _AD_EXCLUSION_KEYWORDS):
                continue
            if not re.search(r"(?:\d{2}/\d{2}\s+\d{2}:\d{2}|부분취소|후불하이패스)", body):
                continue
            if not re.search(r"(?:[\d,]+\s*원|KRW\s*[\d,]+)", body, re.IGNORECASE):
                continue
            if not re.search(r"(승인|일시불|할부|체크|법인|카드|부분취소|취소|결제|후불하이패스)", body):
                continue

            all_parsed_rows.append(
                {
                    "sms_id": sms_id,
                    "sender": address,
                    "date_ms": date_ms,
                    "received_at": datetime.fromtimestamp(date_ms / 1000.0).strftime("%Y-%m-%dT%H:%M:%S"),
                    "raw_message": body,
                }
            )

    all_parsed_rows.sort(key=lambda x: x["date_ms"], reverse=True)
    return all_parsed_rows[:limit]


def import_phone_sms_to_ledger(
    cursor: sqlite3.Cursor,
    limit: int = 30,
    device_serial: Optional[str] = None,
    raw_adb_output: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch card SMS messages from connected USB phone, parse, classify, and insert into ledger."""
    messages = fetch_card_sms_from_adb(
        limit=limit,
        device_serial=device_serial,
        raw_adb_output=raw_adb_output,
    )

    inserted_ids: List[int] = []
    skipped_duplicates = 0
    canceled_count = 0
    failed_count = 0
    parsed_preview: List[Dict[str, Any]] = []

    for msg in reversed(messages):
        try:
            parsed = parse_korean_card_sms(msg["raw_message"], fallback_time=msg["received_at"])
        except ValueError:
            failed_count += 1
            continue

        if parsed.is_cancel:
            cursor.execute(
                """
                SELECT tx_id FROM card_transactions
                WHERE card_last4 = ?
                  AND total_amount = ?
                  AND raw_merchant_name = ?
                  AND reconcile_status != 'CANCELED'
                ORDER BY transacted_at DESC
                LIMIT 1
                """,
                (parsed.card_last4, abs(parsed.total_amount), parsed.raw_merchant_name),
            )
            orig_row = cursor.fetchone()
            if orig_row:
                cursor.execute(
                    """
                    UPDATE card_transactions
                    SET reconcile_status = 'CANCELED', updated_at = CURRENT_TIMESTAMP
                    WHERE tx_id = ?
                    """,
                    (orig_row["tx_id"],),
                )
                canceled_count += 1
                continue

        cursor.execute(
            """
            SELECT tx_id FROM card_transactions
            WHERE card_last4 = ?
              AND transacted_at = ?
              AND total_amount = ?
              AND raw_merchant_name = ?
            LIMIT 1
            """,
            (parsed.card_last4, parsed.transacted_at, parsed.total_amount, parsed.raw_merchant_name),
        )
        if cursor.fetchone():
            skipped_duplicates += 1
            continue

        rule_eval = evaluate_tax_rules(cursor, parsed.raw_merchant_name)
        supply_value, vat_amount = calculate_supply_and_vat(
            parsed.total_amount, rule_eval["vat_deduction_type"]
        )

        cursor.execute(
            """
            INSERT INTO card_transactions (
                card_last4, approval_no, transacted_at, raw_merchant_name,
                total_amount, supply_value, vat_amount,
                account_code, account_name, vat_deduction_type,
                non_deduct_reason, is_vehicle_expense, reconcile_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SMS_ONLY')
            """,
            (
                parsed.card_last4,
                parsed.approval_no,
                parsed.transacted_at,
                parsed.raw_merchant_name,
                parsed.total_amount,
                supply_value,
                vat_amount,
                rule_eval["account_code"],
                rule_eval["account_name"],
                rule_eval["vat_deduction_type"],
                rule_eval["non_deduct_reason"],
                1 if rule_eval["is_vehicle_expense"] else 0,
            ),
        )
        tx_id = int(cursor.lastrowid)
        inserted_ids.append(tx_id)
        parsed_preview.append(
            {
                "tx_id": tx_id,
                "sender": msg["sender"],
                "transacted_at": parsed.transacted_at,
                "card_last4": parsed.card_last4,
                "raw_merchant_name": parsed.raw_merchant_name,
                "total_amount": parsed.total_amount,
                "account_code": rule_eval["account_code"],
                "account_name": rule_eval["account_name"],
                "vat_deduction_type": rule_eval["vat_deduction_type"],
            }
        )

    return {
        "status": "imported",
        "fetched_sms_count": len(messages),
        "inserted_count": len(inserted_ids),
        "skipped_duplicates": skipped_duplicates,
        "canceled_count": canceled_count,
        "failed_count": failed_count,
        "inserted_tx_ids": inserted_ids,
        "preview": parsed_preview[:15],
    }
