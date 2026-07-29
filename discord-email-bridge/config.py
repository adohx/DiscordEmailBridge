"""Load and validate configuration from environment variables (.env)."""

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass
class Config:
    discord_bot_token: str
    discord_channel_id: int
    discord_guild_id: Optional[int]

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str

    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str

    target_emails: List[str]
    # Lowercased allowed sender email -> display name shown in Discord.
    allowed_email_senders: Dict[str, str]

    email_poll_interval_seconds: int
    state_file: str
    email_message_id_domain: str

    include_deleted_content: bool


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


def _optional_int(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be an integer, got: {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _require_email_list(name: str) -> List[str]:
    raw = _require(name)
    emails = [part.strip() for part in raw.split(",") if part.strip()]
    if not emails:
        raise ConfigError(f"Environment variable {name} must contain at least one email address")
    return emails


def _require_email_name_map(emails_var: str, names_var: str) -> Dict[str, str]:
    """Parse two paired comma-separated lists into {lowercased email: name}.

    The Nth email is paired with the Nth name, so both lists must be the same
    length and have no blank entries -- a mismatch almost always means a
    missing comma, and failing loudly here beats silently misattributing
    replies to the wrong name.
    """
    raw_emails = _require(emails_var)
    raw_names = _require(names_var)
    emails = [part.strip() for part in raw_emails.split(",")]
    names = [part.strip() for part in raw_names.split(",")]
    if len(emails) != len(names) or not all(emails) or not all(names):
        raise ConfigError(
            f"{emails_var} and {names_var} must both be non-empty, comma-separated lists of "
            f"the same length ({len(emails)} email(s) vs {len(names)} name(s))"
        )
    return {email.lower(): name for email, name in zip(emails, names)}


def load_config() -> Config:
    """Load configuration from .env / environment variables.

    Raises ConfigError if anything required is missing or invalid.
    """
    load_dotenv()

    config = Config(
        discord_bot_token=_require("DISCORD_BOT_TOKEN"),
        discord_channel_id=_require_int("DISCORD_CHANNEL_ID"),
        discord_guild_id=_optional_int("DISCORD_GUILD_ID"),
        smtp_host=_require("SMTP_HOST"),
        smtp_port=_require_int("SMTP_PORT"),
        smtp_user=_require("SMTP_USER"),
        smtp_password=_require("SMTP_PASSWORD"),
        smtp_from=_require("SMTP_FROM"),
        imap_host=_require("IMAP_HOST"),
        imap_port=_require_int("IMAP_PORT"),
        imap_user=_require("IMAP_USER"),
        imap_password=_require("IMAP_PASSWORD"),
        target_emails=_require_email_list("TARGET_EMAILS"),
        allowed_email_senders=_require_email_name_map("ALLOWED_EMAIL_SENDERS", "ALLOWED_EMAIL_SENDER_NAMES"),
        email_poll_interval_seconds=_require_int("EMAIL_POLL_INTERVAL_SECONDS"),
        state_file=os.getenv("STATE_FILE", "state.json"),
        email_message_id_domain=os.getenv("EMAIL_MESSAGE_ID_DOMAIN", "bridge.local"),
        include_deleted_content=_bool("INCLUDE_DELETED_CONTENT", True),
    )

    logger.info("Configuration loaded successfully.")
    return config
