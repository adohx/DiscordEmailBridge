# Discord Email Bridge

A minimal, single-user Discord ↔ Email bidirectional bridge. It lets a project
member who can't use Discord participate in the discussion of one fixed
Discord channel via email.

This is an **MVP (minimum viable product)**, not a general-purpose product.

## Strict MVP Scope

```text
One Discord channel
One bridge email account
One target email user
One allowed reply email address
Plain-text messages only
```

Not supported: multiple channels, multiple users, multiple servers, Discord
threads, exact email-thread mapping, attachment forwarding, full HTML
rendering, a web admin panel, OAuth, rich-text messages, etc.

## Architecture

```text
Discord channel
    ↕
Bridge program (this repo), connects to Discord via a Discord Bot Token
    ↕
SMTP (send) / IMAP (receive) bridge email account
    ↕
One email user (TARGET_EMAIL / ALLOWED_EMAIL_SENDER)
```

Only one program runs: it is simultaneously a Discord bot, an SMTP client,
and an IMAP client.

Flow:

1. Someone posts a message in the Discord channel → the bot formats it as an
   email → sends it via SMTP to `TARGET_EMAIL`.
2. `ALLOWED_EMAIL_SENDER` replies to the email → the program periodically
   checks the inbox via IMAP → forwards the reply content back to the
   Discord channel.

## Project Structure

```text
discord-email-bridge/
  main.py            # Entry point, starts the Discord client + email polling task
  config.py           # Loads and validates config from .env
  discord_client.py    # Discord bot: receives messages, sends email replies, mention cleanup
  mail_sender.py       # Discord → Email: sends via SMTP
  mail_reader.py        # Email → Discord: polls via IMAP, dedupes, strips quoted history
  state.py            # Reads/writes state.json (processed email IDs)
  pyproject.toml       # Project dependency manifest (managed by uv)
  uv.lock              # Locked dependency versions
  .env.example
  discord-email-bridge.service   # Optional systemd service example
  README.md
  .gitignore
```

## 1. Create a Discord Bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and give it a name (e.g. `Email Bridge`).
3. Go to the **Bot** page on the left, click **Add Bot**.
4. On the Bot page:
   * Turn off **Public Bot** (this is a private bridge tool, no one else needs to add it).
   * Turn on **Message Content Intent** (the program needs to read message content).
5. Click **Reset Token** to get the Bot Token, copy it — you'll fill it into
   `DISCORD_BOT_TOKEN` in `.env` later.
   **Never commit this token to Git or share it with anyone.**

## 2. Invite the Bot with Minimal Permissions

Do not grant the bot `Administrator` permission. The bot only needs access
to one channel, and only needs these permissions:

```text
View Channel
Read Message History
Send Messages
```

Invite steps:

1. In the Developer Portal, go to **OAuth2 → URL Generator**.
2. Check the `bot` scope.
3. Under Bot Permissions, check only:
   * View Channels
   * Send Messages
   * Read Message History
4. Copy the generated URL, open it in a browser, choose the Discord server to
   invite it to, and authorize.
5. After inviting, go to the target channel's settings and confirm the bot
   can view it. If the server has other private channels, it's recommended
   to explicitly grant access only to this one channel and leave the rest
   at their default/invisible state.

## 3. Get the Discord Channel ID

1. In the Discord client, go to **Settings → Advanced** and enable
   **Developer Mode**.
2. Right-click the target channel to bridge and select **Copy Channel ID**.
3. Put this ID into `DISCORD_CHANNEL_ID` in `.env`.

## 4. Configure SMTP / IMAP

It's recommended to use a dedicated bridge email account, not your personal
primary mailbox.

Using Gmail as an example:

