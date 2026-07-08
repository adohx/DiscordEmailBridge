"""Poll the bridge mailbox over IMAP and hand off valid replies to a callback.

Kept intentionally simple: connect, look at unread messages, filter by
allowed sender + dedup state, extract a clean plain-text body, and call
back into main.py to deliver it to Discord.
"""

import logging
import re
from typing import Callable, Optional

from imap_tools import AND, MailBox, MailMessage, MailMessageFlags

from config import Config
from state import State

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

# Callback signature: (sender_email, clean_body_text) -> True if successfully
# delivered to Discord (in which case the email is marked processed + seen).
OnValidEmail = Callable[[str, str], bool]


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


def _get_email_id(msg: MailMessage) -> str:
    message_id = msg.headers.get("message-id")
    if message_id:
        return message_id[0]
    return f"uid:{msg.uid}"


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

            if state.is_processed(email_id):
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

            delivered = on_valid_email(sender, clean_body)
            if delivered:
                state.mark_processed(email_id)
                mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
                logger.info("Email %s delivered to Discord and marked processed.", email_id)
            else:
                logger.error("Failed to deliver email %s to Discord; will retry on next poll.", email_id)
