"""Configuration and namespace constants for SoloLedger."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"


class Settings:
    """Application settings loaded from environment variables."""

    @property
    def db_path(self) -> str:
        return os.getenv("SOLOLEDGER_DB_PATH", str(PROJECT_ROOT / "sololedger_prod.db"))

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
    def schema_path(self) -> Path:
        custom = os.getenv("SOLOLEDGER_SCHEMA_PATH")
        return Path(custom) if custom else DEFAULT_SCHEMA_PATH


settings = Settings()
