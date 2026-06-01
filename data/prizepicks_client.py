from __future__ import annotations

import json
import logging
from datetime import date, timezone
from pathlib import Path

import requests

from config import SPORT_CONFIG, PRIZEPICKS_URL

logger = logging.getLogger(__name__)

PROPS_FILE = Path("props.json")


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


_PP_HEADER_VARIANTS = [
    # Variant 1: Desktop Chrome (original)
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://app.prizepicks.com/",
        "Origin": "https://app.prizepicks.com",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    },
    # Variant 2: Safari macOS
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4.1 Safari/605.1.15"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
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
        "Referer": "https://app.prizepicks.com/",
        "Origin": "https://app.prizepicks.com",
        "X-Device-Id": "prizepicks-client",
    },
]


class PrizePicksLiveClient:
    """
    Fetches live player prop lines directly from the PrizePicks projections API.
    No API key required — used as a free fallback when The Odds API is exhausted.
    Tries multiple header strategies in case Cloudflare bot detection blocks one.
    """

    def fetch_props(self, sport_config: dict) -> list[dict]:
        sport_name = sport_config["name"]

        # Try the configured league_id first; if it returns 0 props (likely wrong
        # ID), look up the correct ID via the leagues endpoint and retry once.
        configured_id = sport_config.get("prizepicks_league_id")
        if not configured_id:
            return []

        props = self._fetch_with_league_id(configured_id, sport_config)
        if props:
            return props

        # Configured ID returned nothing useful — try dynamic lookup.
        discovered_id = self._lookup_league_id(sport_name)
        if discovered_id and discovered_id != configured_id:
            logger.warning(
                "PrizePicks %s: configured league_id=%s returned no usable props "
                "(it may have been reassigned to a different sport — e.g. UFC/MMA). "
                "Retrying with dynamically discovered league_id=%s. "
                "Update 'prizepicks_league_id' in config.py to %s to make this permanent.",
                sport_name, configured_id, discovered_id, discovered_id,
            )
            props = self._fetch_with_league_id(discovered_id, sport_config)
            if props:
                return props

        return []

    def _fetch_with_league_id(self, league_id: int, sport_config: dict) -> list[dict]:
        sport_name = sport_config["name"]
        params = {"league_id": league_id, "per_page": 250, "single_stat": "true"}

        for i, headers in enumerate(_PP_HEADER_VARIANTS):
            try:
                resp = requests.get(
                    PRIZEPICKS_URL,
                    params=params,
                    headers=headers,
                    timeout=20,
                )
                if resp.status_code == 403:
                    logger.debug(
                        "PrizePicks API 403 with header variant %d for %s — trying next",
                        i + 1, sport_name,
                    )
                    continue
                resp.raise_for_status()
                data = resp.json()
                props = self._parse(data, sport_config)
                logger.info(
                    "PrizePicks API: %d %s props fetched (league_id=%s, variant %d)",
                    len(props), sport_name, league_id, i + 1,
                )
                return props
            except requests.exceptions.HTTPError:
                continue
            except Exception as exc:
                logger.warning("PrizePicks API error for %s (variant %d): %s", sport_name, i + 1, exc)
                break

        logger.warning(
            "PrizePicks API unavailable for %s (league_id=%s) — all %d header variants returned 403 or error.",
            sport_name, league_id, len(_PP_HEADER_VARIANTS),
        )
        return []

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
        for headers in _PP_HEADER_VARIANTS:
            try:
                resp = requests.get(
                    "https://api.prizepicks.com/leagues",
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code == 403:
                    continue
                resp.raise_for_status()
                data = resp.json()
                mapping: dict[str, int] = {}
                for obj in data.get("data", []):
                    name = obj.get("attributes", {}).get("name", "")
                    try:
                        lid = int(obj.get("id"))
                    except (TypeError, ValueError):
                        continue
                    if name:
                        mapping[name] = lid
                logger.info("PrizePicks /leagues: discovered %d leagues", len(mapping))
                return mapping
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
PrizePicks API did not return props (may be temporarily
unavailable or blocking automated requests).
A props.json file has been created for you to fill in.

Fill props.json manually with today's PrizePicks lines,
then re-run: python3 main.py

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

    def fetch_props(self, sport_config: dict) -> list[dict]:
        props = PrizePicksLiveClient().fetch_props(sport_config)
        if props:
            return props

        return self._load_from_file(sport_config)

    # ------------------------------------------------------------------
    # Manual fallback — read from props.json
    # ------------------------------------------------------------------

    def _load_from_file(self, sport_config: dict) -> list[dict]:
        sport_name = sport_config["name"]
        prop_stat_map = sport_config["prop_stat_map"]

        if not PROPS_FILE.exists():
            self._write_template()
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
            "PrizePicks API unavailable — loaded %d %s props from props.json. "
            "Lines in this file may not match current PrizePicks lines. "
            "Update props.json with today's PrizePicks lines before using these picks.",
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
