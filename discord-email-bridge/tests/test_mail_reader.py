from types import SimpleNamespace

import pytest

from conftest import make_config
from mail_reader import (
    MAX_ATTACHMENT_BYTES,
    _extract_forwardable_attachments,
    _extract_plain_text,
    _get_email_id,
    _parse_references,
    _resolve_parent_discord_message_id,
    _resolve_sender_name,
    strip_quoted_history,
)
from state import State


def make_msg(headers=None, text="", html="", uid="1", attachments=None):
    """A minimal stand-in for imap_tools.MailMessage covering the attributes
    mail_reader.py actually touches."""
    return SimpleNamespace(
        headers={k: [v] for k, v in (headers or {}).items()},
        text=text,
        html=html,
        uid=uid,
        attachments=attachments or [],
    )


def make_attachment(filename, content_type, payload=b"data"):
    return SimpleNamespace(filename=filename, content_type=content_type, payload=payload, size=len(payload))


class TestStripQuotedHistory:
    def test_keeps_plain_reply_untouched(self):
        assert strip_quoted_history("hello there") == "hello there"

    def test_drops_gt_quoted_lines(self):
        text = "my reply\n> quoted line 1\n> quoted line 2"
        assert strip_quoted_history(text) == "my reply"

    def test_stops_at_on_wrote_marker(self):
        text = "my reply\nOn Mon, Jan 1, 2026 at 10:00 AM Alice <a@x.com> wrote:\nold content"
        assert strip_quoted_history(text) == "my reply"

    def test_stops_at_original_message_marker(self):
        text = "my reply\n-----Original Message-----\nFrom: bob@x.com"
        assert strip_quoted_history(text) == "my reply"

    def test_stops_at_from_header_marker(self):
        text = "my reply\nFrom: bob@x.com\nSubject: hi"
        assert strip_quoted_history(text) == "my reply"

    def test_returns_empty_for_pure_quote(self):
        assert strip_quoted_history("> only quoted content") == ""

    def test_keeps_content_after_forwarded_message_header_block(self):
        text = (
            "FYI\n"
            "---------- Forwarded message ---------\n"
            "From: Someone <someone@example.com>\n"
            "Date: Mon, Jan 1, 2026 at 10:00 AM\n"
            "Subject: Project update\n"
            "To: Jason <jason@example.com>\n"
            "\n"
            "Here is the actual forwarded content that must survive."
        )
        assert strip_quoted_history(text) == (
            "FYI\n"
            "---------- Forwarded message ---------\n"
            "Here is the actual forwarded content that must survive."
        )

    def test_forwarded_content_with_gt_quotes_still_stripped(self):
        text = (
            "---------- Forwarded message ---------\n"
            "From: Someone <someone@example.com>\n"
            "Subject: hi\n"
            "\n"
            "real content\n"
            "> some quoted line inside the forward\n"
        )
        assert strip_quoted_history(text) == "---------- Forwarded message ---------\nreal content"

    def test_forwarded_content_still_stops_at_a_later_reply_marker(self):
        text = (
            "---------- Forwarded message ---------\n"
            "From: Someone <someone@example.com>\n"
            "Subject: hi\n"
            "\n"
            "real content\n"
            "On Mon, Jan 1, 2026 Alice wrote:\n"
            "old quoted stuff\n"
        )
        assert strip_quoted_history(text) == "---------- Forwarded message ---------\nreal content"


class TestGetEmailId:
    def test_uses_normalized_message_id_when_present(self):
        msg = make_msg(headers={"message-id": "abc@example.com"})
        assert _get_email_id(msg) == "<abc@example.com>"

    def test_falls_back_to_uid(self):
        msg = make_msg(uid="42")
        assert _get_email_id(msg) == "imap:42"


class TestParseReferences:
    def test_empty_when_no_header(self):
        assert _parse_references(make_msg()) == []

    def test_splits_and_normalizes_multiple_ids(self):
        msg = make_msg(headers={"references": "<a@x> b@y  <c@z>"})
        assert _parse_references(msg) == ["<a@x>", "<b@y>", "<c@z>"]


class TestExtractPlainText:
    def test_returns_text_when_present(self):
        msg = make_msg(text="hello")
        assert _extract_plain_text(msg) == "hello"

    def test_returns_none_for_whitespace_only_text_and_no_html(self):
        msg = make_msg(text="   ")
        assert _extract_plain_text(msg) is None

    def test_returns_none_for_html_only(self):
        msg = make_msg(text="", html="<p>hi</p>")
        assert _extract_plain_text(msg) is None

    def test_returns_none_for_completely_empty(self):
        assert _extract_plain_text(make_msg()) is None


