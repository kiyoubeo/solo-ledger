"""Unit and integration tests for Toss Payments corporate card email scrolling and reconciliation."""

import base64
import json
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from fastapi.testclient import TestClient

from sololedger.api import app
from sololedger.config import settings
from sololedger.db import get_connection, init_db
from sololedger.email_watcher import (
    _discover_imap_promotion_mailboxes,
    _discover_imap_scroll_mailboxes,
    _extract_eml_parts,
    _resolve_account_email_address,
    classify_toss_card_ownership,
    extract_orders_from_email_content,
    load_email_watcher_config,
    parse_raw_email_bytes_or_text,
    parse_toss_payments_email,
    save_email_watcher_config,
    scroll_imap_toss_payments_emails,
)
from sololedger.importers import _normalize_datetime_str
from sololedger.scraper_service import (
    extract_coupang_orders_from_content,
    ingest_scraped_payload,
    reconcile_scraped_ecommerce_orders,
)
from sololedger.sms_parser import (
    is_blocked_personal_card_code,
    match_card_code_pattern,
    match_registered_corp_card_code,
    register_corp_card,
    save_corp_card_policy,
)
from sololedger.telegram_bot import _detect_memo_account_override


def _build_toss_eml_bytes(
    *,
    message_id: str,
    subject: str,
    date_rfc: str,
    product: str,
    amount_str: str,
    card_issuer: str,
    paid_at_str: str,
    approval_no: str,
    order_no: str,
    seller: str,
    tx_token: str,
    cancel_amount_str: str = "",
) -> bytes:
    cancel_row = (
        f"<tr><th>부분취소금액</th><td>{cancel_amount_str}</td></tr>"
        if cancel_amount_str
        else ""
    )
    html_body = f"""<!DOCTYPE html>
<html><body>
  <table>
    <tr><th>구매상품</th><td>{product}</td></tr>
    <tr><th>결제금액</th><td>{amount_str}</td></tr>
    {cancel_row}
    <tr><th>결제수단</th><td>신용카드</td></tr>
    <tr><th>결제카드</th><td>{card_issuer}</td></tr>
    <tr><th>할부기간</th><td>일시불</td></tr>
    <tr><th>결제일시</th><td>{paid_at_str}</td></tr>
    <tr><th>승인번호</th><td>{approval_no}</td></tr>
    <tr><th>주문번호</th><td>{order_no}</td></tr>
    <tr><th>판매자</th><td>{seller}</td></tr>
  </table>
  <a href="https://dashboard.tosspayments.com/receipt/redirection?transactionId={tx_token}&amp;ref=email">
    매출전표
  </a>
  <div>토스페이먼츠 주식회사 | 사업자등록번호 : 411-86-01799</div>
</body></html>"""

    msg = EmailMessage()
    msg["From"] = "토스페이먼츠 <bill@bill-mail.tosspayments.com>"
    msg["To"] = "corp-test@example.com"
    msg["Subject"] = subject
    msg["Date"] = date_rfc
    msg["Message-ID"] = message_id
    msg.set_content(f"구매상품 : {product}\n결제금액 : {amount_str}\n판매자 : {seller}")
    msg.add_alternative(html_body, subtype="html")
    return msg.as_bytes()


def test_wildcard_card_code_matching(tmp_path: Path) -> None:
    save_corp_card_policy(
        {
            "corp_only_mode": True,
            "strict_registered_only": True,
            "blocked_personal_cards": ["6534", "8508", "6*4*"],
        },
        archive_dir=tmp_path,
    )
    register_corp_card("1175", issuer="WOORI", archive_dir=tmp_path)
    register_corp_card("3081", issuer="WOORI", archive_dir=tmp_path)

    assert match_card_code_pattern("117*", "1175") is True
    assert match_card_code_pattern("308*", "3081") is True
    assert match_card_code_pattern("607*", "6*4*") is False

    assert match_registered_corp_card_code("117*", archive_dir=tmp_path) == "1175"
    assert match_registered_corp_card_code("3081", archive_dir=tmp_path) == "3081"
    assert match_registered_corp_card_code("653*", archive_dir=tmp_path) is None
    assert is_blocked_personal_card_code("653*", archive_dir=tmp_path) is True
    assert is_blocked_personal_card_code("850*", archive_dir=tmp_path) is True
    assert is_blocked_personal_card_code("117*", archive_dir=tmp_path) is False


def test_parse_toss_payments_email_separates_seller_from_pg_footer() -> None:
    eml_bytes = _build_toss_eml_bytes(
        message_id="<toss-1@example.com>",
        subject="주식회사 테스트법인님, (주)한국무역정보통신에서 결제한 내역이 도착했어요.",
        date_rfc="Tue, 22 Sep 2026 11:45:10 +0900",
        product="사업자범용 2년 인증서(ON)",
        amount_str="165,000원",
        card_issuer="우리비씨",
        paid_at_str="2026/09/22 11:45:08",
        approval_no="55272054",
        order_no="ORD-20260922-001",
        seller="(주)한국무역정보통신",
        tx_token="ENC_TID_CORP_1",
    )
    parts = _extract_eml_parts(eml_bytes)
    parsed = parse_toss_payments_email(
        subject=parts["subject"],
        sender=parts["sender"],
        date_str=parts["date_str"],
        body_text=parts["body_text"],
        body_html=parts["body_html"],
    )
    assert parsed is not None
    assert parsed["event_type"] == "PAYMENT"
    assert parsed["buyer_greeting"] == "주식회사 테스트법인"
    assert parsed["merchant_name"] == "(주)한국무역정보통신"
    assert parsed["item_name"] == "사업자범용 2년 인증서(ON)"
    assert parsed["paid_amount"] == 165000
    assert parsed["card_issuer"] == "우리비씨"
    assert parsed["approval_no"] == "55272054"
    assert parsed["transacted_at"] == "2026-09-22 11:45:08"
    assert "ENC_TID_CORP_1" in parsed["receipt_url"]

    orders = extract_orders_from_email_content(
        subject=parts["subject"],
        sender=parts["sender"],
        date_str=parts["date_str"],
        body_text=parts["body_text"],
        body_html=parts["body_html"],
    )
    assert len(orders) == 1
    assert orders[0]["발신처"] == "토스페이먼츠"
    assert orders[0]["상점명"] == "(주)한국무역정보통신"
    assert orders[0]["사업자번호"] == ""  # Never polluted with Toss PG 4118601799


def test_classify_toss_card_ownership_blocks_personal_and_allows_corp(tmp_path: Path) -> None:
    save_corp_card_policy(
        {
            "corp_only_mode": True,
            "strict_registered_only": True,
            "blocked_personal_cards": ["6534", "8508"],
        },
        archive_dir=tmp_path,
    )
    register_corp_card("1175", issuer="WOORI", archive_dir=tmp_path)

    # 1. Personal Lotte BIN -> rejected
    p_lotte = classify_toss_card_ownership(
        {"card_number_masked": "94091511****472*", "card_name": "롯데"},
        archive_dir=tmp_path,
    )
    assert p_lotte["is_corporate"] is False

    # 2. Personal Woori BIN -> rejected
    p_woori = classify_toss_card_ownership(
        {"card_number_masked": "95000100****389*", "card_name": "우리"},
        archive_dir=tmp_path,
    )
    assert p_woori["is_corporate"] is False

    # 3. Registered corp card wildcard 117* -> resolved to 1175
    c_reg = classify_toss_card_ownership(
        {"card_number_masked": "53275000****117*", "card_name": "우리"},
        archive_dir=tmp_path,
    )
    assert c_reg["is_corporate"] is True
    assert c_reg["card_last4"] == "1175"

    # 4. Corporate BIN 910020 -> allowed and marked for auto-registration
    c_bin = classify_toss_card_ownership(
        {"card_number_masked": "91002003****901*", "card_name": "우리비씨"},
        archive_dir=tmp_path,
    )
    assert c_bin["is_corporate"] is True
    assert c_bin["card_last4"] == "901*"
    assert c_bin["should_auto_register"] is True


class _FakeImapScrollServer:
    """Mock IMAP4_SSL server supporting All Mail discovery and paginated UID FETCH."""

    def __init__(self, messages_by_uid: Dict[bytes, bytes]) -> None:
        self._messages = messages_by_uid
        self.selected_mailboxes: List[str] = []
        self.fetch_batches: List[bytes] = []

    def login(self, user: str, password: str) -> tuple[str, List[bytes]]:
        return "OK", [b"Logged in"]

    def list(self) -> tuple[str, List[bytes]]:
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
        ]

    def select(self, mailbox: str = "INBOX", readonly: bool = True) -> tuple[str, List[bytes]]:
        self.selected_mailboxes.append(mailbox)
        return "OK", [b"5"]

    def uid(self, command: str, *args: Any) -> tuple[str, List[Any]]:
        cmd = command.upper()
        if cmd == "SEARCH":
            sorted_uids = sorted(self._messages.keys(), key=lambda x: int(x))
            return "OK", [b" ".join(sorted_uids)]
        if cmd == "FETCH":
            uid_csv = args[0]
            self.fetch_batches.append(uid_csv)
            out: List[Any] = []
            for u in uid_csv.split(b","):
                raw = self._messages.get(u.strip())
                if raw:
                    out.append((b"1 (UID " + u + b" RFC822 {" + str(len(raw)).encode() + b"})", raw))
            return "OK", out
        return "BAD", []

    def logout(self) -> tuple[str, List[bytes]]:
        return "BYE", [b"Logout"]


