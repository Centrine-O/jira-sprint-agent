"""Loads and validates settings.

Secrets come from the .env file (never committed).
Non-secret board settings will come from config.yaml (added in a later step).
"""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_MARKERS = ("your-site", "you@example.com", "paste-your")


class Secrets(BaseSettings):
    """Credentials read from .env. Token values are hidden when printed."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    jira_base_url: str = Field(alias="JIRA_BASE_URL")
    jira_email: str = Field(alias="JIRA_EMAIL")
    jira_api_token: SecretStr = Field(alias="JIRA_API_TOKEN")
    anthropic_api_key: SecretStr = Field(alias="ANTHROPIC_API_KEY")

    @field_validator("jira_base_url")
    @classmethod
    def clean_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("JIRA_BASE_URL must start with https://")
        if not value.endswith(".atlassian.net"):
            raise ValueError(
                "JIRA_BASE_URL should look like https://yourteam.atlassian.net "
                "(nothing after .atlassian.net)"
            )
        return value

    @field_validator("jira_email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        value = value.strip()
        if "@" not in value:
            raise ValueError("JIRA_EMAIL does not look like an email address")
        return value

    def placeholder_problems(self) -> list[str]:
        """Return the names of any values still left as template placeholders."""
        problems = []
        checks = {
            "JIRA_BASE_URL": self.jira_base_url,
            "JIRA_EMAIL": self.jira_email,
            "JIRA_API_TOKEN": self.jira_api_token.get_secret_value(),
            "ANTHROPIC_API_KEY": self.anthropic_api_key.get_secret_value(),
        }
        for name, value in checks.items():
            if not value.strip() or any(m in value for m in PLACEHOLDER_MARKERS):
                problems.append(name)
        return problems


@lru_cache
def get_secrets() -> Secrets:
    """Load secrets once and reuse them."""
    return Secrets()  # type: ignore[call-arg]