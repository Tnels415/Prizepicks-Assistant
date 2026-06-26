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
print("  Prop Analyzer — Setup Diagnostic")
print("=" * 55)

errors = 0

# --- Python version ---
print("\n[0] Checking Python version...")
v = sys.version_info
if v >= (3, 9):
    print(f"  OK  Python {v.major}.{v.minor}.{v.micro}")
else:
    print(f"  WARN  Python {v.major}.{v.minor} — recommend 3.9+")

# --- Core package imports ---
print("\n[1] Checking core package imports...")
for module, pkg in [
    ("requests", "requests"),
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("dotenv", "python-dotenv"),
    ("tenacity", "tenacity"),
]:
    try:
        __import__(module)
        print(f"  OK  {module}")
    except ImportError:
        print(f"  MISSING  {module}  (install: pip install {pkg})")
        errors += 1

# --- Analyzer module imports ---
print("\n[2] Checking analyzer imports (catches code bugs)...")
try:
    from analysis import market as _market
    p_over, _ = _market.devig_two_way(-110, -110)
    assert abs(p_over - 50.0) < 0.01
    print("  OK  analysis.market (de-vig math)")
except Exception as exc:
    print(f"  FAIL  analysis.market: {exc}")
    errors += 1

try:
    from data.injury_client import classify_injury_severity
    assert classify_injury_severity("Out") == "out"
    assert classify_injury_severity("Questionable") == "questionable"
    assert classify_injury_severity(None) == "none"
    print("  OK  data.injury_client (tiered classification)")
except Exception as exc:
    print(f"  FAIL  data.injury_client: {exc}")
    errors += 1

try:
    from data.draftkings_client import _normalize_name
    assert _normalize_name("P.J. Washington Jr.") == "pj washington"
    print("  OK  data.draftkings_client (name normalization)")
except Exception as exc:
    print(f"  FAIL  data.draftkings_client: {exc}")
    errors += 1

# --- .env keys ---
print("\n[3] Checking .env variables...")
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
print("\n[4] Testing The Odds API connection...")
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
                print("  NOTE — No NBA games in window. NBA season may have ended,")
                print("         or games haven't been listed yet. Check MLB is working.")
        else:
            print(f"  FAIL — Unexpected HTTP {resp.status_code}")
            errors += 1
    except Exception as exc:
        print(f"  FAIL — Could not reach api.the-odds-api.com: {exc}")
        errors += 1

# --- PrizePicks availability ---
print("\n[5] Testing PrizePicks API connection...")
try:
    _pp_url = "https://api.prizepicks.com/projections"
    _pp_params = {"league_id": 2, "per_page": 5, "single_stat": "true"}
    _pp_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4.1 Safari/605.1.15"
        ),
        "Accept": "application/json",
        "Referer": "https://app.prizepicks.com/",
        "Origin": "https://app.prizepicks.com",
    }

    _pp_resp = None
    _pp_method = ""

    # Try curl_cffi first (better Cloudflare bypass via real TLS fingerprint)
    try:
        from curl_cffi import requests as _cf
        _cf_session = _cf.Session(impersonate="chrome124")
        _cf_session.get("https://app.prizepicks.com/", headers=_pp_headers, timeout=8)
        import time as _t; _t.sleep(1)
        _pp_resp = _cf_session.get(_pp_url, params=_pp_params, headers=_pp_headers, timeout=12)
        _pp_method = "curl_cffi/chrome124"
        print("  OK  — curl_cffi installed (TLS fingerprint bypass active)")
    except ImportError:
        print("  WARN — curl_cffi not installed. Install with:  pip3 install curl-cffi")
        print("         Falling back to plain requests (may get 403 from Cloudflare).")
        import requests as _req
        _pp_resp = _req.get(_pp_url, params=_pp_params, headers=_pp_headers, timeout=12)
        _pp_method = "requests"
    except Exception as _cf_exc:
        print(f"  WARN — curl_cffi attempt failed: {_cf_exc}")
        import requests as _req
        _pp_resp = _req.get(_pp_url, params=_pp_params, headers=_pp_headers, timeout=12)
        _pp_method = "requests"

    if _pp_resp is not None:
        if _pp_resp.status_code == 200:
            count = len(_pp_resp.json().get("data", []))
            print(f"  OK  — PrizePicks reachable via {_pp_method}, {count} MLB projections in sample")
            if count == 0:
                print("  NOTE — 0 MLB props returned. Two likely causes:")
                print("         (a) PrizePicks hasn't posted today's lines yet — try after 11 AM ET")
                print("         (b) league_id=2 may have changed — the system will auto-discover the correct ID")
        elif _pp_resp.status_code == 403:
            print(f"  WARN — PrizePicks returned 403 via {_pp_method} (Cloudflare bot block).")
            if "curl_cffi" not in _pp_method:
                print("         Install curl_cffi for a better bypass:  pip3 install curl-cffi")
            print("         Options: (1) wait 1-2 hours and retry; (2) fill props.json manually")
            errors += 1
        else:
            print(f"  WARN — PrizePicks HTTP {_pp_resp.status_code}: {_pp_resp.text[:80]}")
except Exception as exc:
    print(f"  WARN — PrizePicks unreachable: {exc}")

# --- DraftKings reachability ---
print("\n[6] Testing DraftKings (market-odds anchor)...")
try:
    import requests
    resp = requests.get(
        "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/40625",
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 Safari/605.1.15",
            "Accept": "application/json",
            "Referer": "https://sportsbook.draftkings.com/",
        },
        params={"format": "json"},
        timeout=8,
    )
    if resp.status_code == 200:
        print("  OK  — DraftKings reachable; market-odds blend will be active")
    elif resp.status_code == 403:
        print("  WARN — DraftKings returned 403. Market-odds blend will be skipped.")
        print("         Model runs on pure statistical analysis (still fully functional).")
    else:
        print(f"  WARN — DraftKings HTTP {resp.status_code}")
except Exception as exc:
    print(f"  WARN — DraftKings unreachable: {exc}")
    print("         Market-odds blend skipped; model runs on pure analysis (still functional).")

# --- Active sports check ---
print("\n[7] Active sports today...")
try:
    from config import get_active_sports, SPORT_CONFIG
    from datetime import date
    active = get_active_sports()
    print(f"  Active sports: {', '.join(active) if active else '(none)'}")
    if not active:
        print("  WARN — No sports active this month. Check SPORT_CONFIG active_months.")
        errors += 1
except Exception as exc:
    print(f"  FAIL  config: {exc}")
    errors += 1

# --- Summary ---
print("\n" + "=" * 55)
if errors == 0:
    print("  All checks passed. Run: python3 main.py")
    print("  NOTE: Prop lines are usually posted after 1 PM ET.")
else:
    print(f"  {errors} issue(s) found — fix them before running main.py.")
    print()
    print("  Your .env should contain:")
    print("    THE_ODDS_API_KEY=<key from the-odds-api.com>")
    print("    EMAIL_FROM=tnelson8822@gmail.com")
    print("    EMAIL_PASSWORD=<16-char Gmail App Password>")
    print("    EMAIL_TO=tnelson8822@gmail.com")
print("=" * 55 + "\n")

sys.exit(errors)
