from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
from conftest import make_config
from mail_reader import IncomingEmail
from state import State


def make_discord_message(
    message_id=1,
    author_name="Alice",
    content="hello",
    reference_to=None,
):
    reference = SimpleNamespace(message_id=reference_to) if reference_to is not None else None
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(display_name=author_name),
        content=content,
        reference=reference,
    )


@pytest.fixture
def state(tmp_path):
    return State(str(tmp_path / "state.json"))


@pytest.fixture
def config():
    return make_config()


class TestHandleDiscordMessage:
    async def test_sends_email_and_records_mapping(self, config, state):
        message = make_discord_message(message_id=1, author_name="Alice", content="hi there")

        with patch("mail_sender.send_discord_message_as_email") as send_mock:
            await main.handle_discord_message(config, state, message)

        send_mock.assert_called_once()
        mapping = state.get_by_discord_message_id("1")
        assert mapping["author_name"] == "Alice"
        assert mapping["content"] == "hi there"
        assert mapping["delivery_status"] == "sent"

    async def test_skips_already_mapped_message(self, config, state):
        state.add_mapping({"bridge_id": "b1", "discord_message_id": "1", "content": "old"})
        message = make_discord_message(message_id=1)

        with patch("mail_sender.send_discord_message_as_email") as send_mock:
            await main.handle_discord_message(config, state, message)

        send_mock.assert_not_called()

    async def test_smtp_failure_does_not_record_mapping(self, config, state):
        message = make_discord_message(message_id=2)

        with patch("mail_sender.send_discord_message_as_email", side_effect=OSError("smtp down")):
            await main.handle_discord_message(config, state, message)

        assert state.get_by_discord_message_id("2") is None

    async def test_reply_with_known_parent_builds_reply_context(self, config, state):
        state.add_mapping(
            {
                "bridge_id": "parent-bridge",
                "discord_message_id": "10",
                "author_name": "Bob",
                "content": "original",
                "email_message_id": "<parent@bridge.local>",
                "email_references": [],
            }
        )
        message = make_discord_message(message_id=11, author_name="Alice", content="reply text", reference_to=10)

        with patch("mail_sender.send_discord_message_as_email") as send_mock:
            await main.handle_discord_message(config, state, message)

        _, kwargs = send_mock.call_args
        assert kwargs["reply_context"] == ("Bob", "original")
        assert kwargs["in_reply_to"] == "<parent@bridge.local>"
        assert kwargs["references"] == ["<parent@bridge.local>"]

        mapping = state.get_by_discord_message_id("11")
        assert mapping["discord_parent_message_id"] == "10"

    async def test_reply_with_unknown_parent_still_sends(self, config, state):
        message = make_discord_message(message_id=12, reference_to=999)

        with patch("mail_sender.send_discord_message_as_email") as send_mock:
            await main.handle_discord_message(config, state, message)

        send_mock.assert_called_once()
        mapping = state.get_by_discord_message_id("12")
        assert mapping["discord_parent_message_id"] is None


class TestHandleMessageEdit:
    async def _seed_mapping(self, state, **overrides):
        mapping = {
            "bridge_id": "b1",
            "discord_message_id": "1",
            "author_name": "Alice",
            "content": "original content",
            "email_message_id": "<msg1@bridge.local>",
        }
        mapping.update(overrides)
        state.add_mapping(mapping)

    async def test_ignores_unmapped_message(self, config, state):
        message = make_discord_message(message_id=999, content="new")
        with patch("mail_sender.send_edit_notification") as send_mock:
            await main.handle_message_edit(config, state, message)
        send_mock.assert_not_called()

    async def test_ignores_already_deleted_message(self, config, state):
        await self._seed_mapping(state, status="deleted")
        message = make_discord_message(message_id=1, content="new content")
        with patch("mail_sender.send_edit_notification") as send_mock:
            await main.handle_message_edit(config, state, message)
        send_mock.assert_not_called()

    async def test_ignores_unchanged_content(self, config, state):
        await self._seed_mapping(state, content="same")
        message = make_discord_message(message_id=1, content="same")
        with patch("mail_sender.send_edit_notification") as send_mock:
            await main.handle_message_edit(config, state, message)
        send_mock.assert_not_called()

    async def test_missing_email_message_id_skips_send(self, config, state):
        await self._seed_mapping(state, email_message_id=None)
        message = make_discord_message(message_id=1, content="changed")
        with patch("mail_sender.send_edit_notification") as send_mock:
            await main.handle_message_edit(config, state, message)
        send_mock.assert_not_called()

    async def test_sends_notification_and_updates_state(self, config, state):
        await self._seed_mapping(state, content="original content")
        message = make_discord_message(message_id=1, content="updated content")

        with patch("mail_sender.send_edit_notification") as send_mock:
            await main.handle_message_edit(config, state, message)

        send_mock.assert_called_once()
        mapping = state.get_by_discord_message_id("1")
        assert mapping["content"] == "updated content"
        assert mapping["edit_version"] == 1

    async def test_smtp_failure_does_not_update_state(self, config, state):
        await self._seed_mapping(state, content="original content")
        message = make_discord_message(message_id=1, content="updated content")

        with patch("mail_sender.send_edit_notification", side_effect=OSError("smtp down")):
            await main.handle_message_edit(config, state, message)

        mapping = state.get_by_discord_message_id("1")
        assert mapping["content"] == "original content"
        assert mapping["edit_version"] == 0


