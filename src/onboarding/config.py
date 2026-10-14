"""Runtime configuration, loaded from environment or .env."""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: object) -> object:
    """Accept "a,b , c" as ["a", "b", "c"].

    pydantic-settings parses list fields as JSON by default, which makes for
    unpleasant .env files. Comma-separated is what people actually type.
    """
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Harvest -------------------------------------------------------------
    harvest_token: SecretStr
    harvest_account_id: str
    harvest_user_agent: str = "Aurora Onboarding (it@auroratechnical.example)"

    # --- BambooHR ------------------------------------------------------------
    bamboo_api_key: SecretStr
    bamboo_subdomain: str
    bamboo_webhook_private_key: SecretStr | None = None
    bamboo_signature_format: str = "body_then_timestamp"

    # --- Behaviour -----------------------------------------------------------
    # Defaults to True deliberately: every Harvest user is a billable seat, so
    # writing must be the thing you opt into, never the thing you forget to
    # opt out of.
    dry_run: bool = True
    max_hire_age_days: int = 30
    harvest_default_roles: list[str] = Field(default_factory=list)
    bamboo_contractor_statuses: list[str] = Field(
        default_factory=lambda: ["Contractor", "Temp", "Intern"]
    )
    bamboo_skipped_statuses: list[str] = Field(default_factory=lambda: ["Terminated"])

    log_level: str = "INFO"
    json_logs: bool = True

    @field_validator(
        "harvest_default_roles",
        "bamboo_contractor_statuses",
        "bamboo_skipped_statuses",
        mode="before",
    )
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("bamboo_signature_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        from onboarding.webhook_auth import SIGNATURE_FORMATS

        if value not in SIGNATURE_FORMATS:
            raise ValueError(
                f"bamboo_signature_format must be one of {sorted(SIGNATURE_FORMATS)}"
            )
        return value