def test_scroll_imap_toss_payments_emails_end_to_end(tmp_path: Path, monkeypatch: Any) -> None:
    db_file = tmp_path / "toss_scroll_test.db"
    arch_dir = tmp_path / "archive"
    arch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SOLOLEDGER_DB_PATH", str(db_file))
    monkeypatch.setenv("SOLOLEDGER_ARCHIVE_DIR", str(arch_dir))

    init_db(str(db_file))
    save_corp_card_policy(
        {
            "corp_only_mode": True,
            "strict_registered_only": True,
            "block_unmatched_email_orders": True,
            "auto_register_on_statement_upload": True,
            "blocked_personal_cards": ["6534", "8508"],
        },
        archive_dir=arch_dir,
    )
    register_corp_card("1175", issuer="WOORI", archive_dir=arch_dir)
    save_email_watcher_config(
        {
            "provider": "GMAIL",
            "imap_host": "imap.gmail.com",
            "imap_port": 993,
            "email_address": "corp-test@example.com",
            "app_password": "dummy-app-password",
            "enabled": True,
        },
        archive_dir=arch_dir,
    )

    conn = get_connection(str(db_file))
    cur = conn.cursor()
    # Seed an existing Hometax PG row ("토스페이먼츠 주식회사", 74,900 KRW, card 1175)
    cur.execute(
        """
        INSERT INTO merchants (biz_reg_no, merchant_name, tax_type_code, biz_status, is_pg_provider)
        VALUES ('4118601799', '토스페이먼츠 주식회사', 'GENERAL', 'ACTIVE', 1)
        """
    )
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '1175', NULL, '2026-01-08 12:00:00', '토스페이먼츠 주식회사', '4118601799',
            74900, 68091, 6809, '830', '소모품비', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    existing_pg_tx_id = int(cur.lastrowid)
    conn.commit()

    fake_msgs = {
        # 1. Matches existing PG row (74,900 KRW on 2026-01-08, card 117*)
        b"101": _build_toss_eml_bytes(
            message_id="<m101@example.com>",
            subject="고객님, 스튜디오가맹점에서 결제한 내역이 도착했어요.",
            date_rfc="Thu, 08 Jan 2026 14:10:00 +0900",
            product="설계 도면 바인더 세트",
            amount_str="74,900원",
            card_issuer="우리",
            paid_at_str="2026/01/08 14:09:55",
            approval_no="11223344",
            order_no="ORD-101",
            seller="스튜디오가맹점",
            tx_token="TID_101",
        ),
        # 2. Personal card purchase (Lotte 940915...) -> must be skipped!
        b"102": _build_toss_eml_bytes(
            message_id="<m102@example.com>",
            subject="홍길동님, 개인쇼핑몰에서 결제한 내역이 도착했어요.",
            date_rfc="Wed, 17 Jun 2026 21:23:00 +0900",
            product="개인 의류 세트",
            amount_str="96,500원",
            card_issuer="롯데",
            paid_at_str="2026/06/17 21:23:00",
            approval_no="28235007",
            order_no="ORD-102",
            seller="개인쇼핑몰",
            tx_token="TID_102",
        ),
        # 3. Corporate card purchase (Woori BC Corp BIN 910020, 901*) -> must be auto-created!
        b"103": _build_toss_eml_bytes(
            message_id="<m103@example.com>",
            subject="주식회사 테스트법인님, (주)한국무역정보통신에서 결제한 내역이 도착했어요.",
            date_rfc="Tue, 22 Sep 2026 11:45:08 +0900",
            product="사업자범용 2년 인증서(ON)",
            amount_str="165,000원",
            card_issuer="우리비씨",
            paid_at_str="2026/09/22 11:45:08",
            approval_no="55272054",
            order_no="ORD-103",
            seller="(주)한국무역정보통신",
            tx_token="TID_103",
        ),
        # 4. Companion corporate purchase on same card 901* with generic '고객님' greeting -> cohort match!
        b"104": _build_toss_eml_bytes(
            message_id="<m104@example.com>",
            subject="고객님, 주식회사 오늘의집페이에서 결제한 내역이 도착했어요.",
            date_rfc="Sat, 07 Nov 2026 00:35:16 +0900",
            product="인테리어 스탠드 조명",
            amount_str="159,000원",
            card_issuer="우리비씨",
            paid_at_str="2026/11/07 00:35:16",
            approval_no="52654265",
            order_no="ORD-104",
            seller="주식회사 오늘의집페이",
            tx_token="TID_104",
        ),
        # 5. Fully canceled purchase (billed_amount = 0) -> must be skipped!
        b"105": _build_toss_eml_bytes(
            message_id="<m105@example.com>",
            subject="주식회사 테스트법인님, 취소상점에서 결제한 내역이 도착했어요.",
            date_rfc="Sun, 08 Nov 2026 10:00:00 +0900",
            product="취소된 모니터 암",
            amount_str="88,000원",
            card_issuer="우리비씨",
            paid_at_str="2026/11/08 10:00:00",
            approval_no="99887766",
            order_no="ORD-105",
            seller="취소상점",
            tx_token="TID_105",
        ),
    }

    fake_slips: Dict[str, Optional[Dict[str, Any]]] = {
        "TID_101": {
            "card_name": "우리",
            "card_number_masked": "53275000****117*",
            "card_tail4": "117*",
            "card_bin6": "532750",
            "slip_status": "완료",
            "is_cancel": False,
            "has_partial_cancel": False,
            "auth_number": "11223344",
            "paid_at": "2026-01-08 14:09:55",
            "order_id": "ORD-101",
            "product_name": "설계 도면 바인더 세트",
            "paid_amount": 74900,
            "billed_amount": 74900,
            "supply_value": 68091,
            "vat_amount": 6809,
            "tax_free_amount": 0,
            "merchant_name": "스튜디오가맹점",
            "biz_reg_no": "1208102922",
            "sales_slip_url": "https://dashboard.tosspayments.com/receipt/sales-slip?transactionId=S101",
        },
        "TID_102": {
            "card_name": "롯데",
            "card_number_masked": "94091512****597*",
            "card_tail4": "597*",
            "card_bin6": "940915",
            "slip_status": "완료",
            "is_cancel": False,
            "has_partial_cancel": False,
            "auth_number": "28235007",
            "paid_at": "2026-06-17 21:23:00",
            "order_id": "ORD-102",
            "product_name": "개인 의류 세트",
            "paid_amount": 96500,
            "billed_amount": 96500,
            "supply_value": 87727,
            "vat_amount": 8773,
            "tax_free_amount": 0,
            "merchant_name": "개인쇼핑몰",
            "biz_reg_no": "",
            "sales_slip_url": "https://dashboard.tosspayments.com/receipt/sales-slip?transactionId=S102",
        },
        "TID_103": {
            "card_name": "우리비씨",
            "card_number_masked": "91002003****901*",
            "card_tail4": "901*",
            "card_bin6": "910020",
            "slip_status": "완료",
            "is_cancel": False,
            "has_partial_cancel": False,
            "auth_number": "55272054",
            "paid_at": "2026-09-22 11:45:08",
            "order_id": "ORD-103",
            "product_name": "사업자범용 2년 인증서(ON)",
            "paid_amount": 165000,
            "billed_amount": 165000,
            "supply_value": 150000,
            "vat_amount": 15000,
            "tax_free_amount": 0,
            "merchant_name": "(주)한국무역정보통신",
            "biz_reg_no": "1208102922",
            "sales_slip_url": "https://dashboard.tosspayments.com/receipt/sales-slip?transactionId=S103",
        },
        "TID_104": {
            "card_name": "우리비씨",
            "card_number_masked": "91002003****901*",
            "card_tail4": "901*",
            "card_bin6": "910020",
            "slip_status": "완료",
            "is_cancel": False,
            "has_partial_cancel": False,
            "auth_number": "52654265",
            "paid_at": "2026-11-07 00:35:16",
            "order_id": "ORD-104",
            "product_name": "인테리어 스탠드 조명",
            "paid_amount": 159000,
            "billed_amount": 159000,
            "supply_value": 144545,
            "vat_amount": 14455,
            "tax_free_amount": 0,
            "merchant_name": "주식회사 오늘의집페이",
            "biz_reg_no": "3738803242",
            "sales_slip_url": "https://dashboard.tosspayments.com/receipt/sales-slip?transactionId=S104",
        },
        "TID_105": {
            "card_name": "우리비씨",
            "card_number_masked": "91002003****901*",
            "card_tail4": "901*",
            "card_bin6": "910020",
            "slip_status": "취소",
            "is_cancel": True,
            "has_partial_cancel": False,
            "auth_number": "99887766",
            "paid_at": "2026-11-08 10:00:00",
            "order_id": "ORD-105",
            "product_name": "취소된 모니터 암",
            "paid_amount": 88000,
            "billed_amount": 0,
            "supply_value": 0,
            "vat_amount": 0,
            "tax_free_amount": 0,
            "merchant_name": "취소상점",
            "biz_reg_no": "",
            "sales_slip_url": "https://dashboard.tosspayments.com/receipt/sales-slip?transactionId=S105",
        },
    }

    fake_server = _FakeImapScrollServer(fake_msgs)

    def _mock_slip_fetch(receipt_url: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        for k, v in fake_slips.items():
            if k in receipt_url:
                return v
        return None

    with (
        patch("sololedger.email_watcher.imaplib.IMAP4_SSL", return_value=fake_server),
        patch("sololedger.email_watcher.fetch_toss_sales_slip_metadata", side_effect=_mock_slip_fetch),
    ):
        res = scroll_imap_toss_payments_emails(
            cur,
            archive_dir=arch_dir,
            page_size=10,
            max_pages=5,
            verify_receipt_slip=True,
            auto_create_corp_tx=True,
        )
        conn.commit()

        assert res["status"] == "scrolled"
        assert res["parsed_toss_count"] == 5
        assert res["corp_matched_existing_count"] == 1
        assert res["corp_created_count"] == 2
        assert res["skipped_personal_count"] == 1
        assert res["skipped_canceled_count"] == 1
        assert '"[Gmail]/All Mail"' in fake_server.selected_mailboxes

        # Verify existing PG row was enriched with approval_no, sub-merchant biz_reg_no, and statement
        cur.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (existing_pg_tx_id,))
        updated_pg = dict(cur.fetchone())
        assert updated_pg["approval_no"] == "11223344"
        assert updated_pg["biz_reg_no"] == "1208102922"
        assert "스튜디오가맹점" in updated_pg["counterparty_info"]
        assert "설계 도면 바인더 세트" in updated_pg["counterparty_info"]

        # Re-running scroll must be 100% idempotent (0 duplicate rows created)
        res2 = scroll_imap_toss_payments_emails(
            cur,
            archive_dir=arch_dir,
            page_size=10,
            max_pages=5,
            verify_receipt_slip=True,
            auto_create_corp_tx=True,
        )
        conn.commit()
        assert res2["corp_created_count"] == 0
        assert res2["corp_matched_existing_count"] == 3

    conn.close()

    # Verify API endpoint and Dashboard UI panel via FastAPI TestClient
    client = TestClient(app)
    with (
        patch("sololedger.email_watcher.imaplib.IMAP4_SSL", return_value=fake_server),
        patch("sololedger.email_watcher.fetch_toss_sales_slip_metadata", side_effect=_mock_slip_fetch),
    ):
        api_resp = client.post(
            "/api/v1/scraper/email-scroll-toss",
            headers={"X-API-Key": settings.api_key},
            json={"start_date": "2026-01-01", "end_date": "2026-12-31"},
        )
        assert api_resp.status_code == 200
        api_json = api_resp.json()
        assert api_json["status"] == "scrolled"
        assert api_json["corp_total_count"] == 3

    dash_resp = client.get("/dashboard", headers={"X-API-Key": settings.api_key})
    assert dash_resp.status_code == 200
    assert "toss-scroll-panel" in dash_resp.text
    assert "triggerTossEmailScroll" in dash_resp.text


