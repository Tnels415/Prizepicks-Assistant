from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from data.base_stats_client import BaseStatsClient
from data.schedule_client import ScheduleClient
import analysis.distribution_model as _dist_model
from data.news_client import get_player_news_sentiment
from data.injury_client import get_player_injury_status, classify_injury_severity
from analysis.historical_stats import HistoricalStatsCalculator
from analysis.factor_scorer import FactorScorer
from analysis.watchability import watchable_label
from analysis import market as _market
from data.draftkings_client import _normalize_name as _market_norm
from config import (
    SPORT_CONFIG, GOBLIN_STD_THRESHOLD,
    MARKET_BLEND_WEIGHT, INJURY_QUESTIONABLE_SHRINK, INJURY_PROBABLE_SHRINK,
)

logger = logging.getLogger(__name__)


@dataclass
class PropResult:
    player_name: str
    team_abbr: str
    position: str
    stat_type: str
    line: float
    direction: str                  # "OVER" or "UNDER"
    hit_probability: float          # composite 0–100 for this direction
    over_probability: float
    under_probability: float
    predicted_value: float
    season_avg: float
    last_5_avg: float
    last_10_avg: float
    hit_rate_20: float              # raw 0.0–1.0 over last 20 games
    games_analyzed: int
    opponent_team_abbr: str
    opponent_def_rank: int | None
    location: str
    rest_days: int
    h2h_hit_rate: float
    h2h_avg: float
    h2h_sample_size: int
    sport: str = "NBA"
    key_factors: list[str] = field(default_factory=list)
    data_quality: str = "full"      # "full", "partial", "minimal"
    raw_adjustments: dict = field(default_factory=dict)
    rank: int = 0
    # Per-player historical signal fields
    std_dev: float = 0.0
    hit_rate_5: float = 0.5
    hit_rate_10: float = 0.5
    # TV broadcast info for the game this prop belongs to
    broadcasts: list = field(default_factory=list)  # [{"network", "market"}]
    broadcast_label: str = ""                        # display string, e.g. "ABC"
    # Edge + tier (Phase 3)
    edge: float = 0.0          # hit_probability/100 − PRIZEPICKS_BREAKEVEN
    tier: str = "speculative"  # "A" (high confidence) or "speculative"


