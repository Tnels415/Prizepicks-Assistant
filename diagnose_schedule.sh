#!/usr/bin/env bash
#
# diagnose_schedule.sh — report why the daily scheduled run did or didn't fire.
# Read-only: inspects scheduler state, interpreter resolution, and recent logs.
#
# Usage:  ./diagnose_schedule.sh

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.prizepicks.assistant.daily"
OS="$(uname -s)"
MARKER="$REPO_DIR/logs/last_success.date"

echo "============================================================"
echo " Daily-schedule diagnostic"
echo " Repo:  $REPO_DIR"
echo " OS:    $OS"
echo " Now:   $(date '+%Y-%m-%d %H:%M:%S %Z')  (Pacific: $(TZ=America/Los_Angeles date '+%H:%M'))"
echo "============================================================"

# --- 1. Runner present? -----------------------------------------------------
echo
echo "[1] Runner script"
if [ -x "$REPO_DIR/run_daily.sh" ]; then
    echo "  OK   run_daily.sh exists and is executable"
else
    echo "  FAIL run_daily.sh missing or not executable  (fix: chmod +x run_daily.sh)"
fi

# --- 2. Which python would a SCHEDULED run use? ------------------------------
# Simulate the minimal PATH launchd/cron provide, plus the extension
# run_daily.sh adds, and probe the same candidate list it uses.
echo
echo "[2] Interpreter resolution under scheduler PATH"
FOUND=""
for candidate in \
    "$REPO_DIR/.venv/bin/python3" "$REPO_DIR/venv/bin/python3" "$REPO_DIR/env/bin/python3" \
    "/opt/homebrew/bin/python3" "/usr/local/bin/python3" "/usr/bin/python3"; do
    [ -x "$candidate" ] || continue
    if "$candidate" -c "import requests" >/dev/null 2>&1; then
        echo "  OK   would use: $candidate  (has project dependencies)"
        FOUND="$candidate"
        break
    else
        echo "  --   $candidate exists but LACKS dependencies (import requests failed)"
    fi
done
if [ -z "$FOUND" ]; then
    echo "  FAIL No python3 with the project dependencies found."
    echo "       Fix: pip3 install -r requirements.txt  (using your Terminal's python3)"
fi

# --- 3. Schedule installed & loaded? -----------------------------------------
echo
echo "[3] Schedule installation"
if [ "$OS" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    if [ -f "$PLIST" ]; then
        echo "  OK   LaunchAgent plist exists: $PLIST"
        if grep -q "StartInterval" "$PLIST"; then
            echo "       Type: interval firing (every 30 min + in-script 9:15 AM PT guard) — current design"
        else
            echo "       Type: OLD single-fire design — re-run ./setup_schedule.sh to upgrade"
        fi
        if launchctl list 2>/dev/null | grep -q "$LABEL"; then
            echo "       launchctl status: LOADED"
            launchctl list | grep "$LABEL" | sed 's/^/         /'
            echo "         (columns: PID  LastExitCode  Label — exit 0 = last invocation OK)"
        else
            echo "  FAIL  Plist exists but NOT loaded into launchd."
            echo "        Fix: ./setup_schedule.sh   (reinstalls and loads it)"
        fi
    else
        echo "  FAIL No LaunchAgent installed. Fix: ./setup_schedule.sh"
    fi
elif [ "$OS" = "Linux" ]; then
    if crontab -l 2>/dev/null | grep -q "$LABEL"; then
        echo "  OK   cron entry installed:"
        crontab -l 2>/dev/null | grep "$LABEL" | sed 's/^/         /'
    else
        echo "  FAIL No cron entry found. Fix: ./setup_schedule.sh"
    fi
fi

# --- 4. Success marker / run history -----------------------------------------
echo
echo "[4] Run history"
if [ -f "$MARKER" ]; then
    echo "  Last successful run (Pacific date): $(cat "$MARKER")"
    if [ "$(cat "$MARKER")" = "$(TZ=America/Los_Angeles date '+%Y-%m-%d')" ]; then
        echo "  OK   Already ran successfully today."
    else
        echo "  NOTE Has not succeeded yet today (will run in the next 30-min slot after 9:15 AM PT)."
    fi
else
    echo "  No success marker yet — the guarded runner has never completed successfully."
fi
RUNLOG="$REPO_DIR/logs/daily_run.log"
if [ -f "$RUNLOG" ]; then
    echo "  Last log entries:"
    grep -E "Scheduled run:|Run finished|ERROR" "$RUNLOG" | tail -4 | sed 's/^/         /'
else
    echo "  No daily_run.log yet — the wrapper has never executed on this machine."
fi

# --- 5. launchd stderr (macOS) ------------------------------------------------
if [ "$OS" = "Darwin" ]; then
    echo
    echo "[5] launchd stderr (logs/launchd.err.log — last 5 lines)"
    ERRLOG="$REPO_DIR/logs/launchd.err.log"
    if [ -s "$ERRLOG" ]; then
        tail -5 "$ERRLOG" | sed 's/^/         /'
    else
        echo "         (empty or absent — no launchd-level errors recorded)"
    fi
fi

# --- 6. "runs happen but pull no props" diagnostics --------------------------
# A scheduler that fires correctly but a props pipeline that comes up empty
# looks identical to "nothing happened" from the outside. Check the two most
# likely causes directly: an env var (proxy/VPN) present in your interactive
# shell but invisible to the scheduler, and whether each sportsbook is even
# reachable from here right now.
echo
echo "[6] Props pipeline: env + live reachability"
echo "  Proxy env vars visible in THIS shell (won't reach the scheduler unless"
echo "  ./setup_schedule.sh was re-run after they were set):"
found_proxy=0
for var in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy NO_PROXY no_proxy; do
    val="${!var:-}"
    if [ -n "$val" ]; then
        echo "    $var is SET"
        found_proxy=1
    fi
done
[ "$found_proxy" = "0" ] && echo "    (none set)"
echo "  Live reachability probe (5s timeout each):"
for url in \
    "https://api.prizepicks.com/leagues" \
    "https://www.bovada.lv" \
    "https://sportsbook.draftkings.com" \
    "https://api.underdogfantasy.com" \
    "https://sbapi.fanduel.com"; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null)"
    [ -z "$code" ] && code="FAIL"
    echo "    $url -> HTTP $code"
done
echo "  Compare this to the '-- quick reachability probe --' block logged inside"
echo "  logs/daily_run.log by the actual scheduled run — if THIS shell reaches a"
echo "  site but the scheduled run's log shows it failing, that's an environment"
echo "  difference (proxy/VPN/DNS) between your Terminal and the scheduler."
echo "  Also check logs/props_$(date '+%Y-%m-%d').log (DEBUG level) for exact"
echo "  per-source HTTP status codes from the actual analyzer run."

echo
echo "============================================================"
echo " To force a run right now:   ./run_daily.sh --now"
echo "============================================================"