class TestHandleMessageDelete:
    async def _seed_mapping(self, state, **overrides):
        mapping = {
            "bridge_id": "b1",
            "discord_message_id": "1",
            "author_name": "Alice",
            "content": "original content",
            "email_message_id": "<msg1@bridge.local>",
        }
        mapping.update(overrides)
        state.add_mapping(mapping)

    async def test_ignores_unmapped_message(self, config, state):
        with patch("mail_sender.send_delete_notification") as send_mock:
            await main.handle_message_delete(config, state, "999")
        send_mock.assert_not_called()

    async def test_ignores_already_notified_delete(self, config, state):
        await self._seed_mapping(state, delete_notification_sent=True)
        with patch("mail_sender.send_delete_notification") as send_mock:
            await main.handle_message_delete(config, state, "1")
        send_mock.assert_not_called()

    async def test_missing_email_message_id_skips_send(self, config, state):
        await self._seed_mapping(state, email_message_id=None)
        with patch("mail_sender.send_delete_notification") as send_mock:
            await main.handle_message_delete(config, state, "1")
        send_mock.assert_not_called()

    async def test_include_deleted_content_true_passes_content(self, state):
        config = make_config(include_deleted_content=True)
        await self._seed_mapping(state, content="secret content")

        with patch("mail_sender.send_delete_notification") as send_mock:
            await main.handle_message_delete(config, state, "1")

        args, _ = send_mock.call_args
        assert args[2] == "secret content"

    async def test_include_deleted_content_false_omits_content(self, state):
        config = make_config(include_deleted_content=False)
        await self._seed_mapping(state, content="secret content")

        with patch("mail_sender.send_delete_notification") as send_mock:
            await main.handle_message_delete(config, state, "1")

        args, _ = send_mock.call_args
        assert args[2] is None

    async def test_sends_and_marks_state_deleted(self, config, state):
        await self._seed_mapping(state)

        with patch("mail_sender.send_delete_notification") as send_mock:
            await main.handle_message_delete(config, state, "1")

        send_mock.assert_called_once()
        assert state.is_deleted("1") is True


class TestHandleIncomingEmail:
    def make_incoming(self, **overrides):
        defaults = dict(
            sender="user@example.com",
            sender_name="Alice",
            body="hello from email",
            email_message_id="<e1@x>",
            in_reply_to=None,
            references=[],
            parent_discord_message_id=None,
        )
        defaults.update(overrides)
        return IncomingEmail(**defaults)

    async def test_successful_delivery_records_mapping(self, config, state):
        incoming = self.make_incoming()
        fake_message = SimpleNamespace(id=555)

        with patch("main.deliver_email_to_channel", return_value=(fake_message, False)) as deliver_mock:
            result = await main.handle_incoming_email(client=object(), config=config, state=state, incoming=incoming)

        assert result is True
        deliver_mock.assert_called_once()
        mapping = state.get_by_discord_message_id("555")
        assert mapping["author_name"] == "user@example.com"

    async def test_failed_delivery_returns_false_and_no_mapping(self, config, state):
        incoming = self.make_incoming()

        with patch("main.deliver_email_to_channel", return_value=(None, False)):
            result = await main.handle_incoming_email(client=object(), config=config, state=state, incoming=incoming)

        assert result is False
        assert state.get_by_discord_message_id("555") is None

    async def test_parent_deleted_flag_passed_through(self, config, state):
        state.add_mapping({"bridge_id": "b1", "discord_message_id": "10"})
        state.record_delete("10", "2026-01-01T00:00:00+00:00")
        incoming = self.make_incoming(parent_discord_message_id="10")
        fake_message = SimpleNamespace(id=556)

        with patch("main.deliver_email_to_channel", return_value=(fake_message, False)) as deliver_mock:
            await main.handle_incoming_email(client=object(), config=config, state=state, incoming=incoming)

        _, kwargs = deliver_mock.call_args
        assert kwargs["parent_deleted"] is True

    async def test_attachments_and_notes_passed_through(self, config, state):
        from mail_reader import EmailAttachment

        incoming = self.make_incoming(
            attachments=[EmailAttachment("a.png", "image/png", b"123")],
            attachment_notes=["note"],
        )
        fake_message = SimpleNamespace(id=557)

        with patch("main.deliver_email_to_channel", return_value=(fake_message, False)) as deliver_mock:
            await main.handle_incoming_email(client=object(), config=config, state=state, incoming=incoming)

        _, kwargs = deliver_mock.call_args
        assert len(kwargs["attachments"]) == 1
        assert kwargs["attachment_notes"] == ["note"]
