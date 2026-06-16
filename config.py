from __future__ import annotations

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


def get_nhl_season() -> str:
    today = date.today()
    if today.month >= 10:
        return f"{today.year}{today.year + 1}"
    return f"{today.year - 1}{today.year}"


def get_active_sports() -> list[str]:
    """Return sport keys that have active seasons this month."""
    month = date.today().month
    return [s for s, cfg in SPORT_CONFIG.items() if month in cfg["active_months"]]


def load_config() -> dict:
    required = ["EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise ConfigError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in your credentials."
        )
    # Regional/streaming networks the user can access (comma-separated), used to
    # decide which games are "watchable on TV" beyond national broadcasts.
    tv_networks = {
        n.strip() for n in os.getenv("TV_NETWORKS", "").split(",") if n.strip()
    }
    return {
        "balldontlie_api_key": os.getenv("BALLDONTLIE_API_KEY"),
        "odds_api_key": os.getenv("THE_ODDS_API_KEY"),
        "email_from": os.getenv("EMAIL_FROM"),
        "email_password": os.getenv("EMAIL_PASSWORD"),
        "email_to": os.getenv("EMAIL_TO", "tnelson8822@gmail.com"),
        "smtp_host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.getenv("SMTP_PORT", "587")),
        "tv_networks": tv_networks,
    }


NBA_SEASON = get_current_season()
NBA_SEASON_YEAR = get_current_season_year()
NBA_SEASON_TYPE = "Regular Season"

PRIZEPICKS_URL = "https://api.prizepicks.com/projections"
PRIZEPICKS_LEAGUE_ID = 7   # NBA (legacy constant kept for compatibility)
PRIZEPICKS_PER_PAGE = 250

BALLDONTLIE_BASE_URL = "https://api.balldontlie.io/v1"

# ── NBA legacy constants (still used by existing code) ──────────────────────
PROP_STAT_MAP = {
    "Points": "PTS", "Rebounds": "REB", "Assists": "AST",
    "3-PT Made": "FG3M", "Steals": "STL", "Blocks": "BLK",
    "Turnovers": "TOV",
    "FG Made": "FGM", "FG Attempted": "FGA",
    "3-PT Attempted": "FG3A",
    "Two Pointers Made": "2PM", "Two Pointers Attempted": "2PA",
    "Free Throws Made": "FTM", "Free Throws Attempted": "FTA",
    "Defensive Rebounds": "DREB", "Offensive Rebounds": "OREB",
    "Personal Fouls": "PF",
    "Pts+Reb+Ast": None, "Pts+Ast": None, "Pts+Reb": None, "Reb+Ast": None,
    "Blks+Stls": None,
}

COMBO_STAT_MAP = {
    "Pts+Reb+Ast": ["PTS", "REB", "AST"],
    "Pts+Ast": ["PTS", "AST"],
    "Pts+Reb": ["PTS", "REB"],
    "Reb+Ast": ["REB", "AST"],
    "Blks+Stls": ["BLK", "STL"],
}

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

NBA_API_TIMEOUT = 15         # stats.nba.com is slow; 8s caused false timeouts
NBA_API_RETRY_ATTEMPTS = 2
NBA_API_RETRY_MIN_WAIT = 1
NBA_API_RETRY_MAX_WAIT = 3
STATS_API_TIMEOUT = 8       # shared timeout for NHL/MLB requests
GOBLIN_STD_THRESHOLD = 1.5  # std devs below/above season avg to flag as goblin/demon

# PrizePicks NHL Goalie Fantasy Score formula weights.
# NOTE: PrizePicks does not publicly document this formula — these values are a
# community approximation.  Verify by opening any Goalie Fantasy Score prop inside
# the PrizePicks app and tapping the scoring-chart info icon, then update here.
NHL_GOALIE_FANTASY_FORMULA: dict[str, float] = {
    "save":           0.6,    # points per save
    "win":            6.0,    # points for a win decision
    "ot_loss":        2.0,    # points for an OT/SO loss
    "shutout_bonus":  4.0,    # bonus points when goals_against == 0
    "goal_against":  -1.8,    # points per goal allowed (negative)
}

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

# ── Team name → abbreviation maps ──────────────────────────────────────────

