#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV="${KRAKEN_WEB_VENV:-$HOME/.local/share/kraken-web/venv}"

if [[ ! -x "$VENV/bin/python" ]]; then
  printf 'Kraken Web is not installed. Run: %s/install.sh\n' "$ROOT" >&2
  exit 1
fi

export KRAKEN_LIQUIDCTL_SOURCE="${KRAKEN_LIQUIDCTL_SOURCE:-$ROOT/../liquidctl}"
exec "$VENV/bin/python" -m uvicorn server:app --host 127.0.0.1 --port 8787 --app-dir "$ROOT"