def _build_simple_eml_bytes(
    *,
    message_id: str,
    sender: str,
    subject: str,
    date_rfc: str,
    body_text: str,
    body_html: str = "",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "corp-test@example.com"
    msg["Subject"] = subject
    msg["Date"] = date_rfc
    msg["Message-ID"] = message_id
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    return msg.as_bytes()


def test_unified_year_email_sync_paginates_beyond_600_and_includes_toss_slips(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Verify /api/v1/scraper/email-sync scrolls >600 messages (365d default) and unifies Toss slips."""
    db_file = tmp_path / "unified_year_sync.db"
    arch_dir = tmp_path / "archive"
    arch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SOLOLEDGER_DB_PATH", str(db_file))
    monkeypatch.setenv("SOLOLEDGER_ARCHIVE_DIR", str(arch_dir))

    init_db(str(db_file))
    # Default lookback_days without prior config must now be 365
    default_cfg = load_email_watcher_config(arch_dir)
    assert default_cfg["lookback_days"] == 365

    save_corp_card_policy(
        {
            "corp_only_mode": True,
            "strict_registered_only": True,
            "block_unmatched_email_orders": True,
            "auto_register_on_statement_upload": True,
            "blocked_personal_cards": ["6534", "8508"],
        },
        archive_dir=arch_dir,
    )
    register_corp_card("1175", issuer="WOORI", archive_dir=arch_dir)
    save_email_watcher_config(
        {
            "provider": "GMAIL",
            "imap_host": "imap.gmail.com",
            "imap_port": 993,
            "email_address": "corp-test@example.com",
            "app_password": "dummy-app-password",
            "enabled": True,
        },
        archive_dir=arch_dir,
    )

    conn = get_connection(str(db_file))
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO merchants (biz_reg_no, merchant_name, tax_type_code, biz_status, is_pg_provider)
        VALUES
            ('1208800767', '쿠팡(주)', 'GENERAL', 'ACTIVE', 1),
            ('4118601799', '토스페이먼츠 주식회사', 'GENERAL', 'ACTIVE', 1)
        """
    )
    # 1) Early-year Coupang transaction (2026-01-12, 42,800 KRW)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '1175', '33445566', '2026-01-12 10:20:00', '쿠팡(주)', '1208800767',
            42800, 38909, 3891, '830', '소모품비', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    early_coupang_tx_id = int(cur.lastrowid)

    # 2) Early-year Toss Payments PG transaction (2026-02-14, 55,000 KRW)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '1175', NULL, '2026-02-14 15:00:00', '토스페이먼츠 주식회사', '4118601799',
            55000, 50000, 5000, '830', '소모품비', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    early_toss_tx_id = int(cur.lastrowid)
    conn.commit()
    conn.close()

    # Build 625 messages so UID 1..4 sit beyond the legacy 600-message cutoff (UIDs 26..625)
    fake_msgs: Dict[bytes, bytes] = {}
    fake_msgs[b"1"] = _build_simple_eml_bytes(
        message_id="<yr-cp-1@example.com>",
        sender="쿠팡 <noreply@coupang.com>",
        subject="[쿠팡] 주문이 완료되었습니다.",
        date_rfc="Mon, 12 Jan 2026 10:21:00 +0900",
        body_text=(
            "주문일 : 2026-01-12\n"
            "상품명 : 복사용지 A4 프리미엄 박스세트\n"
            "총 결제금액 : 42,800원\n"
        ),
        body_html=(
            "<table>"
            "<tr><th>상품명</th><td>복사용지 A4 프리미엄 박스세트</td></tr>"
            "<tr><th>결제금액</th><td>42,800원</td></tr>"
            "<tr><th>결제일시</th><td>2026-01-12 10:20:30</td></tr>"
            "</table>"
        ),
    )
    fake_msgs[b"2"] = _build_toss_eml_bytes(
        message_id="<yr-toss-2@example.com>",
        subject="고객님, (주)클라우드솔루션에서 결제한 내역이 도착했어요.",
        date_rfc="Sat, 14 Feb 2026 15:01:00 +0900",
        product="클라우드 백업 연간 구독권",
        amount_str="55,000원",
        card_issuer="우리",
        paid_at_str="2026/02/14 15:00:12",
        approval_no="77889900",
        order_no="ORD-SYNC-2",
        seller="(주)클라우드솔루션",
        tx_token="TID_SYNC_2",
    )
    fake_msgs[b"3"] = _build_toss_eml_bytes(
        message_id="<yr-toss-3@example.com>",
        subject="홍길동님, 개인마트에서 결제한 내역이 도착했어요.",
        date_rfc="Sun, 15 Mar 2026 18:30:00 +0900",
        product="개인 생활용품 세트",
        amount_str="33,000원",
        card_issuer="롯데",
        paid_at_str="2026/03/15 18:30:00",
        approval_no="44332211",
        order_no="ORD-SYNC-3",
        seller="개인마트",
        tx_token="TID_SYNC_3",
    )
    fake_msgs[b"4"] = _build_toss_eml_bytes(
        message_id="<yr-toss-4@example.com>",
        subject="주식회사 테스트법인님, (주)디자인랩에서 결제한 내역이 도착했어요.",
        date_rfc="Wed, 14 Oct 2026 09:15:00 +0900",
        product="CAD 심볼 라이브러리 라이선스",
        amount_str="110,000원",
        card_issuer="우리비씨",
        paid_at_str="2026/10/14 09:15:00",
        approval_no="66554433",
        order_no="ORD-SYNC-4",
        seller="(주)디자인랩",
        tx_token="TID_SYNC_4",
    )
    for uid_num in range(5, 626):
        uid_b = str(uid_num).encode("ascii")
        fake_msgs[uid_b] = _build_simple_eml_bytes(
            message_id=f"<noise-{uid_num}@example.com>",
            sender="Newsletter <news@example.com>",
            subject=f"[광고] 주간 뉴스레터 안내 #{uid_num}",
            date_rfc="Thu, 05 Nov 2026 09:00:00 +0900",
            body_text="프로모션 안내 메일입니다.",
        )

    fake_slips: Dict[str, Optional[Dict[str, Any]]] = {
        "TID_SYNC_2": {
            "card_name": "우리",
            "card_number_masked": "53275000****117*",
            "card_tail4": "117*",
            "card_bin6": "532750",
            "slip_status": "완료",
            "is_cancel": False,
            "has_partial_cancel": False,
            "auth_number": "77889900",
            "paid_at": "2026-02-14 15:00:12",
            "order_id": "ORD-SYNC-2",
            "product_name": "클라우드 백업 연간 구독권",
            "paid_amount": 55000,
            "billed_amount": 55000,
            "supply_value": 50000,
            "vat_amount": 5000,
            "tax_free_amount": 0,
            "merchant_name": "(주)클라우드솔루션",
            "biz_reg_no": "1208102922",
            "sales_slip_url": "https://dashboard.tosspayments.com/receipt/sales-slip?transactionId=SS2",
        },
        "TID_SYNC_3": {
            "card_name": "롯데",
            "card_number_masked": "94091512****597*",
            "card_tail4": "597*",
            "card_bin6": "940915",
            "slip_status": "완료",
            "is_cancel": False,
            "has_partial_cancel": False,
            "auth_number": "44332211",
            "paid_at": "2026-03-15 18:30:00",
            "order_id": "ORD-SYNC-3",
            "product_name": "개인 생활용품 세트",
            "paid_amount": 33000,
            "billed_amount": 33000,
            "supply_value": 30000,
            "vat_amount": 3000,
            "tax_free_amount": 0,
            "merchant_name": "개인마트",
            "biz_reg_no": "",
            "sales_slip_url": "https://dashboard.tosspayments.com/receipt/sales-slip?transactionId=SS3",
        },
        "TID_SYNC_4": {
            "card_name": "우리비씨",
            "card_number_masked": "91002003****901*",
            "card_tail4": "901*",
            "card_bin6": "910020",
            "slip_status": "완료",
            "is_cancel": False,
            "has_partial_cancel": False,
            "auth_number": "66554433",
            "paid_at": "2026-10-14 09:15:00",
            "order_id": "ORD-SYNC-4",
            "product_name": "CAD 심볼 라이브러리 라이선스",
            "paid_amount": 110000,
            "billed_amount": 110000,
            "supply_value": 100000,
            "vat_amount": 10000,
            "tax_free_amount": 0,
            "merchant_name": "(주)디자인랩",
            "biz_reg_no": "3738803242",
            "sales_slip_url": "https://dashboard.tosspayments.com/receipt/sales-slip?transactionId=SS4",
        },
    }

    def _mock_slip_fetch(receipt_url: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        for k, v in fake_slips.items():
            if k in receipt_url:
                return v
        return None

    fake_server = _FakeImapScrollServer(fake_msgs)
    client = TestClient(app)

    with (
        patch("sololedger.email_watcher.imaplib.IMAP4_SSL", return_value=fake_server),
        patch("sololedger.email_watcher.fetch_toss_sales_slip_metadata", side_effect=_mock_slip_fetch),
    ):
        sync_resp = client.post(
            "/api/v1/scraper/email-sync",
            headers={"X-API-Key": settings.api_key},
            json={"lookback_days": 365, "force_rescan": True},
        )
        assert sync_resp.status_code == 200
        sync_json = sync_resp.json()
        assert sync_json["status"] == "synced"
        assert sync_json["lookback_days"] == 365
        assert sync_json["scrolled_pages"] >= 5
        assert sync_json["matched_count"] == 3
        assert sync_json["created_count"] == 1
        assert sync_json["toss_scroll_summary"]["parsed_toss_count"] == 3
        assert sync_json["toss_scroll_summary"]["corp_matched_existing_count"] == 1
        assert sync_json["toss_scroll_summary"]["corp_created_count"] == 1
        assert sync_json["toss_scroll_summary"]["skipped_personal_count"] == 1

        # Verify subsequent incremental & recent-rescan calls are idempotent
        sync_inc = client.post(
            "/api/v1/scraper/email-sync",
            headers={"X-API-Key": settings.api_key},
            json={"recent_rescan_days": 0},
        )
        assert sync_inc.status_code == 200
        assert sync_inc.json()["created_count"] == 0
        assert sync_inc.json()["newly_stated_count"] == 0

        sync_re = client.post(
            "/api/v1/scraper/email-sync",
            headers={"X-API-Key": settings.api_key},
            json={"recent_rescan_days": 7},
        )
        assert sync_re.status_code == 200
        assert sync_re.json()["created_count"] == 0
        assert sync_re.json()["newly_stated_count"] == 0

    conn_check = get_connection(str(db_file))
    cur_check = conn_check.cursor()
    # Oldest UID=1 Coupang email (beyond legacy 600 cap) must have matched early_coupang_tx_id
    cur_check.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (early_coupang_tx_id,))
    cp_row = dict(cur_check.fetchone())
    assert "복사용지 A4 프리미엄 박스세트" in str(cp_row["counterparty_info"] or "")

    # UID=2 Toss email must have enriched early_toss_tx_id via SalesSlip API
    cur_check.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (early_toss_tx_id,))
    toss_row = dict(cur_check.fetchone())
    assert toss_row["approval_no"] == "77889900"
    assert toss_row["biz_reg_no"] == "1208102922"
    assert "(주)클라우드솔루션" in str(toss_row["counterparty_info"] or "")
    conn_check.close()


