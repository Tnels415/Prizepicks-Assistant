from __future__ import annotations

"""
Multi-Sport Prop Betting Analyzer
----------------------------------
Fetches today's NBA, NHL, and MLB (and NFL in season) player prop lines,
analyzes each using historical stats, and emails a ranked HTML report.

Usage:
    python main.py

Cron (daily 10 AM Eastern):
    0 10 * * * /usr/bin/python3 /path/to/main.py >> /var/log/props.log 2>&1
"""

import copy
import logging
import sys
import time
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import load_config, ConfigError, get_active_sports, SPORT_CONFIG
from data.prizepicks_client import OddsAPIClient
from data.schedule_client import ScheduleClient
from data.nba_stats_client import NBAStatsClient
from data.nhl_stats_client import NHLStatsClient
from data.mlb_stats_client import MLBStatsClient
from data.nfl_stats_client import NFLStatsClient
from analysis.prop_analyzer import PropAnalyzer, PropResult, rank_and_tier
from analysis import correlation as _correlation
from analysis.watchability import is_watchable
from learning.history_store import HistoryStore
from learning.outcome_fetcher import OutcomeFetcher
from learning.calibrator import Calibrator, Corrections
from email_sender.template import render_email_html, render_plain_text, render_yesterday_section
from email_sender.emailer import EmailSender


def setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"props_{date.today().isoformat()}.log"

    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")

    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def _infer_games_from_props(props: list[dict]) -> list[dict]:
    """Build minimal placeholder game dicts from props' team_abbr values.

    Used when all schedule APIs are unavailable.  Pairs consecutive unique teams
    so _determine_game_context can match each player to a game entry.  Opponent
    IDs will be 0 for unpaired odd-teams, meaning the opponent-defense factor
    returns 0 for those players — all other factors still work normally.
    """
    from data.schedule_client import _TEAM_ABBR_TO_ID
    seen: list[str] = []
    for p in props:
        abbr = str(p.get("team_abbr", "")).upper()
        if abbr and abbr not in seen:
            seen.append(abbr)
    games: list[dict] = []
    for i in range(0, len(seen), 2):
        home = seen[i]
        away = seen[i + 1] if i + 1 < len(seen) else ""
        games.append({
            "game_id": f"inferred_{home}_{away or 'UNK'}",
            "home_team_id": _TEAM_ABBR_TO_ID.get(home, 0),
            "home_team_abbr": home,
            "away_team_id": _TEAM_ABBR_TO_ID.get(away, 0),
            "away_team_abbr": away,
            "game_status": "inferred_from_props",
            "broadcasts": [],
        })
    return games


def _top_picks(results: list, n: int = 10) -> list:
    """Return top-n picks using the canonical tier-aware sort (A-tier first by edge,
    then speculative by hit_probability).  Assigns rank 1..n on the trimmed list."""
    ranked = rank_and_tier(results)[:n]
    for i, r in enumerate(ranked, 1):
        r.rank = i
    return ranked


def _build_stats_clients(cfg: dict) -> dict:
    return {
        "NBA": NBAStatsClient(balldontlie_api_key=cfg.get("balldontlie_api_key")),
        "NHL": NHLStatsClient(),
        "MLB": MLBStatsClient(),
        "NFL": NFLStatsClient(),
    }


