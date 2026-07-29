from unittest.mock import MagicMock, patch

import mail_sender
from conftest import make_config


class TestBuildMessageId:
    def test_builds_expected_format(self):
        config = make_config(email_message_id_domain="bridge.local")
        result = mail_sender.build_message_id(config, "12345", "abcdef12-3456-7890")
        assert result == "<discord-12345-abcdef12@bridge.local>"


class TestSanitizeForHeader:
    def test_leaves_normal_text_untouched(self):
        assert mail_sender._sanitize_for_header("Alice") == "Alice"

    def test_strips_carriage_returns(self):
        assert "\r" not in mail_sender._sanitize_for_header("Alice\r\nBcc: x@y.com")

    def test_replaces_newlines_with_space(self):
        assert mail_sender._sanitize_for_header("Alice\nBob") == "Alice Bob"


class TestBuildSubject:
    def test_normal_short_content(self):
        subject = mail_sender._build_subject("Alice", "hello world")
        assert subject == "[Discord Bridge] Alice: hello world"

    def test_truncates_long_first_line(self):
        content = "x" * 200
        subject = mail_sender._build_subject("Alice", content)
        assert len(subject) <= mail_sender.SUBJECT_MAX_LENGTH
        assert subject.endswith("…")

    def test_uses_only_first_line_of_multiline_content(self):
        subject = mail_sender._build_subject("Alice", "first line\nsecond line")
        assert "second line" not in subject

    def test_sanitizes_author_name_with_newline(self):
        subject = mail_sender._build_subject("Evil\r\nBcc: x@y.com", "hi")
        assert "\r" not in subject
        assert "\n" not in subject

    def test_very_long_author_name_does_not_crash(self):
        subject = mail_sender._build_subject("A" * 200, "hi")
        assert len(subject) <= mail_sender.SUBJECT_MAX_LENGTH


class TestBuildBody:
    def test_plain_message_no_reply_context(self):
        body = mail_sender._build_body("Alice", "hello", None)
        assert "Alice wrote in Discord" in body
        assert "hello" in body

    def test_reply_context_quotes_parent(self):
        body = mail_sender._build_body("Alice", "sure thing", ("Bob", "original message"))
        assert "Alice replied to Bob" in body
        assert "> original message" in body
        assert "sure thing" in body


class TestSendDiscordMessageAsEmail:
    def test_sends_to_all_target_emails_with_expected_headers(self):
        config = make_config(target_emails=["a@example.com", "b@example.com"])
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp

        with patch("smtplib.SMTP", return_value=fake_smtp) as smtp_cls:
            mail_sender.send_discord_message_as_email(
                config,
                "Alice",
                "hello",
                email_message_id="<msg1@bridge.local>",
                bridge_id="b1",
                discord_message_id="d1",
            )

        smtp_cls.assert_called_once_with(config.smtp_host, config.smtp_port, timeout=30)
        fake_smtp.starttls.assert_called_once()
        fake_smtp.login.assert_called_once_with(config.smtp_user, config.smtp_password)
        sent_message = fake_smtp.send_message.call_args[0][0]
        assert sent_message["To"] == "a@example.com, b@example.com"
        assert sent_message["Message-ID"] == "<msg1@bridge.local>"
        assert sent_message["X-Discord-Bridge-ID"] == "b1"
        assert sent_message["X-Discord-Message-ID"] == "d1"
        assert "In-Reply-To" not in sent_message

    def test_includes_in_reply_to_and_references_when_given(self):
        config = make_config()
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp

        with patch("smtplib.SMTP", return_value=fake_smtp):
            mail_sender.send_discord_message_as_email(
                config,
                "Alice",
                "hello",
                email_message_id="<msg2@bridge.local>",
                bridge_id="b2",
                discord_message_id="d2",
                in_reply_to="<parent@bridge.local>",
                references=["<root@bridge.local>", "<parent@bridge.local>"],
            )

        sent_message = fake_smtp.send_message.call_args[0][0]
        assert sent_message["In-Reply-To"] == "<parent@bridge.local>"
        assert sent_message["References"] == "<root@bridge.local> <parent@bridge.local>"

    def test_smtp_failure_propagates(self):
        import smtplib

        config = make_config()
        with patch("smtplib.SMTP", side_effect=smtplib.SMTPException("boom")):
            try:
                mail_sender.send_discord_message_as_email(
                    config,
                    "Alice",
                    "hello",
                    email_message_id="<msg3@bridge.local>",
                    bridge_id="b3",
                    discord_message_id="d3",
                )
                assert False, "expected SMTPException to propagate"
            except smtplib.SMTPException:
                pass


class TestSendEditNotification:
    def test_sets_updated_subject_and_references_original(self):
        config = make_config()
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp

        with patch("smtplib.SMTP", return_value=fake_smtp):
            new_id = mail_sender.send_edit_notification(
                config, "Alice", "old text", "new text", "<original@bridge.local>", "d1", 1
            )

        sent_message = fake_smtp.send_message.call_args[0][0]
        assert sent_message["Subject"] == "[Updated] Discord message from Alice"
        assert sent_message["In-Reply-To"] == "<original@bridge.local>"
        assert new_id != "<original@bridge.local>"


class TestSendDeleteNotification:
    def test_includes_original_content_when_configured(self):
        config = make_config(include_deleted_content=True)
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp

        with patch("smtplib.SMTP", return_value=fake_smtp):
            mail_sender.send_delete_notification(config, "Alice", "deleted content", "<original@bridge.local>", "d1")

        body = fake_smtp.send_message.call_args[0][0].get_content()
        assert "deleted content" in body

    def test_omits_content_when_none(self):
        config = make_config()
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp

        with patch("smtplib.SMTP", return_value=fake_smtp):
            mail_sender.send_delete_notification(config, "Alice", None, "<original@bridge.local>", "d1")

        body = fake_smtp.send_message.call_args[0][0].get_content()
        assert "was deleted." in body


class TestSendReactionNotification:
    def test_sets_subject_and_references_original(self):
        config = make_config()
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp

        with patch("smtplib.SMTP", return_value=fake_smtp):
            new_id = mail_sender.send_reaction_notification(
                config, "Bob", "👍", "my original message", "<original@bridge.local>", "d1"
            )

        sent_message = fake_smtp.send_message.call_args[0][0]
        assert sent_message["Subject"] == "[Reaction] Bob reacted 👍"
        assert sent_message["In-Reply-To"] == "<original@bridge.local>"
        assert new_id != "<original@bridge.local>"

    def test_body_includes_reactor_and_original_content(self):
        config = make_config()
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp

        with patch("smtplib.SMTP", return_value=fake_smtp):
            mail_sender.send_reaction_notification(
                config, "Bob", "👍", "my original message", "<original@bridge.local>", "d1"
            )

        body = fake_smtp.send_message.call_args[0][0].get_content()
        assert "Bob reacted 👍" in body
        assert "my original message" in body

    def test_sanitizes_reactor_name_with_newline(self):
        config = make_config()
        fake_smtp = MagicMock()
        fake_smtp.__enter__.return_value = fake_smtp

        with patch("smtplib.SMTP", return_value=fake_smtp):
            mail_sender.send_reaction_notification(
                config, "Evil\r\nBcc: x@y.com", "👍", "content", "<original@bridge.local>", "d1"
            )

        subject = fake_smtp.send_message.call_args[0][0]["Subject"]
        assert "\r" not in subject
        assert "\n" not in subject
