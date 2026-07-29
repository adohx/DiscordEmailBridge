# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/), versions follow
[SemVer](https://semver.org/). The current version lives in
[`pyproject.toml`](pyproject.toml).

## [Unreleased]

### Added
- Multiple email recipients: `TARGET_EMAILS` replaces `TARGET_EMAIL` and
  accepts a comma-separated list, so a Discord message (and its edit/delete
  notifications) can be broadcast to more than one mailbox.
  `ALLOWED_EMAIL_SENDER` is unchanged — replies are still accepted from only
  one address.
- `deploy.sh`: one-shot installer for the systemd deployment (creates the
  dedicated system user, copies the project into `/opt`, installs and
  enables the service). `discord-email-bridge.service` also gained
  sandboxing directives (`ProtectSystem=strict`, `NoNewPrivileges`, etc).
- Email → Discord image attachments: image attachments under 8 MB are now
  forwarded as Discord files (an email can be attachment-only, with no text
  body, and still get through). Non-image or oversized attachments are
  dropped with a visible `⚠️` notice in the Discord message instead of
  disappearing silently; if the image upload itself fails, the message is
  retried without it and a notice is added.
- Automated test suite (`pytest`, `pytest-asyncio`): unit tests for the pure
  logic in `state.py`, `mail_reader.py`, `discord_client.py`, `mail_sender.py`,
  plus handler-level tests in `main.py` with Discord/SMTP mocked out. Run with
  `uv run pytest` from `discord-email-bridge/`.

### Fixed
- A Discord display name containing a stray CR/LF character would make
  `EmailMessage` reject the Subject header and silently drop the whole
  message; author names are now sanitized before being placed in a header.

### Planned — 0.2
- Docker-based deployment (Dockerfile / docker-compose), so the bridge can be
  started without a manual Python/uv setup.

### Planned — 0.3
- Finalize the email-side conversation rules: decide whether reply emails
  should reuse the original subject (`Re:` prefix) so subject-based mail
  clients group them correctly, not just `References`-based clients.

### Planned — 0.4
- HTML-only email support: extract readable content from emails that have no
  `text/plain` part instead of skipping them.

### Planned — 0.5
- Discord Thread support: bridge messages posted inside threads under the
  configured channel, not just the channel's own top-level messages.

## [0.1.0] - 2026-07-14

### Added
- Discord → Email: forward channel messages as plain-text email to
  `TARGET_EMAIL`.
- Email → Discord: poll the bridge mailbox over IMAP and forward
  `ALLOWED_EMAIL_SENDER` replies back to the channel.
- Discord reply ↔ email `In-Reply-To`/`References` mapping, so replies show
  up as real Discord replies and threaded emails.
- Edit/delete lifecycle sync: `[Updated]`/`[Deleted]` notification emails
  when a bridged Discord message is edited or deleted; replying to a since-
  deleted parent message degrades to a plain channel message instead of
  failing.
- Local JSON state persistence (`state.json`) with atomic writes and
  corrupt-file backup/recovery.
- Loop/duplicate protection: ignore the bot's own messages, restrict inbound
  email to `ALLOWED_EMAIL_SENDER`, dedupe by Discord message id and email
  Message-ID.
- Mention sanitization (`@everyone`/`@here`) on email-sourced Discord
  messages.
- Required environment variable validation at startup (fails fast instead of
  running with missing config).