def main() -> int:
    setup_logging()
    logger = logging.getLogger("main")
    start = time.time()
    today = date.today()
    yesterday = today - timedelta(days=1)

    active_sports = get_active_sports()

    logger.info("=" * 60)
    logger.info("Multi-Sport Prop Analyzer starting — %s", today.isoformat())
    logger.info("Active sports this month: %s", ", ".join(active_sports))

    # --- Configuration (needed early for stats clients) --------------------
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        return 1

    # --- Build stats clients upfront (all sports) --------------------------
    stats_clients = _build_stats_clients(cfg)

    # --- Learning layer (graceful degradation) ----------------------------
    store: HistoryStore | None = None
    corrections_by_sport: dict[str, Corrections] = {}
    yesterday_results: list = []
    cumulative_stats: dict = {}

    try:
        store = HistoryStore()
        fetcher = OutcomeFetcher(store, sport_clients=stats_clients)
        n_evaluated = fetcher.evaluate_yesterday(yesterday)
        logger.info("Evaluated %d picks from %s", n_evaluated, yesterday.isoformat())

        brier_scores: dict[str, float] = {}
        for sport in active_sports:
            calibrator = Calibrator(store, sport=sport)
            corr = calibrator.compute_corrections(today)
            corrections_by_sport[sport] = corr
            if corr.brier_score is not None:
                brier_scores[sport] = corr.brier_score

        # Yesterday's results for the email — all sports combined
        yesterday_results = store.get_results_for_date(yesterday)
        cumulative_stats = store.get_cumulative_accuracy()
        cumulative_stats["brier_scores"] = brier_scores
    except Exception as exc:
        logger.warning("Learning layer failed (%s) — running with raw model", exc)
        store = None

    sender = EmailSender(
        smtp_host=cfg["smtp_host"],
        smtp_port=cfg["smtp_port"],
        from_addr=cfg["email_from"],
        password=cfg["email_password"],
    )

    def send(subject: str, html: str, plain: str) -> None:
        sender.send_report(cfg["email_to"], subject, html, plain)

    schedule = ScheduleClient()
    odds_client = OddsAPIClient()

    # --- Per-sport analysis ------------------------------------------------
    results_by_sport: dict[str, list[PropResult]] = {}
    tv_results_by_sport: dict[str, list[PropResult]] = {}
    broadcast_coverage: dict[str, bool] = {}   # sport → True if any props had broadcast data
    any_games = False
    props_failed_sports: list[str] = []   # sports where prop loading returned nothing

    for sport_name in active_sports:
        sport_cfg = SPORT_CONFIG[sport_name]
        emoji = sport_cfg.get("emoji", "")
        logger.info("-" * 40)
        logger.info("%s %s analysis", emoji, sport_name)

        # Check for games today (via schedule).
        # games is None  → all schedule APIs failed (unknown state)
        # games == []    → API confirmed no games today (season over / off-day)
        # games is list  → games scheduled today, proceed normally
        games = schedule.get_todays_games(sport_key=sport_cfg["odds_sport_key"])
        preloaded_props: list | None = None

        if games is not None and not games:
            # Schedule API responded and confirmed zero games today — skip entirely.
            # Do NOT fall back to props: PrizePicks may still list stale lines from
            # a previous day even when no games are actually scheduled.
            logger.info("No %s games today (confirmed by schedule API) — skipping", sport_name)
            continue

        if games is None:
            # All schedule APIs errored — try fetching props before giving up.
            # If there are real props, infer game context from team abbreviations
            # so analysis can proceed even without a schedule source.
            preloaded_props = odds_client.fetch_props(sport_cfg)
            if not preloaded_props:
                logger.info("No %s games today — skipping", sport_name)
                continue
            games = _infer_games_from_props(preloaded_props)
            if not games:
                logger.info("No %s games today — skipping", sport_name)
                continue
            # Inject inferred games into the schedule cache so PropAnalyzer
            # sees them when it calls schedule.get_todays_games internally.
            from data import schedule_client as _sc
            _sc._SCHEDULE_CACHE[sport_cfg["odds_sport_key"]] = games
            logger.warning(
                "%s: schedule APIs unavailable — inferred %d game(s) from props. "
                "Analysis proceeds without opponent-defense context.",
                sport_name, len(games),
            )

        any_games = True
        logger.info("Games today (%s): %d", sport_name, len(games))

        # Fetch props (skip if already loaded during schedule fallback)
        props = preloaded_props if preloaded_props is not None else odds_client.fetch_props(sport_cfg)
        if not props:
            logger.warning(
                "No %s props loaded — PrizePicks API unavailable and props.json is empty. "
                "Fill props.json with today's PrizePicks lines and re-run.",
                sport_name,
            )
            props_failed_sports.append(sport_name)
            continue

        logger.info("Fetched %d %s props", len(props), sport_name)

        # Analyze
        corrections = corrections_by_sport.get(sport_name, Corrections())
        analyzer = PropAnalyzer(
            stats_client=stats_clients[sport_name],
            schedule_client=schedule,
            sport_config=sport_cfg,
            corrections=corrections,
        )
        sport_results = analyzer.analyze_all_props(props)

        if sport_results:
            # Save ALL analyzed props to history.db BEFORE trimming to top picks.
            # Calibration needs volume — if we only save the top 10 per day we'd need
            # months to accumulate the 15-sample minimum per probability bucket.
            # Saving the full set gives 50-100+ data points/day/sport, which means
            # factor weights and calibration corrections can activate within a week.
            if store is not None:
                try:
                    all_player_ids = {
                        r.player_name: stats_clients[sport_name].find_player_id(r.player_name)
                        for r in sport_results
                    }
                    n_saved = store.save_predictions(
                        sport_results, today, all_player_ids, sport=sport_name
                    )
                    logger.info(
                        "Saved %d/%d %s predictions (all analyzed) to history.db",
                        n_saved, len(sport_results), sport_name,
                    )
                except Exception as exc:
                    logger.warning("Failed to save %s predictions: %s", sport_name, exc)

            # Build the TV-watchable view from the FULL analyzed set (before the
            # all-picks top-10 trim) so watchable picks aren't pre-trimmed away.
            # Use deep copies so re-ranking the TV view doesn't clobber the
            # all-picks ranks (both views share the same PropResult objects).
            tv_networks = cfg.get("tv_networks", set())
            watchable = [
                copy.deepcopy(r) for r in sport_results
                if is_watchable(r.broadcasts, tv_networks)
            ]
            with_broadcasts = sum(1 for r in sport_results if r.broadcasts)
            broadcast_coverage[sport_name] = with_broadcasts > 0
            logger.info(
                "%s: %d/%d props have broadcast data; %d watchable (TV_NETWORKS=%s)",
                sport_name, with_broadcasts, len(sport_results), len(watchable),
                cfg.get("tv_networks") or "(not set — only national networks count)",
            )
            if watchable:
                tv_results_by_sport[sport_name] = _top_picks(watchable)

            sport_results = _top_picks(sport_results)
            results_by_sport[sport_name] = sport_results
            top = sport_results[0]
            logger.info(
                "%s: top pick — %s %s %s %.0f%%",
                sport_name, top.player_name, top.direction, top.stat_type, top.hit_probability,
            )
        else:
            logger.warning(
                "%s: 0 directions ranked from %d props loaded — "
                "all players were skipped (no game log data, player ID not found, "
                "or game context mismatch). Check logs above for per-player details. "
                "If stats.nba.com / NHL / MLB APIs are blocked, warm the disk cache by "
                "running when the API is accessible; cached data lives in cache/game_logs/.",
                sport_name, len(props),
            )

    # --- No games at all? --------------------------------------------------
    if not any_games:
        msg = f"No games scheduled today ({today.strftime('%B %d, %Y')}) for any active sport."
        logger.info(msg)
        send(
            f"Prop Picks - {today.strftime('%B %d, %Y')} - No Games Today",
            f"<p>{msg}</p>",
            msg,
        )
        return 0

    if not results_by_sport:
        if props_failed_sports and not any(
            s for s in active_sports
            if s not in props_failed_sports
            and schedule.get_todays_games(sport_key=SPORT_CONFIG[s]["odds_sport_key"])
        ):
            # Every sport with games today had props loading fail — it's a data source issue
            logger.error(
                "No prop lines could be loaded for any sport (%s). "
                "The PrizePicks API is currently unavailable. "
                "Manually fill props.json with today's PrizePicks lines and re-run.",
                ", ".join(props_failed_sports),
            )
            msg = (
                "No prop lines were available today — the PrizePicks API is currently "
                "unavailable.<br><br>"
                "<b>To fix:</b> manually fill <code>props.json</code> with today's "
                "PrizePicks lines and re-run."
            )
        else:
            logger.error(
                "Analysis produced no results for any sport. "
                "Check the logs above for per-player skip reasons "
                "(player not found, no game log, or game context mismatch). "
                "If the stats API (stats.nba.com / nhle.com / statsapi.mlb.com) is "
                "blocked or rate-limited, game logs will be empty. "
                "Disk-cached data (cache/game_logs/) will be used automatically on "
                "subsequent runs once the cache has been warmed."
            )
            msg = "Analysis produced no results today. Check the application logs."
        send(
            f"Prop Picks - {today.strftime('%B %d, %Y')} - No Data Available",
            f"<p>{msg}</p>",
            msg.replace("<br>", "\n").replace("<b>", "").replace("</b>", "")
                .replace("<br><br>", "\n\n").replace("<code>", "").replace("</code>", "")
                .replace("<a href='https://the-odds-api.com'>the-odds-api.com</a>", "the-odds-api.com"),
        )
        return 1

    duration = time.time() - start
    total_results = sum(len(r) for r in results_by_sport.values())
    logger.info(
        "All sports analyzed in %.1fs: %d total directions across %d sport(s)",
        duration, total_results, len(results_by_sport),
    )

    # --- Email ---------------------------------------------------------------
    active_names = " + ".join(results_by_sport.keys())
    subject = (
        f"Prop Picks - {today.strftime('%B %d, %Y')} - "
        f"{total_results} Picks [{active_names}]"
    )

    # Build correlation-aware power-play suggestions for the email.
    _suggested_entries = _build_suggested_entries(results_by_sport)

    yest_html = render_yesterday_section(yesterday_results, cumulative_stats, yesterday)
    html_body = render_email_html(
        results_by_sport, today, duration,
        yesterday_section_html=yest_html,
        tv_results_by_sport=tv_results_by_sport,
        broadcast_coverage=broadcast_coverage,
        suggested_entries=_suggested_entries,
    )
    plain_body = render_plain_text(
        results_by_sport, today,
        yesterday_results=yesterday_results,
        cumulative_stats=cumulative_stats,
        yesterday_date=yesterday,
        tv_results_by_sport=tv_results_by_sport,
        broadcast_coverage=broadcast_coverage,
        suggested_entries=_suggested_entries,
    )

    send(subject, html_body, plain_body)

    # --- Yesterday's results to stdout --------------------------------------
    yest_evaluated = [r for r in yesterday_results if r.get("correct") in (0, 1)]
    if yest_evaluated:
        n_correct = sum(1 for r in yest_evaluated if r["correct"] == 1)
        n_total   = len(yest_evaluated)
        pct_corr  = round(n_correct / n_total * 100) if n_total else 0
        pct_wrong = 100 - pct_corr
        cum_total   = cumulative_stats.get("total_evaluated", 0)
        cum_correct = cumulative_stats.get("total_correct", 0)
        cum_pct     = cumulative_stats.get("accuracy_pct", 0.0)

        print("\n" + "=" * 60)
        print(f"YESTERDAY'S RESULTS — {yesterday.strftime('%B %d, %Y')}")
        print("=" * 60)
        print(
            f"  {n_correct}/{n_total} correct  "
            f"({pct_corr}% correct | {pct_wrong}% incorrect)"
        )
        if cum_total:
            print(f"  All-time: {cum_correct}/{cum_total} ({cum_pct}%)")
        print()

        correct_picks   = [r for r in yest_evaluated if r["correct"] == 1]
        incorrect_picks = [r for r in yest_evaluated if r["correct"] == 0]

        def _fmt_result(r: dict) -> str:
            actual = r.get("actual_value")
            actual_str = f"{actual:.1f}" if actual is not None else "?"
            dir_label = "OVER " if r.get("direction") == "OVER" else "UNDER"
            return (
                f"    {r.get('player_name',''):<22} "
                f"{dir_label} {r.get('stat_type',''):<14} "
                f"Line:{r.get('line','')!s:<6}  Actual:{actual_str}  "
                f"({r.get('hit_probability', 0):.0f}%)"
            )

        if correct_picks:
            print(f"  CORRECT ({pct_corr}%):")
            for r in correct_picks:
                print(f"  ✓{_fmt_result(r)}")
            print()
        if incorrect_picks:
            print(f"  INCORRECT ({pct_wrong}%):")
            for r in incorrect_picks:
                print(f"  ✗{_fmt_result(r)}")

    # --- Summary to stdout --------------------------------------------------
    print("\n" + "=" * 60)
    print(f"TOP PICKS — {today.strftime('%B %d, %Y')}")
    print("=" * 60)
    for sport_name, sport_results in results_by_sport.items():
        emoji = SPORT_CONFIG[sport_name].get("emoji", "")
        print(f"\n  {emoji}  {sport_name} — TOP 10 PICKS")
        for r in sport_results[:10]:
            direction_label = "OVER " if r.direction == "OVER" else "UNDER"
            print(
                f"    {r.rank:>3}. {r.player_name:<22} "
                f"{direction_label} {r.stat_type:<14} Line:{r.line:<6.1f} "
                f"Prob:{r.hit_probability:.0f}%"
            )
    # --- Suggested correlated power-play entries ---------------------------
    _print_suggested_entries(results_by_sport)

    print("\n" + "=" * 60)
    print(f"Report emailed to {cfg['email_to']}")
    print(f"Total runtime: {duration:.1f}s\n")

    return 0


