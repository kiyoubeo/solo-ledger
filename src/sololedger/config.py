"""Configuration and namespace constants for SoloLedger."""

import os
import sys
from pathlib import Path
from typing import Optional


def _resolve_bundle_root(
    frozen: Optional[bool] = None,
    meipass: Optional[str] = None,
    executable: Optional[str] = None,
) -> Path:
    """Return read-only resource bundle root (supports PyInstaller _MEIPASS)."""
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        raw_meipass = getattr(sys, "_MEIPASS", None) if meipass is None else meipass
        if raw_meipass:
            return Path(raw_meipass).resolve()
        return Path(executable or sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def _resolve_project_root(
    home_env: Optional[str] = None,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> Path:
    """Return mutable runtime workspace directory (DB, .env, archive) across source & .exe runs."""
    explicit_home = home_env if home_env is not None else os.getenv("SOLOLEDGER_HOME", "")
    if explicit_home.strip():
        return Path(explicit_home.strip()).resolve()

    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        exe_dir = Path(executable or sys.executable).resolve().parent
        markers = ("sololedger_prod.db", "sololedger_test.db", ".env", "pyproject.toml")
        if any((exe_dir / m).exists() for m in markers):
            return exe_dir
        build_dir_names = {"dist", "build", "release", "bin"}
        if exe_dir.name.lower() in build_dir_names:
            parent_dir = exe_dir.parent
            if any((parent_dir / m).exists() for m in markers):
                return parent_dir
        if exe_dir.parent.name.lower() in build_dir_names:
            grandparent_dir = exe_dir.parent.parent
            if any((grandparent_dir / m).exists() for m in markers):
                return grandparent_dir
        return exe_dir

    return Path(__file__).resolve().parent.parent.parent


BUNDLE_ROOT = _resolve_bundle_root()
PROJECT_ROOT = _resolve_project_root()


def resolve_bundled_path(*parts: str) -> Path:
    """Resolve a static file from PROJECT_ROOT first, falling back to BUNDLE_ROOT."""
    project_candidate = PROJECT_ROOT.joinpath(*parts)
    if project_candidate.exists():
        return project_candidate
    bundle_candidate = BUNDLE_ROOT.joinpath(*parts)
    if bundle_candidate.exists():
        return bundle_candidate
    return project_candidate


DEFAULT_SCHEMA_PATH = resolve_bundled_path("sql", "schema.sql")


def _load_dotenv_file() -> None:
    """Load key=value pairs from PROJECT_ROOT/.env when not running inside pytest."""
    if "pytest" in sys.modules:
        return
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        clean_key = key.strip()
        clean_val = val.strip().strip('"').strip("'")
        if clean_key:
            os.environ.setdefault(clean_key, clean_val)


_load_dotenv_file()


class Settings:
    """Application settings loaded from environment variables."""

    @property
    def app_env(self) -> str:
        return os.getenv("SOLOLEDGER_ENV", "development")

    @property
    def db_path(self) -> str:
        default_name = "sololedger_test.db" if self.app_env == "test" else "sololedger_prod.db"
        return os.getenv("SOLOLEDGER_DB_PATH", str(PROJECT_ROOT / default_name))

    @property
    def api_key(self) -> str:
        return os.getenv("SOLOLEDGER_API_KEY", "dev-sololedger-secret-key")

    @property
    def admin_password(self) -> str:
        return os.getenv("SOLOLEDGER_ADMIN_PASSWORD", "")

    @property
    def require_auth(self) -> bool:
        explicit = os.getenv("SOLOLEDGER_AUTH_REQUIRED", "").strip().lower()
        if explicit in ("1", "true", "yes", "on"):
            return True
        if explicit in ("0", "false", "no", "off"):
            return False
        return self.app_env == "production" or bool(self.admin_password.strip())

    @property
    def telegram_bot_token(self) -> str:
        return os.getenv("TELEGRAM_BOT_TOKEN", "")

    @property
    def telegram_chat_id(self) -> str:
        return os.getenv("TELEGRAM_CHAT_ID", "")

    @property
    def nts_api_service_key(self) -> str:
        return os.getenv("NTS_API_SERVICE_KEY", "")

    @property
    def nts_api_base_url(self) -> str:
        return os.getenv("NTS_API_BASE_URL", "https://api.odcloud.kr/api/nts-businessman/v1")

    @property
    def nts_cache_ttl_days(self) -> int:
        return int(os.getenv("NTS_CACHE_TTL_DAYS", "30"))

    @property
    def archive_dir(self) -> Path:
        custom = os.getenv("SOLOLEDGER_ARCHIVE_DIR")
        if custom:
            p = Path(custom)
            return p if p.is_absolute() else (PROJECT_ROOT / p)
        folder = "archive_test" if self.app_env == "test" else "archive"
        return PROJECT_ROOT / folder

    @property
    def nas_backup_dir(self) -> Path:
        custom = os.getenv("SOLOLEDGER_NAS_DIR")
        if custom:
            return Path(custom)
        folder = "SoloLedger_Test" if self.app_env == "test" else "SoloLedger"
        for candidate in (
            Path(r"Z:\HDD1\Data"),
            Path(r"Z:\HDD1\Home.ybkim"),
            Path(r"Z:\HDD1\Home.admin"),
        ):
            if candidate.exists():
                return candidate / folder
        return Path(rf"Z:\{folder}")

    @property
    def gemini_api_key(self) -> str:
        return os.getenv("GEMINI_API_KEY", "")

    @property
    def schema_path(self) -> Path:
        custom = os.getenv("SOLOLEDGER_SCHEMA_PATH")
        return Path(custom) if custom else DEFAULT_SCHEMA_PATH


settings = Settings()
