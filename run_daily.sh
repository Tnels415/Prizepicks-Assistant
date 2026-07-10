#!/usr/bin/env bash
#
# run_daily.sh — wrapper that runs the prop analyzer once.
# Invoked by the scheduler (launchd on macOS / cron on Linux) every 30 minutes;
# a guard makes it a no-op except the first eligible slot at/after 9:15 AM Pacific.
#
# Usage:
#   ./run_daily.sh          # guarded: runs only if >= 9:15 AM PT and not yet run today
#   ./run_daily.sh --now    # bypass the guard (manual testing)
#
# Why the guard design: a single fire-at-9:15 schedule silently skips the day
# if the machine is off/asleep/logged-out at that exact minute. Firing every
# 30 min and letting this guard decide guarantees the run happens in the first
# awake window at/after 9:15 AM PT.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

mkdir -p logs
LOG="$REPO_DIR/logs/daily_run.log"
MARKER="$REPO_DIR/logs/last_success.date"

FORCE=0
[ "${1:-}" = "--now" ] && FORCE=1

# launchd/cron run with a minimal PATH that misses Homebrew — extend it so
# `python3` resolves the same way it does in an interactive Terminal.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# --- Pick a python3 that actually has the project's dependencies -----------
# Under launchd, bare `command -v python3` finds Apple's /usr/bin/python3,
# which has no pip packages. Probe candidates and take the first that can
# import a required dependency.
PY=""
CANDIDATES=(
    "$REPO_DIR/.venv/bin/python3"
    "$REPO_DIR/venv/bin/python3"
    "$REPO_DIR/env/bin/python3"
    "$(command -v python3 2>/dev/null || true)"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "/usr/bin/python3"
)
for candidate in "${CANDIDATES[@]}"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if "$candidate" -c "import requests" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    {
        echo ""
        echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
        echo "ERROR: no python3 with the project dependencies was found."
        echo "Candidates probed: ${CANDIDATES[*]}"
        echo "Fix: pip3 install -r requirements.txt   (with the python you use in Terminal)"
    } >> "$LOG"
    exit 1
fi

# --- Guard: run only at/after 9:15 AM Pacific, once per day ----------------
TARGET_HOUR_PT=9
TARGET_MINUTE_PT=15
if [ "$FORCE" != "1" ]; then
    TODAY_PT="$(TZ=America/Los_Angeles date '+%Y-%m-%d')"
    # %H/%M are zero-padded (e.g. "09") for portability across BSD (macOS) and
    # GNU date — neither reliably supports the no-padding "%-H" GNU extension.
    # Force base-10 with the 10# prefix so bash arithmetic doesn't misparse a
    # leading zero as an octal digit (e.g. "09" is invalid octal and would
    # otherwise raise "value too great for base").
    HOUR_PT="$(TZ=America/Los_Angeles date '+%H')"
    MIN_PT="$(TZ=America/Los_Angeles date '+%M')"
    NOW_MINUTES=$(( 10#$HOUR_PT * 60 + 10#$MIN_PT ))
    TARGET_MINUTES=$(( TARGET_HOUR_PT * 60 + TARGET_MINUTE_PT ))
    if [ "$NOW_MINUTES" -lt "$TARGET_MINUTES" ]; then
        exit 0   # before the window — silent no-op
    fi
    if [ -f "$MARKER" ] && [ "$(cat "$MARKER" 2>/dev/null)" = "$TODAY_PT" ]; then
        exit 0   # already ran successfully today — silent no-op
    fi
fi

echo "" >> "$LOG"
echo "===== Scheduled run: $(date '+%Y-%m-%d %H:%M:%S %Z') =====" >> "$LOG"
echo "Using interpreter: $PY" >> "$LOG"

# --- Environment diagnostics -------------------------------------------------
# launchd/cron give this script a stripped-down environment compared to an
# interactive Terminal — no shell rc files are sourced, so anything the user
# relies on there (a proxy, a VPN-only DNS resolver, custom env vars) is
# silently absent here even though the PATH/python fix above is in place.
# Log what's actually present so a "scheduled run gets 0 props but a manual
# Terminal run gets props" report can be diagnosed from this file alone,
# instead of guessing.
{
    echo "-- environment --"
    echo "PATH=$PATH"
    echo "HOME=$HOME"
    for var in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy NO_PROXY no_proxy; do
        val="${!var:-}"
        [ -n "$val" ] && echo "$var is SET (masked)" || true
    done
    echo "-- quick reachability probe (5s timeout each) --"
    for url in \
        "https://api.prizepicks.com/leagues" \
        "https://www.bovada.lv" \
        "https://sportsbook.draftkings.com" \
        "https://api.underdogfantasy.com" \
        "https://sbapi.fanduel.com"; do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null)"
        [ -z "$code" ] && code="FAIL"
        echo "  $url -> HTTP $code"
    done
    echo "-- end diagnostics --"
} >> "$LOG" 2>&1

# Run the analyzer; capture the exit code without aborting the wrapper.
STATUS=0
"$PY" main.py >> "$LOG" 2>&1 || STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    TZ=America/Los_Angeles date '+%Y-%m-%d' > "$MARKER"
fi

echo "===== Run finished with exit code $STATUS at $(date '+%H:%M:%S %Z') =====" >> "$LOG"
exit $STATUS
