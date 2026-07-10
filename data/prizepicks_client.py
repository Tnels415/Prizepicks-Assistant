from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from datetime import date, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path

try:
    from curl_cffi import requests as _cf_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

try:
    import browser_cookie3 as _browser_cookie3
    _HAS_BROWSER_COOKIES = True
except ImportError:
    _HAS_BROWSER_COOKIES = False

import requests

from config import SPORT_CONFIG, PRIZEPICKS_URL

logger = logging.getLogger(__name__)

PROPS_FILE = Path("props.json")


def _normalize_player_name(name: str) -> str:
    """Normalize a player name for cross-source de-duplication when merging
    props from multiple books (e.g. "Luka Dončić" vs "Luka Doncic")."""
    try:
        from data.draftkings_client import _normalize_name
        return _normalize_name(name)
    except Exception:
        return (name or "").strip().lower()


# Module-level cache of PrizePicks league name → ID, fetched lazily on first
# request.  Avoids hardcoding league_ids that PrizePicks may change.
_PP_LEAGUE_MAP: dict[str, int] | None = None

# Stat types that belong to non-target sports (e.g. MMA/UFC) and should be
# silently discarded regardless of which sport is being fetched.  PrizePicks
# periodically reassigns league IDs; when a configured ID ends up pointing at
# MMA we'd otherwise flood the log with WARNING-level "unrecognized stat type"
# messages before the dynamic-league-ID fallback has a chance to run.
_GLOBALLY_IGNORED_STAT_TYPES: frozenset[str] = frozenset({
    # MMA / UFC
    "Significant Strikes",
    "Total Rounds",
    "Fight Time (Mins)",
    "Takedowns",
    "KD",
    "Submission Attempts",
    # Fantasy-score composite lines (not a real game stat)
    "Fantasy Score",
})

# Sport name → list of PrizePicks league name patterns to match (case-insensitive
# substring match against the league's "name" attribute in the /leagues response).
_PP_LEAGUE_NAME_PATTERNS = {
    "NBA": ["NBA"],
    "NHL": ["NHL"],
    "NFL": ["NFL"],
    "MLB": ["MLB"],
}


# Browser identifiers for curl_cffi impersonation (real TLS fingerprints).
# Tried in order; first 200 response wins.
_CURL_CFFI_BROWSERS = [
    "chrome124", "chrome120", "chrome116", "safari17_0", "safari15_5",
]

_PP_HEADER_VARIANTS = [
    # Variant 1: Desktop Chrome — full set including sec-ch-ua hints
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://app.prizepicks.com/",
        "Origin": "https://app.prizepicks.com",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Connection": "keep-alive",
    },
    # Variant 2: Safari macOS
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4.1 Safari/605.1.15"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://app.prizepicks.com/",
        "Origin": "https://app.prizepicks.com",
    },
    # Variant 3: Mobile Chrome (Android)
    {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.6367.82 Mobile Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://app.prizepicks.com/",
        "Origin": "https://app.prizepicks.com",
        "X-Device-Id": "prizepicks-client",
    },
]

_PP_BASE_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://app.prizepicks.com/",
    "Origin": "https://app.prizepicks.com",
}