1. Enable two-factor authentication on the bridge mailbox.
2. Generate an **App Password** at [Google App Passwords](https://myaccount.google.com/apppasswords).
3. Fill in `.env`:

   ```env
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=your-bridge-account@gmail.com
   SMTP_PASSWORD=<app password>
   SMTP_FROM=your-bridge-account@gmail.com

   IMAP_HOST=imap.gmail.com
   IMAP_PORT=993
   IMAP_USER=your-bridge-account@gmail.com
   IMAP_PASSWORD=<app password>
   ```

For other email providers, the same idea applies: fill in the corresponding
SMTP/IMAP host and port, and use an app-specific password provided by the
provider (if supported) rather than your login password.

## 5. Configure `.env`

Copy the example file and fill it in:

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```env
DISCORD_BOT_TOKEN=          # Bot token obtained in step 1
DISCORD_CHANNEL_ID=         # Channel ID obtained in step 3

SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=

IMAP_HOST=
IMAP_PORT=993
IMAP_USER=
IMAP_PASSWORD=

TARGET_EMAIL=                # Target mailbox that receives Discord messages
ALLOWED_EMAIL_SENDER=        # The only email address allowed to reply and be forwarded back to Discord

EMAIL_POLL_INTERVAL_SECONDS=60
STATE_FILE=state.json
```

In the simplest case, `TARGET_EMAIL` and `ALLOWED_EMAIL_SENDER` are the same person.

**`.env` must not be committed to Git** (already excluded via `.gitignore`).

## 6. Install Dependencies

This project uses [uv](https://docs.astral.sh/uv/) to manage dependencies
and the virtual environment. Install uv (if you haven't already):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then sync dependencies (this will automatically create `.venv` and install
according to `uv.lock`):

```bash
uv sync
```

## 7. Run the Program

```bash
uv run main.py
```

On a successful start, the logs will show, in order: config loaded
successfully, Discord bot connected, email polling started.
Press `Ctrl+C` to stop the program.

## 8. Test Discord → Email

1. Make sure the program is running.
2. Post a message in the channel corresponding to `DISCORD_CHANNEL_ID` using
   any (non-bot) account.
3. Check the inbox of `TARGET_EMAIL` — you should receive an email with a
   subject like `[Discord Bridge] username: message content...`.

## 9. Test Email → Discord

1. Using the `ALLOWED_EMAIL_SENDER` mailbox, reply directly to the email
   received in step 8 (or send a new email to the bridge mailbox).
2. Wait up to `EMAIL_POLL_INTERVAL_SECONDS` seconds (default 60 seconds).
3. Check the Discord channel — a message like the following should appear:
   ```text
   📧 Email reply:

   Email body content
   ```
4. If an email is sent to the bridge mailbox from a different address, the
   program should ignore it (the log will record "not an allowed sender").

## 10. Running Long-Term on Ubuntu with systemd (Optional)

1. Deploy the project to, e.g., `/opt/discord-email-bridge` (make sure the
   running user has uv installed, e.g. via
   `curl -LsSf https://astral.sh/uv/install.sh | sh`).
2. Prepare `.env` in that directory.
3. Edit the `discord-email-bridge.service` file included in the repo, and
   update `WorkingDirectory`, `ExecStart` (path to the uv executable), and
   `User` to match your actual deployment path and running user.
4. Install and start the service:

   ```bash
   sudo cp discord-email-bridge.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now discord-email-bridge
   sudo systemctl status discord-email-bridge
   journalctl -u discord-email-bridge -f
   ```

## Security Notes

* **Do not** commit `.env` to Git.
* **Do not** grant the Discord Bot `Administrator` permission — only View
  Channel / Read Message History / Send Messages.
* Use a dedicated bridge email account, not your personal primary mailbox.
* If using Gmail, always use an **App Password**, never your account login
  password.
* This MVP only supports one allowed reply email address
  (`ALLOWED_EMAIL_SENDER`); emails from any other sender are ignored.
* The source code contains no hardcoded secrets — all sensitive information
  is read from `.env`.

## Known MVP Limitations

* Only supports one Discord channel, one bridge mailbox, one target email
  user, and one allowed reply sender.
* Only plain-text emails are processed; HTML-only emails are skipped and
  logged.
* Cleanup of quoted email history is heuristic (based on common markers like
  `On ... wrote:` / `-----Original Message-----`), and is not guaranteed to
  be 100% clean.
* Discord threads, attachment forwarding, and rich-text formatting are not
  supported.
* There is no precise conversation/thread mapping between emails and
  Discord messages — it's simply a straightforward back-and-forth forward.
* Deduplication is based on the local `state.json` file; if this file is
  deleted, historical emails may be reprocessed.
* Discord messages longer than about 1800 characters are truncated, with
  `[Message truncated]` appended to the end of the email body.