def test_normalize_datetime_formats() -> None:
    assert _normalize_datetime_str("2026.3.15. 20:20:33") == "2026-03-15 20:20:33"
    assert _normalize_datetime_str("2026년 02월28일 17시58분") == "2026-02-28 17:58:00"
    assert _normalize_datetime_str("2026. 1. 13. PM 5시 32분 51초") == "2026-01-13 17:32:51"


def test_imap_mailbox_discovery_large_arc_and_recipient_resolution() -> None:
    class _FakeNaverImap:
        def list(self) -> tuple[str, List[bytes]]:
            return (
                "OK",
                [
                    b'(\\HasNoChildren) "/" "INBOX"',
                    b'(\\HasNoChildren) "/" "&zK2tbAC3rLDIHA-"',
                    b'(\\HasNoChildren) "/" "&1QS4XLqowVg-"',
                    b'(\\Trash \\HasNoChildren) "/" "Trash"',
                    b'(\\Junk \\HasNoChildren) "/" "Spam"',
                    b'(\\Noselect \\HasChildren) "/" "[Folder]"',
                ],
            )

    discovered = _discover_imap_scroll_mailboxes(_FakeNaverImap(), "INBOX")
    assert discovered == ["INBOX", "&zK2tbAC3rLDIHA-"]
    assert (
        _resolve_account_email_address(
            {"email_address": "pommoq", "imap_host": "imap.naver.com", "provider": "NAVER"}
        )
        == "pommoq@naver.com"
    )

    arc_pad = "ARC-Seal: i=1; a=rsa-sha256; b=" + ("A" * 2400)
    html_body = (
        "<html><body><table>"
        "<tr><th>주문일자</th><td>2026.3.15. 20:20:33</td></tr>"
        "<tr><th>구매 상세내역</th><th>쿠팡가</th><th>수량</th>"
        "<th>구매금액</th><th>판매자</th></tr>"
        "<tr><td>사무용 라벨 롤지</td><td>19,800원</td><td>1</td>"
        "<td>19,800원</td><td>쿠팡(주)</td></tr>"
        "<tr><th>총 결제금액</th><td>19,800원</td></tr>"
        "</table></body></html>"
    )
    b64_html = base64.b64encode(html_body.encode("utf-8")).decode("ascii")
    raw_eml = (
        f"{arc_pad}\r\n"
        "From: 쿠팡 <noreply@coupang.com>\r\n"
        "Subject: [쿠팡] 주문이 완료되었습니다.\r\n"
        "Date: Sun, 15 Mar 2026 20:21:00 +0900\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/html; charset="utf-8"\r\n'
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{b64_html}\r\n"
    ).encode("utf-8")
    parsed = parse_raw_email_bytes_or_text(raw_eml)
    assert len(parsed) == 1
    assert parsed[0]["결제금액"] == 19800
    assert parsed[0]["주문일시"].startswith("2026-03-15")


