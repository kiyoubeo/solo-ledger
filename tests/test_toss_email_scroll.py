"""Unit and integration tests for Toss Payments corporate card email scrolling and reconciliation."""

from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from fastapi.testclient import TestClient

from sololedger.api import app
from sololedger.config import settings
from sololedger.db import get_connection, init_db
from sololedger.email_watcher import (
    _extract_eml_parts,
    classify_toss_card_ownership,
    extract_orders_from_email_content,
    load_email_watcher_config,
    parse_toss_payments_email,
    save_email_watcher_config,
    scroll_imap_toss_payments_emails,
)
from sololedger.sms_parser import (
    is_blocked_personal_card_code,
    match_card_code_pattern,
    match_registered_corp_card_code,
    register_corp_card,
    save_corp_card_policy,
)


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

