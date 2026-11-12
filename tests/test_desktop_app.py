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
from sololedger.config import _resolve_bundle_root, _resolve_project_root
from sololedger.desktop_app import (
    EmbeddedServerController,
    build_desktop_start_url,
    enable_webview_accelerator_keys,
    ensure_safe_stdio,
    find_free_port,
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
        "function openCtrlFSearch(",
        "function closeCtrlFSearch(",
        "function stepCtrlFMatch(",
        "function buildCtrlFRegex(",
        "function onCtrlFRowFilterToggle(",
        "ev.code === 'KeyF'",
        "ev.key === 'F3'",
    ):
        assert expected_token in html_text

    # Verify no accidental double-brace f-string artifacts leaked into rendered HTML/JS
    assert "{{" not in html_text
    assert "}}" not in html_text

    # Verify /manual also includes Ctrl+F search bar & handlers
    manual_resp = client.get("/manual")
    assert manual_resp.status_code == 200
    manual_html = manual_resp.text
    assert 'id="ctrlf-search-bar"' in manual_html
    assert "🔎 매뉴얼 검색 (Ctrl+F)" in manual_html
    assert "window.openManualFind" in manual_html
    assert "window.stepManualFind" in manual_html
