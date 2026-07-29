import json

import pytest

from state import State, normalize_content, normalize_message_id


class TestNormalizeMessageId:
    def test_adds_angle_brackets(self):
        assert normalize_message_id("abc@example.com") == "<abc@example.com>"

    def test_leaves_already_bracketed_alone(self):
        assert normalize_message_id("<abc@example.com>") == "<abc@example.com>"

    def test_strips_surrounding_whitespace(self):
        assert normalize_message_id("  <abc@example.com>  ") == "<abc@example.com>"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_returns_none_for_empty_input(self, value):
        assert normalize_message_id(value) is None


class TestNormalizeContent:
    def test_returns_empty_string_for_none(self):
        assert normalize_content(None) == ""

    def test_strips_surrounding_whitespace(self):
        assert normalize_content("  hello  ") == "hello"

    def test_normalizes_crlf_to_lf(self):
        assert normalize_content("line1\r\nline2") == "line1\nline2"


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "state.json")


class TestStateFreshFile:
    def test_creates_file_on_first_load(self, state_file):
        State(state_file)
        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)
        assert data["processed_email_ids"] == []
        assert data["message_mappings"] == {}


class TestStateMapping:
    def test_add_and_get_by_discord_message_id(self, state_file):
        state = State(state_file)
        state.add_mapping(
            {
                "bridge_id": "b1",
                "discord_message_id": "d1",
                "email_message_id": "<e1@x>",
                "author_name": "Alice",
                "content": "hi",
            }
        )
        assert state.has_discord_message("d1")
        mapping = state.get_by_discord_message_id("d1")
        assert mapping["author_name"] == "Alice"
        # Lifecycle defaults get filled in.
        assert mapping["status"] == "active"
        assert mapping["edit_version"] == 0

    def test_get_by_discord_message_id_missing_returns_none(self, state_file):
        state = State(state_file)
        assert state.get_by_discord_message_id("nope") is None

    def test_get_by_email_message_id_normalizes_lookup(self, state_file):
        state = State(state_file)
        state.add_mapping(
            {"bridge_id": "b1", "discord_message_id": "d1", "email_message_id": "e1@x"}
        )
        # Looked up without the angle brackets -- normalization should still match.
        assert state.get_by_email_message_id("e1@x")["discord_message_id"] == "d1"
        assert state.get_by_email_message_id("<e1@x>")["discord_message_id"] == "d1"

    def test_get_by_email_message_id_none_input(self, state_file):
        state = State(state_file)
        assert state.get_by_email_message_id(None) is None

    def test_get_by_bridge_id(self, state_file):
        state = State(state_file)
        state.add_mapping({"bridge_id": "b1", "discord_message_id": "d1"})
        assert state.get_by_bridge_id("b1")["discord_message_id"] == "d1"
        assert state.get_by_bridge_id("missing") is None

    def test_persists_across_reload(self, state_file):
        state = State(state_file)
        state.add_mapping({"bridge_id": "b1", "discord_message_id": "d1", "content": "hi"})

        reloaded = State(state_file)
        assert reloaded.get_by_discord_message_id("d1")["content"] == "hi"


class TestStateEditDeleteLifecycle:
    def test_record_edit_updates_content_and_version(self, state_file):
        state = State(state_file)
        state.add_mapping({"bridge_id": "b1", "discord_message_id": "d1", "content": "old"})

        state.record_edit("d1", "new", "fingerprint1", "2026-01-01T00:00:00+00:00")

        mapping = state.get_by_discord_message_id("d1")
        assert mapping["content"] == "new"
        assert mapping["edit_version"] == 1
        assert mapping["last_edit_fingerprint"] == "fingerprint1"

    def test_record_edit_on_unknown_message_is_a_noop(self, state_file):
        state = State(state_file)
        # Should not raise.
        state.record_edit("missing", "new", "fp", "2026-01-01T00:00:00+00:00")

    def test_record_delete_marks_status_and_flag(self, state_file):
        state = State(state_file)
        state.add_mapping({"bridge_id": "b1", "discord_message_id": "d1"})

        state.record_delete("d1", "2026-01-01T00:00:00+00:00")

        mapping = state.get_by_discord_message_id("d1")
        assert mapping["status"] == "deleted"
        assert mapping["delete_notification_sent"] is True
        assert state.is_deleted("d1") is True

    def test_is_deleted_false_for_active_or_unknown(self, state_file):
        state = State(state_file)
        state.add_mapping({"bridge_id": "b1", "discord_message_id": "d1"})
        assert state.is_deleted("d1") is False
        assert state.is_deleted("unknown") is False


class TestStateEmailDedup:
    def test_mark_and_check_processed(self, state_file):
        state = State(state_file)
        assert state.is_email_processed("<e1@x>") is False
        state.mark_email_processed("<e1@x>")
        assert state.is_email_processed("<e1@x>") is True

    def test_processed_ids_persist_across_reload(self, state_file):
        state = State(state_file)
        state.mark_email_processed("<e1@x>")
        reloaded = State(state_file)
        assert reloaded.is_email_processed("<e1@x>") is True


class TestStateCorruptFileRecovery:
    def test_corrupt_json_backs_up_and_resets(self, state_file):
        with open(state_file, "w", encoding="utf-8") as f:
            f.write("{not valid json")

        state = State(state_file)

        assert state.message_mappings == {}
        assert state.processed_email_ids == set()
        # A .corrupt-<timestamp> backup should exist next to the original.
        backups = list((__import__("pathlib").Path(state_file).parent).glob("state.json.corrupt-*"))
        assert len(backups) == 1

    def test_wrong_shape_json_backs_up_and_resets(self, state_file):
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({"processed_email_ids": "not-a-list"}, f)

        state = State(state_file)

        assert state.message_mappings == {}
