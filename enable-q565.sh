#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DROPIN_DIR="/etc/systemd/system/coolercontrold.service.d"

sudo mkdir -p "$DROPIN_DIR"
sudo install -m 0644 "$ROOT/kraken-q565.conf" "$DROPIN_DIR/kraken-q565.conf"
sudo systemctl daemon-reload
sudo systemctl restart coolercontrold.service
systemctl --user restart kraken-web.service

printf 'Q565 acceleration enabled. CoolerControl and Kraken Web restarted.\n'