def _build_suggested_entries(results_by_sport: dict[str, list]) -> list:
    """Build correlation-aware power-play entries from the day's picks."""
    from config import POWER_PLAY_PAYOUTS, SUGGESTED_ENTRY_SIZES

    combined: list = []
    for sport_results in results_by_sport.values():
        combined.extend(sport_results)
    candidates = rank_and_tier(combined)[:20]
    if not candidates or len(candidates) < min(SUGGESTED_ENTRY_SIZES):
        return []
    return _correlation.recommend_power_entries(
        candidates, SUGGESTED_ENTRY_SIZES, POWER_PLAY_PAYOUTS, min_ev=0.0
    )


def _print_suggested_entries(results_by_sport: dict[str, list]) -> None:
    """Print correlation-aware power-play entries to stdout."""
    entries = _build_suggested_entries(results_by_sport)
    if not entries:
        return

    print("\n" + "=" * 60)
    print("SUGGESTED CORRELATED POWER PLAYS (+EV, correlation-adjusted)")
    print("=" * 60)
    for e in entries:
        ev_pct = (e.expected_value or 0.0) * 100.0
        print(
            f"\n  {len(e.legs)}-pick power  |  hit {e.joint_probability * 100:.1f}%  |  "
            f"corr {e.avg_correlation:+.2f}  |  EV {ev_pct:+.1f}%"
        )
        for lg in e.legs:
            direction = "OVER " if lg.direction == "OVER" else "UNDER"
            print(
                f"      {lg.player_name:<22} {direction} {lg.stat_type:<14} "
                f"{lg.line:<6.1f} ({lg.hit_probability:.0f}%)"
            )


if __name__ == "__main__":
    sys.exit(main())
