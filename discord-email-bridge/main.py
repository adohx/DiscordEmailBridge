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
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import discord

import mail_sender
from config import Config, ConfigError, load_config
from discord_client import BridgeClient, deliver_email_to_channel
from mail_reader import IncomingEmail, poll_mailbox
from state import State

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


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


async def handle_incoming_email(
    client: BridgeClient, config: Config, state: State, incoming: IncomingEmail
) -> bool:
    """Deliver an email reply into Discord (as a real reply when possible) and record the mapping."""
    discord_message, was_real_reply = await deliver_email_to_channel(
        client,
        config.discord_channel_id,
        incoming.body,
        reply_to_discord_message_id=incoming.parent_discord_message_id,
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
    logger.info("Starting Discord Email Bridge...")

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return

    state = State(config.state_file)

    async def on_discord_message(message: discord.Message) -> None:
        await handle_discord_message(config, state, message)

    client = BridgeClient(config, on_discord_message)

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
