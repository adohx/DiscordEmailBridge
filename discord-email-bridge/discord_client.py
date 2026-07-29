"""Discord side of the bridge: receive channel messages, deliver email replies back."""

import io
import logging
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

import discord

from config import Config
from mail_reader import EmailAttachment

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 1800
TRUNCATION_NOTICE = "\n\n[Message truncated]"
EMAIL_REPLY_PREFIX_TEMPLATE = "📧 Email reply from {name}:\n\n"
EMAIL_REPLY_UNAVAILABLE_PREFIX_TEMPLATE = "📧 Email reply from {name} to an unavailable message:\n\n"
EMAIL_REPLY_DELETED_PREFIX_TEMPLATE = "📧 Email reply from {name} to a deleted Discord message:\n\n"

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

# Called with (discord_message_id, emoji_display, reactor_name) whenever a
# reaction is added to a message in the bridged channel (guild/channel
# already checked, reactor isn't a bot). The callback decides whether this
# message is one the email user cares about (see main.handle_reaction_add).
OnDiscordReactionAdd = Callable[[str, str, str], Awaitable[None]]


def clean_discord_mentions(text: str) -> str:
    """Neutralize @everyone / @here so forwarded email content can't ping the server."""
    return text.replace("@everyone", "＠everyone").replace("@here", "＠here")


def format_email_reply_for_discord(
    text: str,
    sender_name: str,
    *,
    unavailable: bool = False,
    deleted: bool = False,
    notes: Optional[Sequence[str]] = None,
) -> str:
    if deleted:
        template = EMAIL_REPLY_DELETED_PREFIX_TEMPLATE
    elif unavailable:
        template = EMAIL_REPLY_UNAVAILABLE_PREFIX_TEMPLATE
    else:
        template = EMAIL_REPLY_PREFIX_TEMPLATE
    prefix = template.format(name=clean_discord_mentions(sender_name))
    cleaned = clean_discord_mentions(text)

    body_budget = MAX_MESSAGE_LENGTH - len(prefix)
    if len(cleaned) > body_budget:
        truncate_to = max(body_budget - len(TRUNCATION_NOTICE), 0)
        cleaned = cleaned[:truncate_to] + TRUNCATION_NOTICE

    formatted = prefix + cleaned
    if notes:
        separator = "\n\n" if cleaned else ""
        formatted += separator + "\n".join(f"⚠️ {note}" for note in notes)
    return formatted


def _build_discord_files(attachments: Sequence[EmailAttachment]) -> List[discord.File]:
    """Build fresh discord.File objects -- they're single-use and can't be resent."""
    return [discord.File(io.BytesIO(att.payload), filename=att.filename) for att in attachments]


async def _send_with_attachment_fallback(send, text: str, files: List[discord.File]) -> discord.Message:
    """Try sending with files attached; on failure, retry text-only with a notice.

    `send` is an async callable taking (content, files) -> discord.Message. Raises
    discord.DiscordException if even the text-only retry fails.
    """
    if files:
        try:
            return await send(text, files)
        except discord.DiscordException:
            logger.exception("Failed to upload %d attachment(s) to Discord; retrying without them.", len(files))
            text += f"\n\n⚠️ {len(files)} attachment(s) failed to upload and were not forwarded."
    return await send(text, None)


class BridgeClient(discord.Client):
    """Discord client that only cares about one guild/channel."""

    def __init__(
        self,
        config: Config,
        on_discord_message: OnDiscordMessage,
        on_discord_message_edit: Optional[OnDiscordMessageEdit] = None,
        on_discord_message_delete: Optional[OnDiscordMessageDelete] = None,
        on_discord_reaction_add: Optional[OnDiscordReactionAdd] = None,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.on_discord_message = on_discord_message
        self.on_discord_message_edit = on_discord_message_edit
        self.on_discord_message_delete = on_discord_message_delete
        self.on_discord_reaction_add = on_discord_reaction_add

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

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.on_discord_reaction_add is None:
            return

        if not self._is_bridged_channel(payload.guild_id, payload.channel_id):
            return

        if payload.member is not None and payload.member.bot:
            return

        reactor_name = payload.member.display_name if payload.member is not None else str(payload.user_id)

        try:
            await self.on_discord_reaction_add(str(payload.message_id), str(payload.emoji), reactor_name)
        except Exception:
            logger.exception("Error while handling reaction on Discord message %s.", payload.message_id)


async def deliver_email_to_channel(
    client: discord.Client,
    channel_id: int,
    text: str,
    sender_name: str,
    reply_to_discord_message_id: Optional[str] = None,
    parent_deleted: bool = False,
    attachments: Optional[Sequence[EmailAttachment]] = None,
    attachment_notes: Optional[Sequence[str]] = None,
) -> Tuple[Optional[discord.Message], bool]:
    """Deliver an email-derived message into the bridged Discord channel.

    `sender_name` is shown in the message prefix ("Email reply from {name}")
    so the channel can tell who wrote it -- see config.ALLOWED_EMAIL_SENDER_NAME.

    If reply_to_discord_message_id is given, attempts a real Discord reply to
    that message first; falls back to a plain channel message if the
    original message can't be fetched (e.g. deleted) or the reply fails.

    If parent_deleted is True, the parent is already known (via local state)
    to be deleted, so no reply attempt is made at all -- see
    discord-message-edit-delete-sync.md #12.

    `attachments` are forwardable attachments (images, PDF, DOC, DOCX)
    extracted from the source email; they are uploaded as Discord files.
    `attachment_notes` are human-readable notices about attachments that were
    NOT forwarded (wrong format, too large) -- always appended to the message
    text so nothing is dropped silently. If uploading the files themselves
    fails, the message is retried without them and a notice is appended too.

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
        formatted = format_email_reply_for_discord(text, sender_name, deleted=True, notes=attachment_notes)
    elif reply_to_discord_message_id:
        try:
            original_message = await channel.fetch_message(int(reply_to_discord_message_id))
            formatted = format_email_reply_for_discord(text, sender_name, unavailable=False, notes=attachment_notes)

            async def _reply(content: str, files: Optional[List[discord.File]]) -> discord.Message:
                return await original_message.reply(
                    content, files=files or None, allowed_mentions=discord.AllowedMentions.none()
                )

            sent = await _send_with_attachment_fallback(_reply, formatted, _build_discord_files(attachments or []))
            return sent, True
        except (discord.DiscordException, ValueError) as exc:
            logger.warning(
                "Could not reply to Discord message %s (%s); falling back to a normal channel message.",
                reply_to_discord_message_id,
                exc,
            )
            formatted = format_email_reply_for_discord(text, sender_name, unavailable=True, notes=attachment_notes)
    else:
        formatted = format_email_reply_for_discord(text, sender_name, unavailable=False, notes=attachment_notes)

    try:
        async def _channel_send(content: str, files: Optional[List[discord.File]]) -> discord.Message:
            return await channel.send(content, files=files or None, allowed_mentions=discord.AllowedMentions.none())

        sent = await _send_with_attachment_fallback(_channel_send, formatted, _build_discord_files(attachments or []))
        return sent, False
    except discord.DiscordException as exc:
        logger.error("Discord API error while sending message to channel %s: %s", channel_id, exc)
        return None, False
