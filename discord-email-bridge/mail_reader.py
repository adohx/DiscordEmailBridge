"""Poll the bridge mailbox over IMAP and hand off valid replies to a callback.

Kept intentionally simple: connect, look at unread messages, filter by
allowed sender + dedup state, extract a clean plain-text body, resolve
which (if any) Discord message it's replying to, and call back into
main.py to deliver it to Discord. See docs/discord-email-message-mapping.md.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

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

# Gmail's "forward" marker. Unlike the reply-quote markers above, what
# follows this is NOT old content the recipient already has -- it's the
# whole reason the person forwarded the email -- so it must not be dropped.
# Only the header block directly under the marker (From/Date/Subject/To/Cc)
# is skipped; the real forwarded body after it is kept.
_FORWARD_MARKER = re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE)
_FORWARD_HEADER_FIELD = re.compile(r"^\s*(From|Date|Sent|To|Cc|Subject):\s*.*$", re.IGNORECASE)

# Discord's default per-file upload limit is 10 MB on non-boosted servers;
# stay comfortably under it.
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

# Non-image attachment types forwarded as-is (uploaded to Discord as a plain
# file, not rendered/converted). Exact MIME type match, unlike images which
# match on the "image/" prefix.
_ALLOWED_DOCUMENT_CONTENT_TYPES = {
    "application/pdf",
    "application/msword",  # .doc
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
}


@dataclass
class EmailAttachment:
    """An attachment extracted from an email, ready to forward to Discord as a file."""

    filename: str
    content_type: str
    payload: bytes


@dataclass
class IncomingEmail:
    """A validated, ready-to-deliver email reply plus its threading context."""

    sender: str
    sender_name: str
    body: str
    email_message_id: Optional[str]
    in_reply_to: Optional[str]
    references: List[str]
    parent_discord_message_id: Optional[str]
    attachments: List[EmailAttachment] = field(default_factory=list)
    # Human-readable notices for attachments that were NOT forwarded (wrong
    # format, too large, ...) -- always shown on the Discord side so a
    # dropped attachment is never silent. See mail-attachment handling.
    attachment_notes: List[str] = field(default_factory=list)


# Callback signature: (IncomingEmail) -> True if successfully delivered to
# Discord (in which case the email is marked processed + seen).
OnValidEmail = Callable[[IncomingEmail], bool]


def strip_quoted_history(text: str) -> str:
    """Drop quoted reply history and '>' quote lines from an email body.

    A forwarded message's header block is skipped rather than triggering a
    hard stop like the reply-quote markers do, since the content after it is
    new, not something the recipient has already seen.
    """
    lines = text.splitlines()
    kept = []
    in_forward_header = False
    for line in lines:
        if in_forward_header:
            if not line.strip() or _FORWARD_HEADER_FIELD.match(line):
                continue
            in_forward_header = False

        if line.strip().startswith(">"):
            continue
        if _FORWARD_MARKER.match(line):
            in_forward_header = True
            kept.append(line)
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


def _resolve_sender_name(config: Config, sender_email: str) -> Optional[str]:
    """Return the configured display name for an allowed sender, or None if not allowed."""
    return config.allowed_email_senders.get(sender_email.strip().lower())


def _extract_plain_text(msg: MailMessage) -> Optional[str]:
    if msg.text and msg.text.strip():
        return msg.text
    if msg.html:
        logger.info("Email %s only has an HTML body; HTML emails are not supported in MVP, skipping.", msg.uid)
        return None
    return None


def _is_forwardable_content_type(content_type: str) -> bool:
    return content_type.startswith("image/") or content_type in _ALLOWED_DOCUMENT_CONTENT_TYPES


def _extract_forwardable_attachments(msg: MailMessage) -> Tuple[List[EmailAttachment], List[str]]:
    """Split an email's attachments into forwardable files and skip notices.

    Only images and a small set of document types (PDF, DOC, DOCX) under
    MAX_ATTACHMENT_BYTES are forwarded to Discord, as-is -- no rendering or
    conversion. Everything else (wrong format, too large) is dropped, but
    always produces a human-readable note so the Discord side knows something
    was left out instead of it silently vanishing.
    """
    attachments: List[EmailAttachment] = []
    notes: List[str] = []
    for att in msg.attachments:
        name = att.filename or "(unnamed attachment)"
        if not _is_forwardable_content_type(att.content_type):
            notes.append(
                f"Attachment skipped: {name} ({att.content_type}) — unsupported format, only images, "
                "PDF, DOC, and DOCX are forwarded."
            )
            continue
        if att.size > MAX_ATTACHMENT_BYTES:
            size_mb = att.size / (1024 * 1024)
            limit_mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
            notes.append(f"Attachment skipped: {name} ({size_mb:.1f} MB) — exceeds the {limit_mb} MB forwarding limit.")
            continue
        attachments.append(EmailAttachment(filename=name, content_type=att.content_type, payload=att.payload))
    return attachments, notes


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
            sender_name = _resolve_sender_name(config, sender)

            if sender_name is None:
                logger.info("Ignoring email from %s: not an allowed sender.", sender)
                mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
                continue

            if state.is_email_processed(email_id):
                logger.info("Skipping email %s: already processed.", email_id)
                mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
                continue

            body = _extract_plain_text(msg)
            clean_body = strip_quoted_history(body) if body else ""

            attachments, attachment_notes = _extract_forwardable_attachments(msg)
            if attachments or attachment_notes:
                logger.info(
                    "Email %s has %d attachment(s) to forward and %d skipped.",
                    email_id,
                    len(attachments),
                    len(attachment_notes),
                )

            if not clean_body and not attachments and not attachment_notes:
                logger.info("Email %s has no forwardable content (no text, no attachments), skipping.", email_id)
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
                sender_name=sender_name,
                body=clean_body,
                email_message_id=normalize_message_id(_get_header(msg, "message-id")),
                in_reply_to=in_reply_to,
                references=references,
                parent_discord_message_id=parent_discord_message_id,
                attachments=attachments,
                attachment_notes=attachment_notes,
            )

            delivered = on_valid_email(incoming)
            if delivered:
                state.mark_email_processed(email_id)
                mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
                logger.info("Email %s delivered to Discord and marked processed.", email_id)
            else:
                logger.error("Failed to deliver email %s to Discord; will retry on next poll.", email_id)
