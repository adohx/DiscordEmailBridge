"""Send Discord messages out as plain-text email via SMTP.

Every outgoing email carries a unique Message-ID plus bridge headers so
inbound replies can be mapped back to the originating Discord message. See
docs/discord-email-message-mapping.md.
"""

import logging
import smtplib
import uuid
from email.message import EmailMessage
from typing import Optional, Sequence, Tuple

from config import Config
from state import normalize_message_id

logger = logging.getLogger(__name__)

SUBJECT_MAX_LENGTH = 80
SUBJECT_PREFIX = "[Discord Bridge] "


def build_message_id(config: Config, discord_message_id: str, bridge_id: str) -> str:
    bridge_id_short = bridge_id.split("-")[0]
    return f"<discord-{discord_message_id}-{bridge_id_short}@{config.email_message_id_domain}>"


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


def _build_body(author_name: str, content: str, reply_context: Optional[Tuple[str, str]]) -> str:
    footer = "---\nReply to this email to send a message back to the Discord channel."

    if reply_context is None:
        return f"{author_name} wrote in Discord:\n\n{content}\n\n{footer}"

    parent_author, parent_content = reply_context
    quoted = "\n".join(f"> {line}" for line in parent_content.splitlines()) or "> "
    return (
        f"{author_name} replied to {parent_author} in Discord:\n\n"
        f"{quoted}\n\n"
        f"{content}\n\n"
        f"{footer}"
    )


def send_discord_message_as_email(
    config: Config,
    author_name: str,
    content: str,
    *,
    email_message_id: str,
    bridge_id: str,
    discord_message_id: str,
    in_reply_to: Optional[str] = None,
    references: Optional[Sequence[str]] = None,
    reply_context: Optional[Tuple[str, str]] = None,
) -> None:
    """Send a single Discord message to TARGET_EMAIL over SMTP.

    Raises smtplib.SMTPException / OSError on failure; caller is responsible
    for catching and logging so one failed email doesn't crash the program,
    and for not recording a message mapping until this call succeeds.
    """
    message = EmailMessage()
    message["Subject"] = _build_subject(author_name, content)
    message["From"] = config.smtp_from
    message["To"] = config.target_email
    message["Message-ID"] = normalize_message_id(email_message_id)
    message["X-Discord-Bridge-ID"] = bridge_id
    message["X-Discord-Message-ID"] = discord_message_id

    normalized_in_reply_to = normalize_message_id(in_reply_to)
    if normalized_in_reply_to:
        message["In-Reply-To"] = normalized_in_reply_to

    normalized_references = [ref for ref in (normalize_message_id(r) for r in (references or [])) if ref]
    if normalized_references:
        message["References"] = " ".join(normalized_references)

    message.set_content(_build_body(author_name, content, reply_context))

    _send(config, message)


def _send(config: Config, message: EmailMessage) -> None:
    """Deliver a fully-built EmailMessage over SMTP.

    Raises smtplib.SMTPException / OSError on failure; caller is responsible
    for catching and logging so one failed email doesn't crash the program.
    """
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(config.smtp_user, config.smtp_password)
        smtp.send_message(message)

    logger.info(
        "Sent email to %s (subject: %s, message-id: %s)",
        config.target_email,
        message["Subject"],
        message["Message-ID"],
    )


def send_edit_notification(
    config: Config,
    author_name: str,
    old_content: str,
    new_content: str,
    original_email_message_id: str,
    discord_message_id: str,
    edit_version: int,
) -> str:
    """Send an [Updated] notification for an edited Discord message.

    Returns the new notification email's Message-ID. Raises
    smtplib.SMTPException / OSError on failure; caller must not update state
    until this call succeeds. See discord-message-edit-delete-sync.md #7-8.
    """
    email_message_id = (
        f"<edit-{discord_message_id}-{edit_version}-{uuid.uuid4().hex[:8]}@{config.email_message_id_domain}>"
    )
    original_email_message_id = normalize_message_id(original_email_message_id)

    message = EmailMessage()
    message["Subject"] = f"[Updated] Discord message from {author_name}"
    message["From"] = config.smtp_from
    message["To"] = config.target_email
    message["Message-ID"] = email_message_id
    message["In-Reply-To"] = original_email_message_id
    message["References"] = original_email_message_id
    message["X-Discord-Bridge-Event"] = "edited"
    message["X-Discord-Message-ID"] = discord_message_id

    message.set_content(
        f"{author_name} edited a Discord message.\n\n"
        f"Previous version:\n\n{old_content}\n\n"
        f"Updated version:\n\n{new_content}"
    )

    _send(config, message)
    return email_message_id


def send_delete_notification(
    config: Config,
    author_name: str,
    original_content: Optional[str],
    original_email_message_id: str,
    discord_message_id: str,
) -> str:
    """Send a [Deleted] notification for a deleted Discord message.

    Returns the new notification email's Message-ID. Raises
    smtplib.SMTPException / OSError on failure; caller must not update state
    until this call succeeds. See discord-message-edit-delete-sync.md #10-11.
    """
    email_message_id = f"<delete-{discord_message_id}-{uuid.uuid4().hex[:8]}@{config.email_message_id_domain}>"
    original_email_message_id = normalize_message_id(original_email_message_id)

    message = EmailMessage()
    message["Subject"] = f"[Deleted] Discord message from {author_name}"
    message["From"] = config.smtp_from
    message["To"] = config.target_email
    message["Message-ID"] = email_message_id
    message["In-Reply-To"] = original_email_message_id
    message["References"] = original_email_message_id
    message["X-Discord-Bridge-Event"] = "deleted"
    message["X-Discord-Message-ID"] = discord_message_id

    if original_content:
        body = f"A Discord message from {author_name} was deleted.\n\nOriginal message:\n\n{original_content}"
    else:
        body = f"A Discord message from {author_name} was deleted."
    message.set_content(body)

    _send(config, message)
    return email_message_id
