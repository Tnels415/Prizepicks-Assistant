#!/usr/bin/env bash
#
# run_daily.sh — wrapper that runs the prop analyzer once.
# Invoked by the daily scheduler (launchd on macOS / cron on Linux).
#
# It changes into the repo directory, activates a virtualenv if one exists,
# runs main.py, and appends all output to logs/daily_run.log so you can see
# what happened on days you weren't watching.

set -euo pipefail

# Absolute path to this script's directory == the repo root.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

mkdir -p logs
LOG="$REPO_DIR/logs/daily_run.log"

echo "" >> "$LOG"
echo "===== Scheduled run: $(date '+%Y-%m-%d %H:%M:%S %Z') =====" >> "$LOG"

# Prefer a project virtualenv if present; otherwise use system python3.
PY=""
for candidate in ".venv/bin/python3" "venv/bin/python3" "env/bin/python3"; do
    if [ -x "$REPO_DIR/$candidate" ]; then
        PY="$REPO_DIR/$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
    echo "ERROR: no python3 found (looked for a venv and system python3)." >> "$LOG"
    exit 1
fi

echo "Using interpreter: $PY" >> "$LOG"

# Run the analyzer. Both stdout and stderr go to the log.
"$PY" main.py >> "$LOG" 2>&1
STATUS=$?

echo "===== Run finished with exit code $STATUS at $(date '+%H:%M:%S %Z') =====" >> "$LOG"
exit $STATUS
