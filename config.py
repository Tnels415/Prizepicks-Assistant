import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    pass


def get_current_season() -> str:
    today = date.today()
    if today.month >= 10:
        return f"{today.year}-{str(today.year + 1)[-2:]}"
    return f"{today.year - 1}-{str(today.year)[-2:]}"


def get_current_season_year() -> int:
    today = date.today()
    if today.month >= 10:
        return today.year
    return today.year - 1


def load_config() -> dict:
    required = ["EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise ConfigError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in your credentials."
        )
    return {
        "balldontlie_api_key": os.getenv("BALLDONTLIE_API_KEY"),
        "email_from": os.getenv("EMAIL_FROM"),
        "email_password": os.getenv("EMAIL_PASSWORD"),
        "email_to": os.getenv("EMAIL_TO", "tnelson8822@gmail.com"),
        "smtp_host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.getenv("SMTP_PORT", "587")),
    }


NBA_SEASON = get_current_season()
NBA_SEASON_YEAR = get_current_season_year()
NBA_SEASON_TYPE = "Regular Season"

PRIZEPICKS_URL = "https://api.prizepicks.com/projections"
PRIZEPICKS_LEAGUE_ID = 7
PRIZEPICKS_PER_PAGE = 250

BALLDONTLIE_BASE_URL = "https://api.balldontlie.io/v1"

# Maps PrizePicks stat_type strings to NBA game log DataFrame column names.
# None means a computed combination (handled in historical_stats.py).
PROP_STAT_MAP = {
    "Points": "PTS",
    "Rebounds": "REB",
    "Assists": "AST",
    "3-PT Made": "FG3M",
    "Steals": "STL",
    "Blocks": "BLK",
    "Turnovers": "TOV",
    "Pts+Reb+Ast": None,
    "Pts+Ast": None,
    "Pts+Reb": None,
    "Reb+Ast": None,
}

COMBO_STAT_MAP = {
    "Pts+Reb+Ast": ["PTS", "REB", "AST"],
    "Pts+Ast": ["PTS", "AST"],
    "Pts+Reb": ["PTS", "REB"],
    "Reb+Ast": ["REB", "AST"],
}

# Opponent defensive stat columns per prop type (from MeasureType=Opponent)
OPPONENT_STAT_COL = {
    "Points": "OPP_PTS",
    "Rebounds": "OPP_REB",
    "Assists": "OPP_AST",
    "3-PT Made": "OPP_FG3M",
    "Steals": "OPP_STL",
    "Blocks": "OPP_BLK",
    "Turnovers": "OPP_TOV",
    "Pts+Reb+Ast": "OPP_PTS",
    "Pts+Ast": "OPP_PTS",
    "Pts+Reb": "OPP_PTS",
    "Reb+Ast": "OPP_REB",
}

NBA_API_TIMEOUT = 30
NBA_API_RETRY_ATTEMPTS = 3
NBA_API_RETRY_MIN_WAIT = 2
NBA_API_RETRY_MAX_WAIT = 10

PRIZEPICKS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://app.prizepicks.com/",
    "Origin": "https://app.prizepicks.com",
}