class PropAnalyzer:

    def __init__(
        self,
        stats_client: BaseStatsClient,
        schedule_client: ScheduleClient,
        sport_config: dict | None = None,
        corrections=None,  # learning.calibrator.Corrections | None
    ) -> None:
        self._stats = stats_client
        self._schedule = schedule_client
        self._sport = sport_config or SPORT_CONFIG["NBA"]
        self._corrections = corrections
        self._calc = HistoricalStatsCalculator(
            prop_stat_map=self._sport["prop_stat_map"],
            combo_stat_map=self._sport["combo_stat_map"],
        )
        self._scorer = FactorScorer(sport_config=self._sport)
        self._team_stats: pd.DataFrame | None = None
        self._team_abbr_to_id: dict[str, int] = {}
        self._team_id_to_abbr: dict[int, str] = {}
        self._todays_games: list[dict] = []
        self._market_odds: dict[tuple[str, str], dict] = {}

    def analyze_all_props(self, props: list[dict]) -> list[PropResult]:
        sport_key = self._sport["odds_sport_key"]
        logger.info("Pre-loading %s team stats…", self._sport["name"])
        self._team_stats = self._stats.get_team_advanced_stats()
        self._stats.get_player_usage()
        self._team_abbr_to_id = self._schedule.get_team_abbr_to_id()
        self._team_id_to_abbr = self._schedule.get_team_id_to_abbr()
        self._todays_games = self._schedule.get_todays_games(sport_key=sport_key)

        # Market-odds anchor (Phase 2): pre-fetch DraftKings Over/Under odds so
        # each prop's model probability can be blended toward the devigged sharp
        # number.  Fully graceful — an empty map means the model runs unanchored.
        if MARKET_BLEND_WEIGHT > 0:
            try:
                from data.draftkings_client import DraftKingsPropsClient
                self._market_odds = DraftKingsPropsClient().fetch_market_odds(self._sport)
            except Exception as exc:
                logger.warning("Market-odds fetch failed (%s) — running unanchored", exc)
                self._market_odds = {}

        results: list[PropResult] = []
        seen_players: dict[str, pd.DataFrame] = {}
        n_skipped = 0

        for prop in props:
            try:
                prop_results = self._analyze_single_prop(prop, seen_players)
                if prop_results:
                    results.extend(prop_results)
                else:
                    n_skipped += 1
            except Exception as exc:
                logger.error(
                    "Unexpected error analyzing %s %s: %s",
                    prop.get("player_name"), prop.get("stat_type"), exc,
                    exc_info=True,
                )
                n_skipped += 1

        results.sort(key=lambda r: r.hit_probability, reverse=True)
        for i, r in enumerate(results, 1):
            r.rank = i

        # Compute edge + tier for every result (Phase 3)
        _assign_edge_and_tier(results)

        unique_players = len(results) // 2 if results else 0
        logger.info(
            "%s analysis complete: %d players ranked (%d prop directions)  |  %d props skipped",
            self._sport["name"], unique_players, len(results), n_skipped,
        )
        return results

    def _analyze_single_prop(
        self,
        prop: dict,
        player_cache: dict,
    ) -> list[PropResult] | None:
        player_name = prop["player_name"]
        stat_type = prop["stat_type"]
        line = prop["line"]
        team_abbr = prop.get("team_abbr", "")
        position = prop.get("position", "")
        event_home = prop.get("event_home_abbr", "")
        event_away = prop.get("event_away_abbr", "")

        player_id = self._stats.find_player_id(player_name)
        if player_id is None:
            logger.warning("Skipping %s — player ID not found", player_name)
            return None

        # Tiered injury handling (checked before the game-log fetch to avoid
        # wasted API calls).  Out/Doubtful → suppress entirely.  Questionable /
        # Probable → keep the prop but shrink confidence toward 50% later, so we
        # don't blanket-discard players who are likely to play their usual role.
        injury_status = get_player_injury_status(player_name, self._sport["name"])
        injury_severity = classify_injury_severity(injury_status)
        if injury_severity in ("out", "doubtful"):
            logger.info(
                "Suppressing %s %s — injury designation: %s (%s)",
                player_name, stat_type, injury_status, injury_severity,
            )
            return []
        if injury_severity in ("questionable", "probable"):
            logger.info(
                "%s %s — playing through %s (%s): confidence shrunk",
                player_name, stat_type, injury_status, injury_severity,
            )

        if player_name not in player_cache:
            player_cache[player_name] = self._stats.get_player_game_log(player_id)
        game_log = player_cache[player_name]

        # Derive team_abbr from game log MATCHUP when Odds API didn't supply it
        if not team_abbr and not game_log.empty and "MATCHUP" in game_log.columns:
            matchup = str(game_log["MATCHUP"].iloc[0])
            team_abbr = matchup.split()[0].upper()
            # If the event is known, verify the derived team is actually in it.
            # A mid-season trade means the game log's most recent entry may name
            # an old team (e.g. "DAL" for a player now on "DET").  Resetting here
            # lets the event-team lookup below find the correct current team.
            if event_home or event_away:
                event_teams = {t.upper() for t in (event_home, event_away) if t}
                if team_abbr not in event_teams:
                    logger.debug(
                        "%s: game-log team '%s' not in today's event (%s @ %s)"
                        " — player may have been traded; trying event teams",
                        player_name, team_abbr, event_away, event_home,
                    )
                    team_abbr = ""

        if not team_abbr and (event_home or event_away):
            for candidate in (event_home, event_away):
                if candidate and self._determine_game_context(candidate):
                    team_abbr = candidate
                    break

        context = self._determine_game_context(team_abbr)
        if context is None and (event_home or event_away):
            # Schedule lookup failed (API down or abbr mismatch) — derive context
            # directly from the Odds API event data we already have.
            context = self._context_from_event_data(team_abbr, event_home, event_away)
        if context is None:
            logger.info(
                "Skipping %s — no game today for team '%s' (event: %s @ %s)",
                player_name, team_abbr, event_away, event_home,
            )
            return None

        opponent_abbr = context["opponent_abbr"]
        opponent_id = context["opponent_id"]
        location = context["location"]

        if game_log.empty:
            logger.warning("Skipping %s — no game log data", player_name)
            return None

        rest_days = self._calc.infer_rest_days(game_log)
        hit_rate_20, games_analyzed = self._calc.calculate_hit_rate(game_log, stat_type, line)
        avgs = self._calc.calculate_averages(game_log, stat_type)
        h2h = self._calc.calculate_h2h_stats(game_log, stat_type, line, opponent_abbr)
        location_avg = self._calc.calculate_location_avg(game_log, stat_type, location)

        data_quality = self._assess_quality(games_analyzed)

        if games_analyzed < 3:
            logger.warning(
                "Skipping %s %s — only %d games in log", player_name, stat_type, games_analyzed
            )
            return None

        delta_season = self._scorer.score_season_avg_vs_line(avgs["season_avg"], line)
        delta_form = self._scorer.score_recent_form(avgs["last_5_avg"], avgs["last_10_avg"], line)
        delta_h2h = self._scorer.score_h2h_this_season(
            h2h["hit_rate"], h2h["sample_size"], h2h["reliable"]
        )
        delta_opp, opp_rank = self._scorer.score_opponent_defense(
            self._team_stats, stat_type, opponent_id
        )
        delta_loc = self._scorer.score_home_away(location, location_avg, line)
        delta_rest = self._scorer.score_rest_days(rest_days)
        delta_pace = self._scorer.score_pace(
            self._team_stats,
            self._team_abbr_to_id.get(team_abbr, 0),
            opponent_id,
            stat_type,
        )

        # Per-player historical signal factors
        consistency = self._calc.calculate_consistency(game_log, stat_type, line)
        hit_rates   = self._calc.calculate_hit_rates_multi_window(game_log, stat_type, line)

        # Analyst sentiment from ESPN news headlines (graceful degradation to 0)
        try:
            news_sentiment = get_player_news_sentiment(player_name, self._sport["name"])
        except Exception:
            news_sentiment = 0.0

        # Goblin/demon detection — API pick_type takes precedence; std-dev heuristic is fallback.
        # IMPORTANT: pick_type defaults to "unknown" (not "standard") when the API returns null,
        # so pick_type == "unknown" means we genuinely don't know the line type.
        pick_type = prop.get("pick_type", "unknown")
        n_variants = prop.get("n_variants", 1)
        is_goblin = pick_type == "goblin"
        is_demon  = pick_type == "demon"

        _std = consistency["std_dev"]
        _avg = avgs["season_avg"]

        if not is_goblin and not is_demon:
            if _std > 0 and _avg > 0:
                # Primary heuristic: line is more than N std-devs from the mean.
                if line < _avg - GOBLIN_STD_THRESHOLD * _std:
                    is_goblin = True
                    logger.debug(
                        "Goblin heuristic: %s %s line=%.1f avg=%.1f std=%.1f",
                        player_name, stat_type, line, _avg, _std,
                    )
                elif line > _avg + GOBLIN_STD_THRESHOLD * _std:
                    is_demon = True
                    logger.debug(
                        "Demon heuristic: %s %s line=%.1f avg=%.1f std=%.1f",
                        player_name, stat_type, line, _avg, _std,
                    )
            elif _avg > 0:
                # Fallback when std_dev is 0 (fewer than 5 games logged).
                # Use ratio comparison: line > 2x avg → demon; line < 0.4x avg → goblin.
                if line > _avg * 2.0:
                    is_demon = True
                    logger.debug(
                        "Demon ratio fallback: %s %s line=%.1f avg=%.1f",
                        player_name, stat_type, line, _avg,
                    )
                elif line < _avg * 0.4:
                    is_goblin = True
                    logger.debug(
                        "Goblin ratio fallback: %s %s line=%.1f avg=%.1f",
                        player_name, stat_type, line, _avg,
                    )

        # UNDER availability policy.
        #
        # PrizePicks only allows UNDER on standard lines, never on goblin or demon lines.
        # The PrizePicks API returns rank_type=null for ALL projection types, so we cannot
        # reliably determine line type from any single API field. The only situation where
        # we can be confident a line is standard is when PrizePicks returned 3 variants
        # for the same (player, stat): sorted as [goblin, standard, demon], the middle
        # value is definitively the standard line.
        #
        # n_variants tracks how many PP projections existed:
        #   1 → unknown type (could be standalone goblin, demon, or standard)
        #   2 → unknown which two types were present
        #   3 → middle = standard (UNDER safe to play)
        #
        # For manual props.json entries with no goblin/demon flag, n_variants is set to 3
        # to indicate the user explicitly marked it as a standard line.
        under_ok = (not is_goblin and not is_demon) and n_variants >= 3

        delta_consistency = self._scorer.score_consistency(
            consistency["std_dev"], avgs["season_avg"], line
        )
        delta_hit_trend = self._scorer.score_hit_rate_trend(
            hit_rates["hr_5"], hit_rates["hr_10"], hit_rates["hr_20"]
        )
        delta_trend_dir = self._scorer.score_trend_direction(
            avgs["last_5_avg"], avgs["last_10_avg"], avgs["season_avg"], line
        )
        delta_sentiment = self._scorer.score_analyst_sentiment(news_sentiment)

        # Compute predicted value early — needed as the projected mean for the
        # distribution model and for PropResult construction below.
        predicted = self._compute_predicted_value(
            avgs["last_5_avg"], avgs["last_10_avg"], avgs["season_avg"]
        )

        # --- Distribution-based probability base (Phase 1) -------------------
        # Replace the raw hit_rate_20 * 100 base with P(over) from a fitted
        # Negative Binomial (small-count stats) or Gaussian (continuous/large).
        # When the distribution fit fails (too few games, zero variance, etc.)
        # the call returns None and we fall back to the legacy empirical base.
        stat_series = self._calc.get_stat_series(game_log, stat_type)
        dist_result = _dist_model.prob_over(stat_type, line, predicted, stat_series)
        dist_base: float | None = dist_result[0] if dist_result is not None else None

        # Factors that are directly derived from the same game-log series are
        # downweighted when the distribution already captures that information.
        # Independent contextual factors (h2h, opponent, location, rest, pace,
        # sentiment) are left at full weight.
        _DIST_SCALE = 0.5   # scale for series-redundant factors when dist is used
        if dist_base is not None:
            adj_season    = delta_season    * _DIST_SCALE
            adj_form      = delta_form      * _DIST_SCALE
            adj_h2h       = delta_h2h                       # independent
            adj_opp       = delta_opp                       # independent
            adj_loc       = delta_loc                       # independent
            adj_rest      = delta_rest                      # independent
            adj_pace      = delta_pace                      # independent
            adj_consist   = delta_consistency * _DIST_SCALE
            adj_hit_trend = delta_hit_trend   * _DIST_SCALE
            adj_trend_dir = delta_trend_dir   * _DIST_SCALE
            adj_sentiment = delta_sentiment                 # independent
            logger.debug(
                "%s %s: dist base=%.1f%% (was hit_rate=%.1f%%)",
                player_name, stat_type, dist_base, hit_rate_20 * 100,
            )
        else:
            adj_season = delta_season; adj_form = delta_form
            adj_h2h = delta_h2h; adj_opp = delta_opp; adj_loc = delta_loc
            adj_rest = delta_rest; adj_pace = delta_pace
            adj_consist = delta_consistency; adj_hit_trend = delta_hit_trend
            adj_trend_dir = delta_trend_dir; adj_sentiment = delta_sentiment

        adjustments = [adj_season, adj_form, adj_h2h, adj_opp,
                       adj_loc, adj_rest, adj_pace,
                       adj_consist, adj_hit_trend, adj_trend_dir,
                       adj_sentiment]

        learned_weights = (
            self._corrections.factor_weights
            if self._corrections and self._corrections.has_sufficient_data
            else None
        )
        over_prob = self._scorer.compute_composite_probability(
            hit_rate_20, adjustments,
            learned_weights=learned_weights,
            base_prob=dist_base,
        )

        if self._corrections and self._corrections.has_sufficient_data:
            bucket_mid = int(over_prob // 10) * 10 + 5
            cal_delta = self._corrections.calibration_map.get(bucket_mid, 0.0)
            bias_delta = self._corrections.stat_type_bias.get(stat_type, 0.0)
            over_prob = max(5.0, min(95.0, over_prob + cal_delta + bias_delta))

        # Player-level correction — applied independently of the 7-day gate so
        # individual accuracy data helps immediately once ≥10 samples exist.
        if self._corrections:
            player_delta = self._corrections.player_bias.get((player_name, stat_type), 0.0)
            if player_delta:
                over_prob = max(5.0, min(95.0, over_prob + player_delta))
                logger.debug(
                    "%s %s: player-level bias %.1fpp applied", player_name, stat_type, player_delta
                )

        # --- Injury confidence shrink (questionable/probable) ----------------
        # Pull the probability toward 50% so an uncertain availability reduces
        # edge without discarding the pick.  Reduces both OVER and UNDER edge.
        if injury_severity == "questionable":
            over_prob = 50.0 + (over_prob - 50.0) * (1.0 - INJURY_QUESTIONABLE_SHRINK)
        elif injury_severity == "probable":
            over_prob = 50.0 + (over_prob - 50.0) * (1.0 - INJURY_PROBABLE_SHRINK)

        # --- Market-odds blend (Phase 2) -------------------------------------
        # Anchor the model toward the devigged sharp Over/Under probability.
        # Only blend when DraftKings offers the SAME line as PrizePicks — a
        # devigged probability is only valid for the line it was priced at.
        market_over: float | None = None
        mkt = self._market_odds.get((_market_norm(player_name), stat_type))
        if mkt is not None and abs(float(mkt["line"]) - float(line)) < 0.01:
            devig = _market.devig_two_way(mkt["over_odds"], mkt["under_odds"])
            if devig is not None:
                market_over = devig[0]
                over_prob = _market.blend(over_prob, market_over, MARKET_BLEND_WEIGHT)
                logger.debug(
                    "%s %s: blended model→market %.1f%% (w=%.2f, mkt=%.1f%%)",
                    player_name, stat_type, over_prob, MARKET_BLEND_WEIGHT, market_over,
                )

        under_prob = 100.0 - over_prob

        raw_adj = {
            "season_avg_vs_line": delta_season,
            "recent_form": delta_form,
            "h2h_this_season": delta_h2h,
            "opponent_defense": delta_opp,
            "home_away": delta_loc,
            "rest_days": delta_rest,
            "pace": delta_pace,
            "consistency": delta_consistency,
            "hit_rate_trend": delta_hit_trend,
            "trend_direction": delta_trend_dir,
            "analyst_sentiment": delta_sentiment,
            # Distribution model diagnostic — stored but excluded from FACTOR_NAMES
            # so the calibrator's factor-weight loop ignores it.
            "_dist_over": round(dist_base, 2) if dist_base is not None else None,
            "_market_over": round(market_over, 2) if market_over is not None else None,
        }

        key_factors = self._build_key_factors(raw_adj, avgs, h2h, line, opponent_abbr, opp_rank)

        base_kwargs = dict(
            player_name=player_name,
            team_abbr=team_abbr,
            position=position,
            stat_type=stat_type,
            line=line,
            over_probability=over_prob,
            under_probability=under_prob,
            predicted_value=predicted,
            season_avg=avgs["season_avg"],
            last_5_avg=avgs["last_5_avg"],
            last_10_avg=avgs["last_10_avg"],
            hit_rate_20=hit_rate_20,
            games_analyzed=games_analyzed,
            opponent_team_abbr=opponent_abbr,
            opponent_def_rank=opp_rank,
            location=location,
            rest_days=rest_days,
            h2h_hit_rate=h2h["hit_rate"],
            h2h_avg=h2h["avg"],
            h2h_sample_size=h2h["sample_size"],
            sport=self._sport["name"],
            key_factors=key_factors,
            data_quality=data_quality,
            raw_adjustments=raw_adj,
            std_dev=consistency["std_dev"],
            hit_rate_5=hit_rates["hr_5"],
            hit_rate_10=hit_rates["hr_10"],
            broadcasts=context.get("broadcasts", []),
            broadcast_label=watchable_label(context.get("broadcasts", [])),
        )

        results = []
        # PrizePicks only offers OVER on goblin and demon lines — UNDER is never
        # available for either. Goblin = artificially low line (easy OVER, reduced payout).
        # Demon = artificially high line (hard OVER, boosted payout).
        # UNDER is only safe when we know the line is standard (n_variants >= 3).
        results.append(PropResult(direction="OVER", hit_probability=over_prob, **base_kwargs))
        if under_ok:
            results.append(PropResult(direction="UNDER", hit_probability=under_prob, **base_kwargs))
        if is_goblin:
            logger.info(
                "Goblin — UNDER suppressed: %s %s (line=%.1f, avg=%.1f)",
                player_name, stat_type, line, avgs["season_avg"],
            )
        elif is_demon:
            logger.info(
                "Demon  — UNDER suppressed: %s %s (line=%.1f, avg=%.1f)",
                player_name, stat_type, line, avgs["season_avg"],
            )
        elif not under_ok:
            logger.info(
                "UNDER suppressed (ambiguous line type): %s %s (line=%.1f, avg=%.1f) — "
                "%d PP variant(s); need 3 to confirm standard. Only OVER is safe.",
                player_name, stat_type, line, avgs["season_avg"], n_variants,
            )
        return results

    def _determine_game_context(self, team_abbr: str) -> dict | None:
        team_abbr_upper = team_abbr.upper()
        for game in self._todays_games:
            if game["home_team_abbr"].upper() == team_abbr_upper:
                return {
                    "opponent_abbr": game["away_team_abbr"],
                    "opponent_id": game["away_team_id"],
                    "location": "Home",
                    "broadcasts": game.get("broadcasts", []),
                }
            if game["away_team_abbr"].upper() == team_abbr_upper:
                return {
                    "opponent_abbr": game["home_team_abbr"],
                    "opponent_id": game["home_team_id"],
                    "location": "Away",
                    "broadcasts": game.get("broadcasts", []),
                }
        return None

    def _context_from_event_data(
        self,
        team_abbr: str,
        event_home: str,
        event_away: str,
    ) -> dict | None:
        """Build game context from Odds API event fields when schedule lookup fails.

        This is a schedule-independent fallback: if the schedule API is down or
        abbreviations don't align, we still know which teams are playing from the
        prop event itself.
        """
        if not event_home or not event_away:
            return None

        ta = team_abbr.upper() if team_abbr else ""
        eh = event_home.upper()
        ea = event_away.upper()

        if ta == eh:
            opp_id = self._team_abbr_to_id.get(ea, 0)
            return {"opponent_abbr": event_away, "opponent_id": opp_id,
                    "location": "Home", "broadcasts": []}
        if ta == ea:
            opp_id = self._team_abbr_to_id.get(eh, 0)
            return {"opponent_abbr": event_home, "opponent_id": opp_id,
                    "location": "Away", "broadcasts": []}

        return None

    @staticmethod
    def _compute_predicted_value(last_5: float, last_10: float, season: float) -> float:
        return round(0.5 * last_5 + 0.35 * last_10 + 0.15 * season, 1)

    @staticmethod
    def _assess_quality(games_analyzed: int) -> str:
        if games_analyzed >= 15:
            return "full"
        if games_analyzed >= 5:
            return "partial"
        return "minimal"

    def _build_key_factors(
        self,
        adjustments: dict,
        avgs: dict,
        h2h: dict,
        line: float,
        opponent_abbr: str,
        opp_rank: int | None,
    ) -> list[str]:
        factors = []

        factors.append(f"Avg {avgs['last_10_avg']:.1f} last 10G (line: {line})")

        if opp_rank is not None:
            def ordinal(n: int) -> str:
                if 11 <= n <= 13:
                    return f"{n}th"
                s = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
                return f"{n}{s}"
            factors.append(f"Opp allows {ordinal(opp_rank)}-most (rank {opp_rank})")

        if h2h["reliable"] and h2h["sample_size"] >= 2:
            hits = round(h2h["hit_rate"] * h2h["sample_size"])
            factors.append(
                f"{hits}/{h2h['sample_size']} vs {opponent_abbr} this season "
                f"({h2h['hit_rate']:.0%})"
            )

        # Only real factor adjustments are eligible for the "key factors" display.
        # Diagnostic entries are excluded: underscore-prefixed keys (_dist_over,
        # _market_over) are model internals, and any None value means the factor
        # was not computed — both would break the abs()-based sort below.
        scored_adj = [
            (k, v) for k, v in adjustments.items()
            if not k.startswith("_") and v is not None
        ]
        sorted_adj = sorted(scored_adj, key=lambda x: abs(x[1]), reverse=True)
        for key, val in sorted_adj[:2]:
            if abs(val) < 0.5:
                continue
            label = key.replace("_", " ").title()
            sign = "+" if val > 0 else ""
            factors.append(f"{label}: {sign}{val:.1f}pp")

        return factors[:5]


# ---------------------------------------------------------------------------
# Edge + tier helpers (Phase 3)
# ---------------------------------------------------------------------------

def _assign_edge_and_tier(results: list[PropResult]) -> None:
    """Compute edge and tier for every PropResult in-place.

    edge = hit_probability/100 − PRIZEPICKS_BREAKEVEN

    A-tier when ALL of:
    - hit_probability >= A_TIER_MIN_PROB
    - edge >= A_TIER_MIN_EDGE
    - data_quality != "minimal"
    - games_analyzed >= MIN_GAMES_FOR_A_TIER
    """
    from config import PRIZEPICKS_BREAKEVEN, A_TIER_MIN_PROB, A_TIER_MIN_EDGE, MIN_GAMES_FOR_A_TIER
    for r in results:
        r.edge = round(r.hit_probability / 100.0 - PRIZEPICKS_BREAKEVEN, 4)
        qualifies = (
            r.hit_probability >= A_TIER_MIN_PROB
            and r.edge >= A_TIER_MIN_EDGE
            and r.data_quality != "minimal"
            and r.games_analyzed >= MIN_GAMES_FOR_A_TIER
        )
        r.tier = "A" if qualifies else "speculative"


def rank_and_tier(results: list[PropResult]) -> list[PropResult]:
    """Return a sorted copy: A-tier picks first (by edge desc), speculative below (by prob desc).

    This is the single canonical sort used by both main.py and the email template
    to guarantee consistent ordering.
    """
    a_tier = sorted(
        [r for r in results if r.tier == "A"],
        key=lambda r: r.edge, reverse=True,
    )
    speculative = sorted(
        [r for r in results if r.tier != "A"],
        key=lambda r: r.hit_probability, reverse=True,
    )
    return a_tier + speculative
