"""Standalone Native Desktop Window Application (pywebview + Embedded FastAPI) for SoloLedger."""

import argparse
import ctypes
import io
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
import uvicorn

from sololedger import __version__
from sololedger.api import _create_session_token
from sololedger.api import app as fastapi_app
from sololedger.config import PROJECT_ROOT, resolve_bundled_path, settings

DEFAULT_DESKTOP_HOST = "0.0.0.0"
DEFAULT_DESKTOP_PORT = 8000
DEFAULT_WINDOW_WIDTH = 1480
DEFAULT_WINDOW_HEIGHT = 940
APP_USER_MODEL_ID = "kr.sololedger.desktop"


def ensure_safe_stdio(log_dir: Optional[Path] = None) -> Optional[Path]:
    """Attach safe UTF-8 file or memory streams when running in windowless (--noconsole) mode."""
    if sys.stdout is not None and sys.stderr is not None:
        return None

    target_dir = log_dir or (PROJECT_ROOT / "logs")
    log_file_path: Optional[Path] = None
    stream: Any = None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = target_dir / "desktop_app.log"
        stream = open(log_file_path, mode="a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        log_file_path = None
        stream = io.StringIO()

    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return log_file_path


def configure_windows_app_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Register Windows AppUserModelID so taskbar icon and pinning bind to SoloLedger."""
    if sys.platform != "win32":
        return False
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception:
        return False


def is_sololedger_running(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Return True if a SoloLedger FastAPI instance responds healthy on host:port."""
    url = f"http://{host}:{port}/health"
    try:
        resp = httpx.get(url, timeout=timeout)
        if resp.status_code != 200:
            return False
        payload = resp.json()
        return payload.get("status") == "ok" and payload.get("service") == "sololedger-api"
    except Exception:
        return False


def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
    """Check whether TCP host:port is free to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def find_free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for an ephemeral available local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def negotiate_server_endpoint(
    preferred_port: int = DEFAULT_DESKTOP_PORT,
    bind_host: str = DEFAULT_DESKTOP_HOST,
) -> Dict[str, Any]:
    """Decide whether to reuse an existing SoloLedger server or start an embedded one."""
    if is_sololedger_running(preferred_port, host="127.0.0.1"):
        return {
            "mode": "reuse_existing",
            "bind_host": bind_host,
            "port": preferred_port,
            "local_base_url": f"http://127.0.0.1:{preferred_port}",
        }

    check_host = "127.0.0.1" if bind_host in ("0.0.0.0", "") else bind_host
    if is_port_available(preferred_port, host=check_host):
        chosen_port = preferred_port
    else:
        chosen_port = find_free_port(host="127.0.0.1")

    return {
        "mode": "start_embedded",
        "bind_host": bind_host,
        "port": chosen_port,
        "local_base_url": f"http://127.0.0.1:{chosen_port}",
    }


def build_desktop_start_url(local_base_url: str, start_path: str = "/dashboard") -> str:
    """Construct initial WebView URL, adding signed loopback session bootstrap if auth is enabled."""
    normalized_path = start_path if start_path.startswith("/") else f"/{start_path}"
    base = local_base_url.rstrip("/")
    if settings.require_auth:
        token = _create_session_token()
        return f"{base}/desktop-launch?token={quote(token)}&next_path={quote(normalized_path)}"
    return f"{base}{normalized_path}"


class EmbeddedServerController:
    """Manage a background Uvicorn server thread bound to the desktop window lifecycle."""

    def __init__(self, bind_host: str = DEFAULT_DESKTOP_HOST, port: int = DEFAULT_DESKTOP_PORT) -> None:
        self.bind_host = bind_host
        self.port = port
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 15.0) -> str:
        """Start Uvicorn in a daemon thread and block until /health responds 200 OK."""
        if self.is_alive:
            return f"http://127.0.0.1:{self.port}"

        config = uvicorn.Config(
            app=fastapi_app,
            host=self.bind_host,
            port=self.port,
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config=config)
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._server = server

        thread = threading.Thread(
            target=server.run,
            name="sololedger-embedded-uvicorn",
            daemon=True,
        )
        self._thread = thread
        thread.start()

        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if is_sololedger_running(self.port, host="127.0.0.1", timeout=0.6):
                return f"http://127.0.0.1:{self.port}"
            if not thread.is_alive():
                raise RuntimeError(
                    f"Embedded SoloLedger server exited unexpectedly before binding port {self.port}."
                )
            time.sleep(0.1)

        self.stop(timeout=2.0)
        raise TimeoutError(f"Timed out waiting for embedded SoloLedger server on port {self.port}.")

    def stop(self, timeout: float = 5.0) -> None:
        """Signal Uvicorn to exit gracefully and join background thread."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        self._server = None


def resolve_app_icon_path() -> Optional[Path]:
    """Locate bundled SoloLedger .ico icon if present."""
    icon_candidate = resolve_bundled_path("assets", "sololedger.ico")
    return icon_candidate if icon_candidate.exists() else None


def enable_webview_accelerator_keys(window: Any) -> bool:
    """Enable WebView2 browser accelerator keys (Ctrl+F, F3, F5, Ctrl+P) even when debug=False."""
    native = getattr(window, "native", None)
    if native is None:
        return False
    webview_ctrl = getattr(native, "webview", None)
    if webview_ctrl is None:
        browser_obj = getattr(native, "browser", None)
        webview_ctrl = getattr(browser_obj, "webview", None)
    if webview_ctrl is None:
        return False

    applied = {"ok": False}

    def _apply() -> None:
        try:
            core = getattr(webview_ctrl, "CoreWebView2", None)
            wv_settings = getattr(core, "Settings", None) if core is not None else None
            if wv_settings is not None:
                wv_settings.AreBrowserAcceleratorKeysEnabled = True
                applied["ok"] = True
        except Exception:
            applied["ok"] = False

    try:
        if getattr(webview_ctrl, "InvokeRequired", False):
            try:
                from System import Action  # type: ignore[import-not-found]

                webview_ctrl.Invoke(Action(_apply))
            except Exception:
                webview_ctrl.Invoke(_apply)
        else:
            _apply()
    except Exception:
        return False
    return bool(applied["ok"])


def register_webview_window_hooks(window: Any) -> None:
    """Attach lifecycle hooks to enable keyboard accelerators (e.g. Ctrl+F) on WebView2 ready."""
    events = getattr(window, "events", None)
    if events is None:
        return

    def _on_ready() -> None:
        enable_webview_accelerator_keys(window)

    for ev_name in ("shown", "loaded"):
        ev_obj = getattr(events, ev_name, None)
        if ev_obj is not None:
            try:
                ev_obj += _on_ready
            except Exception:
                pass


def run_desktop_app(
    host: Optional[str] = None,
    port: Optional[int] = None,
    start_path: str = "/dashboard",
    width: int = DEFAULT_WINDOW_WIDTH,
    height: int = DEFAULT_WINDOW_HEIGHT,
    debug: bool = False,
    check_only: bool = False,
    check_output: Optional[Path] = None,
) -> Dict[str, Any]:
    """Start (or attach to) local SoloLedger server and open native WebView2 desktop window."""
    ensure_safe_stdio()
    configure_windows_app_id()

    resolved_host = host or os.getenv("SOLOLEDGER_DESKTOP_HOST", DEFAULT_DESKTOP_HOST)
    resolved_port = int(port or os.getenv("SOLOLEDGER_DESKTOP_PORT", str(DEFAULT_DESKTOP_PORT)))

    negotiation = negotiate_server_endpoint(preferred_port=resolved_port, bind_host=resolved_host)
    mode = negotiation["mode"]
    active_port = int(negotiation["port"])
    local_base_url = str(negotiation["local_base_url"])

    controller: Optional[EmbeddedServerController] = None
    try:
        if mode == "start_embedded":
            controller = EmbeddedServerController(bind_host=resolved_host, port=active_port)
            controller.start(timeout=15.0)

        start_url = build_desktop_start_url(local_base_url=local_base_url, start_path=start_path)

        if check_only:
            with httpx.Client(follow_redirects=True, timeout=5.0) as client:
                health_resp = client.get(f"{local_base_url}/health")
                dash_resp = client.get(start_url)
            summary: Dict[str, Any] = {
                "status": "ok" if (health_resp.status_code == 200 and dash_resp.status_code == 200) else "error",
                "mode": mode,
                "version": __version__,
                "port": active_port,
                "local_base_url": local_base_url,
                "db_path": str(settings.db_path),
                "health_status": health_resp.status_code,
                "dashboard_status": dash_resp.status_code,
            }
            if check_output is not None:
                check_output.parent.mkdir(parents=True, exist_ok=True)
                check_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            if summary["status"] != "ok":
                raise RuntimeError(f"Desktop self-check failed: {summary}")
            return summary

        import webview

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = bool(debug)

        storage_dir = PROJECT_ROOT / "logs" / "webview_profile"
        storage_dir.mkdir(parents=True, exist_ok=True)
        icon_path = resolve_app_icon_path()

        window_title = f"SoloLedger v{__version__} — 법인 회계·세무증빙 원장"
        window = webview.create_window(
            title=window_title,
            url=start_url,
            width=width,
            height=height,
            min_size=(1024, 680),
            resizable=True,
            text_select=True,
            zoomable=True,
            confirm_close=False,
        )
        register_webview_window_hooks(window)
        gui_backend = "edgechromium" if sys.platform == "win32" else None
        webview.start(
            gui=gui_backend,
            debug=bool(debug),
            private_mode=False,
            storage_path=str(storage_dir),
            icon=str(icon_path) if icon_path else None,
        )
        return {
            "status": "closed",
            "mode": mode,
            "version": __version__,
            "port": active_port,
            "local_base_url": local_base_url,
        }
    finally:
        if controller is not None:
            controller.stop(timeout=5.0)


def build_desktop_arg_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser for standalone SoloLedger desktop app."""
    parser = argparse.ArgumentParser(
        prog="sololedger-desktop",
        description=f"SoloLedger v{__version__} Standalone Windows Desktop Application",
    )
    parser.add_argument("--host", default=None, help="Bind host for embedded FastAPI server (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Preferred TCP port (default: 8000)")
    parser.add_argument("--path", default="/dashboard", help="Initial route path (default: /dashboard)")
    parser.add_argument("--width", type=int, default=DEFAULT_WINDOW_WIDTH, help="Initial window width")
    parser.add_argument("--height", type=int, default=DEFAULT_WINDOW_HEIGHT, help="Initial window height")
    parser.add_argument("--debug", action="store_true", help="Enable WebView2 developer tools")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run headless embedded server + /dashboard readiness check and exit",
    )
    parser.add_argument(
        "--check-output",
        default=None,
        help="Optional JSON file path to write --check-only result",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    """Entrypoint for `sololedger-desktop` and `SoloLedger.exe`."""
    parser = build_desktop_arg_parser()
    args = parser.parse_args(argv)
    out_path = Path(args.check_output) if args.check_output else None
    result = run_desktop_app(
        host=args.host,
        port=args.port,
        start_path=args.path,
        width=args.width,
        height=args.height,
        debug=bool(args.debug),
        check_only=bool(args.check_only),
        check_output=out_path,
    )
    if args.check_only and sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