def test_naverpay_point_split_proration_and_noise_filtering() -> None:
    html_split = (
        "<html><body>"
        "<p>고객님께서 네이버페이로 주문하신 내역입니다.</p>"
        "<table><tr><th>주문일자</th><td>2026.01.10</td></tr></table>"
        "<h3>결제상세</h3>"
        "<table>"
        "<tr><td>주문금액</td><td>39,290원</td></tr>"
        "<tr><td>네이버페이 포인트</td><td>21,283원</td></tr>"
        "<tr><td>신용카드</td><td>18,007원</td></tr>"
        "</table>"
        "<h3>주문상품</h3>"
        "<table>"
        "<tr><td>유기농 두부 300g</td><td>주문금액</td><td>10,000원</td>"
        "<td>수량</td><td>1</td><td>주식회사 컬리 | 사업자명 : 주식회사 컬리</td></tr>"
        "<tr><td>무항생제 계란 10구</td><td>주문금액</td><td>9,290원</td>"
        "<td>수량</td><td>1</td><td>주식회사 컬리 | 사업자명 : 주식회사 컬리</td></tr>"
        "<tr><td>국내산 양파 1kg</td><td>주문금액</td><td>12,000원</td>"
        "<td>수량</td><td>1</td><td>주식회사 컬리 | 사업자명 : 주식회사 컬리</td></tr>"
        "<tr><td>친환경 대파 1단</td><td>주문금액</td><td>8,000원</td>"
        "<td>수량</td><td>1</td><td>주식회사 컬리 | 사업자명 : 주식회사 컬리</td></tr>"
        "</table>"
        "<p>네이버파이낸셜(주) 사업자등록번호 524-86-01528</p>"
        "</body></html>"
    )
    orders = extract_orders_from_email_content(
        subject="[네이버페이] 주문이 완료되었습니다.",
        sender="네이버페이 <naverpay@navercorp.com>",
        date_str="Sat, 10 Jan 2026 11:00:00 +0900",
        body_html=html_split,
    )
    assert len(orders) == 1
    ord0 = orders[0]
    assert ord0["결제금액"] == 18007
    assert ord0["사업자번호"] != "5248601528"
    assert "컬리" in str(ord0.get("입점판매자") or ord0.get("상점명") or "")
    assert len(ord0["품목내역"]) == 4
    assert sum(int(li["price"]) for li in ord0["품목내역"]) == 18007

    html_all_points = (
        "<html><body>"
        "<p>고객님께서 네이버페이로 주문하신 내역입니다.</p>"
        "<p>주문일자</p><p>2026.01.10</p>"
        "<p>결제상세</p><p>주문금액</p><p>15,000원</p><p>네이버페이 포인트</p><p>15,000원</p>"
        "<p>주문상품</p><p>전액포인트상품</p><p>주문금액</p><p>15,000원</p>"
        "</body></html>"
    )
    assert (
        extract_orders_from_email_content(
            subject="[네이버페이] 주문이 완료되었습니다.",
            sender="네이버페이 <naverpay@navercorp.com>",
            body_html=html_all_points,
        )
        == []
    )

    for noise_subj in (
        "[네이버페이] 구매를 확정 부탁드립니다.",
        "[네이버페이] 자동구매확정 예정 안내",
        "[네이버페이] 주문하신 상품 발송 안내",
    ):
        assert (
            extract_orders_from_email_content(
                subject=noise_subj,
                sender="네이버페이 <naverpay@navercorp.com>",
                body_text="주문일자 : 2026-01-10\n상품명 : 테스트\n결제금액 : 10,000원",
            )
            == []
        )


def test_coupang_six_col_discount_proration_and_policy_noise() -> None:
    html_6col = (
        "<html><body>"
        "<table>"
        "<tr><th>주문일자</th><td>2026.02.04 14:20:00</td></tr>"
        "<tr><th>구매 상세내역</th><th>쿠팡가</th><th>수량</th>"
        "<th>구매금액</th><th>배송정보</th><th>판매자</th></tr>"
        "<tr><td>스틸 몰딩 2m</td><td>12,000원</td><td>1</td>"
        "<td>12,000원</td><td>무료&nbsp;</td><td>금속자재몰</td></tr>"
        "<tr><td>스테인리스 도어스토퍼</td><td>10,000원</td><td>1</td>"
        "<td>10,000원</td><td>무료배송</td><td>금속자재몰</td></tr>"
        "<tr><th>총 결제금액</th><td>19,500원&nbsp;</td></tr>"
        "<tr><th>결제수단</th><td>우리카드(일시불)</td></tr>"
        "</table>"
        "</body></html>"
    )
    orders = extract_orders_from_email_content(
        subject="[쿠팡] 주문이 완료되었습니다.",
        sender="쿠팡 <noreply@coupang.com>",
        date_str="Wed, 04 Feb 2026 14:20:00 +0900",
        body_html=html_6col,
    )
    assert len(orders) == 1
    ord0 = orders[0]
    assert ord0["결제금액"] == 19500
    assert "우리" in str(ord0.get("카드사") or "")
    assert len(ord0["품목내역"]) == 2
    assert sum(int(li["price"]) for li in ord0["품목내역"]) == 19500

    assert (
        extract_orders_from_email_content(
            subject="[쿠팡] 로켓배송 정책 변경 안내",
            sender="쿠팡 <noreply@coupang.com>",
            body_text="변경 안내 최소 주문금액 : 19,800원",
        )
        == []
    )


def test_ksnet_payco_parsing_and_memo_override() -> None:
    ksnet_html = (
        "<html><body>"
        "<h3>구매내역</h3>"
        "<table><tr>"
        "<td>우리카드</td>"
        "<td>2026년 02월28일 17시58분</td>"
        "<td>[DIY] US 오리진 하프앤하프 외 1</td>"
        "<td>신용카드</td>"
        "<td>피자헛 장안동점</td>"
        "<td>84294928</td>"
        "</tr></table>"
        "<p>결제금액</p><p>42,500원</p>"
        "<p>주식회사 케이에스넷 사업자등록번호 120-81-97322</p>"
        "</body></html>"
    )
    ksnet_orders = extract_orders_from_email_content(
        subject="[KSNET] 신용카드 결제내역 안내",
        sender="KSNET <no-reply@ksnet.co.kr>",
        date_str="Sat, 28 Feb 2026 17:58:10 +0900",
        body_html=ksnet_html,
    )
    assert len(ksnet_orders) == 1
    ks0 = ksnet_orders[0]
    assert ks0["상점명"] == "피자헛 장안동점"
    assert ks0["승인번호"] == "84294928"
    assert ks0["결제금액"] == 42500
    assert ks0["사업자번호"] == ""
    acc_res = _detect_memo_account_override("피자헛 장안동점 [DIY] US 오리진 하프앤하프 외 1")
    assert acc_res is not None
    assert acc_res[:2] == ("811", "복리후생비")

    payco_orders = extract_orders_from_email_content(
        subject="[PAYCO] 결제내역 안내",
        sender="NHN PAYCO <help@payco.com>",
        date_str="Tue, 10 Feb 2026 12:00:00 +0900",
        body_text=(
            "상품명 : 테스트 원재료 패키지\n"
            "결제금액 : 33,000원\n"
            "결제일시 : 2026-02-10 12:00:00\n"
            "NHN페이코(주) 사업자등록번호 563-81-00723\n"
        ),
    )
    assert len(payco_orders) == 1
    assert payco_orders[0]["사업자번호"] != "5638100723"


