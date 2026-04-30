from __future__ import annotations

"""
Diagnostic script — run this to check your setup before running main.py.
Usage: python3 check.py
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

print("\n" + "=" * 55)
print("  NBA Prop Analyzer — Setup Diagnostic")
print("=" * 55)

errors = 0

# --- .env keys ---
print("\n[1] Checking .env variables...")
checks = {
    "THE_ODDS_API_KEY": "Odds API key (auto prop lines)",
    "EMAIL_FROM":       "Gmail address to send from",
    "EMAIL_PASSWORD":   "Gmail App Password",
    "EMAIL_TO":         "Recipient email address",
}
for var, desc in checks.items():
    val = os.getenv(var)
    if val and val not in ("your_odds_api_key_here", "your_free_key_here",
                           "sender@gmail.com", "your_gmail_app_password"):
        preview = val[:6] + "..." if len(val) > 6 else val
        print(f"  OK  {var:<22} ({desc}) — {preview}")
    else:
        print(f"  MISSING  {var:<22} ({desc})")
        errors += 1

# --- Odds API live test ---
print("\n[2] Testing The Odds API connection...")
odds_key = os.getenv("THE_ODDS_API_KEY")
if not odds_key or odds_key == "your_odds_api_key_here":
    print("  SKIP — THE_ODDS_API_KEY not set")
else:
    try:
        import requests
        resp = requests.get(
            "https://api.the-odds-api.com/v4/sports/basketball_nba/events",
            params={"apiKey": odds_key, "dateFormat": "iso"},
            timeout=10,
        )
        if resp.status_code == 401:
            print("  FAIL — API key is invalid (401 Unauthorized)")
            errors += 1
        elif resp.status_code == 200:
            events = resp.json()
            remaining = resp.headers.get("x-requests-remaining", "?")
            now = datetime.now(timezone.utc)
            window_start = now - timedelta(hours=4)
            window_end   = now + timedelta(hours=20)
            todays = []
            for e in events:
                try:
                    ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
                    if window_start <= ct <= window_end:
                        todays.append(e)
                except Exception:
                    pass
            print(f"  OK  — API key valid  |  {remaining} requests remaining this month")
            print(f"  OK  — {len(todays)} NBA game(s) found in today's window")
            for e in todays:
                ct_str = e.get("commence_time", "")[:16].replace("T", " ") + " UTC"
                print(f"        {e.get('away_team')} @ {e.get('home_team')}  ({ct_str})")
            if not todays:
                print("  NOTE — No games in window yet. If games are scheduled today,")
                print("         the Odds API may not have listed them yet.")
        else:
            print(f"  FAIL — Unexpected HTTP {resp.status_code}")
            errors += 1
    except Exception as exc:
        print(f"  FAIL — Could not reach api.the-odds-api.com: {exc}")
        errors += 1

# --- Summary ---
print("\n" + "=" * 55)
if errors == 0:
    print("  All checks passed. Run: python3 main.py")
    print("  NOTE: Prop lines are usually posted after 1 PM ET.")
else:
    print(f"  {errors} issue(s) found — fix them in your .env file.")
    print()
    print("  Your .env should contain:")
    print("    THE_ODDS_API_KEY=<key from the-odds-api.com>")
    print("    EMAIL_FROM=tnelson8822@gmail.com")
    print("    EMAIL_PASSWORD=<16-char Gmail App Password>")
    print("    EMAIL_TO=tnelson8822@gmail.com")
print("=" * 55 + "\n")

sys.exit(errors)
