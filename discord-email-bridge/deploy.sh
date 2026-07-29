#!/usr/bin/env bash
# Installs Discord Email Bridge as a systemd service on Ubuntu/Debian.
#
# What it does:
#   1. Creates a dedicated, unprivileged system user to run the bridge.
#   2. Copies this project into APP_DIR (owned by that user).
#   3. Installs discord-email-bridge.service into systemd, pointed at APP_DIR.
#   4. Enables the service (does NOT start it -- you must fill in .env first).
#
# Safe to re-run: it will not overwrite an existing .env or state.json in
# APP_DIR, and re-copying source files just refreshes the code.
#
# Usage: sudo ./deploy.sh

set -euo pipefail

APP_USER="${APP_USER:-discordbridge}"
APP_DIR="${APP_DIR:-/opt/discord-email-bridge}"
SERVICE_NAME="discord-email-bridge"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root (it creates a system user, writes to /opt and /etc/systemd/system)." >&2
    echo "Re-run with: sudo ./deploy.sh" >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1 && [[ ! -x "/home/${APP_USER}/.local/bin/uv" ]]; then
    echo "uv not found. Install it first: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "(Run that as the ${APP_USER} user, or adjust ExecStart in the .service file to your uv path.)" >&2
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
    echo "Creating system user ${APP_USER}..."
    useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
else
    echo "User ${APP_USER} already exists, skipping creation."
fi

echo "Copying project into ${APP_DIR}..."
mkdir -p "$APP_DIR"
rsync -a --delete \
    --exclude '.venv' \
    --exclude '.env' \
    --exclude 'state.json' \
    --exclude '__pycache__' \
    "${SOURCE_DIR}/" "${APP_DIR}/"

if [[ ! -f "${APP_DIR}/.env" ]]; then
    echo "No .env in ${APP_DIR} yet -- copying .env.example. YOU MUST EDIT IT before starting the service."
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
else
    echo ".env already exists in ${APP_DIR}, leaving it untouched."
fi

chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"

echo "Installing systemd unit..."
sed -e "s#/opt/discord-email-bridge#${APP_DIR}#g" \
    -e "s#/home/discordbridge/.local/bin/uv#/home/${APP_USER}/.local/bin/uv#g" \
    -e "s#User=discordbridge#User=${APP_USER}#g" \
    "${SOURCE_DIR}/${SERVICE_NAME}.service" > "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

cat <<EOF

Done. Before starting the service:
  1. Edit ${APP_DIR}/.env with real Discord/SMTP/IMAP credentials.
  2. Make sure uv is installed and runnable by ${APP_USER}
     (su - ${APP_USER} -s /bin/bash -c 'uv --version').

Then start it:
  sudo systemctl start ${SERVICE_NAME}
  sudo systemctl status ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f
EOF
