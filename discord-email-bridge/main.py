"""Entry point: wires together the Discord client and the email polling loop.

    main.py
      load config
      load state
      start Discord client
      start email polling loop
"""

import asyncio
import logging

import discord

import mail_sender
from config import ConfigError, load_config
from discord_client import BridgeClient, send_email_reply_to_channel
from mail_reader import poll_mailbox
from state import State

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def email_poll_loop(client: BridgeClient, config, state: State) -> None:
    """Periodically check the mailbox for replies and forward them to Discord."""
    loop = asyncio.get_running_loop()

    def on_valid_email(sender: str, text: str) -> bool:
        # poll_mailbox runs in a worker thread; hop back onto the bot's
        # event loop to actually send the Discord message.
        future = asyncio.run_coroutine_threadsafe(
            send_email_reply_to_channel(client, config.discord_channel_id, text), loop
        )
        try:
            return future.result()
        except Exception:
            logger.exception("Error delivering email from %s to Discord.", sender)
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

    async def handle_discord_message(author_name: str, content: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, mail_sender.send_discord_message_as_email, config, author_name, content
            )
        except Exception:
            logger.exception("SMTP error while sending Discord message to %s.", config.target_email)

    client = BridgeClient(config, handle_discord_message)

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
