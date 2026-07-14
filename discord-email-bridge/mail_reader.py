"""Poll the bridge mailbox over IMAP and hand off valid replies to a callback.

Kept intentionally simple: connect, look at unread messages, filter by
allowed sender + dedup state, extract a clean plain-text body, resolve
which (if any) Discord message it's replying to, and call back into
main.py to deliver it to Discord. See docs/discord-email-message-mapping.md.
"""

import logging
import re
from dataclasses import dataclass
from typing import Callable, List, Optional

from imap_tools import AND, MailBox, MailMessage, MailMessageFlags

from config import Config
from state import State, normalize_message_id

logger = logging.getLogger(__name__)

# Lines that mark the start of quoted history in a reply. Once one of these
# is seen, everything from that point on is dropped. Not meant to be
# perfect -- just good enough to stop re-sending whole email chains.
_QUOTE_MARKERS = [
    re.compile(r"^\s*On .{0,150} wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*From:\s*.+$", re.IGNORECASE),
    re.compile(r"^\s*Sent:\s*.+$", re.IGNORECASE),
    re.compile(r"^\s*To:\s*.+$", re.IGNORECASE),
    re.compile(r"^\s*Subject:\s*.+$", re.IGNORECASE),
]


@dataclass
class IncomingEmail:
    """A validated, ready-to-deliver email reply plus its threading context."""

    sender: str
    body: str
    email_message_id: Optional[str]
    in_reply_to: Optional[str]
    references: List[str]
    parent_discord_message_id: Optional[str]


# Callback signature: (IncomingEmail) -> True if successfully delivered to
# Discord (in which case the email is marked processed + seen).
OnValidEmail = Callable[[IncomingEmail], bool]


def strip_quoted_history(text: str) -> str:
    """Drop quoted reply history and '>' quote lines from an email body."""
    lines = text.splitlines()
    kept = []
    for line in lines:
        if line.strip().startswith(">"):
            continue
        if any(pattern.match(line) for pattern in _QUOTE_MARKERS):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _get_header(msg: MailMessage, name: str) -> Optional[str]:
    values = msg.headers.get(name)
    if not values:
        return None
    return values[0]


def _get_email_id(msg: MailMessage) -> str:
    normalized = normalize_message_id(_get_header(msg, "message-id"))
    if normalized:
        return normalized
    return f"imap:{msg.uid}"


def _parse_references(msg: MailMessage) -> List[str]:
    raw = _get_header(msg, "references")
    if not raw:
        return []
    return [ref for ref in (normalize_message_id(token) for token in raw.split()) if ref]


def _resolve_parent_discord_message_id(
    state: State,
    in_reply_to: Optional[str],
    references: List[str],
    bridge_id_header: Optional[str],
) -> Optional[str]:
    """Resolution order per docs/discord-email-message-mapping.md #9:
    In-Reply-To -> References (newest first) -> X-Discord-Bridge-ID -> none.
    """
    if in_reply_to:
        mapping = state.get_by_email_message_id(in_reply_to)
        if mapping:
            logger.info("Resolved email reply parent via In-Reply-To header.")
            return mapping["discord_message_id"]

    for ref in reversed(references):
        mapping = state.get_by_email_message_id(ref)
        if mapping:
            logger.info("Resolved email reply parent via References header.")
            return mapping["discord_message_id"]

    if bridge_id_header:
        mapping = state.get_by_bridge_id(bridge_id_header.strip())
        if mapping:
            logger.info("Resolved email reply parent via X-Discord-Bridge-ID header.")
            return mapping["discord_message_id"]

    return None


def _extract_plain_text(msg: MailMessage) -> Optional[str]:
    if msg.text and msg.text.strip():
        return msg.text
    if msg.html:
        logger.info("Email %s only has an HTML body; HTML emails are not supported in MVP, skipping.", msg.uid)
        return None
    logger.info("Email %s has an empty body, skipping.", msg.uid)
    return None


def poll_mailbox(config: Config, state: State, on_valid_email: OnValidEmail) -> None:
    """Connect once, process all currently-unread messages, then return."""
    logger.info("Polling mailbox %s for new email...", config.imap_user)

    with MailBox(config.imap_host, config.imap_port).login(
        config.imap_user, config.imap_password, initial_folder="INBOX"
    ) as mailbox:
        messages = list(mailbox.fetch(AND(seen=False), mark_seen=False))

        if not messages:
            return

        logger.info("Found %d new email(s).", len(messages))

        for msg in messages:
            email_id = _get_email_id(msg)
            sender = (msg.from_ or "").strip()

            if sender.lower() != config.allowed_email_sender.lower():
                logger.info("Ignoring email from %s: not the allowed sender.", sender)
                mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
                continue

            if state.is_email_processed(email_id):
                logger.info("Skipping email %s: already processed.", email_id)
                mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
                continue

            body = _extract_plain_text(msg)
            if body is None:
                mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
                continue

            clean_body = strip_quoted_history(body)
            if not clean_body:
                logger.info("Email %s has no content after removing quoted history, skipping.", email_id)
                mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
                continue

            in_reply_to = normalize_message_id(_get_header(msg, "in-reply-to"))
            references = _parse_references(msg)
            bridge_id_header = _get_header(msg, "x-discord-bridge-id")
            parent_discord_message_id = _resolve_parent_discord_message_id(
                state, in_reply_to, references, bridge_id_header
            )
            if parent_discord_message_id:
                logger.info(
                    "Email %s resolved as a reply to Discord message %s.", email_id, parent_discord_message_id
                )
            else:
                logger.info("Email %s has no resolvable Discord parent; sending as a new message.", email_id)

            incoming = IncomingEmail(
                sender=sender,
                body=clean_body,
                email_message_id=normalize_message_id(_get_header(msg, "message-id")),
                in_reply_to=in_reply_to,
                references=references,
                parent_discord_message_id=parent_discord_message_id,
            )

            delivered = on_valid_email(incoming)
            if delivered:
                state.mark_email_processed(email_id)
                mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
                logger.info("Email %s delivered to Discord and marked processed.", email_id)
            else:
                logger.error("Failed to deliver email %s to Discord; will retry on next poll.", email_id)
