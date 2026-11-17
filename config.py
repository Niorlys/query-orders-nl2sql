"""Validated configuration loaded by pydantic-settings from .env and environment."""

from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, SecretStr, StringConstraints, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from errors import QueryServiceNotReadyError


PROJECT_ROOT = Path(__file__).resolve().parent
DOTENV_PATH = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENAI_", populate_by_name=True, extra="ignore", allow_inf_nan=False,
    )

    api_key: SecretStr
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = "gpt-6-luna"
    base_url: AnyHttpUrl = AnyHttpUrl("https://api.openai.com/v1")
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    total_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_retries: int = Field(default=2, ge=0, le=5)
    timezone: str = Field(default="America/Mexico_City", validation_alias="APP_TIMEZONE")
    database_path: Path = Field(
        default=PROJECT_ROOT / "data" / "processed" / "pedidos.sqlite3", validation_alias="DATABASE_PATH",
    )

    @field_validator("api_key")
    @classmethod
    def require_key(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError("API key is required")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value):
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Base URL must not contain credentials, query or fragment")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator("database_path")
    @classmethod
    def resolve_database(cls, value):
        return value if value.is_absolute() else PROJECT_ROOT / value

    @classmethod
    def from_env(cls):
        try:
            return cls(_env_file=DOTENV_PATH, _env_file_encoding="utf-8")
        except (ValidationError, OSError, ZoneInfoNotFoundError, ValueError) as error:
            # Do not stringify validation errors: they may include secret input values.
            raise QueryServiceNotReadyError("Invalid or missing provider configuration") from error
