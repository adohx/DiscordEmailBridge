"""Discord side of the bridge: receive channel messages, deliver email replies back."""

import logging
from typing import Awaitable, Callable, Optional, Tuple

import discord

from config import Config

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 1800
TRUNCATION_NOTICE = "\n\n[Message truncated]"
EMAIL_REPLY_PREFIX = "📧 Email reply:\n\n"
EMAIL_REPLY_UNAVAILABLE_PREFIX = "📧 Email reply to an unavailable message:\n\n"

# Called with the raw discord.Message whenever a valid message arrives in
# the bridged channel (guild/channel already checked, author isn't a bot,
# content isn't empty).
OnDiscordMessage = Callable[[discord.Message], Awaitable[None]]


def clean_discord_mentions(text: str) -> str:
    """Neutralize @everyone / @here so forwarded email content can't ping the server."""
    return text.replace("@everyone", "＠everyone").replace("@here", "＠here")


def format_email_reply_for_discord(text: str, *, unavailable: bool = False) -> str:
    prefix = EMAIL_REPLY_UNAVAILABLE_PREFIX if unavailable else EMAIL_REPLY_PREFIX
    cleaned = clean_discord_mentions(text)

    body_budget = MAX_MESSAGE_LENGTH - len(prefix)
    if len(cleaned) > body_budget:
        truncate_to = max(body_budget - len(TRUNCATION_NOTICE), 0)
        cleaned = cleaned[:truncate_to] + TRUNCATION_NOTICE

    return prefix + cleaned


class BridgeClient(discord.Client):
    """Discord client that only cares about one guild/channel."""

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

        if self.config.discord_guild_id is not None:
            guild_id = message.guild.id if message.guild else None
            if guild_id != self.config.discord_guild_id:
                logger.info("Ignoring Discord message from non-bridged guild %s.", guild_id)
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
            await self.on_discord_message(message)
        except Exception:
            logger.exception("Error while handling Discord message from %s.", message.author)


async def deliver_email_to_channel(
    client: discord.Client,
    channel_id: int,
    text: str,
    reply_to_discord_message_id: Optional[str] = None,
) -> Tuple[Optional[discord.Message], bool]:
    """Deliver an email-derived message into the bridged Discord channel.

    If reply_to_discord_message_id is given, attempts a real Discord reply to
    that message first; falls back to a plain channel message if the
    original message can't be fetched (e.g. deleted) or the reply fails.

    Returns (sent_message, was_real_reply). sent_message is None on failure.
    """
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.DiscordException as exc:
            logger.error("Could not find Discord channel %s: %s", channel_id, exc)
            return None, False

    if reply_to_discord_message_id:
        try:
            original_message = await channel.fetch_message(int(reply_to_discord_message_id))
            formatted = format_email_reply_for_discord(text, unavailable=False)
            sent = await original_message.reply(formatted, allowed_mentions=discord.AllowedMentions.none())
            return sent, True
        except (discord.DiscordException, ValueError) as exc:
            logger.warning(
                "Could not reply to Discord message %s (%s); falling back to a normal channel message.",
                reply_to_discord_message_id,
                exc,
            )
            formatted = format_email_reply_for_discord(text, unavailable=True)
    else:
        formatted = format_email_reply_for_discord(text, unavailable=False)

    try:
        sent = await channel.send(formatted, allowed_mentions=discord.AllowedMentions.none())
        return sent, False
    except discord.DiscordException as exc:
        logger.error("Discord API error while sending message to channel %s: %s", channel_id, exc)
        return None, False
