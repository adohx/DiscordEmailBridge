# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/), versions follow
[SemVer](https://semver.org/). The current version lives in
[`pyproject.toml`](pyproject.toml).

## [Unreleased]

### Planned — 0.2
- Docker-based deployment (Dockerfile / docker-compose), so the bridge can be
  started without a manual Python/uv setup.

### Planned — 0.3
- Finalize the email-side conversation rules: decide whether reply emails
  should reuse the original subject (`Re:` prefix) so subject-based mail
  clients group them correctly, not just `References`-based clients.
- Automated test suite.

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
