from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from data.nba_stats_client import NBAStatsClient
from data.schedule_client import ScheduleClient
from analysis.historical_stats import HistoricalStatsCalculator
from analysis.factor_scorer import FactorScorer

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
    key_factors: list[str] = field(default_factory=list)
    data_quality: str = "full"      # "full", "partial", "minimal"
    raw_adjustments: dict = field(default_factory=dict)
    rank: int = 0


class PropAnalyzer:

    def __init__(
        self,
        stats_client: NBAStatsClient,
        schedule_client: ScheduleClient,
    ):
        self._stats = stats_client
        self._schedule = schedule_client
        self._calc = HistoricalStatsCalculator()
        self._scorer = FactorScorer()
        self._team_stats: pd.DataFrame | None = None
        self._team_abbr_to_id: dict[str, int] = {}
        self._team_id_to_abbr: dict[int, str] = {}
        self._todays_games: list[dict] = []

    def analyze_all_props(self, props: list[dict]) -> list[PropResult]:
        logger.info("Pre-loading team stats…")
        self._team_stats = self._stats.get_team_advanced_stats()
        self._stats.get_player_usage()
        self._team_abbr_to_id = self._schedule.get_team_abbr_to_id()
        self._team_id_to_abbr = self._schedule.get_team_id_to_abbr()
        self._todays_games = self._schedule.get_todays_games()

        results: list[PropResult] = []
        seen_players: dict[str, pd.DataFrame] = {}  # cache per player name

        for prop in props:
            try:
                prop_results = self._analyze_single_prop(prop, seen_players)
                if prop_results:
                    results.extend(prop_results)
            except Exception as exc:
                logger.error(
                    "Unexpected error analyzing %s %s: %s",
                    prop.get("player_name"), prop.get("stat_type"), exc,
                    exc_info=True,
                )

        # Sort all results (both OVER and UNDER rows) by hit_probability descending
        results.sort(key=lambda r: r.hit_probability, reverse=True)
        for i, r in enumerate(results, 1):
            r.rank = i

        logger.info("Analysis complete: %d prop directions ranked", len(results))
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

        # Find player ID
        player_id = self._stats.find_player_id(player_name)
        if player_id is None:
            logger.warning("Skipping %s — player ID not found", player_name)
            return None

        # Fetch game log early so we can derive team_abbr if missing
        if player_name not in player_cache:
            player_cache[player_name] = self._stats.get_player_game_log(player_id)
        game_log = player_cache[player_name]

        # Derive team_abbr from game log MATCHUP when Odds API didn't supply it
        if not team_abbr and not game_log.empty and "MATCHUP" in game_log.columns:
            matchup = str(game_log["MATCHUP"].iloc[0])
            team_abbr = matchup.strip()[:3].upper()

        # If still unknown, try each team in the event
        if not team_abbr and (event_home or event_away):
            for candidate in (event_home, event_away):
                if candidate and self._determine_game_context(candidate):
                    team_abbr = candidate
                    break

        # Game context for today
        context = self._determine_game_context(team_abbr)
        if context is None:
            logger.debug("Skipping %s — no game today for team %s", player_name, team_abbr)
            return None

        opponent_abbr = context["opponent_abbr"]
        opponent_id = context["opponent_id"]
        location = context["location"]

        if game_log.empty:
            logger.warning("Skipping %s — no game log data", player_name)
            return None

        # Rest days from game log dates
        rest_days = self._calc.infer_rest_days(game_log)

        # Historical stats
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

        # Factor scores
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

        adjustments = [delta_season, delta_form, delta_h2h, delta_opp,
                       delta_loc, delta_rest, delta_pace]
        over_prob = self._scorer.compute_composite_probability(hit_rate_20, adjustments)
        under_prob = 100.0 - over_prob

        predicted = self._compute_predicted_value(
            avgs["last_5_avg"], avgs["last_10_avg"], avgs["season_avg"]
        )

        raw_adj = {
            "season_avg_vs_line": delta_season,
            "recent_form": delta_form,
            "h2h_this_season": delta_h2h,
            "opponent_defense": delta_opp,
            "home_away": delta_loc,
            "rest_days": delta_rest,
            "pace": delta_pace,
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
            key_factors=key_factors,
            data_quality=data_quality,
            raw_adjustments=raw_adj,
        )

        over_result = PropResult(
            direction="OVER",
            hit_probability=over_prob,
            **base_kwargs,
        )
        under_result = PropResult(
            direction="UNDER",
            hit_probability=under_prob,
            **base_kwargs,
        )

        return [over_result, under_result]

    def _determine_game_context(self, team_abbr: str) -> dict | None:
        team_abbr_upper = team_abbr.upper()
        for game in self._todays_games:
            if game["home_team_abbr"].upper() == team_abbr_upper:
                return {
                    "opponent_abbr": game["away_team_abbr"],
                    "opponent_id": game["away_team_id"],
                    "location": "Home",
                }
            if game["away_team_abbr"].upper() == team_abbr_upper:
                return {
                    "opponent_abbr": game["home_team_abbr"],
                    "opponent_id": game["home_team_id"],
                    "location": "Away",
                }
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

        factors.append(
            f"Avg {avgs['last_10_avg']:.1f} last 10G (line: {line})"
        )

        if opp_rank is not None:
            def ordinal(n: int) -> str:
                if 11 <= n <= 13:
                    return f"{n}th"
                s = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
                return f"{n}{s}"
            factors.append(f"Opp allows {ordinal(opp_rank)}-most in category (rank {opp_rank}/30)")

        if h2h["reliable"] and h2h["sample_size"] >= 2:
            hits = round(h2h["hit_rate"] * h2h["sample_size"])
            factors.append(
                f"{hits}/{h2h['sample_size']} vs {opponent_abbr} this season "
                f"({h2h['hit_rate']:.0%})"
            )

        sorted_adj = sorted(adjustments.items(), key=lambda x: abs(x[1]), reverse=True)
        for key, val in sorted_adj[:2]:
            if abs(val) < 0.5:
                continue
            label = key.replace("_", " ").title()
            sign = "+" if val > 0 else ""
            factors.append(f"{label}: {sign}{val:.1f}pp")

        return factors[:5]
