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
- `deploy.sh`: one-shot installer for the systemd deployment (creates the
  dedicated system user, copies the project into `/opt`, installs and
  enables the service). `discord-email-bridge.service` also gained
  sandboxing directives (`ProtectSystem=strict`, `NoNewPrivileges`, etc).
- Email → Discord attachment forwarding: image, PDF, DOC, and DOCX
  attachments under 8 MB are now forwarded as Discord files, as-is (an email
  can be attachment-only, with no text body, and still get through).
  Unsupported or oversized attachments are dropped with a visible `⚠️`
  notice in the Discord message instead of disappearing silently; if the
  upload itself fails, the message is retried without it and a notice is
  added. Requires the bot to have the `Attach Files` channel permission --
  without it, every upload fails with `discord.errors.Forbidden: 403
  Forbidden (error code: 50013): Missing Permissions` and falls back to the
  text-only + notice path. See the Developer/User Manual for how to grant it.
- Automated test suite (`pytest`, `pytest-asyncio`): unit tests for the pure
  logic in `state.py`, `mail_reader.py`, `discord_client.py`, `mail_sender.py`,
  plus handler-level tests in `main.py` with Discord/SMTP mocked out. Run with
  `uv run pytest` from `discord-email-bridge/`.
- CI (`.github/workflows/tests.yml`): runs the test suite on every push/PR to
  `main` via GitHub Actions.
- Multiple allowed reply senders: `ALLOWED_EMAIL_SENDER` is replaced by
  `ALLOWED_EMAIL_SENDERS` (comma-separated), paired positionally with the new
  `ALLOWED_EMAIL_SENDER_NAMES` (the Nth email gets the Nth name). Each
  allowed sender's reply now shows up in Discord as "📧 Email reply from
  {name}:" instead of the previous generic "📧 Email reply:". Names are set
  explicitly rather than parsed from the email, since a `From` display name
  isn't trustworthy input. A length mismatch between the two lists fails
  config loading immediately instead of silently misattributing a reply.
- Discord reaction → email notifications: reacting to a Discord message that
  the email user originally sent via email now sends a `[Reaction]`
  follow-up email (threaded onto the original via `In-Reply-To`), so they
  get feedback on their own messages without leaving email. Scoped
  deliberately narrow to avoid flooding their inbox: reactions on
  Discord-native messages are not forwarded, and neither are reaction
  removals. No new Discord permission is needed -- `guild_reactions` is a
  non-privileged intent already included in `Intents.default()`.

### Fixed
- A Discord display name containing a stray CR/LF character would make
  `EmailMessage` reject the Subject header and silently drop the whole
  message; author names are now sanitized before being placed in a header.
- Forwarding an email (Gmail's "---------- Forwarded message ---------"
  format) into the bridge silently dropped the entire forwarded body: the
  quote-stripping heuristic treated the forward's `From:`/`Subject:` header
  block the same as a reply's quoted history and cut everything from that
  point on, including the actual content the sender meant to share. Now only
  the header block right under the forward marker is skipped; the real body
  after it is kept.

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
