"""Send Discord messages out as plain-text email via SMTP."""

import logging
import smtplib
from email.message import EmailMessage

from config import Config

logger = logging.getLogger(__name__)

SUBJECT_MAX_LENGTH = 80
SUBJECT_PREFIX = "[Discord Bridge] "


def _build_subject(author_name: str, content: str) -> str:
    prefix = f"{SUBJECT_PREFIX}{author_name}: "
    available = SUBJECT_MAX_LENGTH - len(prefix)
    if available <= 0:
        # Prefix alone already exceeds the budget; just truncate the whole thing.
        return (prefix + content)[:SUBJECT_MAX_LENGTH]

    first_line = content.splitlines()[0] if content.splitlines() else content
    if len(first_line) > available:
        first_line = first_line[: max(available - 1, 0)] + "…"
    return prefix + first_line


def _build_body(author_name: str, content: str) -> str:
    return (
        f"{author_name} wrote in Discord:\n\n"
        f"{content}\n\n"
        "---\n"
        "Reply to this email to send a message back to the Discord channel."
    )


def send_discord_message_as_email(config: Config, author_name: str, content: str) -> None:
    """Send a single Discord message to TARGET_EMAIL over SMTP.

    Raises smtplib.SMTPException / OSError on failure; caller is responsible
    for catching and logging so one failed email doesn't crash the program.
    """
    message = EmailMessage()
    message["Subject"] = _build_subject(author_name, content)
    message["From"] = config.smtp_from
    message["To"] = config.target_email
    message.set_content(_build_body(author_name, content))

    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(config.smtp_user, config.smtp_password)
        smtp.send_message(message)

    logger.info("Sent email to %s (subject: %s)", config.target_email, message["Subject"])