class PrizePicksLiveClient:
    """
    Fetches live player prop lines directly from the PrizePicks projections API.
    No API key required — used as a free fallback when The Odds API is exhausted.
    Tries multiple header strategies in case Cloudflare bot detection blocks one.
    """

    # Failure reason codes returned by _fetch_with_league_id
    _REASON_OK = "ok"
    _REASON_BLOCKED = "blocked"       # every attempt returned HTTP 403
    _REASON_NOT_POSTED = "not_posted" # API returned 200 but 0 usable props
    _REASON_ERROR = "error"           # network / parse error

    @staticmethod
    def _get_browser_cookies() -> dict[str, str]:
        """Extract PrizePicks cookies from the user's installed browser.

        Uses browser_cookie3 when available.  Falls back to an empty dict
        gracefully — the rest of the fetch logic still runs without cookies.
        """
        if not _HAS_BROWSER_COOKIES:
            return {}
        domain = "prizepicks.com"
        for browser_fn_name in ("chrome", "chromium", "safari", "firefox", "edge", "brave"):
            try:
                fn = getattr(_browser_cookie3, browser_fn_name)
                cj = fn(domain_name=domain)
                cookies = {c.name: c.value for c in cj if domain in c.domain}
                if cookies:
                    logger.debug(
                        "PrizePicks: extracted %d cookies from %s browser",
                        len(cookies), browser_fn_name,
                    )
                    return cookies
            except Exception:
                continue
        return {}

    @staticmethod
    def _fetch_via_system_curl(url: str, params: dict, headers: dict, cookies: dict) -> bytes | None:
        """Fetch url using the system `curl` binary.

        System curl on macOS uses Apple SecureTransport (a completely different
        TLS stack from Python's ssl module), so its JA3 fingerprint looks like a
        real browser to Cloudflare.  On Linux it uses the system OpenSSL build,
        which is also distinct from Python's bundled one.

        Returns raw response bytes on success, None on any error.
        """
        curl_path = shutil.which("curl")
        if not curl_path:
            return None

        # Build query string
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        full_url = f"{url}?{qs}"

        cmd = [curl_path, "-s", "--max-time", "25", "--compressed", full_url]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        if cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            cmd += ["-H", f"Cookie: {cookie_str}"]
        # Write HTTP response code to stderr-adjacent output so we can check it
        cmd += ["-w", "\n__STATUS__:%{http_code}"]

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            output = result.stdout.decode("utf-8", errors="replace")
            # Split off the appended status code
            if "\n__STATUS__:" in output:
                body, status_str = output.rsplit("\n__STATUS__:", 1)
                status = int(status_str.strip())
            else:
                body = output
                status = 0
            if status == 200:
                return body.encode("utf-8")
            logger.warning(
                "PrizePicks system curl returned HTTP %d — %s",
                status, "bot block (403)" if status == 403 else "unexpected status",
            )
            return None
        except Exception as exc:
            logger.debug("PrizePicks system curl failed: %s", exc)
            return None

    def fetch_props(self, sport_config: dict) -> tuple[list[dict], str]:
        """Return (props, reason) where reason is one of the _REASON_* constants."""
        sport_name = sport_config["name"]
        configured_id = sport_config.get("prizepicks_league_id")
        if not configured_id:
            return [], self._REASON_ERROR

        props, reason = self._fetch_with_league_id(configured_id, sport_config)
        if props:
            return props, self._REASON_OK

        # If we were blocked we can't discover a new ID either — bail early.
        if reason == self._REASON_BLOCKED:
            return [], reason

        # Got through but 0 props — league_id may have been reassigned (e.g. to UFC).
        # Try dynamic discovery and retry once.
        discovered_id = self._lookup_league_id(sport_name)
        if discovered_id and discovered_id != configured_id:
            logger.warning(
                "PrizePicks %s: league_id=%s returned no usable props "
                "(may have been reassigned to a different sport). "
                "Retrying with dynamically discovered league_id=%s. "
                "Update 'prizepicks_league_id' in config.py to %s to make this permanent.",
                sport_name, configured_id, discovered_id, discovered_id,
            )
            props, reason = self._fetch_with_league_id(discovered_id, sport_config)
            if props:
                return props, self._REASON_OK

        return [], reason

    def _fetch_with_league_id(self, league_id: int, sport_config: dict) -> tuple[list[dict], str]:
        """Try all HTTP strategies and return (props, reason)."""
        sport_name = sport_config["name"]
        params = {"league_id": league_id, "per_page": 250, "single_stat": "true"}
        got_any_200 = False

        def _handle_raw(raw_bytes: bytes, label: str) -> tuple[list[dict], str] | None:
            """Parse raw JSON bytes; return result tuple or None on parse error."""
            nonlocal got_any_200
            got_any_200 = True
            try:
                data = json.loads(raw_bytes)
            except Exception as exc:
                logger.warning("PrizePicks %s (%s): JSON parse error: %s", sport_name, label, exc)
                return None
            props = self._parse(data, sport_config)
            if props:
                logger.info(
                    "PrizePicks API: %d %s props fetched via %s (league_id=%s)",
                    len(props), sport_name, label, league_id,
                )
                return props, self._REASON_OK
            logger.warning(
                "PrizePicks %s (league_id=%s) via %s: 200 OK but 0 usable props. "
                "PrizePicks may not have posted today's lines yet (usually after "
                "~11 AM ET). Try re-running after noon, or fill props.json manually.",
                sport_name, league_id, label,
            )
            return [], self._REASON_NOT_POSTED

        # Grab browser cookies once — used by all strategies that can accept them.
        browser_cookies = self._get_browser_cookies()
        if browser_cookies:
            logger.debug("PrizePicks: using %d cookie(s) from local browser", len(browser_cookies))

        # --- Strategy 1: system curl (native TLS stack — macOS SecureTransport /
        #     Linux OpenSSL build — different JA3 fingerprint from Python ssl) ---
        raw = self._fetch_via_system_curl(PRIZEPICKS_URL, params, _PP_BASE_HEADERS, browser_cookies)
        if raw is not None:
            result = _handle_raw(raw, "system-curl")
            if result is not None:
                return result

        # --- Strategy 2: curl_cffi — Python browser TLS impersonation ----------
        if _HAS_CURL_CFFI:
            for browser in _CURL_CFFI_BROWSERS:
                try:
                    session = _cf_requests.Session(impersonate=browser)
                    if browser_cookies:
                        session.cookies.update(browser_cookies)
                    try:
                        session.get("https://app.prizepicks.com/", headers=_PP_BASE_HEADERS, timeout=10)
                        time.sleep(1)
                    except Exception:
                        pass
                    resp = session.get(PRIZEPICKS_URL, params=params, headers=_PP_BASE_HEADERS, timeout=20)
                    if resp.status_code == 403:
                        logger.warning(
                            "PrizePicks %s: 403 via curl_cffi/%s. Trying next fingerprint.",
                            sport_name, browser,
                        )
                        time.sleep(2)
                        continue
                    resp.raise_for_status()
                    result = _handle_raw(resp.content, f"curl_cffi/{browser}")
                    if result is not None:
                        return result
                except Exception as exc:
                    logger.debug("PrizePicks curl_cffi/%s error for %s: %s", browser, sport_name, exc)
        else:
            logger.debug("curl_cffi not installed — skipping (pip3 install curl-cffi to enable)")

        # --- Strategy 3: plain requests — varied headers -----------------------
        for i, hdrs in enumerate(_PP_HEADER_VARIANTS):
            merged = {**hdrs}
            try:
                s = requests.Session()
                if browser_cookies:
                    s.cookies.update(browser_cookies)
                resp = s.get(PRIZEPICKS_URL, params=params, headers=merged, timeout=20)
                if resp.status_code == 403:
                    logger.warning(
                        "PrizePicks %s: 403 via requests/variant-%d. Trying next.",
                        sport_name, i + 1,
                    )
                    time.sleep(2)
                    continue
                resp.raise_for_status()
                result = _handle_raw(resp.content, f"requests/variant-{i+1}")
                if result is not None:
                    return result
            except requests.exceptions.HTTPError:
                continue
            except Exception as exc:
                logger.warning("PrizePicks %s requests/variant-%d error: %s", sport_name, i + 1, exc)
                break

        if got_any_200:
            return [], self._REASON_NOT_POSTED

        logger.warning(
            "PrizePicks %s (league_id=%s): all strategies returned 403. "
            "Possible fixes:\n"
            "  1. Open PrizePicks in your browser (sets cookies), then re-run.\n"
            "  2. Install curl-cffi:  pip3 install curl-cffi\n"
            "  3. Install browser_cookie3:  pip3 install browser-cookie3\n"
            "  4. Fill props.json manually with today's lines.",
            sport_name, league_id,
        )
        return [], self._REASON_BLOCKED

    def _lookup_league_id(self, sport_name: str) -> int | None:
        """Fetch /leagues and resolve sport_name → league_id by name match."""
        global _PP_LEAGUE_MAP
        if _PP_LEAGUE_MAP is None:
            _PP_LEAGUE_MAP = self._fetch_leagues()
        if not _PP_LEAGUE_MAP:
            return None
        patterns = _PP_LEAGUE_NAME_PATTERNS.get(sport_name, [sport_name])
        for pattern in patterns:
            for league_name, league_id in _PP_LEAGUE_MAP.items():
                if pattern.lower() == league_name.lower():
                    return league_id
        # Substring fallback
        for pattern in patterns:
            for league_name, league_id in _PP_LEAGUE_MAP.items():
                if pattern.lower() in league_name.lower():
                    return league_id
        logger.warning(
            "PrizePicks: no league_id found for %s. Available leagues: %s",
            sport_name, sorted(_PP_LEAGUE_MAP.keys()),
        )
        return None

    def _fetch_leagues(self) -> dict[str, int]:
        """Fetch the public PrizePicks /leagues endpoint and return name → id map."""
        url = "https://api.prizepicks.com/leagues"

        def _parse_raw(raw: bytes) -> dict[str, int]:
            data = json.loads(raw)
            mapping: dict[str, int] = {}
            for obj in data.get("data", []):
                name = obj.get("attributes", {}).get("name", "")
                try:
                    lid = int(obj.get("id"))
                except (TypeError, ValueError):
                    continue
                if name:
                    mapping[name] = lid
            return mapping

        browser_cookies = self._get_browser_cookies()

        # Strategy 1: system curl
        raw = self._fetch_via_system_curl(url, {}, _PP_BASE_HEADERS, browser_cookies)
        if raw:
            try:
                m = _parse_raw(raw)
                logger.info("PrizePicks /leagues: %d leagues via system-curl", len(m))
                return m
            except Exception:
                pass

        # Strategy 2: curl_cffi
        if _HAS_CURL_CFFI:
            for browser in _CURL_CFFI_BROWSERS[:2]:
                try:
                    resp = _cf_requests.get(url, headers=_PP_BASE_HEADERS,
                                            impersonate=browser, timeout=15)
                    if resp.status_code == 200:
                        m = _parse_raw(resp.content)
                        logger.info("PrizePicks /leagues: %d leagues via curl_cffi/%s", len(m), browser)
                        return m
                except Exception as exc:
                    logger.debug("PrizePicks /leagues curl_cffi/%s failed: %s", browser, exc)

        # Strategy 3: plain requests
        for hdrs in _PP_HEADER_VARIANTS:
            try:
                resp = requests.get(url, headers=hdrs, timeout=15)
                if resp.status_code != 200:
                    continue
                m = _parse_raw(resp.content)
                logger.info("PrizePicks /leagues: %d leagues via requests", len(m))
                return m
            except Exception as exc:
                logger.debug("PrizePicks /leagues fetch failed: %s", exc)
        return {}

    def _parse(self, data: dict, sport_config: dict) -> list[dict]:
        sport_name = sport_config["name"]
        prop_stat_map = sport_config["prop_stat_map"]
        pp_stat_map = sport_config.get("prizepicks_stat_map", {})
        team_name_to_abbr = sport_config.get("team_name_to_abbr", {})

        # Build player lookup: id → {name, team, position}
        players: dict[str, dict] = {}
        for obj in data.get("included", []):
            if obj.get("type") == "new_player":
                attrs = obj.get("attributes", {})
                raw_team = attrs.get("team", "")
                team_abbr = team_name_to_abbr.get(raw_team, raw_team)
                players[obj["id"]] = {
                    "name": attrs.get("name", ""),
                    "team": team_abbr,
                    "position": attrs.get("position", ""),
                }

        all_projections = [p for p in data.get("data", []) if p.get("type") == "projection"]
        logger.debug("PrizePicks %s: %d raw projections received", sport_name, len(all_projections))

        if not all_projections:
            logger.warning(
                "PrizePicks returned 0 %s projections — the league_id (%s) may be incorrect "
                "or PrizePicks has not yet posted lines for today's games.",
                sport_name, sport_config.get("prizepicks_league_id"),
            )

        seen: dict[tuple, dict] = {}
        candidates: dict[tuple, list[dict]] = {}
        skipped_status: dict[str, int] = {}
        skipped_stat: dict[str, int] = {}       # truly unrecognized (not in prizepicks_stat_map)
        explicitly_skipped: dict[str, int] = {} # mapped to None (intentionally unsupported)
        rank_type_vals: dict[str, int] = {}
        _sample_logged = 0   # log full attrs for first few projections to aid diagnostics

        for proj in all_projections:
            attrs = proj.get("attributes", {})

            # Log a sample of raw attributes so we can identify the real goblin/demon field.
            if _sample_logged < 5:
                logger.debug(
                    "PrizePicks %s projection attrs sample #%d: %s",
                    sport_name, _sample_logged + 1,
                    {k: v for k, v in attrs.items()
                     if k in ("rank_type", "pick_type", "odds_type", "line_type",
                               "projection_type", "is_promo", "payout_multiplier",
                               "flash_sale_line_score", "discount_percentage",
                               "stat_type", "line_score", "status")},
                )
                _sample_logged += 1

            # Only include lines not yet started
            status = attrs.get("status")
            if status not in ("pre_game", "pregame", None, ""):
                skipped_status[status] = skipped_status.get(status, 0) + 1
                continue

            # Only include props for games happening today (local date).
            # start_time is an ISO-8601 UTC string, e.g. "2025-06-01T17:00:00Z".
            # Props with no start_time are passed through (offline/manual props.json).
            raw_start = attrs.get("start_time") or attrs.get("start_time_utc") or ""
            if raw_start:
                try:
                    from datetime import datetime as _dt
                    st = _dt.fromisoformat(raw_start.replace("Z", "+00:00"))
                    # Convert to local date for comparison
                    game_date = st.astimezone().date()
                    if game_date != date.today():
                        logger.debug(
                            "Skipping %s prop — game date %s is not today (%s)",
                            attrs.get("stat_type", ""),
                            game_date.isoformat(),
                            date.today().isoformat(),
                        )
                        skipped_status[f"wrong_date:{game_date}"] = (
                            skipped_status.get(f"wrong_date:{game_date}", 0) + 1
                        )
                        continue
                except Exception:
                    pass  # unparseable start_time — let it through

            raw_stat = attrs.get("stat_type", "")
            # Globally-ignored stat types (e.g. MMA stats leaking from a
            # misassigned league_id) — silently discard, no WARNING.
            if raw_stat in _GLOBALLY_IGNORED_STAT_TYPES:
                explicitly_skipped[raw_stat] = explicitly_skipped.get(raw_stat, 0) + 1
                continue
            # Normalize PrizePicks naming to our internal stat names
            stat_type = pp_stat_map.get(raw_stat, raw_stat)
            # None value in map means explicitly unsupported — skip silently
            if stat_type is None:
                explicitly_skipped[raw_stat] = explicitly_skipped.get(raw_stat, 0) + 1
                continue
            # Internal stat name not found in prop_stat_map — truly unrecognized
            if stat_type not in prop_stat_map:
                skipped_stat[raw_stat] = skipped_stat.get(raw_stat, 0) + 1
                continue

            line = attrs.get("line_score")
            if line is None:
                continue

            # Exhaustively check all known PrizePicks fields that may encode
            # goblin/demon/standard. rank_type is null for ALL variants in current
            # API responses, so we cannot rely on any single field — instead we
            # check every candidate field and default to "unknown" (not "standard")
            # when none are set. Using "unknown" as the default is intentional:
            # it prevents false positives where null is misread as "standard".
            pick_type = "unknown"
            for field in ("rank_type", "pick_type", "odds_type", "line_type", "projection_type"):
                raw_val = attrs.get(field)
                if raw_val is not None and str(raw_val).strip():
                    pick_type = str(raw_val).lower().strip()
                    break
            # payout_multiplier heuristic: values < 1.0 → goblin (reduced payout),
            # values > 1.0 → demon (boosted payout), ~1.0 → standard.
            if pick_type == "unknown":
                multiplier = attrs.get("payout_multiplier")
                if multiplier is not None:
                    try:
                        m = float(multiplier)
                        if m < 0.97:
                            pick_type = "goblin"
                        elif m > 1.03:
                            pick_type = "demon"
                        else:
                            pick_type = "standard"
                    except (TypeError, ValueError):
                        pass
            # is_promo flag: goblin/demon lines are often promotions.
            if pick_type == "unknown" and attrs.get("is_promo"):
                pick_type = "promo"  # treat as non-standard

            player_id = (
                proj.get("relationships", {})
                    .get("new_player", {})
                    .get("data", {})
                    .get("id", "")
            )
            player = players.get(player_id, {})
            player_name = player.get("name", "")
            if not player_name:
                continue

            key = (player_name, stat_type)
            prop_dict = {
                "sport": sport_name,
                "projection_id": proj.get("id", str(len(seen))),
                "player_name": player_name,
                "team_abbr": player.get("team", ""),
                "event_home_abbr": "",
                "event_away_abbr": "",
                "position": player.get("position", ""),
                "stat_type": stat_type,
                "line": float(line),
                "start_time": attrs.get("start_time", ""),
                "pick_type": pick_type,
                "prop_source": "PrizePicks",
            }

            candidates.setdefault(key, []).append(prop_dict)
            rank_type_vals[pick_type] = rank_type_vals.get(pick_type, 0) + 1

        # Select the standard projection for each (player, stat).
        # PrizePicks returns up to three variants per prop (goblin, standard, demon).
        # Since rank_type is often null for all of them, we use line-value ordering.
        # n_variants records how many PP projections existed — this is the key signal:
        #   n_variants == 1: unknown type — could be goblin, standard, or demon
        #   n_variants == 2: unknown which two types — could be any combination
        #   n_variants >= 3: middle-sorted line IS the standard line (definitively)
        for key, projs in candidates.items():
            n = len(projs)
            if n == 1:
                chosen = projs[0]
            else:
                # Try explicit rank_type first — only trust it when at least one
                # projection is uniquely labeled "standard".
                standards = [p for p in projs if p["pick_type"] == "standard"]
                others    = [p for p in projs if p["pick_type"] != "standard"]
                if len(standards) == 1 and others:
                    chosen = standards[0]
                else:
                    # rank_type unreliable — use positional median by line value.
                    sorted_projs = sorted(projs, key=lambda p: p["line"])
                    # For 3+: middle index = standard (goblin < standard < demon).
                    # For 2: middle index = 0 (lower) — uncertain which type.
                    mid_idx = (len(sorted_projs) - 1) // 2
                    chosen = sorted_projs[mid_idx]
                    logger.debug(
                        "PrizePicks %s: %d lines for %s %s %s → selected %.1f",
                        sport_name, len(projs), key[0], key[1],
                        [p["line"] for p in sorted_projs], chosen["line"],
                    )
            chosen["from_multiple_lines"] = n > 1
            chosen["n_variants"] = n
            seen[key] = chosen

        logger.debug("PrizePicks %s: rank_type values seen: %s", sport_name, rank_type_vals)
        unknown_ranks = {k: v for k, v in rank_type_vals.items()
                         if k not in ("standard", "goblin", "demon", "unknown", "promo")}
        if unknown_ranks:
            logger.warning(
                "PrizePicks %s: unrecognized rank_type values: %s — "
                "goblin/demon detection may be unreliable. "
                "Check the API response and update pick_type handling if needed.",
                sport_name, unknown_ranks,
            )

        if skipped_status:
            logger.warning(
                "PrizePicks %s: %d projection(s) skipped — unexpected status values: %s. "
                "These props exist but were filtered out because their game has already started "
                "or PrizePicks is using a new status label.",
                sport_name,
                sum(skipped_status.values()),
                dict(skipped_status),
            )
        if explicitly_skipped:
            logger.debug(
                "PrizePicks %s: %d projection(s) silently skipped "
                "(globally ignored or mapped to None in prizepicks_stat_map): %s",
                sport_name,
                sum(explicitly_skipped.values()),
                dict(explicitly_skipped),
            )
        if skipped_stat:
            logger.warning(
                "PrizePicks %s: %d projection(s) skipped — unrecognized stat types: %s. "
                "Add these to prizepicks_stat_map or prop_stat_map in config.py to include them.",
                sport_name,
                sum(skipped_stat.values()),
                dict(skipped_stat),
            )

        return list(seen.values())

