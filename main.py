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


def _top_by_direction(results: list, n: int = 10) -> list:
    """Return top-n OVER and top-n UNDER picks, re-ranked 1..n within each direction."""
    overs  = [r for r in results if r.direction == "OVER"][:n]
    unders = [r for r in results if r.direction == "UNDER"][:n]
    for i, r in enumerate(overs,  1):
        r.rank = i
    for i, r in enumerate(unders, 1):
        r.rank = i
    return overs + unders


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
    odds_client = OddsAPIClient(odds_api_key=cfg.get("odds_api_key"))

    # --- Per-sport analysis ------------------------------------------------
    results_by_sport: dict[str, list[PropResult]] = {}
    any_games = False

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
            logger.warning("No %s props loaded — check API key or props.json", sport_name)
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
            sport_results = _top_by_direction(sport_results)
            results_by_sport[sport_name] = sport_results
            top_over  = next((r for r in sport_results if r.direction == "OVER"),  None)
            top_under = next((r for r in sport_results if r.direction == "UNDER"), None)
            logger.info(
                "%s: top OVER  — %s %s %.0f%%",
                sport_name,
                top_over.player_name  if top_over  else "—",
                top_over.stat_type    if top_over  else "",
                top_over.hit_probability if top_over else 0,
            )
            logger.info(
                "%s: top UNDER — %s %s %.0f%%",
                sport_name,
                top_under.player_name  if top_under  else "—",
                top_under.stat_type    if top_under  else "",
                top_under.hit_probability if top_under else 0,
            )
        else:
            logger.warning(
                "%s: 0 directions ranked from %d props loaded — "
                "all players were skipped (no game log data, player ID not found, "
                "or game context mismatch). Check logs above for per-player details.",
                sport_name, len(props),
            )

        # Save today's picks for tomorrow's evaluation
        if store is not None and sport_results:
            try:
                player_id_map = {
                    r.player_name: stats_clients[sport_name].find_player_id(r.player_name)
                    for r in sport_results
                }
                n_saved = store.save_predictions(
                    sport_results, today, player_id_map, sport=sport_name
                )
                logger.info("Saved %d %s predictions to history.db", n_saved, sport_name)
            except Exception as exc:
                logger.warning("Failed to save %s predictions: %s", sport_name, exc)

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
        logger.error("Analysis produced no results for any sport.")
        msg = "Analysis produced no results today. Check the application logs."
        send(
            f"Prop Picks - {today.strftime('%B %d, %Y')} - Analysis Error",
            f"<p>{msg}</p>",
            msg,
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
    plain_body = render_plain_text(results_by_sport, today)

    send(subject, html_body, plain_body)

    # --- Summary to stdout --------------------------------------------------
    print("\n" + "=" * 60)
    print(f"TOP PICKS — {today.strftime('%B %d, %Y')}")
    print("=" * 60)
    for sport_name, sport_results in results_by_sport.items():
        emoji = SPORT_CONFIG[sport_name].get("emoji", "")
        overs  = [r for r in sport_results if r.direction == "OVER"]
        unders = [r for r in sport_results if r.direction == "UNDER"]
        print(f"\n  {emoji}  {sport_name} — TOP OVERS")
        for r in overs[:5]:
            print(
                f"    {r.rank:>3}. {r.player_name:<22} "
                f"{r.stat_type:<14} Line:{r.line:<6.1f} "
                f"Prob:{r.hit_probability:.0f}%"
            )
        print(f"\n  {emoji}  {sport_name} — TOP UNDERS")
        for r in unders[:5]:
            print(
                f"    {r.rank:>3}. {r.player_name:<22} "
                f"{r.stat_type:<14} Line:{r.line:<6.1f} "
                f"Prob:{r.hit_probability:.0f}%"
            )
    print("\n" + "=" * 60)
    print(f"Report emailed to {cfg['email_to']}")
    print(f"Total runtime: {duration:.1f}s\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
