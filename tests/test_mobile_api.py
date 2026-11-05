"""Unit and integration tests for Phase 6 Mobile Auth, Woori Push Webhook, and Remote Tax Pack Download."""

import io
import zipfile


def test_mobile_auth_login_and_bearer_token_access(client, monkeypatch):
    """Verify POST /api/v1/auth/login issues a valid Bearer token accepted across endpoints."""
    monkeypatch.setenv("SOLOLEDGER_ADMIN_PASSWORD", "my-corp-pass-2026")
    monkeypatch.setenv("SOLOLEDGER_AUTH_REQUIRED", "1")

    # 1. Unauthenticated access to protected read endpoints is blocked when AUTH_REQUIRED=1
    dash_unauth = client.get("/dashboard", follow_redirects=False)
    assert dash_unauth.status_code == 303
    assert dash_unauth.headers["location"] == "/login"

    cfg_unauth = client.get("/api/v1/scraper/email-config")
    assert cfg_unauth.status_code == 401

    # 2. Invalid credential fails with 401
    bad_login = client.post(
        "/api/v1/auth/login",
        json={"credential": "wrong-password", "device_name": "Galaxy S24"},
    )
    assert bad_login.status_code == 401

    # 3. Valid admin password succeeds and returns signed sl1 Bearer token
    ok_login = client.post(
        "/api/v1/auth/login",
        json={"credential": "my-corp-pass-2026", "device_name": "Galaxy S24"},
    )
    assert ok_login.status_code == 200
    body = ok_login.json()
    assert body["status"] == "authenticated"
    assert body["token"].startswith("sl1.")
    assert "6534" in body["card_labels"]

    bearer_headers = {"Authorization": f"Bearer {body['token']}"}
    summary_res = client.get("/api/v1/dashboard/summary", headers=bearer_headers)
    assert summary_res.status_code == 200

    dash_auth = client.get("/dashboard", headers=bearer_headers)
    assert dash_auth.status_code == 200

    # 4. Web form login sets session cookie for browser access
    web_login = client.post(
        "/login",
        content="credential=my-corp-pass-2026",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert web_login.status_code == 303
    assert "sololedger_session=" in web_login.headers.get("set-cookie", "")


def test_woori_card_push_webhook_and_ad_filtering(client):
    """Verify Woori WON Card payment pushes record cleanly while ads/statements are ignored."""
    headers = {"X-API-Key": "test-secret-key"}

    # 1. Promotional push with KRW amount must be ignored, NOT recorded
    ad_res = client.post(
        "/api/v1/webhooks/push",
        headers=headers,
        json={
            "package_name": "com.wooricard.smartapp",
            "title": "[광고] 우리WON카드 특별 이벤트",
            "body": "지금 이벤트 응모 시 최대 30,000원 캐시백 혜택 증정! 수신거부 080-000-0000",
            "received_at": "2026-11-05T10:00:00",
        },
    )
    assert ad_res.status_code == 200
    assert ad_res.json()["status"] == "ignored"

    # 2. Monthly statement arrival push with KRW amount must also be ignored
    stmt_res = client.post(
        "/api/v1/webhooks/push",
        headers=headers,
        json={
            "package_name": "com.wooricard.smartapp",
            "title": "[우리카드] 이용대금명세서 안내",
            "body": "11월 결제예정금액 1,450,000원 명세서가 도착했습니다.",
            "received_at": "2026-11-05T10:05:00",
        },
    )
    assert stmt_res.status_code == 200
    assert stmt_res.json()["status"] == "ignored"

    # 3. Real Woori corporate card approval push records properly
    push_res = client.post(
        "/api/v1/webhooks/push",
        headers=headers,
        json={
            "package_name": "com.wooricard.smartapp",
            "title": "[우리카드] 승인알림",
            "body": "우리(6534)승인\n김*범님\n18,500원 일시불\n11/05 12:30\n스타벅스 여의도역점\n누적 420,000원",
            "received_at": "2026-11-05T12:30:05",
        },
    )
    assert push_res.status_code == 200
    recorded = push_res.json()
    assert recorded["status"] == "recorded"
    assert recorded["card_last4"] == "6534"
    assert recorded["merchant"] == "스타벅스 여의도역점"
    assert recorded["amount"] == 18500
    assert recorded["account_code"] == "811"

    # 4. Duplicate push delivery is idempotently skipped
    dup_res = client.post(
        "/api/v1/webhooks/push",
        headers=headers,
        json={
            "package_name": "com.wooricard.smartapp",
            "title": "[우리카드] 승인알림",
            "body": "우리(6534)승인\n김*범님\n18,500원 일시불\n11/05 12:30\n스타벅스 여의도역점\n누적 420,000원",
            "received_at": "2026-11-05T12:30:05",
        },
    )
    assert dup_res.status_code == 200
    assert dup_res.json()["status"] == "duplicate_skipped"

    # 5. Push without explicit card digits falls back to card_hint
    hint_res = client.post(
        "/api/v1/webhooks/push",
        headers=headers,
        json={
            "package_name": "com.wooricard.smartapp",
            "title": "우리WON카드 승인알림",
            "body": "일시불 9,900원 승인 11/05 15:10 교보문고 광화문점",
            "received_at": "2026-11-05T15:10:00",
            "card_hint": "6534",
        },
    )
    assert hint_res.status_code == 200
    assert hint_res.json()["card_last4"] == "6534"
    assert hint_res.json()["merchant"] == "교보문고 광화문점"


def test_remote_tax_pack_zip_and_csv_download(client):
    """Verify GET /api/v1/exports/tax-pack/{year_month}/download streams valid ZIP & CSV."""
    headers = {"X-API-Key": "test-secret-key"}

    sms_res = client.post(
        "/api/v1/webhooks/sms",
        headers=headers,
        json={
            "sender": "15889955",
            "raw_message": "[Web발신] 우리(6534) 승인 33,000원 일시불 11/05 13:00 다이소영등포점 승인번호:55443322",
            "received_at": "2026-11-05T13:00:00",
        },
    )
    assert sms_res.status_code == 200
    tx_id = sms_res.json()["tx_id"]

    # Generate electronic statement so an HTML receipt exists inside the ZIP
    stmt_res = client.post(
        f"/api/v1/transactions/{tx_id}/statement",
        headers=headers,
        json={"item_name": "사무용 건전지 및 케이블 정리함"},
    )
    assert stmt_res.status_code == 200

    # Verify HTML receipt file includes strict CSP sandbox header
    file_res = client.get(f"/api/v1/transactions/{tx_id}/receipt-file")
    assert file_res.status_code == 200
    assert "sandbox" in file_res.headers.get("content-security-policy", "")

    # Invalid year_month is rejected with 400
    bad_ym = client.get("/api/v1/exports/tax-pack/2026-11/download", headers=headers)
    assert bad_ym.status_code == 400

    # Download ZIP directly (auto-generates pack on demand)
    zip_res = client.get("/api/v1/exports/tax-pack/202611/download", headers=headers)
    assert zip_res.status_code == 200
    assert zip_res.headers["content-type"] == "application/zip"
    assert "attachment;" in zip_res.headers.get("content-disposition", "")

    with zipfile.ZipFile(io.BytesIO(zip_res.content), "r") as zf:
        names = zf.namelist()
        assert "SoloLedger_기장원장_202611.csv" in names
        assert any(n.startswith("receipts/") for n in names)
        csv_text = zf.read("SoloLedger_기장원장_202611.csv").decode("utf-8-sig")
        assert "다이소영등포점" in csv_text
        assert "사무용 건전지 및 케이블 정리함" in csv_text

    # Download standalone CSV
    csv_res = client.get("/api/v1/exports/tax-pack/202611/csv", headers=headers)
    assert csv_res.status_code == 200
    assert "다이소영등포점" in csv_res.content.decode("utf-8-sig")


def test_production_destructive_and_unc_guards(client, monkeypatch):
    """Verify reset-test, seed-demo, and arbitrary UNC NAS paths are blocked in production."""
    headers = {"X-API-Key": "test-secret-key"}
    monkeypatch.setenv("SOLOLEDGER_ENV", "production")

    reset_res = client.post("/api/v1/system/reset-test", headers=headers)
    assert reset_res.status_code == 403

    seed_res = client.post("/api/v1/system/seed-demo", headers=headers, json={})
    assert seed_res.status_code == 403

    unc_res = client.post(
        "/api/v1/system/backup-nas",
        headers=headers,
        json={"nas_dir": r"\\198.51.100.99\share\exfil"},
    )
    assert unc_res.status_code == 400


def test_download_android_apk_endpoint(client, tmp_path, monkeypatch):
    """Verify GET /download/apk serves the compiled Android package when present."""
    from sololedger import api as api_mod

    fake_root = tmp_path / "repo"
    dist_dir = fake_root / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "SoloLedger-mobile.apk").write_bytes(b"PK\x03\x04FAKE-APK-BINARY")
    monkeypatch.setattr(api_mod, "PROJECT_ROOT", fake_root)

    res = client.get("/download/apk")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/vnd.android.package-archive"
    assert res.content.startswith(b"PK\x03\x04")
