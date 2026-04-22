"""Runtime settings. FRD B.4."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.paths import env_file


def _load_env_file() -> None:
    """Load ~/.claude-equity-momentum/.env if present. No-op otherwise.
    Called at import time; worker also watches this file for hot reload.
    """
    p: Path = env_file()
    if p.exists():
        load_dotenv(p, override=True)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


_load_env_file()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    dhan_client_id: str = Field(default="")
    dhan_access_token: str = Field(default="")

    web_host: str = Field(default="127.0.0.1", alias="EMRB_WEB_HOST")
    web_port: int = Field(default=8765, alias="EMRB_WEB_PORT")
    log_level: str = Field(default="INFO", alias="EMRB_LOG_LEVEL")
    timezone: str = Field(default="Asia/Kolkata", alias="EMRB_TIMEZONE")

    dhan_api_base: str = Field(default="https://api.dhan.co/v2", alias="EMRB_DHAN_API_BASE")


def load_settings() -> Settings:
    _load_env_file()
    return Settings()
