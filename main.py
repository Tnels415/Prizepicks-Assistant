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
from analysis.prop_analyzer import PropAnalyzer, PropResult
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


def _top_picks(results: list, n: int = 10) -> list:
    """Return top-n picks by hit_probability regardless of direction."""
    top = sorted(results, key=lambda r: r.hit_probability, reverse=True)[:n]
    for i, r in enumerate(top, 1):
        r.rank = i
    return top


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

        for sport in active_sports:
            calibrator = Calibrator(store, sport=sport)
            corrections_by_sport[sport] = calibrator.compute_corrections(today)

        # Yesterday's results for the email — all sports combined
        yesterday_results = store.get_results_for_date(yesterday)
        cumulative_stats = store.get_cumulative_accuracy()
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
    any_games = False
    props_failed_sports: list[str] = []   # sports where prop loading returned nothing

    for sport_name in active_sports:
        sport_cfg = SPORT_CONFIG[sport_name]
        emoji = sport_cfg.get("emoji", "")
        logger.info("-" * 40)
        logger.info("%s %s analysis", emoji, sport_name)

        # Check for games today (via schedule)
        games = schedule.get_todays_games(sport_key=sport_cfg["odds_sport_key"])
        if not games:
            logger.info("No %s games today — skipping", sport_name)
            continue

        any_games = True
        logger.info("Games today (%s): %d", sport_name, len(games))

        # Fetch props
        props = odds_client.fetch_props(sport_cfg)
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

    yest_html = render_yesterday_section(yesterday_results, cumulative_stats, yesterday)
    html_body = render_email_html(results_by_sport, today, duration, yesterday_section_html=yest_html)
    plain_body = render_plain_text(
        results_by_sport, today,
        yesterday_results=yesterday_results,
        cumulative_stats=cumulative_stats,
        yesterday_date=yesterday,
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
    print("\n" + "=" * 60)
    print(f"Report emailed to {cfg['email_to']}")
    print(f"Total runtime: {duration:.1f}s\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