PROPS_FILE_INSTRUCTIONS = """
=======================================================
  ACTION REQUIRED: No prop lines available today
=======================================================
All live prop sources (PrizePicks, DraftKings, Underdog,
FanDuel) returned no data or are temporarily unavailable.
A props.json file has been created for you to fill in.

Fill props.json manually with today's lines from any
sportsbook, then re-run: python3 main.py

props.json format (remove these examples first):
  [
    {"sport": "NBA", "player_name": "Player Name",
     "team_abbr": "BOS", "stat_type": "Points", "line": 27.5},
    {"sport": "NHL", "player_name": "Player Name",
     "team_abbr": "EDM", "stat_type": "Goals", "line": 0.5},
    {"sport": "MLB", "player_name": "Player Name",
     "team_abbr": "LAD", "stat_type": "Hits", "line": 1.5}
  ]

Optional flags (add to any entry):
  "goblin": true  — PrizePicks goblin line (artificially low); UNDER suppressed
  "demon":  true  — PrizePicks demon line  (artificially high); OVER  suppressed

NBA stat types:  Points, Rebounds, Assists, 3-PT Made, Steals,
                 Blocks, Turnovers, Pts+Reb+Ast, Pts+Ast, Pts+Reb
NHL stat types:  Points, Goals, Assists, Shots on Goal, Power Play Points
MLB stat types:  Hits, Home Runs, RBIs, Total Bases, Runs Scored,
                 Stolen Bases, Strikeouts
NFL stat types:  Passing Yards, Rushing Yards, Receiving Yards,
                 Receptions, Passing TDs
=======================================================
"""

