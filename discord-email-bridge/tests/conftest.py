from config import Config


def make_config(**overrides) -> Config:
    """A fully-populated Config with sane test defaults; override what you need."""
    defaults = dict(
        discord_bot_token="test-token",
        discord_channel_id=111,
        discord_guild_id=None,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="bridge@example.com",
        smtp_password="secret",
        smtp_from="bridge@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="bridge@example.com",
        imap_password="secret",
        target_emails=["target@example.com"],
        allowed_email_sender="allowed@example.com",
        email_poll_interval_seconds=60,
        state_file="state.json",
        email_message_id_domain="bridge.local",
        include_deleted_content=True,
    )
    defaults.update(overrides)
    return Config(**defaults)
