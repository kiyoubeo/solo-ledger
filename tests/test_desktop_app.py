"""Unit and integration tests for Standalone Windows Desktop Application (Phase 7)."""

import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from sololedger.api import _create_session_token, app
from sololedger.cli import run_cli
from sololedger.config import (
    _resolve_bundle_root,
    _resolve_project_root,
    ensure_loopback_no_proxy,
)
from sololedger.desktop_app import (
    EmbeddedServerController,
    build_desktop_start_url,
    enable_webview_accelerator_keys,
    ensure_safe_stdio,
    find_free_port,
    is_port_available,
    is_sololedger_running,
    negotiate_server_endpoint,
    register_webview_window_hooks,
    resolve_app_icon_path,
    run_desktop_app,
)


def test_frozen_path_resolution_onefile_in_repo_dist(tmp_path: Path) -> None:
    repo = tmp_path / "SoloLedger"
    dist_dir = repo / "dist"
    meipass_dir = tmp_path / "_MEI99999"
    dist_dir.mkdir(parents=True)
    meipass_dir.mkdir(parents=True)
    (repo / ".env").write_text("SOLOLEDGER_ENV=development\n", encoding="utf-8")
    exe_file = dist_dir / "SoloLedger.exe"
    exe_file.write_bytes(b"MZ")

    resolved_root = _resolve_project_root(home_env="", frozen=True, executable=str(exe_file))
    resolved_bundle = _resolve_bundle_root(
        frozen=True, meipass=str(meipass_dir), executable=str(exe_file)
    )
    assert resolved_root == repo.resolve()
    assert resolved_bundle == meipass_dir.resolve()


def test_frozen_path_resolution_onedir_and_standalone_isolation(tmp_path: Path) -> None:
    # 1. --onedir inside repo/dist/SoloLedger/SoloLedger.exe
    repo = tmp_path / "RepoRoot"
    onedir = repo / "dist" / "SoloLedger"
    onedir.mkdir(parents=True)
    (repo / "sololedger_prod.db").write_bytes(b"")
    onedir_exe = onedir / "SoloLedger.exe"
    onedir_exe.write_bytes(b"MZ")
    assert _resolve_project_root(home_env="", frozen=True, executable=str(onedir_exe)) == repo.resolve()

    # 2. Standalone folder inside user home where parent has an unrelated .env
    user_dir = tmp_path / "UserHome"
    portable_dir = user_dir / "MyLedgerApp"
    portable_dir.mkdir(parents=True)
    (user_dir / ".env").write_text("UNRELATED=1\n", encoding="utf-8")
    portable_exe = portable_dir / "SoloLedger.exe"
    portable_exe.write_bytes(b"MZ")
    assert (
        _resolve_project_root(home_env="", frozen=True, executable=str(portable_exe))
        == portable_dir.resolve()
    )

    # 3. Explicit SOLOLEDGER_HOME override
    custom_home = tmp_path / "CustomWorkspace"
    custom_home.mkdir()
    assert (
        _resolve_project_root(home_env=str(custom_home), frozen=True, executable=str(portable_exe))
        == custom_home.resolve()
    )


def test_ensure_safe_stdio_attaches_log_when_windowless(tmp_path: Path, monkeypatch) -> None:
    orig_out, orig_err = sys.stdout, sys.stderr
    created_streams = []
    try:
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        log_path = ensure_safe_stdio(log_dir=tmp_path)
        assert log_path is not None
        assert log_path.name == "desktop_app.log"
        assert sys.stdout is not None
        assert sys.stderr is not None
        created_streams.append(sys.stdout)
        print("windowless log test")
        sys.stdout.flush()
        assert "windowless log test" in log_path.read_text(encoding="utf-8")
    finally:
        monkeypatch.setattr(sys, "stdout", orig_out)
        monkeypatch.setattr(sys, "stderr", orig_err)
        for st in created_streams:
            if hasattr(st, "close"):
                st.close()


