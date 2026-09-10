#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LIQUIDCTL="$ROOT/../liquidctl"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
VENV="$DATA_HOME/kraken-web/venv"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="$SERVICE_DIR/kraken-web.service"

command -v python3 >/dev/null || { printf 'python3 is required\n' >&2; exit 1; }
command -v ffmpeg >/dev/null || { printf 'ffmpeg is required\n' >&2; exit 1; }
[[ -f "$LIQUIDCTL/pyproject.toml" ]] || { printf 'Expected liquidctl clone at %s\n' "$LIQUIDCTL" >&2; exit 1; }

printf 'Creating local environment at %s\n' "$VENV"
mkdir -p "$(dirname "$VENV")"
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r "$ROOT/requirements.txt"
"$VENV/bin/python" -m pip install --no-deps "$LIQUIDCTL"
PLAYWRIGHT_BROWSERS_PATH="$DATA_HOME/kraken-web/playwright" "$VENV/bin/playwright" install chromium

printf 'Installing Kraken USB permissions (sudo required)\n'
sudo install -m 0644 "$ROOT/71-kraken-web.rules" /etc/udev/rules.d/71-kraken-web.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --action=add
sudo udevadm trigger --subsystem-match=hidraw --action=add

mkdir -p "$SERVICE_DIR"
cat > "$SERVICE" <<EOF
[Unit]
Description=Kraken Web local display controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
EnvironmentFile=-%h/.config/kraken-web.env
Environment=KRAKEN_LIQUIDCTL_SOURCE=$LIQUIDCTL
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PLAYWRIGHT_BROWSERS_PATH=$DATA_HOME/kraken-web/playwright
ExecStart=$VENV/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8787 --app-dir $ROOT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now kraken-web.service
sudo loginctl enable-linger "$USER"

printf '\nInstalled and started Kraken Web.\nOpen http://127.0.0.1:8787\n'
printf 'If USB access still fails, unplug/replug the Kraken USB cable or reboot once.\n'
