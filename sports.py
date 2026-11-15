"""Per-sport settings: divisions, season calendar, playoff format, news feeds and team names.

NHL is the original sport and keeps using the NHL's own API. NFL and NBA come from ESPN's public
site API (see espn.py). Division membership is fixed here, because ESPN's standings don't group
teams by division.
"""
from datetime import date as date_cls

NFL_DIVISIONS = {
    "AFC-E": ("AFC East", "AFC", ["BUF", "MIA", "NE", "NYJ"]),
    "AFC-N": ("AFC North", "AFC", ["BAL", "CIN", "CLE", "PIT"]),
    "AFC-S": ("AFC South", "AFC", ["HOU", "IND", "JAX", "TEN"]),
    "AFC-W": ("AFC West", "AFC", ["DEN", "KC", "LV", "LAC"]),
    "NFC-E": ("NFC East", "NFC", ["DAL", "NYG", "PHI", "WSH"]),
    "NFC-N": ("NFC North", "NFC", ["CHI", "DET", "GB", "MIN"]),
    "NFC-S": ("NFC South", "NFC", ["ATL", "CAR", "NO", "TB"]),
    "NFC-W": ("NFC West", "NFC", ["ARI", "LAR", "SF", "SEA"]),
}

NBA_DIVISIONS = {
    "EAST-ATL": ("Atlantic", "Eastern", ["BOS", "BKN", "NY", "PHI", "TOR"]),
    "EAST-CEN": ("Central", "Eastern", ["CHI", "CLE", "DET", "IND", "MIL"]),
    "EAST-SE": ("Southeast", "Eastern", ["ATL", "CHA", "MIA", "ORL", "WSH"]),
    "WEST-NW": ("Northwest", "Western", ["DEN", "MIN", "OKC", "POR", "UTAH"]),
    "WEST-PAC": ("Pacific", "Western", ["GS", "LAC", "LAL", "PHX", "SAC"]),
    "WEST-SW": ("Southwest", "Western", ["DAL", "HOU", "MEM", "NO", "SA"]),
}

SPORTS = {
    "nhl": {
        "name": "NHL",
        "pointsLabel": "pts",
        "winPoints": 2,         # standings points for a regulation or overtime win
        "otPoint": True,        # overtime and shootout losses earn one point
        "perDivision": 3,       # playoff spots guaranteed to each division's top teams
        "wildcards": 2,         # extra playoff spots per conference
        "seasonStart": (10, 1),
        "seasonEnd": (6, 30),
        "seasonKey": "span",    # keys look like 20262027
        "espn": None,
        "feeds": [("ESPN", "https://www.espn.com/espn/rss/nhl/news"),
                  ("CBS Sports", "https://www.cbssports.com/rss/headlines/nhl/")],
        "newsTeams": None,      # NHL team names live in news.py
    },
    "nfl": {
        "name": "NFL",
        "pointsLabel": "wins",
        "winPoints": 1,
        "otPoint": False,
        "perDivision": 1,       # the division winners
        "wildcards": 3,
        "seasonStart": (9, 1),
        "seasonEnd": (2, 15),
        "seasonKey": "single",  # keys look like 2026
        "espn": "football/nfl",
        "espnYearOffset": 0,    # ESPN names an NFL season by its start year
        "divisions": NFL_DIVISIONS,
        "feeds": [("ESPN", "https://www.espn.com/espn/rss/nfl/news"),
                  ("CBS Sports", "https://www.cbssports.com/rss/headlines/nfl/")],
        "newsTeams": {
            "ARI": ["Cardinals"], "ATL": ["Falcons"], "BAL": ["Ravens"], "BUF": ["Bills"],
            "CAR": ["Panthers"], "CHI": ["Bears"], "CIN": ["Bengals"], "CLE": ["Browns"],
            "DAL": ["Cowboys"], "DEN": ["Broncos"], "DET": ["Lions"], "GB": ["Packers"],
            "HOU": ["Texans"], "IND": ["Colts"], "JAX": ["Jaguars"], "KC": ["Chiefs"],
            "LAC": ["Chargers"], "LAR": ["Rams"], "LV": ["Raiders"], "MIA": ["Dolphins"],
            "MIN": ["Vikings"], "NE": ["Patriots"], "NO": ["Saints"], "NYG": ["Giants"],
            "NYJ": ["Jets"], "PHI": ["Eagles"], "PIT": ["Steelers"], "SEA": ["Seahawks"],
            "SF": ["49ers"], "TB": ["Buccaneers"], "TEN": ["Titans"], "WSH": ["Commanders"],
        },
    },
    "nba": {
        "name": "NBA",
        "pointsLabel": "wins",
        "winPoints": 1,
        "otPoint": False,
        "perDivision": 0,       # no divisional guarantee; the top 6 per conference get in directly
        "wildcards": 6,
        "seasonStart": (10, 1),
        "seasonEnd": (6, 30),
        "seasonKey": "span",
        "espn": "basketball/nba",
        "espnYearOffset": 1,    # ESPN names an NBA season by the year it ends (2027 = 2026-27)
        "divisions": NBA_DIVISIONS,
        "feeds": [("ESPN", "https://www.espn.com/espn/rss/nba/news"),
                  ("CBS Sports", "https://www.cbssports.com/rss/headlines/nba/")],
        "newsTeams": {
            "ATL": ["Hawks"], "BKN": ["Nets"], "BOS": ["Celtics"], "CHA": ["Hornets"],
            "CHI": ["Bulls"], "CLE": ["Cavaliers"], "DAL": ["Mavericks"], "DEN": ["Nuggets"],
            "DET": ["Pistons"], "GS": ["Warriors"], "HOU": ["Rockets"], "IND": ["Pacers"],
            "LAC": ["Clippers"], "LAL": ["Lakers"], "MEM": ["Grizzlies"], "MIA": ["Heat"],
            "MIL": ["Bucks"], "MIN": ["Timberwolves"], "NO": ["Pelicans"], "NY": ["Knicks"],
            "OKC": ["Thunder"], "ORL": ["Magic"], "PHI": ["76ers"], "PHX": ["Suns"],
            "POR": ["Trail Blazers"], "SA": ["Spurs"], "SAC": ["Kings"], "TOR": ["Raptors"],
            "UTAH": ["Jazz"], "WSH": ["Wizards"],
        },
    },
}


def get(sport):
    if sport not in SPORTS:
        raise KeyError(sport)
    return SPORTS[sport]


def season_key(sport, year):
    """The season key for a season that starts in `year`."""
    if SPORTS[sport]["seasonKey"] == "span":
        return f"{year}{year + 1}"
    return str(year)


def season_year(sport, key):
    """The year a season starts in, from its key."""
    return int(key[:4])


def season_bounds(sport, key):
    """First and last day of a season, from its key."""
    cfg = SPORTS[sport]
    y = season_year(sport, key)
    (sm, sd), (em, ed) = cfg["seasonStart"], cfg["seasonEnd"]
    return date_cls(y, sm, sd), date_cls(y + 1, em, ed)


def current_season_key(sport, today):
    """The season open for picks today. From August the coming season counts as current."""
    year = today.year if today.month >= 8 else today.year - 1
    return season_key(sport, year)