def test_reconcile_skips_blocked_personal_and_issuer_conflict_and_repairs_polluted(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    db_file = tmp_path / "issuer_guard.db"
    arch_dir = tmp_path / "archive"
    arch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SOLOLEDGER_DB_PATH", str(db_file))
    monkeypatch.setenv("SOLOLEDGER_ARCHIVE_DIR", str(arch_dir))
    init_db(str(db_file))
    save_corp_card_policy(
        {
            "corp_only_mode": True,
            "strict_registered_only": True,
            "block_unmatched_email_orders": True,
            "blocked_personal_cards": ["6364"],
        },
        archive_dir=arch_dir,
    )
    register_corp_card("607*", card_label="우리법인(607*)", issuer="WOORI", archive_dir=arch_dir)

    conn = get_connection(str(db_file))
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '607*', '11223344', '2026-03-15 14:00:00', '이니시스 결제',
            28001, 25455, 2546, '999', '미분류', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    corp_tx_id = int(cur.lastrowid)

    # Seed a stale polluted statement row with NULL approval_no matching real #99 (KSNET)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name,
            counterparty_info, total_amount, supply_value, vat_amount,
            account_code, account_name, vat_deduction_type, reconcile_status
        ) VALUES (
            '607*', NULL, '2026-02-28 17:58:00', '（주）케이에스넷',
            'KSNET을 통하여 결제하신 내역입니다.', 42500, 38636, 3864,
            '830', '소모품비', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    polluted_tx_id = int(cur.lastrowid)
    cur.execute(
        """
        INSERT INTO receipt_ocr_metadata (
            tx_id, source_channel, original_file_path, archived_file_name,
            file_hash_sha256, ocr_total_amount, line_items_json
        ) VALUES (?, 'STATEMENT', 'statement_polluted.html', 'statement_polluted.html', 'deadbeef', 42500, '[]')
        """,
        (polluted_tx_id,),
    )
    conn.commit()

    # Blocked personal card (6364) and cross-issuer conflict (국민 vs 우리법인 607*) must be skipped
    res_skip = reconcile_scraped_ecommerce_orders(
        cur,
        raw_content="[]",
        archive_dir=arch_dir,
        orders_list=[
            {
                "주문일자": "2026-03-15",
                "주문일시": "2026-03-15 14:00:00",
                "상품명": "개인 쇼핑 건",
                "결제금액": 28001,
                "상점명": "이니시스",
                "승인번호": "",
                "카드사": "5570-42XX-XXXX-6364 하나카드",
            },
            {
                "주문일자": "2026-03-15",
                "주문일시": "2026-03-15 14:00:00",
                "상품명": "국민카드 개인주문",
                "결제금액": 28001,
                "상점명": "이니시스",
                "승인번호": "",
                "카드사": "국민카드",
            },
        ],
    )
    assert res_skip["matched_count"] == 0
    assert res_skip["created_count"] == 0
    cur.execute("SELECT counterparty_info FROM card_transactions WHERE tx_id = ?", (corp_tx_id,))
    assert cur.fetchone()["counterparty_info"] is None

    # Polluted memo must not be locked by keep_existing even with NULL approval_no
    res_fix = reconcile_scraped_ecommerce_orders(
        cur,
        raw_content="[]",
        archive_dir=arch_dir,
        orders_list=[
            {
                "주문일자": "2026-02-28",
                "주문일시": "2026-02-28 17:58:00",
                "상품명": "[DIY] US 오리진 하프앤하프 외 1",
                "결제금액": 42500,
                "상점명": "피자헛 장안동점",
                "승인번호": "84294928",
                "카드사": "우리카드",
            }
        ],
    )
    conn.commit()
    assert res_fix["matched_count"] == 1
    assert polluted_tx_id in res_fix["updated_tx_ids"]
    cur.execute(
        """
        SELECT counterparty_info, approval_no, account_code, account_name
        FROM card_transactions
        WHERE tx_id = ?
        """,
        (polluted_tx_id,),
    )
    fixed_row = dict(cur.fetchone())
    assert fixed_row["approval_no"] == "84294928"
    assert fixed_row["account_code"] == "811"
    assert fixed_row["account_name"] == "복리후생비"
    assert "피자헛 장안동점" in str(fixed_row["counterparty_info"] or "")
    assert "US 오리진 하프앤하프" in str(fixed_row["counterparty_info"] or "")
    conn.close()


def test_coupang_email_delivery_group_subheaders_and_card_layout_parsing() -> None:
    """Verify Coupang emails with delivery sub-headers, arrival badges, and split Coupay Money parse cleanly."""
    html_with_subheaders = """<!DOCTYPE html>
<html><body>
  <table>
    <tr><th>주문번호</th><td>2100088776655</td></tr>
    <tr><th>주문일자</th><td>2026.10.26 11:32:10</td></tr>
  </table>
  <table>
    <tr>
      <th>구매 상세내역</th><th>쿠팡가</th><th>수량</th>
      <th>구매금액</th><th>배송정보</th><th>판매자</th>
    </tr>
    <tr><td colspan="6">로켓배송 상품</td></tr>
    <tr>
      <td>
        <div>탐사 3겹 순수 소프트 롤화장지 30m 30롤</div>
        <div>내일(화) 10/27 도착 보장</div>
        <div>무료반품</div>
      </td>
      <td><del>18,900원</del> 13,500원</td>
      <td>2개</td>
      <td>27,000원</td>
      <td>무료배송</td>
      <td>쿠팡(주)</td>
    </tr>
    <tr><td colspan="6">판매자배송 상품</td></tr>
    <tr>
      <td>
        <div>3M 스카치 다용도 포장테이프 6롤 세트</div>
        <div>모레(수) 도착 예정</div>
      </td>
      <td>16,900원</td>
      <td>1개</td>
      <td>16,900원</td>
      <td>무료배송</td>
      <td>오피스툴코리아</td>
    </tr>
  </table>
  <table>
    <tr><th>총 주문금액</th><td>43,900원</td></tr>
    <tr><th>쿠페이 머니</th><td>5,000원</td></tr>
    <tr><th>신용카드 결제금액</th><td>38,900원</td></tr>
    <tr><th>결제수단</th><td>우리카드 (2949) 일시불</td></tr>
    <tr><th>승인번호</th><td>44556677</td></tr>
  </table>
</body></html>"""

    orders = extract_orders_from_email_content(
        subject="[쿠팡] 주문이 완료되었습니다.",
        sender="쿠팡 <noreply@e.coupang.com>",
        date_str="Mon, 26 Oct 2026 11:32:15 +0900",
        body_html=html_with_subheaders,
    )
    assert len(orders) == 1
    cp0 = orders[0]
    assert cp0["발신처"] == "쿠팡"
    assert cp0["주문번호"] == "2100088776655"
    assert cp0["결제금액"] == 38900
    assert cp0["상품합계금액"] == 43900
    assert cp0["승인번호"] == "44556677"
    assert "2949" in str(cp0.get("카드사") or "")
    assert len(cp0["품목내역"]) == 2
    assert sum(int(x["price"]) for x in cp0["품목내역"]) == 38900
    assert "탐사 3겹 순수 소프트 롤화장지" in cp0["품목내역"][0]["item"]
    assert "도착 보장" not in cp0["품목내역"][0]["item"]

    # Modern div/card-layout Coupang email without classic <th>구매 상세내역</th>
    card_layout_html = """<!DOCTYPE html>
<html><body>
  <div>주문번호 2100099887766</div>
  <div>주문일 2026.11.12 09:15:00</div>
  <div>로켓프레시</div>
  <div>곰곰 무항생제 신선 대란 30구</div>
  <div>8,900원 · 2개</div>
  <div>곰곰 유기농 우유 900ml</div>
  <div>11,600원 · 1개</div>
  <div>신용카드 결제금액 : 29,400원</div>
  <div>결제카드 : 우리카드(1175)</div>
</body></html>"""
    card_orders = extract_orders_from_email_content(
        subject="[쿠팡] 주문이 완료되었습니다.",
        sender="쿠팡 <noreply@coupang.com>",
        date_str="Thu, 12 Nov 2026 09:15:10 +0900",
        body_html=card_layout_html,
    )
    assert len(card_orders) == 1
    assert card_orders[0]["결제금액"] == 29400
    assert "1175" in str(card_orders[0].get("카드사") or "")


def test_scroll_imap_coupang_emails_promotion_rescue_and_v2_rescan(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Verify Coupang IMAP scrolling rescues Promotion/Korean All-Mail folders and rescans legacy processed_ids."""
    db_file = tmp_path / "coupang_email_scroll.db"
    arch_dir = tmp_path / "archive"
    arch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SOLOLEDGER_DB_PATH", str(db_file))
    monkeypatch.setenv("SOLOLEDGER_ARCHIVE_DIR", str(arch_dir))

    init_db(str(db_file))
    save_corp_card_policy(
        {
            "corp_only_mode": True,
            "strict_registered_only": True,
            "block_unmatched_email_orders": True,
            "blocked_personal_cards": ["6534"],
        },
        archive_dir=arch_dir,
    )
    register_corp_card("2949", card_label="우리법인(2949)", issuer="WOORI", archive_dir=arch_dir)
    # Seed legacy watcher config where <cp-promo-1@example.com> was previously marked processed under v1
    save_email_watcher_config(
        {
            "provider": "NAVER",
            "imap_host": "imap.naver.com",
            "imap_port": 993,
            "email_address": "corp-naver@naver.com",
            "app_password": "dummy-password",
            "enabled": True,
            "processed_ids": ["<cp-promo-1@example.com>"],
        },
        archive_dir=arch_dir,
    )

    conn = get_connection(str(db_file))
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO merchants (biz_reg_no, merchant_name, tax_type_code, biz_status, is_pg_provider)
        VALUES ('1058765162', '쿠팡페이', 'GENERAL', 'ACTIVE', 1)
        """
    )
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '2949', '99112233', '2026-10-20 13:10:00', '쿠팡페이', '1058765162',
            31800, 28909, 2891, '999', '미분류', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    existing_cp_tx_id = int(cur.lastrowid)
    conn.commit()
    conn.close()

    cp_html_oct = (
        "<html><body><table>"
        "<tr><th>주문일자</th><td>2026.10.20 13:10:05</td></tr>"
        "<tr><th>구매 상세내역</th><th>쿠팡가</th><th>수량</th><th>구매금액</th><th>판매자</th></tr>"
        "<tr><td colspan='5'>로켓배송 상품</td></tr>"
        "<tr><td>A4 서류보관 파일박스 10개입</td><td>31,800원</td><td>1개</td>"
        "<td>31,800원</td><td>쿠팡(주)</td></tr>"
        "<tr><th>총 결제금액</th><td>31,800원</td></tr>"
        "<tr><th>결제수단</th><td>우리카드 (2949)</td></tr>"
        "</table></body></html>"
    )
    cp_html_nov_corp = (
        "<html><body><table>"
        "<tr><th>주문일자</th><td>2026.11.15 16:40:00</td></tr>"
        "<tr><th>구매 상세내역</th><th>쿠팡가</th><th>수량</th><th>구매금액</th><th>판매자</th></tr>"
        "<tr><td colspan='5'>로켓배송 상품</td></tr>"
        "<tr><td>사무실 멀티탭 6구 개별스위치 3m</td><td>24,500원</td><td>1개</td>"
        "<td>24,500원</td><td>쿠팡(주)</td></tr>"
        "<tr><th>총 결제금액</th><td>24,500원</td></tr>"
        "<tr><th>결제수단</th><td>우리카드 (2949)</td></tr>"
        "</table></body></html>"
    )
    cp_html_personal = (
        "<html><body><table>"
        "<tr><th>주문일자</th><td>2026.11.16 20:00:00</td></tr>"
        "<tr><th>구매 상세내역</th><th>쿠팡가</th><th>수량</th><th>구매금액</th><th>판매자</th></tr>"
        "<tr><td>개인 반려동물 간식 세트</td><td>19,900원</td><td>1개</td>"
        "<td>19,900원</td><td>펫스토어</td></tr>"
        "<tr><th>총 결제금액</th><td>19,900원</td></tr>"
        "<tr><th>결제수단</th><td>현대카드 (6534)</td></tr>"
        "</table></body></html>"
    )

    # Modified UTF-7 for "프로모션" -> "&1QS4XLqowVg-"
    promo_folder = "&1QS4XLqowVg-"

    class _FakeCoupangImapServer:
        def __init__(self) -> None:
            self.current_box = "INBOX"
            self.inbox_msgs: Dict[bytes, bytes] = {
                b"11": _build_simple_eml_bytes(
                    message_id="<cp-nov-corp@example.com>",
                    sender="쿠팡 <noreply@coupang.com>",
                    subject="[쿠팡] 주문이 완료되었습니다.",
                    date_rfc="Sun, 15 Nov 2026 16:40:10 +0900",
                    body_text="주문 안내",
                    body_html=cp_html_nov_corp,
                ),
                b"12": _build_simple_eml_bytes(
                    message_id="<cp-nov-pers@example.com>",
                    sender="쿠팡 <noreply@coupang.com>",
                    subject="[쿠팡] 주문이 완료되었습니다.",
                    date_rfc="Mon, 16 Nov 2026 20:00:10 +0900",
                    body_text="주문 안내",
                    body_html=cp_html_personal,
                ),
            }
            self.promo_msgs: Dict[bytes, bytes] = {
                b"21": _build_simple_eml_bytes(
                    message_id="<cp-promo-1@example.com>",
                    sender="쿠팡 <noreply@e.coupang.com>",
                    subject="[쿠팡] 주문이 완료되었습니다.",
                    date_rfc="Tue, 20 Oct 2026 13:10:15 +0900",
                    body_text="주문 안내",
                    body_html=cp_html_oct,
                ),
            }

        def login(self, user: str, password: str) -> tuple[str, List[bytes]]:
            return "OK", [b"Logged in"]

        def list(self) -> tuple[str, List[bytes]]:
            return (
                "OK",
                [
                    b'(\\HasNoChildren) "/" "INBOX"',
                    f'(\\HasNoChildren) "/" "{promo_folder}"'.encode("ascii"),
                ],
            )

        def select(self, mailbox: str = "INBOX", readonly: bool = True) -> tuple[str, List[bytes]]:
            self.current_box = mailbox.strip('"')
            return "OK", [b"2"]

        def uid(self, command: str, *args: Any) -> tuple[str, List[Any]]:
            cmd = command.upper()
            store = self.promo_msgs if self.current_box == promo_folder else self.inbox_msgs
            if cmd == "SEARCH":
                criteria_str = " ".join(str(a) for a in args)
                if "tosspayments" in criteria_str.lower():
                    return "OK", [b""]
                return "OK", [b" ".join(sorted(store.keys(), key=lambda x: int(x)))]
            if cmd == "FETCH":
                uid_csv = args[0]
                out: List[Any] = []
                for u in uid_csv.split(b","):
                    raw = store.get(u.strip())
                    if raw:
                        out.append(
                            (b"1 (UID " + u + b" RFC822 {" + str(len(raw)).encode() + b"})", raw)
                        )
                return "OK", out
            return "BAD", []

        def logout(self) -> tuple[str, List[bytes]]:
            return "BYE", [b"Logout"]

    fake_imap = _FakeCoupangImapServer()
    assert _discover_imap_promotion_mailboxes(fake_imap, ["INBOX"]) == [promo_folder]

    client = TestClient(app)
    with patch("sololedger.email_watcher.imaplib.IMAP4_SSL", return_value=fake_imap):
        # 1) Dedicated Coupang email scroll endpoint
        scroll_res = client.post(
            "/api/v1/scraper/email-scroll-coupang",
            headers={"X-API-Key": settings.api_key},
            json={"start_date": "2026-01-01", "end_date": "2026-12-31"},
        )
        assert scroll_res.status_code == 200
        s_json = scroll_res.json()
        assert s_json["status"] == "scrolled"
        assert s_json["parsed_coupang_count"] == 3
        # 1 existing Oct row + 1 newly created Nov corp row (personal skipped)
        assert s_json["matched_count"] == 2
        assert s_json["created_count"] == 1

        # 2) Unified email-sync also upgrades coupang_parser_version to 2 without duplicating rows
        sync_res = client.post(
            "/api/v1/scraper/email-sync",
            headers={"X-API-Key": settings.api_key},
            json={"lookback_days": 365},
        )
        assert sync_res.status_code == 200
        assert load_email_watcher_config(arch_dir).get("coupang_parser_version") == 2

    conn2 = get_connection(str(db_file))
    cur2 = conn2.cursor()
    cur2.execute("SELECT * FROM card_transactions WHERE tx_id = ?", (existing_cp_tx_id,))
    oct_row = dict(cur2.fetchone())
    assert "A4 서류보관 파일박스" in str(oct_row["counterparty_info"] or "")
    # Upgraded from Coupay PG 1058765162 to Coupang direct 1208800767
    assert oct_row["biz_reg_no"] == "1208800767"
    cur2.execute("SELECT COUNT(*) AS cnt FROM card_transactions")
    assert int(cur2.fetchone()["cnt"]) == 2
    conn2.close()

    dash_html = client.get("/dashboard", headers={"X-API-Key": settings.api_key}).text
    assert "coupang-scroll-panel" in dash_html
    assert "triggerCoupangEmailScroll" in dash_html
    assert "runCoupangBackupRematch" in dash_html


def test_coupang_ssr_dedup_discount_shipping_wow_and_offline_protection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Verify SSR dedup, discount/shipping tolerance, Wow Membership, offline protection, and backup API."""
    db_file = tmp_path / "coupang_reconcile.db"
    arch_dir = tmp_path / "archive"
    arch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SOLOLEDGER_DB_PATH", str(db_file))
    monkeypatch.setenv("SOLOLEDGER_ARCHIVE_DIR", str(arch_dir))

    init_db(str(db_file))
    save_corp_card_policy(
        {
            "corp_only_mode": True,
            "strict_registered_only": True,
            "block_unmatched_email_orders": True,
            "blocked_personal_cards": ["6534"],
        },
        archive_dir=arch_dir,
    )
    register_corp_card("2949", card_label="우리법인(2949)", issuer="WOORI", archive_dir=arch_dir)

    # 1. Verify SSR JSON recursive deduplication & unitPrice * quantity math
    ssr_payload = {
        "orders": [
            {
                "orderId": "202610090001",
                "orderedAt": 1791510000000,  # 2026-10-09 KST
                "orderDate": "2026-10-09",
                "deliveryGroupList": [
                    {
                        "productList": [
                            {
                                "productName": "스카치 초강력 다용도 테이프 리필",
                                "unitPrice": 8330,
                                "quantity": 3,
                                # Nested child mirror that previously caused duplicate extraction
                                "subItem": {
                                    "productName": "스카치 초강력 다용도 테이프 리필",
                                    "unitPrice": 8330,
                                    "quantity": 3,
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }
    extracted_ssr = extract_coupang_orders_from_content(json.dumps(ssr_payload, ensure_ascii=False))
    assert len(extracted_ssr) == 1
    assert int(extracted_ssr[0]["결제금액"]) == 24990
    assert len(extracted_ssr[0]["품목내역"]) == 1

    conn = get_connection(str(db_file))
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO merchants (biz_reg_no, merchant_name, tax_type_code, biz_status, is_pg_provider)
        VALUES
            ('1208800767', '쿠팡(주)', 'GENERAL', 'ACTIVE', 1),
            ('1058765162', '쿠팡페이', 'GENERAL', 'ACTIVE', 1),
            ('1138600000', '주식회사 케이씨피', 'GENERAL', 'ACTIVE', 1),
            ('5120991145', '문경청과', 'GENERAL', 'ACTIVE', 0)
        """
    )
    # Seed 5 realistic ledger transactions:
    # (A) Multi-item Coupang order with coupon/Coupang-Cash discount (list 116,170 -> billed 101,610)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '2949', '10000001', '2026-10-26 15:00:00', '쿠팡페이', '1058765162',
            101610, 92373, 9237, '999', '미분류', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    tx_discount_multi = int(cur.lastrowid)

    # (B) Multi-qty single-item Coupang order with instant discount (8,330 * 3 = 24,990 -> billed 23,360)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '2949', '10000002', '2026-10-09 12:00:00', '쿠팡(주)', '1208800767',
            23360, 21236, 2124, '999', '미분류', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    tx_discount_qty = int(cur.lastrowid)

    # (C) Seller-delivery item + 3,500 KRW shipping fee via KCP (item 9,000 -> billed 12,500)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '2949', '10000003', '2026-02-25 10:00:00', '주식회사 케이씨피', '1138600000',
            12500, 11364, 1136, '999', '미분류', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    tx_shipping_kcp = int(cur.lastrowid)

    # (D) Offline fruit shop '문경청과' (10,000 KRW) previously polluted by a 10,000 KRW Coupang sub-item
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            counterparty_info, total_amount, supply_value, vat_amount,
            account_code, account_name, vat_deduction_type, reconcile_status
        ) VALUES (
            '2949', '10000004', '2026-10-26 17:30:00', '문경청과', '5120991145',
            '문풍지 외풍차단 틈막이 10m', 10000, 9091, 909,
            '830', '소모품비', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    tx_offline_fruit = int(cur.lastrowid)
    cur.execute(
        """
        INSERT INTO receipt_ocr_metadata (
            tx_id, source_channel, original_file_path, archived_file_name,
            file_hash_sha256, ocr_total_amount, line_items_json
        ) VALUES (?, 'STATEMENT', 'bad_fruit.html', 'bad_fruit.html', 'badhash1', 10000, '[]')
        """,
        (tx_offline_fruit,),
    )

    # (E) Coupang Wow Membership monthly fee (7,890 KRW)
    cur.execute(
        """
        INSERT INTO card_transactions (
            card_last4, approval_no, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '2949', '10000005', '2026-10-05 08:15:00', '쿠팡페이', '1058765162',
            7890, 7173, 717, '999', '미분류', 'DEDUCTIBLE', 'HOMETAX_VERIFIED'
        )
        """
    )
    tx_wow = int(cur.lastrowid)
    conn.commit()

    backup_orders = [
        # Summary order on 2026-10-26 (sum 116,170 -> matches tx_discount_multi 101,610)
        {
            "주문일자": "2026-10-26",
            "상품명": "사무용 의자 메쉬 방석 외 2건",
            "결제금액": 116170,
            "발신처": "쿠팡",
            "품목내역": [
                {"item": "사무용 의자 메쉬 방석", "qty": 1, "price": 56170},
                {"item": "듀얼모니터 받침대 선반", "qty": 1, "price": 50000},
                {"item": "문풍지 외풍차단 틈막이 10m", "qty": 1, "price": 10000},
            ],
        },
        # Legacy standalone sub-item on 2026-10-26 (10,000 KRW) -> must NOT match 문경청과, and must self-heal it!
        {
            "주문일자": "2026-10-26",
            "상품명": "문풍지 외풍차단 틈막이 10m",
            "결제금액": 10000,
            "발신처": "쿠팡",
            "품목내역": [{"item": "문풍지 외풍차단 틈막이 10m", "qty": 1, "price": 10000}],
        },
        # Legacy unitPrice entry with qty=3 on 2026-10-09 (8,330 * 3 = 24,990 -> matches tx_discount_qty 23,360)
        {
            "주문일자": "2026-10-09",
            "상품명": "스카치 초강력 다용도 테이프 리필",
            "결제금액": 8330,
            "품목내역": [{"item": "스카치 초강력 다용도 테이프 리필", "qty": 3, "price": 8330}],
        },
        # Seller-delivery order on 2026-02-25 (9,000 + 3,500 shipping -> matches tx_shipping_kcp 12,500)
        {
            "주문일자": "2026-02-25",
            "상품명": "산업용 안전 장갑 10켤레",
            "결제금액": 9000,
            "발신처": "쿠팡",
            "입점판매자": "안전공구상사",
            "품목내역": [{"item": "산업용 안전 장갑 10켤레", "qty": 1, "price": 9000}],
        },
        # Post-statement Nov 2026 order paid with registered corp card 2949 -> auto-created!
        {
            "주문일자": "2026-11-18",
            "상품명": "복사용지 A4 80g 2500매",
            "결제금액": 26900,
            "발신처": "쿠팡",
            "사업자번호": "120-88-00767",
            "카드사": "우리카드 (2949)",
            "품목내역": [{"item": "복사용지 A4 80g 2500매", "qty": 1, "price": 26900}],
        },
        # Post-statement Nov 2026 order without verified corp card -> must NOT be auto-created under policy!
        {
            "주문일자": "2026-11-19",
            "상품명": "미검증 카드 주문 건",
            "결제금액": 15400,
            "발신처": "쿠팡",
            "품목내역": [{"item": "미검증 카드 주문 건", "qty": 1, "price": 15400}],
        },
    ]
    backup_file = arch_dir / "coupang_orders_auto.json"
    backup_file.write_text(json.dumps(backup_orders, ensure_ascii=False), encoding="utf-8")
    conn.close()

    client = TestClient(app)
    rec_resp = client.post(
        "/api/v1/scraper/coupang-reconcile-backup",
        headers={"X-API-Key": settings.api_key},
    )
    assert rec_resp.status_code == 200
    rec_json = rec_resp.json()
    assert rec_json["status"] == "ingested"
    # 3 fuzzy/shipping + 1 post-statement corp card created + 1 Wow Membership = 5 matched!
    assert rec_json["matched_orders"] == 5
    assert rec_json["wow_membership_matched"] == 1
    assert rec_json["created_orders"] == 1

    # Verify dynamic server origin injection in coupang-script endpoint
    script_resp = client.get(
        "/api/v1/scraper/coupang-script",
        headers={"X-API-Key": settings.api_key},
    )
    assert script_resp.status_code == 200
    assert "/ssr/api/myorders/model/order/" in script_resp.json()["script"]

    conn_v = get_connection(str(db_file))
    cur_v = conn_v.cursor()

    # Check (A): matched and line items prorated to 101,610
    cur_v.execute("SELECT counterparty_info FROM card_transactions WHERE tx_id = ?", (tx_discount_multi,))
    assert "사무용 의자 메쉬 방석" in str(cur_v.fetchone()["counterparty_info"] or "")
    cur_v.execute(
        "SELECT line_items_json FROM receipt_ocr_metadata WHERE tx_id = ? AND source_channel = 'STATEMENT'",
        (tx_discount_multi,),
    )
    li_multi = json.loads(str(cur_v.fetchone()["line_items_json"]))
    assert sum(int(x["price"]) for x in li_multi) == 101610

    # Check (B): 8,330 * 3 matched 23,360
    cur_v.execute("SELECT counterparty_info FROM card_transactions WHERE tx_id = ?", (tx_discount_qty,))
    assert "스카치 초강력 다용도 테이프" in str(cur_v.fetchone()["counterparty_info"] or "")

    # Check (C): 9,000 + 3,500 shipping matched 12,500 KCP row
    cur_v.execute("SELECT counterparty_info FROM card_transactions WHERE tx_id = ?", (tx_shipping_kcp,))
    assert "산업용 안전 장갑" in str(cur_v.fetchone()["counterparty_info"] or "")

    # Check (D): Offline fruit store 문경청과 self-healed to NULL / 999 미분류
    cur_v.execute(
        "SELECT counterparty_info, account_code FROM card_transactions WHERE tx_id = ?",
        (tx_offline_fruit,),
    )
    fruit_row = dict(cur_v.fetchone())
    assert fruit_row["counterparty_info"] is None
    assert fruit_row["account_code"] == "999"

    # Check (E): Wow Membership stated & biz_reg_no upgraded to 1208800767
    cur_v.execute(
        "SELECT counterparty_info, biz_reg_no FROM card_transactions WHERE tx_id = ?",
        (tx_wow,),
    )
    wow_row = dict(cur_v.fetchone())
    assert "와우 멤버십" in str(wow_row["counterparty_info"] or "")
    assert wow_row["biz_reg_no"] == "1208800767"

    # Also verify direct ingest_scraped_payload idempotency
    conn_v.close()
    conn_i = get_connection(str(db_file))
    cur_i = conn_i.cursor()
    ing_again = ingest_scraped_payload(
        cur_i,
        target_portal="COUPANG",
        raw_content=backup_file.read_text(encoding="utf-8"),
        archive_dir=arch_dir,
    )
    conn_i.commit()
    conn_i.close()
    assert ing_again["created_orders"] == 0



