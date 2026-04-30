from __future__ import annotations

"""
NBA Prop Betting Analyzer
-------------------------
Fetches today's PrizePicks NBA props, analyzes each using historical stats
from stats.nba.com, and emails a ranked HTML report.

Usage:
    python main.py

Cron (daily 10 AM Eastern):
    0 10 * * * /usr/bin/python3 /path/to/main.py >> /var/log/nba_props.log 2>&1
"""

import logging
import sys
import time
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import load_config, ConfigError, NBA_SEASON
from data.prizepicks_client import PrizePicksClient
from data.schedule_client import ScheduleClient
from data.nba_stats_client import NBAStatsClient
from analysis.prop_analyzer import PropAnalyzer
from email_sender.template import render_email_html, render_plain_text
from email_sender.emailer import EmailSender


def setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"nba_props_{date.today().isoformat()}.log"

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


def main() -> int:
    setup_logging()
    logger = logging.getLogger("main")
    start = time.time()
    today = date.today()

    logger.info("=" * 60)
    logger.info("NBA Prop Analyzer starting — %s  Season: %s", today.isoformat(), NBA_SEASON)

    # --- Configuration --------------------------------------------------
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        return 1

    sender = EmailSender(
        smtp_host=cfg["smtp_host"],
        smtp_port=cfg["smtp_port"],
        from_addr=cfg["email_from"],
        password=cfg["email_password"],
    )

    def send(subject: str, html: str, plain: str) -> None:
        sender.send_report(cfg["email_to"], subject, html, plain)

    # --- Today's games --------------------------------------------------
    schedule = ScheduleClient()
    games = schedule.get_todays_games()

    if not games:
        logger.info("No NBA games scheduled today. Sending notification.")
        msg = f"No NBA games scheduled for {today.strftime('%B %d, %Y')}."
        send(
            f"NBA Prop Picks - {today.strftime('%B %d, %Y')} - No Games Today",
            f"<p>{msg}</p>",
            msg,
        )
        return 0

    logger.info("Games today: %d", len(games))

    # --- Prop lines (The Odds API → props.json fallback) ----------------
    pp_client = PrizePicksClient(odds_api_key=cfg.get("odds_api_key"))
    props = pp_client.fetch_nba_props()

    if not props:
        logger.warning("No props loaded — check props.json or PrizePicks API availability.")
        # Instructions already printed by PrizePicksClient when props.json is missing/template
        return 0

    logger.info("Fetched %d props from PrizePicks", len(props))

    # --- Analysis -------------------------------------------------------
    stats_client = NBAStatsClient(balldontlie_api_key=cfg.get("balldontlie_api_key"))
    analyzer = PropAnalyzer(stats_client, schedule)

    results = analyzer.analyze_all_props(props)

    if not results:
        logger.error("Analysis produced no results. Check logs for details.")
        msg = "Analysis produced no results today. Check the application logs."
        send(
            f"NBA Prop Picks - {today.strftime('%B %d, %Y')} - Analysis Error",
            f"<p>{msg}</p>",
            msg,
        )
        return 1

    duration = time.time() - start
    logger.info(
        "Analysis complete in %.1fs: %d results from %d props",
        duration, len(results), len(props),
    )

    # --- Email ----------------------------------------------------------
    subject = (
        f"NBA Prop Picks - {today.strftime('%B %d, %Y')} - "
        f"{len(results)} Directions Ranked"
    )
    html_body = render_email_html(results, today, duration)
    plain_body = render_plain_text(results, today)

    send(subject, html_body, plain_body)

    # Summary to stdout
    print("\n" + "=" * 60)
    print(f"TOP 10 PICKS — {today.strftime('%B %d, %Y')}")
    print("=" * 60)
    for r in results[:10]:
        print(
            f"  {r.rank:>3}. {r.player_name:<22} {r.direction:<5} "
            f"{r.stat_type:<14} Line:{r.line:<6.1f} "
            f"Prob:{r.hit_probability:.0f}%"
        )
    print("=" * 60)
    print(f"Report emailed to {cfg['email_to']}")
    print(f"Total runtime: {duration:.1f}s\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
