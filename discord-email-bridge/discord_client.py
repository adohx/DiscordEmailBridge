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
EMAIL_REPLY_DELETED_PREFIX = "📧 Email reply to a deleted Discord message:\n\n"

# Called with the raw discord.Message whenever a valid message arrives in
# the bridged channel (guild/channel already checked, author isn't a bot,
# content isn't empty).
OnDiscordMessage = Callable[[discord.Message], Awaitable[None]]

# Called with the freshly-refetched discord.Message whenever a message in the
# bridged channel is edited (guild/channel already checked, author isn't a
# bot). The callback is responsible for mapping lookup, content comparison
# and dedup -- see discord-message-edit-delete-sync.md #5-6.
OnDiscordMessageEdit = Callable[[discord.Message], Awaitable[None]]

# Called with the discord message id (as a string) whenever a message in the
# bridged channel is deleted (guild/channel already checked). The deleted
# message can no longer be fetched, so the callback must rely on local state
# for author/content -- see discord-message-edit-delete-sync.md #9.
OnDiscordMessageDelete = Callable[[str], Awaitable[None]]


def clean_discord_mentions(text: str) -> str:
    """Neutralize @everyone / @here so forwarded email content can't ping the server."""
    return text.replace("@everyone", "＠everyone").replace("@here", "＠here")


def format_email_reply_for_discord(text: str, *, unavailable: bool = False, deleted: bool = False) -> str:
    if deleted:
        prefix = EMAIL_REPLY_DELETED_PREFIX
    elif unavailable:
        prefix = EMAIL_REPLY_UNAVAILABLE_PREFIX
    else:
        prefix = EMAIL_REPLY_PREFIX
    cleaned = clean_discord_mentions(text)

    body_budget = MAX_MESSAGE_LENGTH - len(prefix)
    if len(cleaned) > body_budget:
        truncate_to = max(body_budget - len(TRUNCATION_NOTICE), 0)
        cleaned = cleaned[:truncate_to] + TRUNCATION_NOTICE

    return prefix + cleaned


class BridgeClient(discord.Client):
    """Discord client that only cares about one guild/channel."""

    def __init__(
        self,
        config: Config,
        on_discord_message: OnDiscordMessage,
        on_discord_message_edit: Optional[OnDiscordMessageEdit] = None,
        on_discord_message_delete: Optional[OnDiscordMessageDelete] = None,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.on_discord_message = on_discord_message
        self.on_discord_message_edit = on_discord_message_edit
        self.on_discord_message_delete = on_discord_message_delete

    async def on_ready(self):
        logger.info("Discord bot connected as %s", self.user)

    def _is_bridged_channel(self, guild_id: Optional[int], channel_id: int) -> bool:
        if self.config.discord_guild_id is not None and guild_id != self.config.discord_guild_id:
            return False
        return channel_id == self.config.discord_channel_id

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

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if self.on_discord_message_edit is None:
            return

        if not self._is_bridged_channel(payload.guild_id, payload.channel_id):
            return

        channel = self.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(payload.channel_id)
            except discord.DiscordException:
                logger.exception("Unable to fetch Discord channel %s for edited message.", payload.channel_id)
                return

        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.DiscordException:
            logger.warning(
                "Unable to fetch edited Discord message %s; it may have been deleted since.", payload.message_id
            )
            return

        if message.author.bot:
            return

        try:
            await self.on_discord_message_edit(message)
        except Exception:
            logger.exception("Error while handling edited Discord message %s.", payload.message_id)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if self.on_discord_message_delete is None:
            return

        if not self._is_bridged_channel(payload.guild_id, payload.channel_id):
            return

        try:
            await self.on_discord_message_delete(str(payload.message_id))
        except Exception:
            logger.exception("Error while handling deleted Discord message %s.", payload.message_id)


async def deliver_email_to_channel(
    client: discord.Client,
    channel_id: int,
    text: str,
    reply_to_discord_message_id: Optional[str] = None,
    parent_deleted: bool = False,
) -> Tuple[Optional[discord.Message], bool]:
    """Deliver an email-derived message into the bridged Discord channel.

    If reply_to_discord_message_id is given, attempts a real Discord reply to
    that message first; falls back to a plain channel message if the
    original message can't be fetched (e.g. deleted) or the reply fails.

    If parent_deleted is True, the parent is already known (via local state)
    to be deleted, so no reply attempt is made at all -- see
    discord-message-edit-delete-sync.md #12.

    Returns (sent_message, was_real_reply). sent_message is None on failure.
    """
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.DiscordException as exc:
            logger.error("Could not find Discord channel %s: %s", channel_id, exc)
            return None, False

    if reply_to_discord_message_id and parent_deleted:
        logger.warning(
            "Discord message %s (parent of an email reply) was deleted; sending a normal channel message instead.",
            reply_to_discord_message_id,
        )
        formatted = format_email_reply_for_discord(text, deleted=True)
    elif reply_to_discord_message_id:
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
