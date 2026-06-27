#!/usr/bin/env bash
#
# setup_schedule.sh — install a daily scheduled run of the prop analyzer
# at 9:00 AM Pacific Time.
#
#   macOS  → installs a launchd LaunchAgent (runs at 9 AM; if the Mac was
#            asleep, launchd runs it as soon as the Mac wakes).
#   Linux  → installs a cron job (runs at 9 AM when the machine is on).
#
# Usage:
#   ./setup_schedule.sh            # install / update the daily schedule
#   ./setup_schedule.sh --remove   # uninstall it
#
# Re-running is safe — it replaces any previous entry this script created.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$REPO_DIR/run_daily.sh"
LABEL="com.prizepicks.assistant.daily"
RUN_HOUR=9          # 9 AM ...
RUN_MINUTE=0
TZ_NAME="America/Los_Angeles"   # ... Pacific Time

chmod +x "$RUNNER" 2>/dev/null || true

OS="$(uname -s)"
REMOVE=0
[ "${1:-}" = "--remove" ] && REMOVE=1

# --- Translate 9 AM Pacific into this machine's LOCAL clock time ------------
# Schedulers fire on the machine's local time, so if the computer is not set
# to Pacific we shift the hour to whatever local time equals 9 AM Pacific.
local_hhmm_for_pacific_9am() {
    # Print "HH MM" (local) corresponding to today 09:00 America/Los_Angeles.
    python3 - "$RUN_HOUR" "$RUN_MINUTE" "$TZ_NAME" <<'PYEOF'
import sys
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    print("", end="")
    sys.exit(0)
hour, minute, tzname = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
now = datetime.now()
pac = now.astimezone(ZoneInfo(tzname)).replace(hour=hour, minute=minute, second=0, microsecond=0)
local = pac.astimezone()  # convert back to system local tz
print(f"{local.hour} {local.minute}")
PYEOF
}

read -r LOCAL_HOUR LOCAL_MIN < <(local_hhmm_for_pacific_9am) || true
if [ -z "${LOCAL_HOUR:-}" ]; then
    # zoneinfo unavailable (very old Python) — assume the machine is on Pacific.
    LOCAL_HOUR=$RUN_HOUR
    LOCAL_MIN=$RUN_MINUTE
    echo "NOTE: could not resolve timezone automatically; assuming this machine"
    echo "      is set to Pacific Time. If it isn't, edit the schedule's hour."
fi

case "$OS" in
# =====================================================================
Darwin)
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    if [ "$REMOVE" = "1" ]; then
        launchctl unload "$PLIST" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Removed launchd schedule ($PLIST)."
        exit 0
    fi

    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$RUNNER</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$LOCAL_HOUR</integer>
        <key>Minute</key>
        <integer>$LOCAL_MIN</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$REPO_DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO_DIR/logs/launchd.err.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLISTEOF

    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Installed launchd schedule."
    echo "  Runs daily at ${LOCAL_HOUR}:$(printf '%02d' "$LOCAL_MIN") local time (= 9:00 AM Pacific)."
    echo "  If the Mac is asleep at that time, it runs when the Mac next wakes."
    echo "  Plist: $PLIST"
    echo "  Logs:  $REPO_DIR/logs/daily_run.log"
    ;;
# =====================================================================
Linux)
    CRON_TAG="# $LABEL"
    CRON_LINE="$LOCAL_MIN $LOCAL_HOUR * * * /bin/bash $RUNNER $CRON_TAG"
    # Pull current crontab (may be empty), drop any prior line we added.
    EXISTING="$(crontab -l 2>/dev/null | grep -v "$CRON_TAG" || true)"
    if [ "$REMOVE" = "1" ]; then
        printf '%s\n' "$EXISTING" | crontab -
        echo "Removed cron schedule."
        exit 0
    fi
    printf '%s\n%s\n' "$EXISTING" "$CRON_LINE" | sed '/^$/d' | crontab -
    echo "Installed cron schedule."
    echo "  Runs daily at ${LOCAL_HOUR}:$(printf '%02d' "$LOCAL_MIN") local time (= 9:00 AM Pacific)."
    echo "  The machine must be powered on at that time for the run to fire."
    echo "  Logs: $REPO_DIR/logs/daily_run.log"
    ;;
# =====================================================================
*)
    echo "Unsupported OS '$OS'. This installer handles macOS and Linux."
    echo "On Windows, use Task Scheduler to run: bash $RUNNER"
    exit 1
    ;;
esac
