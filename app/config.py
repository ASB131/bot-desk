import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    token: str
    application_id: str
    public_key: str
    panel_password: str
    port: int
    data_dir: Path
    default_timezone: str


@lru_cache
def get_settings() -> Settings:
    raw_port = os.environ.get("PORT", "23435")
    try:
        port = int(raw_port)
    except ValueError:
        port = 23435
    if not 1 <= port <= 65535:
        port = 23435
    data_dir = Path(os.environ.get("DATA_DIR", "data")).expanduser()
    if not data_dir.is_absolute():
        data_dir = (Path.cwd() / data_dir).resolve()
    return Settings(
        token=os.environ.get("DISCORD_TOKEN", "").strip(),
        application_id=os.environ.get("DISCORD_APPLICATION_ID", "").strip(),
        public_key=os.environ.get("DISCORD_PUBLIC_KEY", "").strip(),
        panel_password=os.environ.get("PANEL_PASSWORD", ""),
        port=port,
        data_dir=data_dir,
        default_timezone=os.environ.get("DEFAULT_TIMEZONE", "UTC").strip() or "UTC",
    )