def test_desktop_launch_endpoint_sets_cookie_and_guards_security(monkeypatch) -> None:
    monkeypatch.setenv("SOLOLEDGER_AUTH_REQUIRED", "true")
    monkeypatch.setenv("SOLOLEDGER_ADMIN_PASSWORD", "corp-pass-9988")

    client = TestClient(app)

    # Unauthenticated /dashboard redirects to /login when auth is required
    unauth = client.get("/dashboard", follow_redirects=False)
    assert unauth.status_code == 303
    assert unauth.headers["location"] == "/login"

    # Invalid token rejected with 401
    bad_resp = client.get("/desktop-launch?token=invalid-token", follow_redirects=False)
    assert bad_resp.status_code == 401

    # Valid signed token sets cookie and redirects safely (blocking external //evil redirects)
    valid_token = _create_session_token()
    evil_redirect = client.get(
        f"/desktop-launch?token={valid_token}&next_path=//evil.example.com",
        follow_redirects=False,
    )
    assert evil_redirect.status_code == 303
    assert evil_redirect.headers["location"] == "/dashboard"
    assert "sololedger_session" in evil_redirect.cookies

    # Subsequent /dashboard request with session cookie succeeds with 200 OK
    dash_ok = client.get("/dashboard")
    assert dash_ok.status_code == 200
    assert "SoloLedger" in dash_ok.text


def test_embedded_server_lifecycle_and_port_negotiation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SOLOLEDGER_AUTH_REQUIRED", "true")
    free_port = find_free_port("127.0.0.1")

    # Start embedded controller and verify reuse detection
    controller = EmbeddedServerController(bind_host="127.0.0.1", port=free_port)
    try:
        base_url = controller.start(timeout=10.0)
        assert base_url == f"http://127.0.0.1:{free_port}"
        assert is_sololedger_running(free_port) is True

        reused = negotiate_server_endpoint(preferred_port=free_port, bind_host="127.0.0.1")
        assert reused["mode"] == "reuse_existing"
        assert reused["port"] == free_port

        start_url = build_desktop_start_url(base_url, "/dashboard")
        assert "/desktop-launch?token=" in start_url
    finally:
        controller.stop(timeout=5.0)

    assert controller.is_alive is False

    # Verify fallback port allocation when preferred port is held by a non-SoloLedger socket
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    occupied_port = int(blocker.getsockname()[1])
    try:
        negotiated = negotiate_server_endpoint(preferred_port=occupied_port, bind_host="127.0.0.1")
        assert negotiated["mode"] == "start_embedded"
        assert negotiated["port"] != occupied_port
    finally:
        blocker.close()

    # Verify headless check_only end-to-end via CLI & check_output JSON
    out_json = tmp_path / "check.json"
    check_port = find_free_port("127.0.0.1")
    summary = run_desktop_app(
        host="127.0.0.1",
        port=check_port,
        check_only=True,
        check_output=out_json,
    )
    assert summary["status"] == "ok"
    assert summary["dashboard_status"] == 200
    saved = json.loads(out_json.read_text(encoding="utf-8"))
    assert saved["status"] == "ok"

    cli_res = run_cli(["desktop", "--host", "127.0.0.1", "--port", str(find_free_port()), "--check-only"])
    assert cli_res["command"] == "desktop"
    assert cli_res["status"] == "ok"


