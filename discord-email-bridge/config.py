"""Load and validate configuration from environment variables (.env)."""

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass
class Config:
    discord_bot_token: str
    discord_channel_id: int

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str

    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str

    target_email: str
    allowed_email_sender: str

    email_poll_interval_seconds: int
    state_file: str


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _require_int(name: str) -> int:
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be an integer, got: {raw!r}") from exc


def load_config() -> Config:
    """Load configuration from .env / environment variables.

    Raises ConfigError if anything required is missing or invalid.
    """
    load_dotenv()

    config = Config(
        discord_bot_token=_require("DISCORD_BOT_TOKEN"),
        discord_channel_id=_require_int("DISCORD_CHANNEL_ID"),
        smtp_host=_require("SMTP_HOST"),
        smtp_port=_require_int("SMTP_PORT"),
        smtp_user=_require("SMTP_USER"),
        smtp_password=_require("SMTP_PASSWORD"),
        smtp_from=_require("SMTP_FROM"),
        imap_host=_require("IMAP_HOST"),
        imap_port=_require_int("IMAP_PORT"),
        imap_user=_require("IMAP_USER"),
        imap_password=_require("IMAP_PASSWORD"),
        target_email=_require("TARGET_EMAIL"),
        allowed_email_sender=_require("ALLOWED_EMAIL_SENDER"),
        email_poll_interval_seconds=_require_int("EMAIL_POLL_INTERVAL_SECONDS"),
        state_file=os.getenv("STATE_FILE", "state.json"),
    )

    logger.info("Configuration loaded successfully.")
    return config
