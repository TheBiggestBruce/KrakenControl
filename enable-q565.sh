#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DROPIN_DIR="/etc/systemd/system/coolercontrold.service.d"
PATCH_DIR="/usr/local/lib/kraken-web/q565_patch"
GROUP="${KRAKEN_Q565_GROUP:-$(id -gn)}"

# Check the encoder with the system Python used by CoolerControl, before making
# changes. Import explicitly so sitecustomize does not run in the installer.
ENCODER="$(python3 -E - "$ROOT/q565_patch" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import q565_rust.q565_rust as encoder
print(encoder.__file__)
PY
)"
getent group "$GROUP" >/dev/null

DROPIN="$(mktemp)"
trap 'rm -f "$DROPIN"' EXIT
cat "$ROOT/kraken-q565.conf" > "$DROPIN"
printf 'Environment="KRAKEN_Q565_GROUP=%s"\n' "$GROUP" >> "$DROPIN"

# CoolerControl starts before network/removable project drives may be mounted.
# Load its Python startup hook from the local disk, not the source checkout.
sudo install -d -m 0755 "$PATCH_DIR/q565_rust" "$DROPIN_DIR"
sudo install -m 0644 "$ROOT/q565_patch/sitecustomize.py" "$ROOT/q565_patch/LICENSE" "$PATCH_DIR/"
sudo install -m 0644 "$ROOT/q565_patch/q565_rust/__init__.py" "$ENCODER" "$PATCH_DIR/q565_rust/"
sudo install -m 0644 "$DROPIN" "$DROPIN_DIR/kraken-q565.conf"
sudo systemctl daemon-reload
sudo systemctl restart coolercontrold.service
systemctl --user restart kraken-web.service

printf 'Q565 acceleration installed locally at %s. CoolerControl and Kraken Web restarted.\n' "$PATCH_DIR"
