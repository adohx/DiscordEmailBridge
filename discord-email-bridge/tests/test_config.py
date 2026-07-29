import pytest

from config import ConfigError, _require_email_list, _require_email_name_map, load_config


class TestRequireEmailList:
    def test_parses_comma_separated_list(self, monkeypatch):
        monkeypatch.setenv("X", "a@example.com, b@example.com ,c@example.com")
        assert _require_email_list("X") == ["a@example.com", "b@example.com", "c@example.com"]

    def test_single_value(self, monkeypatch):
        monkeypatch.setenv("X", "a@example.com")
        assert _require_email_list("X") == ["a@example.com"]

    def test_missing_raises(self, monkeypatch):
        monkeypatch.delenv("X", raising=False)
        with pytest.raises(ConfigError):
            _require_email_list("X")

    def test_only_commas_raises(self, monkeypatch):
        monkeypatch.setenv("X", " , , ")
        with pytest.raises(ConfigError):
            _require_email_list("X")


class TestRequireEmailNameMap:
    def test_pairs_by_position(self, monkeypatch):
        monkeypatch.setenv("EMAILS", "alice@example.com,bob@example.com")
        monkeypatch.setenv("NAMES", "Alice,Bob")
        result = _require_email_name_map("EMAILS", "NAMES")
        assert result == {"alice@example.com": "Alice", "bob@example.com": "Bob"}

    def test_lowercases_email_keys(self, monkeypatch):
        monkeypatch.setenv("EMAILS", "Alice@Example.com")
        monkeypatch.setenv("NAMES", "Alice")
        result = _require_email_name_map("EMAILS", "NAMES")
        assert result == {"alice@example.com": "Alice"}

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("EMAILS", " alice@example.com , bob@example.com ")
        monkeypatch.setenv("NAMES", " Alice , Bob ")
        result = _require_email_name_map("EMAILS", "NAMES")
        assert result == {"alice@example.com": "Alice", "bob@example.com": "Bob"}

    def test_mismatched_lengths_raises(self, monkeypatch):
        monkeypatch.setenv("EMAILS", "alice@example.com,bob@example.com")
        monkeypatch.setenv("NAMES", "Alice")
        with pytest.raises(ConfigError):
            _require_email_name_map("EMAILS", "NAMES")

    def test_missing_names_var_raises(self, monkeypatch):
        monkeypatch.setenv("EMAILS", "alice@example.com")
        monkeypatch.delenv("NAMES", raising=False)
        with pytest.raises(ConfigError):
            _require_email_name_map("EMAILS", "NAMES")

    def test_trailing_comma_creates_blank_entry_and_raises(self, monkeypatch):
        # A trailing comma produces an empty final element on one side but
        # not necessarily the other -- must fail loud, not silently misalign.
        monkeypatch.setenv("EMAILS", "alice@example.com,")
        monkeypatch.setenv("NAMES", "Alice")
        with pytest.raises(ConfigError):
            _require_email_name_map("EMAILS", "NAMES")


class TestLoadConfig:
    @pytest.fixture
    def base_env(self, monkeypatch):
        values = {
            "DISCORD_BOT_TOKEN": "token",
            "DISCORD_CHANNEL_ID": "123",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "bridge@example.com",
            "SMTP_PASSWORD": "secret",
            "SMTP_FROM": "bridge@example.com",
            "IMAP_HOST": "imap.example.com",
            "IMAP_PORT": "993",
            "IMAP_USER": "bridge@example.com",
            "IMAP_PASSWORD": "secret",
            "TARGET_EMAILS": "target@example.com",
            "ALLOWED_EMAIL_SENDERS": "alice@example.com,bob@example.com",
            "ALLOWED_EMAIL_SENDER_NAMES": "Alice,Bob",
            "EMAIL_POLL_INTERVAL_SECONDS": "60",
        }
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        # load_dotenv() with no path finds a .env by walking up from config.py's
        # own file location -- NOT the cwd -- so a real .env sitting next to
        # config.py would otherwise get read and could silently fill in
        # variables this test deliberately unset. Stub it out so these tests
        # only ever see the env vars set above.
        monkeypatch.setattr("config.load_dotenv", lambda *args, **kwargs: None)
        return values

    def test_builds_allowed_email_senders_map(self, base_env):
        config = load_config()
        assert config.allowed_email_senders == {"alice@example.com": "Alice", "bob@example.com": "Bob"}
        assert config.target_emails == ["target@example.com"]

    def test_missing_required_var_raises(self, base_env, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN")
        with pytest.raises(ConfigError):
            load_config()
