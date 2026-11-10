#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$PROJECT_DIR"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.9 or newer is required. Install it from https://www.python.org/downloads/macos/"
  exit 1
fi
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Python 3.9 or newer is required.")'

if [ ! -x .venv/bin/python ]; then
  echo "Preparing the local Python environment..."
  "$PYTHON_BIN" -m venv .venv
fi
if ! .venv/bin/python -c 'import serial; assert serial.VERSION == "3.5"' >/dev/null 2>&1; then
  echo "Installing the serial connection dependency (first launch only)..."
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
fi

if .venv/bin/python - <<'PY'
import json
import urllib.request
import webbrowser
try:
    with urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=1) as response:
        data = json.load(response)
    if data.get('application') != 'ece198-motion-counter':
        raise ValueError('different application')
except Exception:
    raise SystemExit(1)
webbrowser.open('http://127.0.0.1:8765')
print('The dashboard is already running. Opened it in your browser.')
PY
then
  exit 0
fi

echo "Close Arduino IDE's Serial Monitor and Serial Plotter before connecting."
echo "Dashboard: http://127.0.0.1:8765"
echo "Keep this window open. Click Stop & save before pressing Control-C to exit."
exec .venv/bin/python backend/server.py --port 8765 --open-browser