def test_embedded_server_bypasses_http_proxy_and_detects_wildcard_port_conflict(
    tmp_path: Path, monkeypatch
) -> None:
    """Ensure HTTP_PROXY without NO_PROXY never blocks local /health readiness or wildcard port checks."""
    # 1. Simulate dead/external system HTTP_PROXY with missing NO_PROXY
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    port = find_free_port("127.0.0.1")
    out_json = tmp_path / "proxy_check.json"
    summary = run_desktop_app(
        host="127.0.0.1",
        port=port,
        check_only=True,
        check_output=out_json,
    )
    assert summary["status"] == "ok"
    assert summary["health_status"] == 200
    assert summary["dashboard_status"] == 200

    no_proxy_val = ensure_loopback_no_proxy()
    assert "127.0.0.1" in no_proxy_val
    assert "localhost" in no_proxy_val

    # 2. Wildcard 0.0.0.0 listener must be detected as unavailable even when probing default 0.0.0.0 bind_host
    wildcard_blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    wildcard_blocker.bind(("0.0.0.0", 0))
    wildcard_blocker.listen(1)
    wildcard_port = int(wildcard_blocker.getsockname()[1])
    try:
        assert is_port_available(wildcard_port, host="0.0.0.0") is False
        negotiated = negotiate_server_endpoint(preferred_port=wildcard_port, bind_host="0.0.0.0")
        assert negotiated["mode"] == "start_embedded"
        assert negotiated["port"] != wildcard_port
    finally:
        wildcard_blocker.close()

    # 3. Concurrent port-bind race recovery falls back cleanly to a free ephemeral port
    orig_start = EmbeddedServerController.start
    attempts = {"count": 0}

    def flaky_first_start(self: EmbeddedServerController, timeout: float = 15.0) -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError(f"Simulated bind race on port {self.port}")
        return orig_start(self, timeout=timeout)

    monkeypatch.setattr(EmbeddedServerController, "start", flaky_first_start)
    race_port = find_free_port("127.0.0.1")
    race_summary = run_desktop_app(host="127.0.0.1", port=race_port, check_only=True)
    assert attempts["count"] == 2
    assert race_summary["status"] == "ok"


