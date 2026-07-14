"""Entry point: wires together the Discord client and the email polling loop.

    main.py
      load config
      load state
      start Discord client
      start email polling loop

See docs/discord-email-message-mapping.md for the Discord <-> Email message
mapping this module maintains.
"""

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import discord

import mail_sender
from config import Config, ConfigError, load_config
from discord_client import BridgeClient, deliver_email_to_channel
from mail_reader import IncomingEmail, poll_mailbox
from state import State, normalize_content

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def get_version() -> str:
    """Read the project version straight from pyproject.toml (the single source of truth)."""
    pyproject_path = Path(__file__).resolve().parent / "pyproject.toml"
    try:
        text = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


async def handle_discord_message(config: Config, state: State, message: discord.Message) -> None:
    """Send a bridged Discord message out as email and record the mapping."""
    discord_message_id = str(message.id)

    if state.has_discord_message(discord_message_id):
        logger.info("Skipping Discord message %s: already mapped.", discord_message_id)
        return

    author_name = str(message.author.display_name)
    content = (message.content or "").strip()

    parent_discord_message_id: Optional[str] = None
    parent_mapping = None
    if message.reference and message.reference.message_id:
        parent_discord_message_id = str(message.reference.message_id)
        parent_mapping = state.get_by_discord_message_id(parent_discord_message_id)
        if not parent_mapping:
            logger.warning(
                "Discord message %s replies to %s, but no mapping was found for it.",
                discord_message_id,
                parent_discord_message_id,
            )

    reply_context = None
    in_reply_to: Optional[str] = None
    references: List[str] = []
    if parent_mapping:
        reply_context = (parent_mapping["author_name"], parent_mapping["content"])
        if parent_mapping.get("email_message_id"):
            in_reply_to = parent_mapping["email_message_id"]
            references = list(parent_mapping.get("email_references") or [])
            references.append(in_reply_to)

    bridge_id = str(uuid.uuid4())
    email_message_id = mail_sender.build_message_id(config, discord_message_id, bridge_id)

    try:
        await asyncio.to_thread(
            mail_sender.send_discord_message_as_email,
            config,
            author_name,
            content,
            email_message_id=email_message_id,
            bridge_id=bridge_id,
            discord_message_id=discord_message_id,
            in_reply_to=in_reply_to,
            references=references,
            reply_context=reply_context,
        )
    except Exception:
        logger.exception(
            "SMTP error while sending Discord message %s to %s.", discord_message_id, config.target_email
        )
        return

    state.add_mapping(
        {
            "bridge_id": bridge_id,
            "discord_message_id": discord_message_id,
            "discord_parent_message_id": parent_discord_message_id if parent_mapping else None,
            "email_message_id": email_message_id,
            "email_in_reply_to": in_reply_to,
            "email_references": references,
            "author_name": author_name,
            "content": content,
            "delivery_status": "sent",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    logger.info("Mapped Discord message %s to email %s.", discord_message_id, email_message_id)


async def handle_message_edit(config: Config, state: State, message: discord.Message) -> None:
    """Send an [Updated] notification email for an edited, previously-bridged Discord message.

    See docs/discord-message-edit-delete-sync.md #5-8.
    """
    discord_message_id = str(message.id)
    mapping = state.get_by_discord_message_id(discord_message_id)
    if not mapping:
        logger.info("Ignoring edit for unmapped Discord message %s.", discord_message_id)
        return

    if mapping.get("status") == "deleted":
        logger.info("Ignoring edit for already-deleted Discord message %s.", discord_message_id)
        return

    old_content = normalize_content(mapping.get("content"))
    new_content = normalize_content(message.content)
    if old_content == new_content:
        logger.info("Discord message %s edit has no content change, skipping.", discord_message_id)
        return

    fingerprint = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
    if fingerprint == mapping.get("last_edit_fingerprint"):
        logger.info("Discord message %s edit fingerprint unchanged, skipping.", discord_message_id)
        return

    original_email_message_id = mapping.get("email_message_id")
    if not original_email_message_id:
        logger.error(
            "Discord message %s has no original email Message-ID; cannot send edit notification.",
            discord_message_id,
        )
        return

    next_version = mapping.get("edit_version", 0) + 1

    try:
        await asyncio.to_thread(
            mail_sender.send_edit_notification,
            config,
            mapping["author_name"],
            old_content,
            new_content,
            original_email_message_id,
            discord_message_id,
            next_version,
        )
    except Exception:
        logger.exception("SMTP error while sending edit notification for Discord message %s.", discord_message_id)
        return

    edited_at = datetime.now(timezone.utc).isoformat()
    state.record_edit(discord_message_id, new_content, fingerprint, edited_at)
    logger.info(
        "Sent edit notification for Discord message %s (edit_version=%d).", discord_message_id, next_version
    )


async def handle_message_delete(config: Config, state: State, discord_message_id: str) -> None:
    """Send a [Deleted] notification email for a deleted, previously-bridged Discord message.

    See docs/discord-message-edit-delete-sync.md #9-11.
    """
    mapping = state.get_by_discord_message_id(discord_message_id)
    if not mapping:
        logger.info("Ignoring delete for unmapped Discord message %s.", discord_message_id)
        return

    if mapping.get("status") == "deleted" or mapping.get("delete_notification_sent"):
        logger.info("Discord message %s already marked deleted; skipping duplicate notification.", discord_message_id)
        return

    original_email_message_id = mapping.get("email_message_id")
    if not original_email_message_id:
        logger.error(
            "Discord message %s has no original email Message-ID; cannot send delete notification.",
            discord_message_id,
        )
        return

    original_content = mapping.get("content") if config.include_deleted_content else None

    try:
        await asyncio.to_thread(
            mail_sender.send_delete_notification,
            config,
            mapping["author_name"],
            original_content,
            original_email_message_id,
            discord_message_id,
        )
    except Exception:
        logger.exception("SMTP error while sending delete notification for Discord message %s.", discord_message_id)
        return

    deleted_at = datetime.now(timezone.utc).isoformat()
    state.record_delete(discord_message_id, deleted_at)
    logger.info("Sent delete notification for Discord message %s.", discord_message_id)


async def handle_incoming_email(
    client: BridgeClient, config: Config, state: State, incoming: IncomingEmail
) -> bool:
    """Deliver an email reply into Discord (as a real reply when possible) and record the mapping."""
    parent_deleted = bool(
        incoming.parent_discord_message_id and state.is_deleted(incoming.parent_discord_message_id)
    )
    discord_message, was_real_reply = await deliver_email_to_channel(
        client,
        config.discord_channel_id,
        incoming.body,
        reply_to_discord_message_id=incoming.parent_discord_message_id,
        parent_deleted=parent_deleted,
    )
    if discord_message is None:
        return False

    state.add_mapping(
        {
            "bridge_id": str(uuid.uuid4()),
            "discord_message_id": str(discord_message.id),
            "discord_parent_message_id": incoming.parent_discord_message_id if was_real_reply else None,
            "email_message_id": incoming.email_message_id,
            "email_in_reply_to": incoming.in_reply_to,
            "email_references": incoming.references,
            "author_name": incoming.sender,
            "content": incoming.body,
            "delivery_status": "sent",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return True


async def email_poll_loop(client: BridgeClient, config: Config, state: State) -> None:
    """Periodically check the mailbox for replies and forward them to Discord."""
    loop = asyncio.get_running_loop()

    def on_valid_email(incoming: IncomingEmail) -> bool:
        # poll_mailbox runs in a worker thread; hop back onto the bot's
        # event loop to actually send the Discord message.
        future = asyncio.run_coroutine_threadsafe(
            handle_incoming_email(client, config, state, incoming), loop
        )
        try:
            return future.result()
        except Exception:
            logger.exception("Error delivering email from %s to Discord.", incoming.sender)
            return False

    await client.wait_until_ready()

    while not client.is_closed():
        logger.info("Starting mailbox poll cycle.")
        try:
            await loop.run_in_executor(None, poll_mailbox, config, state, on_valid_email)
        except Exception:
            logger.exception("IMAP error while polling mailbox.")
        await asyncio.sleep(config.email_poll_interval_seconds)


async def main() -> None:
    setup_logging()
    logger.info("Starting Discord Email Bridge v%s...", get_version())

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return

    state = State(config.state_file)

    async def on_discord_message(message: discord.Message) -> None:
        await handle_discord_message(config, state, message)

    async def on_discord_message_edit(message: discord.Message) -> None:
        await handle_message_edit(config, state, message)

    async def on_discord_message_delete(discord_message_id: str) -> None:
        await handle_message_delete(config, state, discord_message_id)

    client = BridgeClient(config, on_discord_message, on_discord_message_edit, on_discord_message_delete)

    try:
        await asyncio.gather(
            client.start(config.discord_bot_token),
            email_poll_loop(client, config, state),
        )
    except discord.LoginFailure as exc:
        logger.error("Discord login failed, check DISCORD_BOT_TOKEN: %s", exc)
    finally:
        if not client.is_closed():
            await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Shutting down (KeyboardInterrupt).")