PROPS_TEMPLATE = [
    {"sport": "NBA", "player_name": "Jayson Tatum",  "team_abbr": "BOS", "stat_type": "Points",   "line": 27.5},
    {"sport": "NHL", "player_name": "Connor McDavid", "team_abbr": "EDM", "stat_type": "Points",   "line": 0.5},
    {"sport": "MLB", "player_name": "Shohei Ohtani",  "team_abbr": "LAD", "stat_type": "Hits",     "line": 1.5},
]


class PropLineClient:
    """Fetches prop lines exclusively from PrizePicks, with props.json as manual fallback."""

    # Short in-process retries for a transient blip (a source briefly down, a
    # request timing out). This is intentionally SHORT — the external
    # scheduler (run_daily.sh) already re-invokes the whole process every 30
    # minutes and does not mark a props-empty day as "done" (see main.py's
    # props_failed_sports branch), so the real "wait for lines to post"
    # retry cadence lives at that outer layer, not inside one invocation.
    _RETRY_ATTEMPTS = 2
    _RETRY_DELAY_SECS = 3 * 60  # 3 minutes between in-process retries

    def fetch_props(self, sport_config: dict) -> list[dict]:
        sport_name = sport_config["name"]

        props, reason = self._fetch_merged(sport_config)
        if props:
            return props

        # Empty on the first pass — could be a transient blip (a source
        # timing out) rather than "no lines exist yet." A short retry here
        # smooths that over without blocking the process for long; if it's
        # genuinely too early for lines to be posted, the outer 30-minute
        # scheduler cadence is what actually waits it out across the day.
        #
        # IMPORTANT: this retries the FULL multi-source fetch, not just
        # PrizePicks — PrizePicks is chronically bot-blocked now, so gating
        # retries on ITS specific reason code made this dead code in
        # practice once every other source also came up empty.
        for attempt in range(1, self._RETRY_ATTEMPTS + 1):
            logger.info(
                "All sources returned 0 %s props on attempt %d — waiting %d min "
                "before a quick retry (%d/%d). If this keeps happening across "
                "scheduled runs, lines likely aren't posted yet for today's "
                "games; the scheduler will keep trying every 30 min.",
                sport_name, attempt, self._RETRY_DELAY_SECS // 60,
                attempt, self._RETRY_ATTEMPTS,
            )
            time.sleep(self._RETRY_DELAY_SECS)
            props, reason = self._fetch_merged(sport_config)
            if props:
                return props

        logger.warning(
            "%s: 0 props from every source (PrizePicks, Bovada, DraftKings, "
            "Underdog, FanDuel) after %d attempt(s). Check logs/props_%s.log "
            "for per-source HTTP status/error detail (DEBUG level) to tell "
            "apart 'blocked/network error' from 'lines not posted yet'.",
            sport_name, self._RETRY_ATTEMPTS + 1, date.today().isoformat(),
        )
        return self._load_from_file(sport_config, reason)

    def _fetch_merged(self, sport_config: dict) -> tuple[list[dict], str]:
        """Try PrizePicks, then merge every other reachable book by
        (player, stat) — coverage is the union, not first-source-wins.

        Returns (props, reason). reason is only meaningful when props is
        empty; it reflects PrizePicks's own failure mode for the manual
        props.json fallback message, since PrizePicks was tried first.
        """
        sport_name = sport_config["name"]
        live = PrizePicksLiveClient()

        props, reason = live.fetch_props(sport_config)
        if props:
            return props, PrizePicksLiveClient._REASON_OK

        from data.draftkings_client import DraftKingsPropsClient
        from data.external_props_client import (
            BovadaPropsClient,
            UnderdogPropsClient,
            FanDuelPropsClient,
        )

        merged: dict[tuple, dict] = {}
        per_source_counts: dict[str, int] = {}
        for SourceClass, source_name in [
            (BovadaPropsClient, "Bovada"),       # most reliable free feed first
            (DraftKingsPropsClient, "DraftKings"),
            (UnderdogPropsClient, "Underdog"),
            (FanDuelPropsClient, "FanDuel"),
        ]:
            try:
                ext_props = SourceClass().fetch_props(sport_config)
            except Exception as exc:
                logger.warning("%s props fetch failed: %s", source_name, exc)
                continue
            added = 0
            for p in ext_props or []:
                key = (_normalize_player_name(p.get("player_name", "")), p.get("stat_type", ""))
                if key not in merged:
                    merged[key] = p
                    added += 1
            if added:
                per_source_counts[source_name] = added
            logger.debug(
                "%s %s: %d prop(s) returned this attempt", source_name, sport_name,
                len(ext_props or []),
            )

        if merged:
            logger.info(
                "Loaded %d %s props from %d source(s): %s",
                len(merged), sport_name, len(per_source_counts),
                ", ".join(f"{s}={n}" for s, n in per_source_counts.items()),
            )
            return list(merged.values()), PrizePicksLiveClient._REASON_OK

        return [], reason

    # ------------------------------------------------------------------
    # Manual fallback — read from props.json
    # ------------------------------------------------------------------

    def _load_from_file(self, sport_config: dict, reason: str = "") -> list[dict]:
        sport_name = sport_config["name"]
        prop_stat_map = sport_config["prop_stat_map"]

        if not PROPS_FILE.exists():
            self._write_template()
            if reason == PrizePicksLiveClient._REASON_BLOCKED:
                print(
                    "\n[PROP SOURCES BLOCKED] All sportsbook APIs are blocking requests from "
                    "this machine.\n"
                    "This is usually temporary. Options:\n"
                    "  1. Wait 1–2 hours and re-run\n"
                    "  2. Install curl-cffi for better bypass:  pip3 install curl-cffi\n"
                    "  3. Fill props.json manually (see instructions below)\n"
                )
            elif reason == PrizePicksLiveClient._REASON_NOT_POSTED:
                print(
                    "\n[LINES NOT POSTED] Prop lines haven't been posted yet today.\n"
                    "Lines usually appear after 11 AM–1 PM ET.\n"
                    "  1. Re-run after noon:  python3 main.py\n"
                    "  2. Or fill props.json manually (see instructions below)\n"
                )
            print(PROPS_FILE_INSTRUCTIONS)
            return []

        try:
            raw = json.loads(PROPS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("props.json is not valid JSON: %s", exc)
            return []

        if not isinstance(raw, list) or not raw:
            logger.error("props.json must be a non-empty JSON array.")
            return []

        if raw == PROPS_TEMPLATE:
            print(PROPS_FILE_INSTRUCTIONS.replace(
                "props.json still contains example data — replace with today's lines.",
                "props.json still contains example data — replace with today's lines.",
            ))
            return []

        props = []
        for i, entry in enumerate(raw):
            entry_sport = str(entry.get("sport", "NBA")).upper()
            if entry_sport != sport_name:
                continue
            parsed = self._parse_file_entry(entry, i, prop_stat_map, sport_name)
            if parsed:
                props.append(parsed)

        logger.warning(
            "All live prop sources unavailable — loaded %d %s props from props.json. "
            "Lines in this file may be outdated. "
            "Update props.json with today's current lines before using these picks.",
            len(props), sport_name,
        )
        return props

    def _parse_file_entry(
        self,
        entry: dict,
        idx: int,
        prop_stat_map: dict,
        sport_name: str,
    ) -> dict | None:
        for field in ("player_name", "stat_type", "line"):
            if field not in entry:
                logger.warning("props.json entry %d missing '%s' — skipping", idx, field)
                return None

        stat_type = entry["stat_type"]
        if stat_type not in prop_stat_map:
            logger.warning(
                "props.json entry %d: unknown stat_type '%s' for %s. Valid: %s",
                idx, stat_type, sport_name, ", ".join(prop_stat_map.keys()),
            )
            return None

        try:
            line = float(entry["line"])
        except (TypeError, ValueError):
            logger.warning("props.json entry %d: invalid line value — skipping", idx)
            return None

        if entry.get("goblin"):
            pick_type = "goblin"
        elif entry.get("demon"):
            pick_type = "demon"
        else:
            pick_type = "standard"

        # Manual entries are explicitly typed via goblin/demon flags.
        # Treat explicit "standard" (no flags) as n_variants=3 so the analyzer
        # permits UNDER — the user is intentionally marking it as standard.
        n_variants = 3 if pick_type == "standard" else 1
        return {
            "sport": sport_name,
            "projection_id": str(idx),
            "player_name": str(entry["player_name"]).strip(),
            "team_abbr": str(entry.get("team_abbr", "")).upper().strip(),
            "event_home_abbr": "",
            "event_away_abbr": "",
            "position": str(entry.get("position", "")).strip(),
            "stat_type": stat_type,
            "line": line,
            "start_time": "",
            "pick_type": pick_type,
            "from_multiple_lines": pick_type == "standard",
            "n_variants": n_variants,
        }

    @staticmethod
    def _write_template() -> None:
        PROPS_FILE.write_text(json.dumps(PROPS_TEMPLATE, indent=2), encoding="utf-8")


# Backward-compatible aliases
OddsAPIClient = PropLineClient

class PrizePicksClient(PropLineClient):
    def fetch_nba_props(self) -> list[dict]:
        return self.fetch_props(SPORT_CONFIG["NBA"])
