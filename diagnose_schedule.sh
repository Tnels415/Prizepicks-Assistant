#!/usr/bin/env bash
#
# diagnose_schedule.sh — report why the daily scheduled run did or didn't fire.
# Read-only: it inspects the scheduler state and recent logs, changes nothing.
#
# Usage:  ./diagnose_schedule.sh

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.prizepicks.assistant.daily"
OS="$(uname -s)"

echo "============================================================"
echo " Daily-schedule diagnostic"
echo " Repo:  $REPO_DIR"
echo " OS:    $OS"
echo " Now:   $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "============================================================"

# --- 1. Is the runner present and executable? -----------------------------
echo
echo "[1] Runner script"
if [ -x "$REPO_DIR/run_daily.sh" ]; then
    echo "  OK   run_daily.sh exists and is executable"
else
    echo "  FAIL run_daily.sh missing or not executable"
    echo "       Fix: chmod +x run_daily.sh"
fi

# --- 2. Is the schedule installed? ----------------------------------------
echo
echo "[2] Schedule installation"
if [ "$OS" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    if [ -f "$PLIST" ]; then
        echo "  OK   LaunchAgent plist exists: $PLIST"
        echo "       Scheduled time (local clock):"
        # Print the Hour/Minute the plist will fire at.
        /usr/libexec/PlistBuddy -c "Print :StartCalendarInterval" "$PLIST" 2>/dev/null \
            | sed 's/^/         /'
        echo "       launchctl status:"
        if launchctl list | grep -q "$LABEL"; then
            launchctl list | grep "$LABEL" | sed 's/^/         /'
            echo "         (columns: PID  LastExitCode  Label)"
            echo "         PID '-' = not running right now (normal between runs)."
            echo "         LastExitCode 0 = last run succeeded; non-zero = it ran but failed."
        else
            echo "         NOT LOADED — the plist file exists but launchd isn't tracking it."
            echo "         Fix:  launchctl load \"$PLIST\""
        fi
    else
        echo "  FAIL No LaunchAgent installed."
        echo "       The schedule was never set up on this machine."
        echo "       Fix:  ./setup_schedule.sh"
    fi
elif [ "$OS" = "Linux" ]; then
    if crontab -l 2>/dev/null | grep -q "$LABEL"; then
        echo "  OK   cron entry installed:"
        crontab -l 2>/dev/null | grep "$LABEL" | sed 's/^/         /'
    else
        echo "  FAIL No cron entry found."
        echo "       Fix:  ./setup_schedule.sh"
    fi
fi

# --- 3. Did it actually run recently? -------------------------------------
echo
echo "[3] Recent run history (logs/daily_run.log)"
RUNLOG="$REPO_DIR/logs/daily_run.log"
if [ -f "$RUNLOG" ]; then
    LAST_START="$(grep "Scheduled run:" "$RUNLOG" | tail -1)"
    LAST_FINISH="$(grep "Run finished" "$RUNLOG" | tail -1)"
    if [ -n "$LAST_START" ]; then
        echo "  Last start:  ${LAST_START#*=====}"
        echo "  Last finish: ${LAST_FINISH#*=====}"
    else
        echo "  No 'Scheduled run' entries yet — the wrapper has never executed."
        echo "  (If the schedule is installed, the Mac may have been OFF at run time."
        echo "   launchd catches up after SLEEP, but not after a full power-off.)"
    fi
else
    echo "  No daily_run.log yet — the wrapper has never executed on this machine."
fi

# --- 4. launchd's own stderr (macOS) --------------------------------------
if [ "$OS" = "Darwin" ]; then
    echo
    echo "[4] launchd stderr (logs/launchd.err.log — last 10 lines)"
    ERRLOG="$REPO_DIR/logs/launchd.err.log"
    if [ -s "$ERRLOG" ]; then
        tail -10 "$ERRLOG" | sed 's/^/         /'
    else
        echo "         (empty or absent — no launchd-level errors recorded)"
    fi
fi

echo
echo "============================================================"
echo " Most common causes when [2] is OK but [3] shows no runs:"
echo "   • The Mac was powered OFF (not just asleep) at run time."
echo "   • You are not logged into the macOS user account at run time"
echo "     (LaunchAgents only run while the user is logged in)."
echo " To test the runner right now:   ./run_daily.sh  then re-check [3]."
echo "============================================================"