NBA_TEAM_NAME_TO_ABBR = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP", "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR", "Utah Jazz": "UTA", "Washington Wizards": "WAS",
}

NHL_TEAM_NAME_TO_ABBR = {
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF",
    "Calgary Flames": "CGY", "Carolina Hurricanes": "CAR", "Chicago Blackhawks": "CHI",
    "Colorado Avalanche": "COL", "Columbus Blue Jackets": "CBJ", "Dallas Stars": "DAL",
    "Detroit Red Wings": "DET", "Edmonton Oilers": "EDM", "Florida Panthers": "FLA",
    "Los Angeles Kings": "LAK", "Minnesota Wild": "MIN", "Montreal Canadiens": "MTL",
    "Nashville Predators": "NSH", "New Jersey Devils": "NJD", "New York Islanders": "NYI",
    "New York Rangers": "NYR", "Ottawa Senators": "OTT", "Philadelphia Flyers": "PHI",
    "Pittsburgh Penguins": "PIT", "San Jose Sharks": "SJS", "Seattle Kraken": "SEA",
    "St. Louis Blues": "STL", "Tampa Bay Lightning": "TBL", "Toronto Maple Leafs": "TOR",
    "Utah Hockey Club": "UTA", "Vancouver Canucks": "VAN", "Vegas Golden Knights": "VGK",
    "Washington Capitals": "WSH", "Winnipeg Jets": "WPG",
}

NFL_TEAM_NAME_TO_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}

MLB_TEAM_NAME_TO_ABBR = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS", "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Colorado Rockies": "COL",
    "Detroit Tigers": "DET", "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD", "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Oakland Athletics": "OAK", "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT", "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL", "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
    "Athletics": "OAK",
}

