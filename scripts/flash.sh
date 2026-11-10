#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_DIR"
if command -v arduino-cli >/dev/null 2>&1; then
  CLI_BIN="$(command -v arduino-cli)"
elif [ -x "/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli" ]; then
  CLI_BIN="/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli"
else
  echo "Install Arduino IDE 2 in /Applications, or install arduino-cli."
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
PORT_PATH="${1:-}"
if [ -z "$PORT_PATH" ]; then
  PORT_PATH="$("$CLI_BIN" board list --format json | "$PYTHON_BIN" -c '
import json,sys
boards=json.load(sys.stdin).get("detected_ports",[])
ports=[b["port"]["address"] for b in boards if any(m.get("fqbn")=="arduino:renesas_uno:minima" for m in b.get("matching_boards",[]))]
if len(ports)!=1:
    sys.exit("Connect one UNO R4 Minima, or pass its port: ./scripts/flash.sh /dev/cu.usbmodem101")
print(ports[0])')"
fi

if command -v lsof >/dev/null 2>&1 && lsof -t "$PORT_PATH" >/dev/null 2>&1; then
  echo "The serial port is in use: $PORT_PATH"
  echo "Click Stop & save, stop the local server, and close Arduino Serial Monitor/Plotter, then retry."
  exit 1
fi

if ! "$CLI_BIN" core list | awk '$1 == "arduino:renesas_uno" { found=1 } END { exit !found }'; then
  echo "Installing the official UNO R4 board package..."
  "$CLI_BIN" core update-index
  "$CLI_BIN" core install arduino:renesas_uno@1.6.0
fi

"$CLI_BIN" compile --fqbn arduino:renesas_uno:minima --build-path "$PROJECT_DIR/build/arduino" \
  --upload --port "$PORT_PATH" "$PROJECT_DIR/firmware/ExerciseCounter"
echo "Upload completed. Run ./start-demo.command to open the dashboard."
