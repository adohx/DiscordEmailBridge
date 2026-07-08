"""Discord side of the bridge: receive channel messages, send email replies back."""

import logging
from typing import Awaitable, Callable

import discord

from config import Config

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 1800
TRUNCATION_NOTICE = "\n\n[Message truncated]"
EMAIL_REPLY_PREFIX = "📧 Email reply:\n\n"

# Called with (author_display_name, message_content) whenever a valid
# message arrives in the bridged channel.
OnDiscordMessage = Callable[[str, str], Awaitable[None]]


def clean_discord_mentions(text: str) -> str:
    """Neutralize @everyone / @here so forwarded email content can't ping the server."""
    return text.replace("@everyone", "＠everyone").replace("@here", "＠here")


def format_email_reply_for_discord(text: str) -> str:
    cleaned = clean_discord_mentions(text)

    body_budget = MAX_MESSAGE_LENGTH - len(EMAIL_REPLY_PREFIX)
    if len(cleaned) > body_budget:
        truncate_to = max(body_budget - len(TRUNCATION_NOTICE), 0)
        cleaned = cleaned[:truncate_to] + TRUNCATION_NOTICE

    return EMAIL_REPLY_PREFIX + cleaned


class BridgeClient(discord.Client):
    """Discord client that only cares about one channel."""

    def __init__(self, config: Config, on_discord_message: OnDiscordMessage):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.on_discord_message = on_discord_message

    async def on_ready(self):
        logger.info("Discord bot connected as %s", self.user)

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            logger.info("Ignoring Discord message from bot user %s.", message.author)
            return

        if message.channel.id != self.config.discord_channel_id:
            logger.info("Ignoring Discord message from non-bridged channel %s.", message.channel.id)
            return

        content = (message.content or "").strip()
        if not content:
            logger.info("Ignoring empty Discord message from %s.", message.author)
            return

        logger.info("Received Discord message from %s.", message.author)
        try:
            await self.on_discord_message(str(message.author.display_name), content)
        except Exception:
            logger.exception("Error while handling Discord message from %s.", message.author)


async def send_email_reply_to_channel(client: discord.Client, channel_id: int, text: str) -> bool:
    """Send an email-derived reply into the bridged Discord channel.

    Returns True on success, False on failure (so the caller can decide
    whether to mark the source email as processed).
    """
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.DiscordException as exc:
            logger.error("Could not find Discord channel %s: %s", channel_id, exc)
            return False

    formatted = format_email_reply_for_discord(text)

    try:
        await channel.send(formatted, allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.DiscordException as exc:
        logger.error("Discord API error while sending message to channel %s: %s", channel_id, exc)
        return False