def test_desktop_gui_window_invocation_and_cleanup(monkeypatch) -> None:
    fake_settings: dict = {}
    create_window_mock = MagicMock()
    start_mock = MagicMock()
    fake_webview = SimpleNamespace(
        settings=fake_settings,
        create_window=create_window_mock,
        start=start_mock,
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    port = find_free_port("127.0.0.1")
    result = run_desktop_app(host="127.0.0.1", port=port, width=1400, height=900, check_only=False)

    assert result["status"] == "closed"
    assert result["mode"] == "start_embedded"
    assert fake_settings["ALLOW_DOWNLOADS"] is True
    assert fake_settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is True
    create_window_mock.assert_called_once()
    win_kwargs = create_window_mock.call_args.kwargs
    assert win_kwargs["width"] == 1400
    assert win_kwargs["height"] == 900
    assert win_kwargs["text_select"] is True
    assert win_kwargs["zoomable"] is True
    start_mock.assert_called_once()
    assert resolve_app_icon_path() is not None
    # Server must be stopped after window closes
    assert is_sololedger_running(port, timeout=0.3) is False


def test_webview_accelerator_keys_hook_enables_ctrl_f_direct_and_invoke() -> None:
    # 1. Missing native window safely returns False
    empty_win = SimpleNamespace(native=None)
    assert enable_webview_accelerator_keys(empty_win) is False

    # 2. Direct UI thread execution viaregister_webview_window_hooks
    class FakeEvent:
        def __init__(self) -> None:
            self.callbacks = []

        def __iadd__(self, cb):
            self.callbacks.append(cb)
            return self

        def fire(self) -> None:
            for cb in self.callbacks:
                cb()

    wv_settings = SimpleNamespace(AreBrowserAcceleratorKeysEnabled=False)
    webview_ctrl = SimpleNamespace(
        InvokeRequired=False,
        CoreWebView2=SimpleNamespace(Settings=wv_settings),
    )
    shown_ev = FakeEvent()
    loaded_ev = FakeEvent()
    win = SimpleNamespace(
        native=SimpleNamespace(webview=webview_ctrl),
        events=SimpleNamespace(shown=shown_ev, loaded=loaded_ev),
    )
    register_webview_window_hooks(win)
    assert len(shown_ev.callbacks) == 1
    assert len(loaded_ev.callbacks) == 1
    loaded_ev.fire()
    assert wv_settings.AreBrowserAcceleratorKeysEnabled is True

    # 3. Cross-thread InvokeRequired=True marshaling + browser.webview fallback
    invoked_settings = SimpleNamespace(AreBrowserAcceleratorKeysEnabled=False)
    invoke_calls = []

    def fake_invoke(delegate_fn) -> None:
        invoke_calls.append(delegate_fn)
        delegate_fn()

    fallback_webview_ctrl = SimpleNamespace(
        InvokeRequired=True,
        Invoke=fake_invoke,
        CoreWebView2=SimpleNamespace(Settings=invoked_settings),
    )
    win_invoke = SimpleNamespace(
        native=SimpleNamespace(
            webview=None,
            browser=SimpleNamespace(webview=fallback_webview_ctrl),
        )
    )
    assert enable_webview_accelerator_keys(win_invoke) is True
    assert len(invoke_calls) == 1
    assert invoked_settings.AreBrowserAcceleratorKeysEnabled is True


def test_dashboard_and_manual_ctrl_f_search_bar_and_ledger_filter() -> None:
    client = TestClient(app)

    # Seed demo rows so ledger tables emit data-total-amount attributes
    seed_resp = client.post("/api/v1/system/seed-demo", headers={"X-API-Key": "dev-secret-key"})
    assert seed_resp.status_code in (200, 401)
    if seed_resp.status_code == 401:
        from sololedger.config import settings as cur_settings

        client.post("/api/v1/system/seed-demo", headers={"X-API-Key": cur_settings.api_key})

    dash_resp = client.get("/dashboard?year_month=ALL")
    assert dash_resp.status_code == 200
    html_text = dash_resp.text

    # Verify Ctrl+F floating search bar, toolbar button, filter toggle, and row attributes
    assert 'id="ctrlf-search-bar"' in html_text
    assert 'id="ctrlf-search-input"' in html_text
    assert 'id="ctrlf-match-count"' in html_text
    assert 'id="ctrlf-filter-rows-chk"' in html_text
    assert 'id="ctrlf-filter-summary"' in html_text
    assert "🔎 빠른 검색 (Ctrl+F)" in html_text
    assert 'id="ledger-search-filter-badge"' in html_text
    assert 'id="btn-clear-search-filter"' in html_text
    assert 'id="pending-ledger-tbody"' in html_text
    assert "data-total-amount=" in html_text

    # Verify JS functions and keyboard shortcuts (Ctrl+F, F3, Escape) rendered cleanly
    for expected_token in (
        "function getActiveStickyTopOffset(",
        "function getCtrlFScrollContext(",
        "function captureCtrlFSearchAnchor(",
        "function getCtrlFElementFlowMidY(",
        "function findInitialCtrlFMatchIndex(",
        "function isCtrlFTargetInSafeViewport(",
        "function openCtrlFSearch(",
        "function closeCtrlFSearch(",
        "function stepCtrlFMatch(",
        "function buildCtrlFRegex(",
        "function onCtrlFRowFilterToggle(",
        "ev.code === 'KeyF'",
        "ev.key === 'F3'",
    ):
        assert expected_token in html_text
    assert "activateCtrlFMatch(0," not in html_text

    # Verify no accidental double-brace f-string artifacts leaked into rendered HTML/JS
    assert "{{" not in html_text
    assert "}}" not in html_text

    # Verify /manual also includes Ctrl+F search bar & viewport-aware handlers
    manual_resp = client.get("/manual")
    assert manual_resp.status_code == 200
    manual_html = manual_resp.text
    assert 'id="ctrlf-search-bar"' in manual_html
    assert "🔎 매뉴얼 검색 (Ctrl+F)" in manual_html
    assert "window.openManualFind" in manual_html
    assert "window.stepManualFind" in manual_html
    assert "function findInitialManualHitIndex(" in manual_html
    assert "activateHit(0)" not in manual_html


def test_dashboard_sticky_header_and_excel_row_freeze() -> None:
    """Verify Excel-like sticky table headers, column highlight sync, and reference row freeze."""
    client = TestClient(app)

    from sololedger.config import settings as cur_settings

    seed_resp = client.post("/api/v1/system/seed-demo", headers={"X-API-Key": cur_settings.api_key})
    assert seed_resp.status_code == 200

    dash_resp = client.get("/dashboard?year_month=ALL")
    assert dash_resp.status_code == 200
    html_text = dash_resp.text

    # 1. Verify all 4 dashboard table sections have sticky bars and sticky-ledger-table markup
    for section_id in (
        'id="account-summary-section"',
        'id="card-summary-section"',
        'id="pending-ledger-section"',
        'id="main-ledger-section"',
    ):
        assert section_id in html_text
    assert html_text.count('class="section-sticky-bar"') == 4
    assert html_text.count('class="sticky-ledger-table"') >= 2
    assert 'id="pending-ledger-table"' in html_text
    assert 'id="main-ledger-table"' in html_text

    # 2. Verify sticky CSS rules, column hover highlight, and pinned reference row styles
    for css_token in (
        "body:not(.unfreeze-headers) .section-sticky-bar",
        "body:not(.unfreeze-headers) .sticky-ledger-table > thead > tr > th",
        "top: var(--sticky-bar-h, 44px);",
        ".sticky-ledger-table > thead > tr > th.col-hover-active",
        "body:not(.unfreeze-headers) .sticky-ledger-table > tbody > tr.pinned-ledger-row > td",
        "td.tx-pin-cell",
    ):
        assert css_token in html_text

    # 3. Verify UI controls (freeze ON/OFF buttons, unpin button, scroll-to-top, and pin cells)
    assert 'id="btn-toggle-freeze-header"' in html_text
    assert 'id="btn-ledger-freeze-header"' in html_text
    assert 'id="btn-clear-pinned-rows"' in html_text
    assert "📌 상단 행 고정 ON" in html_text
    assert "📌 행 고정 ON" in html_text
    assert "scrollToDashboardTop()" in html_text
    assert "class='tx-pin-cell'" in html_text

    # 4. Verify JavaScript freeze state, ResizeObserver offset sync, column hover, and row pinning
    for js_token in (
        "FREEZE_HEADER_STORAGE_KEY",
        "function syncStickySectionOffsets(",
        "function updateStickyStuckClasses(",
        "function applyFreezeHeaderState(",
        "function toggleFreezeHeaders(",
        "function clearAllPinnedLedgerRows(",
        "function setHoverHeaderCell(",
        "initStickyLedgerFreeze",
    ):
        assert js_token in html_text

    assert "{{" not in html_text
    assert "}}" not in html_text

    # 5. Verify /manual table headers also use sticky positioning
    manual_resp = client.get("/manual")
    assert manual_resp.status_code == 200
    assert "thead th {" in manual_resp.text
    assert "position: sticky;" in manual_resp.text


def test_dashboard_v15_graph_audit_and_bigquery_modal() -> None:
    """Verify v1.5.0 dashboard button, Phase 8~10 audit/graph/backup modal, and JS wiring."""
    from sololedger import __version__

    client = TestClient(app)
    dash_resp = client.get("/dashboard?year_month=ALL")
    assert dash_resp.status_code == 200
    html_text = dash_resp.text

    assert f"SoloLedger 경비·세무증빙 대시보드 (v{__version__})" in html_text
    assert "🕸️ 세무감사·지식그래프" in html_text
    assert 'id="graph-audit-modal-backdrop"' in html_text
    assert 'id="audit-year-month"' in html_text
    assert 'id="audit-include-exported-chk"' in html_text
    assert 'id="audit-graph-results"' in html_text
    assert 'id="wf-tx-id"' in html_text
    assert 'id="wf-item-hint"' in html_text
    assert 'id="wf-resume-acc"' in html_text
    assert 'id="wf-resume-memo"' in html_text
    assert 'id="wf-graph-results"' in html_text
    assert 'id="bq-graph-results"' in html_text

    for js_token in (
        "function openGraphAuditModal(",
        "function closeGraphAuditModal(",
        "function onGraphAuditBackdropClick(",
        "function runPreExportTaxAudit(",
        "function runTxWorkflowGraph(",
        "function runTxWorkflowFromModal(",
        "function resumeTxWorkflowFromModal(",
        "function runSnapshotBackupNow(",
        "function runBigQueryGraphExport(",
        "closeGraphAuditModal();",
    ):
        assert js_token in html_text

    assert "{{" not in html_text
    assert "}}" not in html_text
    assert html_text.count('class="section-sticky-bar"') == 4


def test_ctrl_f_starts_from_current_viewport_in_headless_chromium(
    tmp_path: Path, db_conn
) -> None:
    """Verify Ctrl+F starts from the current scrolled page/viewport instead of 1/N at page top."""
    import subprocess
    import time

    import httpx

    from sololedger.scraper_service import _CdpWebSocket, _find_local_chromium_executable

    browser_exe = _find_local_chromium_executable()
    if not browser_exe:
        return

    # Seed enough transactions so /dashboard spans multiple scrollable viewports
    for idx in range(1, 55):
        day = (idx % 28) + 1
        db_conn.execute(
            """
            INSERT INTO card_transactions (
                card_last4, approval_no, transacted_at, raw_merchant_name,
                total_amount, supply_value, vat_amount, account_code, account_name,
                vat_deduction_type, counterparty_info, reconcile_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'DEDUCTIBLE', ?, 'HOMETAX_VERIFIED')
            """,
            (
                "우리법인1234",
                f"VP{idx:06d}",
                f"2026-10-{day:02d} 12:{idx:02d}:00",
                f"뷰포트검증상점_{idx:02d}",
                11000,
                10000,
                1000,
                "811",
                "복리후생비",
                f"반복검색키워드 행번호_{idx:02d}",
            ),
        )
    db_conn.commit()

    server_port = find_free_port("127.0.0.1")
    cdp_port = find_free_port("127.0.0.1")
    profile_dir = tmp_path / "cdp_ctrlf_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    controller = EmbeddedServerController(bind_host="127.0.0.1", port=server_port)
    proc = None
    ws = None
    try:
        base_url = controller.start(timeout=10.0)
        dash_url = build_desktop_start_url(base_url, "/dashboard?year_month=ALL")
        manual_url = build_desktop_start_url(base_url, "/manual")

        proc = subprocess.Popen(
            [
                browser_exe,
                "--headless=new",
                f"--remote-debugging-port={cdp_port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile_dir}",
                "--window-size=1360,820",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                dash_url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ws_url = ""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"http://127.0.0.1:{cdp_port}/json", timeout=1.0, trust_env=False)
                if resp.status_code == 200:
                    for tgt in resp.json():
                        if tgt.get("type") == "page" and tgt.get("webSocketDebuggerUrl"):
                            ws_url = tgt["webSocketDebuggerUrl"]
                            break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.15)
        assert ws_url, "Failed to attach CDP WebSocket to headless Chromium"

        ws = _CdpWebSocket(ws_url, timeout=15.0)
        ws.call("Page.enable", msg_id=1)
        ws.call("Runtime.enable", msg_id=2)

        # Wait until #main-ledger-tbody is rendered with seeded rows
        ready = False
        for msg_seq in range(10, 40):
            ev = ws.call(
                "Runtime.evaluate",
                {
                    "expression": "document.querySelectorAll('#main-ledger-tbody tr[data-has-stmt]').length",
                    "returnByValue": True,
                },
                msg_id=msg_seq,
            )
            row_cnt = ev.get("result", {}).get("result", {}).get("value", 0)
            if isinstance(row_cnt, int) and row_cnt >= 50:
                ready = True
                break
            time.sleep(0.15)
        assert ready, "Dashboard rows did not finish loading"

        # 1. Pin top row #0, scroll deep down to row #30, open Ctrl+F and type '뷰포트검증상점'
        js_dash_check = """
        (() => {
          const rows = document.querySelectorAll('#main-ledger-tbody tr[data-has-stmt]');
          rows[0].classList.add('pinned-ledger-row');
          syncStickySectionOffsets();
          const targetRow = rows[30];
          const targetDocTop = targetRow.getBoundingClientRect().top + window.scrollY;
          window.scrollTo(0, Math.max(0, targetDocTop - 160));
          updateStickyStuckClasses();
          const beforeScrollY = window.scrollY;

          openCtrlFSearch();
          const inp = document.getElementById('ctrlf-search-input');
          inp.value = '뷰포트검증상점';
          onCtrlFInput();

          const firstCountText = document.getElementById('ctrlf-match-count').textContent;
          const firstIdx = ctrlFActiveIndex;
          const afterScrollY = window.scrollY;

          // Step next (+1) and verify sequential progression from current viewport match
          stepCtrlFMatch(1);
          const nextIdx = ctrlFActiveIndex;

          // Search a term that ONLY exists in the very first row above current scroll -> wraps to 0
          closeCtrlFSearch();
          window.scrollTo(0, beforeScrollY);
          openCtrlFSearch(rows[0].querySelectorAll('td')[3].textContent.trim());
          const wrappedIdx = ctrlFActiveIndex;

          return {
            totalRows: rows.length,
            beforeScrollY: beforeScrollY,
            afterScrollY: afterScrollY,
            firstIdx: firstIdx,
            nextIdx: nextIdx,
            firstCountText: firstCountText,
            wrappedIdx: wrappedIdx
          };
        })()
        """
        dash_eval = ws.call(
            "Runtime.evaluate",
            {"expression": js_dash_check, "returnByValue": True},
            msg_id=100,
        )
        res = dash_eval["result"]["result"]["value"]
        assert res["beforeScrollY"] > 600
        # Must NOT start at index 0 (1 / N) when scrolled down to row #30 (even with row #0 pinned!)
        assert res["firstIdx"] >= 20, f"Expected viewport match >= 20, got {res}"
        assert not res["firstCountText"].startswith("1 / "), f"Counter started at top: {res}"
        assert res["nextIdx"] == res["firstIdx"] + 1
        # Because match is already visible in viewport, page does not jump back to top
        assert abs(res["afterScrollY"] - res["beforeScrollY"]) < 50
        # Unique match above current viewport cleanly wraps around to index 0
        assert res["wrappedIdx"] == 0

        # 2. Navigate to /manual, scroll down to #sec-5, and verify sticky <aside> TOC does not hijack 1/N
        ws.call("Page.navigate", {"url": manual_url}, msg_id=200)
        manual_ready = False
        for msg_seq in range(201, 230):
            ev = ws.call(
                "Runtime.evaluate",
                {
                    "expression": "Boolean(window.openManualFind && document.getElementById('sec-5'))",
                    "returnByValue": True,
                },
                msg_id=msg_seq,
            )
            if ev.get("result", {}).get("result", {}).get("value") is True:
                manual_ready = True
                break
            time.sleep(0.15)
        assert manual_ready

        js_manual_check = """
        (() => {
          const sec5 = document.getElementById('sec-5');
          const secTop = sec5.getBoundingClientRect().top + window.scrollY;
          window.scrollTo(0, Math.max(0, secTop - 80));
          const beforeY = window.scrollY;
          window.openManualFind();
          const inp = document.getElementById('ctrlf-search-input');
          inp.value = 'CLI';
          inp.dispatchEvent(new Event('input'));
          const countTxt = document.getElementById('ctrlf-match-count').textContent;
          const activeMark = document.querySelector('mark.sl-find-hit.sl-find-active');
          const inMain = Boolean(activeMark && activeMark.closest('main'));
          return {
            beforeY: beforeY,
            afterY: window.scrollY,
            countTxt: countTxt,
            inMain: inMain
          };
        })()
        """
        man_eval = ws.call(
            "Runtime.evaluate",
            {"expression": js_manual_check, "returnByValue": True},
            msg_id=250,
        )
        mres = man_eval["result"]["result"]["value"]
        assert mres["beforeY"] > 500
        assert mres["inMain"] is True, f"Sticky aside hijacked manual find: {mres}"
        assert not mres["countTxt"].startswith("1 / "), f"Manual find started at 1/N: {mres}"
    finally:
        if ws is not None:
            ws.close()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except Exception:
                proc.kill()
        controller.stop(timeout=5.0)


