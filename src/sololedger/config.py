"""Configuration and namespace constants for SoloLedger."""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"


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