class TestExtractImageAttachments:
    def test_no_attachments_returns_empty(self):
        images, notes = _extract_image_attachments(make_msg())
        assert images == []
        assert notes == []

    def test_accepts_image_under_limit(self):
        msg = make_msg(attachments=[make_attachment("cat.png", "image/png", b"x" * 100)])
        images, notes = _extract_image_attachments(msg)
        assert len(images) == 1
        assert images[0].filename == "cat.png"
        assert images[0].payload == b"x" * 100
        assert notes == []

    def test_rejects_non_image_with_note(self):
        msg = make_msg(attachments=[make_attachment("doc.pdf", "application/pdf")])
        images, notes = _extract_image_attachments(msg)
        assert images == []
        assert len(notes) == 1
        assert "doc.pdf" in notes[0]
        assert "unsupported format" in notes[0]

    def test_rejects_oversized_image_with_note(self):
        oversized = make_attachment("huge.jpg", "image/jpeg", b"x" * (MAX_ATTACHMENT_BYTES + 1))
        images, notes = _extract_image_attachments(make_msg(attachments=[oversized]))
        assert images == []
        assert len(notes) == 1
        assert "huge.jpg" in notes[0]
        assert "exceeds" in notes[0]

    def test_accepts_image_exactly_at_limit(self):
        at_limit = make_attachment("edge.png", "image/png", b"x" * MAX_ATTACHMENT_BYTES)
        images, notes = _extract_image_attachments(make_msg(attachments=[at_limit]))
        assert len(images) == 1
        assert notes == []

    def test_unnamed_attachment_gets_placeholder_name(self):
        msg = make_msg(attachments=[make_attachment("", "application/pdf")])
        _, notes = _extract_image_attachments(msg)
        assert "(unnamed attachment)" in notes[0]

    def test_mixed_attachments_split_correctly(self):
        msg = make_msg(
            attachments=[
                make_attachment("good.png", "image/png", b"x" * 10),
                make_attachment("bad.pdf", "application/pdf"),
                make_attachment("huge.jpg", "image/jpeg", b"x" * (MAX_ATTACHMENT_BYTES + 1)),
            ]
        )
        images, notes = _extract_image_attachments(msg)
        assert [i.filename for i in images] == ["good.png"]
        assert len(notes) == 2


class TestResolveParentDiscordMessageId:
    @pytest.fixture
    def state(self, tmp_path):
        s = State(str(tmp_path / "state.json"))
        s.add_mapping({"bridge_id": "bridge-1", "discord_message_id": "d1", "email_message_id": "<msg1@x>"})
        return s

    def test_resolves_via_in_reply_to(self, state):
        result = _resolve_parent_discord_message_id(state, "<msg1@x>", [], None)
        assert result == "d1"

    def test_resolves_via_references_when_no_in_reply_to_match(self, state):
        result = _resolve_parent_discord_message_id(state, None, ["<other@x>", "<msg1@x>"], None)
        assert result == "d1"

    def test_references_checked_newest_first(self, state):
        # "newest first" means iterate references in reverse; msg1 is last in
        # the list (most recently added to References per RFC), so it should
        # be found even though an earlier match would exist further down.
        result = _resolve_parent_discord_message_id(state, None, ["<unrelated@x>", "<msg1@x>"], None)
        assert result == "d1"

    def test_resolves_via_bridge_id_header_as_last_resort(self, state):
        result = _resolve_parent_discord_message_id(state, None, [], "bridge-1")
        assert result == "d1"

    def test_returns_none_when_nothing_matches(self, state):
        result = _resolve_parent_discord_message_id(state, "<unknown@x>", ["<also-unknown@x>"], "unknown-bridge")
        assert result is None

    def test_in_reply_to_takes_priority_over_references(self, state, tmp_path):
        state.add_mapping({"bridge_id": "bridge-2", "discord_message_id": "d2", "email_message_id": "<msg2@x>"})
        result = _resolve_parent_discord_message_id(state, "<msg1@x>", ["<msg2@x>"], None)
        assert result == "d1"


class TestResolveSenderName:
    @pytest.fixture
    def config(self):
        return make_config(allowed_email_senders={"alice@example.com": "Alice", "bob@example.com": "Bob"})

    def test_returns_name_for_allowed_sender(self, config):
        assert _resolve_sender_name(config, "alice@example.com") == "Alice"

    def test_is_case_insensitive(self, config):
        assert _resolve_sender_name(config, "Alice@Example.com") == "Alice"

    def test_strips_whitespace(self, config):
        assert _resolve_sender_name(config, "  bob@example.com  ") == "Bob"

    def test_returns_none_for_unknown_sender(self, config):
        assert _resolve_sender_name(config, "eve@example.com") is None