# ── Per-sport configuration ──────────────────────────────────────────────────
SPORT_CONFIG: dict[str, dict] = {
    "NBA": {
        "name": "NBA",
        "full_name": "NBA Basketball",
        "odds_sport_key": "basketball_nba",
        "active_months": [10, 11, 12, 1, 2, 3, 4, 5],  # Finals end by early June
        "markets": [
            "player_points", "player_rebounds", "player_assists",
            "player_threes", "player_steals", "player_blocks", "player_turnovers",
        ],
        "market_to_stat": {
            "player_points": "Points",
            "player_rebounds": "Rebounds",
            "player_assists": "Assists",
            "player_threes": "3-PT Made",
            "player_steals": "Steals",
            "player_blocks": "Blocks",
            "player_turnovers": "Turnovers",
        },
        "prop_stat_map": {
            "Points": "PTS", "Rebounds": "REB", "Assists": "AST",
            "3-PT Made": "FG3M", "Steals": "STL", "Blocks": "BLK",
            "Turnovers": "TOV",
            "FG Made": "FGM", "FG Attempted": "FGA",
            "3-PT Attempted": "FG3A",
            "Two Pointers Made": "2PM", "Two Pointers Attempted": "2PA",
            "Free Throws Made": "FTM", "Free Throws Attempted": "FTA",
            "Defensive Rebounds": "DREB", "Offensive Rebounds": "OREB",
            "Personal Fouls": "PF",
            # Combo stats — handled via combo_stat_map
            "Pts+Reb+Ast": None, "Pts+Ast": None, "Pts+Reb": None, "Reb+Ast": None,
            "Blks+Stls": None,
        },
        "combo_stat_map": {
            "Pts+Reb+Ast": ["PTS", "REB", "AST"],
            "Pts+Ast": ["PTS", "AST"],
            "Pts+Reb": ["PTS", "REB"],
            "Reb+Ast": ["REB", "AST"],
            "Blks+Stls": ["BLK", "STL"],
        },
        "opponent_stat_col": {
            "Points": "OPP_PTS", "Rebounds": "OPP_REB", "Assists": "OPP_AST",
            "3-PT Made": "OPP_FG3M", "Steals": "OPP_STL", "Blocks": "OPP_BLK",
            "Turnovers": "OPP_TOV", "Pts+Reb+Ast": "OPP_PTS", "Pts+Ast": "OPP_PTS",
            "Pts+Reb": "OPP_PTS", "Reb+Ast": "OPP_REB",
        },
        "outcome_stat_map": {
            "Points": "PTS", "Rebounds": "REB", "Assists": "AST",
            "3-PT Made": "FG3M", "Steals": "STL", "Blocks": "BLK",
            "Turnovers": "TOV",
            "FG Made": "FGM", "FG Attempted": "FGA",
            "3-PT Attempted": "FG3A",
            "Two Pointers Made": "2PM", "Two Pointers Attempted": "2PA",
            "Free Throws Made": "FTM", "Free Throws Attempted": "FTA",
            "Defensive Rebounds": "DREB", "Offensive Rebounds": "OREB",
            "Personal Fouls": "PF",
            "Pts+Reb+Ast": ["PTS", "REB", "AST"],
            "Pts+Ast": ["PTS", "AST"],
            "Pts+Reb": ["PTS", "REB"],
            "Reb+Ast": ["REB", "AST"],
            "Blks+Stls": ["BLK", "STL"],
        },
        "counting_stats": {
            "Points", "Rebounds", "Assists", "3-PT Made",
            "FG Made", "FG Attempted", "3-PT Attempted",
            "Two Pointers Made", "Two Pointers Attempted",
            "Free Throws Made", "Free Throws Attempted",
            "Defensive Rebounds", "Offensive Rebounds",
            "Pts+Reb+Ast", "Pts+Ast", "Pts+Reb", "Reb+Ast", "Blks+Stls",
        },
        "team_name_to_abbr": NBA_TEAM_NAME_TO_ABBR,
        "stats_client_class": "NBAStatsClient",
        "emoji": "🏀",
        "prizepicks_league_id": 7,
        "prizepicks_stat_map": {
            "Blocked Shots": "Blocks",
            "Pts+Rebs+Asts": "Pts+Reb+Ast",
            "Pts+Asts": "Pts+Ast",
            "Pts+Rebs": "Pts+Reb",
            "Rebs+Asts": "Reb+Ast",
            # Flex/"(Combo)" props — map to their standard equivalents
            "Points (Combo)": "Points",
            "Rebounds (Combo)": "Rebounds",
            "Assists (Combo)": "Assists",
            "3-PT Made (Combo)": "3-PT Made",
            "Steals (Combo)": "Steals",
            "Blocks (Combo)": "Blocks",
            "Turnovers (Combo)": "Turnovers",
            "FG Made (Combo)": "FG Made",
            "FG Attempted (Combo)": "FG Attempted",
            "Two Pointers Made (Combo)": "Two Pointers Made",
            "Two Pointers Attempted (Combo)": "Two Pointers Attempted",
            "Free Throws Made (Combo)": "Free Throws Made",
            "Free Throws Attempted (Combo)": "Free Throws Attempted",
            "Defensive Rebounds (Combo)": "Defensive Rebounds",
            "Offensive Rebounds (Combo)": "Offensive Rebounds",
            "Personal Fouls (Combo)": "Personal Fouls",
            # Alternate PrizePicks labels observed in the wild
            "Steals+Blocks": "Blks+Stls",
            "Blocks+Steals": "Blks+Stls",
            # Explicit skips — unsupported stat types (logged at DEBUG, not WARNING)
            "Fantasy Score": None,
            "Dunks": None,
            "Double-Double": None,
            "Points - 1st 3 Minutes": None,
            "Rebounds - 1st 3 Minutes": None,
            "Assists - 1st 3 Minutes": None,
            "Points+Rebounds+Assists - 1st 3 Minutes": None,
            "Quarters with 3+ Points": None,
            "Quarters with 5+ Points": None,
        },
        "dk_event_group_id": 42648,
    },
    "NHL": {
        "name": "NHL",
        "full_name": "NHL Hockey",
        "odds_sport_key": "icehockey_nhl",
        "active_months": [10, 11, 12, 1, 2, 3, 4, 5],  # Stanley Cup Finals end by mid-June
        "markets": [
            "player_points", "player_goals", "player_assists",
            "player_shots_on_goal", "player_power_play_points",
        ],
        "market_to_stat": {
            "player_points": "Points",
            "player_goals": "Goals",
            "player_assists": "Assists",
            "player_shots_on_goal": "Shots on Goal",
            "player_power_play_points": "Power Play Points",
        },
        "prop_stat_map": {
            "Points": "PTS",             # G+A pre-computed in NHLStatsClient
            "Goals": "G",
            "Assists": "A",
            "Shots on Goal": "SOG",
            "Power Play Points": "PPP",
            "Hits": "HITS",
            "Blocked Shots": "BLKS",
            "Plus/Minus": "PLUSMINUS",
            "Time On Ice": "TOI",
            "Goalie Saves": "SAVES",
            "Goals Allowed": "GA",
            "Faceoffs Won": "FOW",
            "Goalie Fantasy Score": "GOALIE_SCORE",
        },
        "combo_stat_map": {},
        "opponent_stat_col": {},   # NHL team-defense stats not readily available
        "outcome_stat_map": {
            "Points": "PTS", "Goals": "G", "Assists": "A",
            "Shots on Goal": "SOG", "Power Play Points": "PPP",
            "Hits": "HITS", "Blocked Shots": "BLKS", "Plus/Minus": "PLUSMINUS",
            "Time On Ice": "TOI", "Goalie Saves": "SAVES", "Goals Allowed": "GA",
            "Faceoffs Won": "FOW", "Goalie Fantasy Score": "GOALIE_SCORE",
        },
        "counting_stats": {
            "Points", "Goals", "Assists", "Shots on Goal",
            "Hits", "Blocked Shots", "Goalie Saves", "Time On Ice",
            "Faceoffs Won", "Goalie Fantasy Score",
        },
        "team_name_to_abbr": NHL_TEAM_NAME_TO_ABBR,
        "stats_client_class": "NHLStatsClient",
        "emoji": "🏒",
        "prizepicks_league_id": 12,
        "prizepicks_stat_map": {
            "Shots On Goal": "Shots on Goal",
            # Explicit skip — ambiguous combo stat with no clean mapping
            "Shots On Goal (Combo)": None,
        },
        "dk_event_group_id": 42133,
    },
    "NFL": {
        "name": "NFL",
        "full_name": "NFL Football",
        "odds_sport_key": "americanfootball_nfl",
        "active_months": [9, 10, 11, 12, 1],
        "markets": [
            "player_pass_yds", "player_pass_tds", "player_rush_yds",
            "player_receptions", "player_reception_yds",
        ],
        "market_to_stat": {
            "player_pass_yds": "Passing Yards",
            "player_pass_tds": "Passing TDs",
            "player_rush_yds": "Rushing Yards",
            "player_receptions": "Receptions",
            "player_reception_yds": "Receiving Yards",
        },
        "prop_stat_map": {
            "Passing Yards": "PASS_YDS",
            "Passing TDs": "PASS_TDS",
            "Rushing Yards": "RUSH_YDS",
            "Receptions": "REC",
            "Receiving Yards": "REC_YDS",
        },
        "combo_stat_map": {},
        "opponent_stat_col": {},
        "outcome_stat_map": {
            "Passing Yards": "PASS_YDS", "Passing TDs": "PASS_TDS",
            "Rushing Yards": "RUSH_YDS", "Receptions": "REC",
            "Receiving Yards": "REC_YDS",
        },
        "counting_stats": {"Passing Yards", "Rushing Yards", "Receiving Yards", "Receptions"},
        "team_name_to_abbr": NFL_TEAM_NAME_TO_ABBR,
        "stats_client_class": "NFLStatsClient",
        "emoji": "🏈",
        "prizepicks_league_id": 9,
        "prizepicks_stat_map": {
            "Pass Yards": "Passing Yards",
            "Pass TDs": "Passing TDs",
            "Rush Yards": "Rushing Yards",
        },
        "dk_event_group_id": 88808,
    },
    "MLB": {
        "name": "MLB",
        "full_name": "MLB Baseball",
        "odds_sport_key": "baseball_mlb",
        "active_months": [4, 5, 6, 7, 8, 9, 10],
        "markets": [
            "batter_hits", "batter_home_runs", "batter_rbis",
            "batter_total_bases", "batter_runs_scored",
            "batter_stolen_bases", "pitcher_strikeouts",
        ],
        "market_to_stat": {
            "batter_hits": "Hits",
            "batter_home_runs": "Home Runs",
            "batter_rbis": "RBIs",
            "batter_total_bases": "Total Bases",
            "batter_runs_scored": "Runs Scored",
            "batter_stolen_bases": "Stolen Bases",
            "pitcher_strikeouts": "Strikeouts",
        },
        "prop_stat_map": {
            # Batter props
            "Hits": "H",
            "Home Runs": "HR",
            "RBIs": "RBI",
            "Total Bases": "TB",
            "Runs Scored": "R",
            "Stolen Bases": "SB",
            "Strikeouts": "SO",
            "Walks": "BB",
            "Doubles": "DOUBLES",
            "Triples": "TRIPLES",
            "Singles": "SINGLES",
            # Pitcher props
            "Hits Allowed": "HA",
            "Earned Runs Allowed": "ER",
            "Walks Allowed": "BB_ALLOWED",
            "Pitches Thrown": "PITCHES",
            "Pitching Outs": "PITCHING_OUTS",
            # Combo stats — handled via combo_stat_map
            "Hits+Runs+RBIs": None,
        },
        "combo_stat_map": {
            "Hits+Runs+RBIs": ["H", "R", "RBI"],
        },
        "opponent_stat_col": {},
        "outcome_stat_map": {
            "Hits": "H", "Home Runs": "HR", "RBIs": "RBI",
            "Total Bases": "TB", "Runs Scored": "R",
            "Stolen Bases": "SB", "Strikeouts": "SO",
            "Walks": "BB", "Doubles": "DOUBLES", "Triples": "TRIPLES", "Singles": "SINGLES",
            "Hits Allowed": "HA", "Earned Runs Allowed": "ER", "Walks Allowed": "BB_ALLOWED",
            "Pitches Thrown": "PITCHES", "Pitching Outs": "PITCHING_OUTS",
            "Hits+Runs+RBIs": ["H", "R", "RBI"],
        },
        "counting_stats": {
            "Hits", "Home Runs", "RBIs", "Total Bases", "Runs Scored", "Stolen Bases",
            "Walks", "Doubles", "Triples", "Singles", "Strikeouts",
            "Hits Allowed", "Walks Allowed", "Pitches Thrown", "Pitching Outs",
            "Hits+Runs+RBIs",
        },
        "team_name_to_abbr": MLB_TEAM_NAME_TO_ABBR,
        "stats_client_class": "MLBStatsClient",
        "emoji": "⚾",
        "prizepicks_league_id": 2,
        "prizepicks_stat_map": {
            # PrizePicks label → internal stat name
            "Pitcher Strikeouts": "Strikeouts",
            "Pitcher Strikeouts (Combo)": "Strikeouts",
            "Hitter Strikeouts": "Strikeouts",
            "Runs": "Runs Scored",
            # Explicit skips — unsupported (logged at DEBUG, not WARNING)
            "Hitter Fantasy Score": None,
            "Pitcher Fantasy Score": None,
            "1st Inning Runs Allowed": None,
            "1st Inning Walks Allowed": None,
        },
        "dk_event_group_id": 40625,
    },
}

