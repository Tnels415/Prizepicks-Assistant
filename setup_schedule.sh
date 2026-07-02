#!/usr/bin/env bash
#
# setup_schedule.sh — install the daily scheduled run of the prop analyzer.
#
# Design: the scheduler fires run_daily.sh every 30 minutes; run_daily.sh's
# internal guard makes every invocation a silent no-op EXCEPT the first one
# at/after 9:00 AM Pacific that hasn't succeeded yet today. This is far more
# reliable than a single fire-at-9:00 schedule, which silently skips the day
# if the machine is off, asleep, or logged out at that exact minute.
#
#   macOS  → launchd LaunchAgent with StartInterval=1800
#   Linux  → cron entry: */30 * * * *
#
# Usage:
#   ./setup_schedule.sh            # install / update
#   ./setup_schedule.sh --remove   # uninstall
#
# Re-running is safe — it replaces any previous entry this script created.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$REPO_DIR/run_daily.sh"
LABEL="com.prizepicks.assistant.daily"
INTERVAL_SECS=1800   # fire every 30 min; run_daily.sh guard picks the right one

chmod +x "$RUNNER" 2>/dev/null || true

OS="$(uname -s)"
REMOVE=0
[ "${1:-}" = "--remove" ] && REMOVE=1

case "$OS" in
# =====================================================================
Darwin)
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    GUI_TARGET="gui/$(id -u)"

    if [ "$REMOVE" = "1" ]; then
        launchctl bootout "$GUI_TARGET/$LABEL" 2>/dev/null \
            || launchctl unload "$PLIST" 2>/dev/null || true
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
    <key>StartInterval</key>
    <integer>$INTERVAL_SECS</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$REPO_DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO_DIR/logs/launchd.err.log</string>
</dict>
</plist>
PLISTEOF

    # Unload any previous version, then load with the modern command,
    # falling back to the legacy one on older macOS.
    launchctl bootout "$GUI_TARGET/$LABEL" 2>/dev/null \
        || launchctl unload "$PLIST" 2>/dev/null || true
    if launchctl bootstrap "$GUI_TARGET" "$PLIST" 2>/dev/null; then
        LOADED_VIA="launchctl bootstrap"
    elif launchctl load "$PLIST" 2>/dev/null; then
        LOADED_VIA="launchctl load (legacy)"
    else
        echo "ERROR: could not load the LaunchAgent. Try logging out/in, then:"
        echo "  launchctl bootstrap $GUI_TARGET \"$PLIST\""
        exit 1
    fi

    echo "Installed launchd schedule (via $LOADED_VIA)."
    echo "  Fires every 30 min; actually runs in the first awake window at/after 9:00 AM Pacific."
    echo "  If the Mac is asleep at 9:00, it catches up as soon as it's awake — no day is skipped"
    echo "  as long as the Mac is on and you are logged in at some point after 9 AM PT."
    echo "  Plist: $PLIST"
    echo "  Logs:  $REPO_DIR/logs/daily_run.log"
    echo ""
    echo "Test it now with:  ./run_daily.sh --now"
    ;;
# =====================================================================
Linux)
    CRON_TAG="# $LABEL"
    CRON_LINE="*/30 * * * * /bin/bash $RUNNER $CRON_TAG"
    EXISTING="$(crontab -l 2>/dev/null | grep -v "$CRON_TAG" || true)"
    if [ "$REMOVE" = "1" ]; then
        printf '%s\n' "$EXISTING" | crontab -
        echo "Removed cron schedule."
        exit 0
    fi
    printf '%s\n%s\n' "$EXISTING" "$CRON_LINE" | sed '/^$/d' | crontab -
    echo "Installed cron schedule."
    echo "  Fires every 30 min; actually runs in the first window at/after 9:00 AM Pacific."
    echo "  Logs: $REPO_DIR/logs/daily_run.log"
    echo ""
    echo "Test it now with:  ./run_daily.sh --now"
    ;;
# =====================================================================
*)
    echo "Unsupported OS '$OS'. This installer handles macOS and Linux."
    echo "On Windows, use Task Scheduler to run: bash $RUNNER  every 30 minutes."
    exit 1
    ;;
esac
