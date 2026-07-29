import discord
import pytest

import discord_client as dc
from mail_reader import EmailAttachment


class TestCleanDiscordMentions:
    def test_neutralizes_everyone(self):
        assert dc.clean_discord_mentions("hey @everyone") == "hey ＠everyone"

    def test_neutralizes_here(self):
        assert dc.clean_discord_mentions("hey @here") == "hey ＠here"

    def test_leaves_other_text_untouched(self):
        assert dc.clean_discord_mentions("hello world") == "hello world"


class TestFormatEmailReplyForDiscord:
    def test_default_prefix(self):
        result = dc.format_email_reply_for_discord("hi")
        assert result == dc.EMAIL_REPLY_PREFIX + "hi"

    def test_unavailable_prefix(self):
        result = dc.format_email_reply_for_discord("hi", unavailable=True)
        assert result.startswith(dc.EMAIL_REPLY_UNAVAILABLE_PREFIX)

    def test_deleted_prefix_takes_priority(self):
        result = dc.format_email_reply_for_discord("hi", unavailable=True, deleted=True)
        assert result.startswith(dc.EMAIL_REPLY_DELETED_PREFIX)

    def test_truncates_long_content(self):
        long_text = "x" * 3000
        result = dc.format_email_reply_for_discord(long_text)
        assert len(result) <= dc.MAX_MESSAGE_LENGTH
        assert result.endswith(dc.TRUNCATION_NOTICE.strip())

    def test_sanitizes_mentions_in_body(self):
        result = dc.format_email_reply_for_discord("ping @everyone now")
        assert "@everyone" not in result

    def test_appends_notes_with_blank_line_when_body_present(self):
        result = dc.format_email_reply_for_discord("hi", notes=["note one", "note two"])
        assert result == dc.EMAIL_REPLY_PREFIX + "hi\n\n⚠️ note one\n⚠️ note two"

    def test_appends_notes_without_leading_blank_line_when_body_empty(self):
        result = dc.format_email_reply_for_discord("", notes=["note one"])
        assert result == dc.EMAIL_REPLY_PREFIX + "⚠️ note one"

    def test_no_notes_block_when_notes_empty(self):
        result = dc.format_email_reply_for_discord("hi", notes=[])
        assert "⚠️" not in result


class TestBuildDiscordFiles:
    def test_builds_one_file_per_attachment(self):
        attachments = [
            EmailAttachment(filename="a.png", content_type="image/png", payload=b"111"),
            EmailAttachment(filename="b.png", content_type="image/png", payload=b"222"),
        ]
        files = dc._build_discord_files(attachments)
        assert len(files) == 2
        assert all(isinstance(f, discord.File) for f in files)
        assert files[0].filename == "a.png"

    def test_empty_list_for_no_attachments(self):
        assert dc._build_discord_files([]) == []


class FakeMessage:
    def __init__(self, mid=999):
        self.id = mid


class FakeChannel:
    """Stands in for a discord.TextChannel, recording every send() call."""

    def __init__(self, fail_with_files=False, raise_on_fetch=False):
        self.fail_with_files = fail_with_files
        self.raise_on_fetch = raise_on_fetch
        self.sent = []

    async def send(self, content, files=None, allowed_mentions=None):
        if files and self.fail_with_files:
            raise discord.DiscordException("simulated upload failure")
        self.sent.append((content, files))
        return FakeMessage()

    async def fetch_message(self, message_id):
        if self.raise_on_fetch:
            raise discord.DiscordException("message not found")
        return FakeReplyTarget(self)


class FakeReplyTarget:
    def __init__(self, channel):
        self.channel = channel

    async def reply(self, content, files=None, allowed_mentions=None):
        return await self.channel.send(content, files=files, allowed_mentions=allowed_mentions)


class FakeClient:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


class TestDeliverEmailToChannel:
    async def test_plain_message_no_attachments(self):
        channel = FakeChannel()
        client = FakeClient(channel)

        message, was_reply = await dc.deliver_email_to_channel(client, 1, "hello")

        assert message is not None
        assert was_reply is False
        assert channel.sent[-1] == (dc.EMAIL_REPLY_PREFIX + "hello", None)

    async def test_sends_image_attachment_successfully(self):
        channel = FakeChannel()
        client = FakeClient(channel)
        attachments = [EmailAttachment("a.png", "image/png", b"123")]

        message, _ = await dc.deliver_email_to_channel(client, 1, "look", attachments=attachments)

        content, files = channel.sent[-1]
        assert message is not None
        assert files is not None and len(files) == 1

    async def test_attachment_notes_appear_in_message_text(self):
        channel = FakeChannel()
        client = FakeClient(channel)

        await dc.deliver_email_to_channel(
            client, 1, "hello", attachment_notes=["Attachment skipped: x.pdf (application/pdf) — unsupported format, only images are forwarded."]
        )

        content, _ = channel.sent[-1]
        assert "⚠️" in content
        assert "x.pdf" in content

    async def test_failed_image_upload_falls_back_to_text_with_notice(self):
        channel = FakeChannel(fail_with_files=True)
        client = FakeClient(channel)
        attachments = [EmailAttachment("a.png", "image/png", b"123")]

        message, _ = await dc.deliver_email_to_channel(client, 1, "hello", attachments=attachments)

        content, files = channel.sent[-1]
        assert message is not None
        assert files is None
        assert "failed to upload" in content

    async def test_reply_to_existing_message_uses_reply(self):
        channel = FakeChannel()
        client = FakeClient(channel)

        message, was_reply = await dc.deliver_email_to_channel(
            client, 1, "hello", reply_to_discord_message_id="123"
        )

        assert was_reply is True
        assert message is not None

    async def test_reply_falls_back_to_plain_message_when_parent_missing(self):
        channel = FakeChannel(raise_on_fetch=True)
        client = FakeClient(channel)

        message, was_reply = await dc.deliver_email_to_channel(
            client, 1, "hello", reply_to_discord_message_id="123"
        )

        assert was_reply is False
        assert message is not None
        assert channel.sent[-1][0].startswith(dc.EMAIL_REPLY_UNAVAILABLE_PREFIX)

    async def test_parent_deleted_skips_reply_attempt_entirely(self):
        channel = FakeChannel(raise_on_fetch=True)  # would blow up if fetch_message were ever called
        client = FakeClient(channel)

        message, was_reply = await dc.deliver_email_to_channel(
            client, 1, "hello", reply_to_discord_message_id="123", parent_deleted=True
        )

        assert was_reply is False
        assert channel.sent[-1][0].startswith(dc.EMAIL_REPLY_DELETED_PREFIX)

    async def test_channel_not_found_returns_none(self):
        class MissingChannelClient:
            def get_channel(self, channel_id):
                return None

            async def fetch_channel(self, channel_id):
                raise discord.DiscordException("no such channel")

        message, was_reply = await dc.deliver_email_to_channel(MissingChannelClient(), 1, "hello")

        assert message is None
        assert was_reply is False