# ── Accuracy-improvement feature flags (Phase 1-4) ──────────────────────────
# Phase 2: market odds blend
MARKET_BLEND_WEIGHT = 0.40          # weight given to devigged sportsbook prob (0=model only)
MARKET_ODDS_MAX_REQUESTS_PER_RUN = 30  # hard cap to protect the free-tier quota

# Injury tiering — confidence shrink applied to players who carry a designation
# but are still expected to play (see analysis/prop_analyzer.py). Fraction of
# the distance to 50% removed: 0.30 means a 70% pick becomes 64%.
INJURY_QUESTIONABLE_SHRINK = 0.30
INJURY_PROBABLE_SHRINK = 0.10

# Phase 3: edge + tiering
# PrizePicks break-even per leg depends on entry type. Public no-vig analysis
# puts 5-/6-leg flex break-even near ~52% (and correlated legs lower it further),
# while 2-/3-pick power plays need ~57%+. 0.54 is a conservative middle value
# tuned for the recommended 5-/6-leg flex entries.
PRIZEPICKS_BREAKEVEN = 0.54         # per-leg flex break-even (5-/6-leg flex)
A_TIER_MIN_PROB = 60.0              # minimum hit_probability (%) to qualify for A-tier
A_TIER_MIN_EDGE = 0.06              # minimum edge above break-even to qualify
MIN_GAMES_FOR_A_TIER = 8            # minimum games_analyzed for A-tier eligibility
