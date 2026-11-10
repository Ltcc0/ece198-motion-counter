#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p build/firmware-tests
python3 - <<'PY'
from pathlib import Path
source = Path('firmware/ExerciseCounter/ExerciseCounter.ino').read_text()
# UNO R4 unsigned long is 32 bits; native macOS unsigned long is 64 bits.
Path('build/firmware-tests/firmware_target_width.inc').write_text(source.replace('unsigned long', 'uint32_t'))
PY
c++ -std=c++17 -Wall -Wextra -Itests/firmware -Ibuild/firmware-tests \
  tests/firmware/test.cpp -o build/firmware-tests/run
build/firmware-tests/run
