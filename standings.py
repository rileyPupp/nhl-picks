"""Standings picks: NHL periods (week, month, season), standings snapshots, and grading.

Pure logic only. The NHL fetches live in server.py next to the other NHL calls, and the
database work sits beside the other pick code there too.

A pick names one team per division. It is correct when that team finishes in the top
TOP_N of its division at the end of the period, judged on the standings snapshot for the
period's last day.
"""
from datetime import date as date_cls, timedelta

PERIODS = ("week", "month", "season")
POINTS_PER_PLACE = 1      # one point for each team placed at its exact final division rank
SEASON_START = (10, 1)    # a season's picks lock on Oct 1 of its first calendar year
SEASON_END = (6, 30)      # a season is graded once the Stanley Cup playoffs are over


class PeriodError(ValueError):
    pass


def _season_years(key):
    """'20262027' -> (2026, 2027)."""
    if not isinstance(key, str) or len(key) != 8 or not key.isdigit():
        raise PeriodError("A season looks like 20262027.")
    y1, y2 = int(key[:4]), int(key[4:])
    if y2 != y1 + 1:
        raise PeriodError("A season looks like 20262027.")
    return y1, y2


def period_bounds(period, key):
    """First and last day of a period.

    Keys: week = the Monday it starts on (2026-11-09), month = YYYY-MM, season = 20262027.
    """
    if period == "week":
        try:
            start = date_cls.fromisoformat(key)
        except (TypeError, ValueError):
            raise PeriodError("A week key is its Monday, like 2026-11-09.")
        if start.weekday() != 0:
            raise PeriodError("A week starts on a Monday.")
        return start, start + timedelta(days=6)
    if period == "month":
        try:
            start = date_cls.fromisoformat(f"{key}-01")
        except (TypeError, ValueError):
            raise PeriodError("A month looks like 2026-11.")
        return start, _next_month(start) - timedelta(days=1)
    if period == "season":
        y1, y2 = _season_years(key)
        return date_cls(y1, *SEASON_START), date_cls(y2, *SEASON_END)
    raise PeriodError("Period must be week, month or season.")


def _next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def current_key(period, today):
    """The period that contains today."""
    if period == "week":
        return (today - timedelta(days=today.weekday())).isoformat()
    if period == "month":
        return today.strftime("%Y-%m")
    if period == "season":
        # From August the coming season is the one open for picks.
        first_year = today.year if today.month >= 8 else today.year - 1
        return f"{first_year}{first_year + 1}"
    raise PeriodError("Period must be week, month or season.")


def next_key(period, key):
    if period == "week":
        return (date_cls.fromisoformat(key) + timedelta(days=7)).isoformat()
    if period == "month":
        return _next_month(date_cls.fromisoformat(f"{key}-01")).strftime("%Y-%m")
    y1, _ = _season_years(key)
    return f"{y1 + 1}{y1 + 2}"


def prev_key(period, key):
    if period == "week":
        return (date_cls.fromisoformat(key) - timedelta(days=7)).isoformat()
    if period == "month":
        return (date_cls.fromisoformat(f"{key}-01") - timedelta(days=1)).strftime("%Y-%m")
    y1, _ = _season_years(key)
    return f"{y1 - 1}{y1}"


def default_key(period, today):
    """The period whose picks are still open: this one if it hasn't started, otherwise the next."""
    key = current_key(period, today)
    start, _ = period_bounds(period, key)
    return key if today < start else next_key(period, key)


def describe(period, key, today):
    """Status of one period on a given day."""
    start, end = period_bounds(period, key)
    return {
        "type": period,
        "key": key,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "pickOpen": today < start,
        "locked": today >= start,
        "final": today > end,
    }


def snapshot_date(status):
    """Which standings to show: the final day for a finished period, otherwise live ('now')."""
    return status["end"] if status["final"] else "now"


def _loc(value):
    """NHL API localized fields come as {'default': ..., 'fr': ...}."""
    if isinstance(value, dict):
        return value.get("default")
    return value


def parse_standings(payload):
    """Flatten an api-web /standings response into one dict per team."""
    rows = []
    for r in payload.get("standings", []):
        team = _loc(r.get("teamAbbrev"))
        if not team:
            continue
        rows.append({
            "team": team,
            "name": _loc(r.get("teamName")) or team,
            "place": _loc(r.get("placeName")) or team,
            "logo": r.get("teamLogo"),
            "division": r.get("divisionAbbrev"),
            "divisionName": r.get("divisionName"),
            "conference": r.get("conferenceAbbrev"),
            "conferenceName": r.get("conferenceName"),
            "gp": r.get("gamesPlayed") or 0,
            "w": r.get("wins") or 0,
            "l": r.get("losses") or 0,
            "otl": r.get("otLosses") or 0,
            "pts": r.get("points") or 0,
            "divisionRank": r.get("divisionSequence") or 0,
            "leagueRank": r.get("leagueSequence") or 0,
            "conferenceRank": r.get("conferenceSequence") or 0,
            "streak": f'{r.get("streakCode") or ""}{r.get("streakCount") or ""}',
        })
    return rows


def group_divisions(rows):
    """[{abbrev, name, conference, teams}] in conference order, teams sorted by division rank."""
    divisions = {}
    for row in rows:
        div = divisions.setdefault(row["division"], {
            "abbrev": row["division"], "name": row["divisionName"],
            "conference": row["conferenceName"], "teams": [],
        })
        div["teams"].append(row)
    for div in divisions.values():
        div["teams"].sort(key=lambda t: t["divisionRank"])
    return sorted(divisions.values(), key=lambda d: (d["conference"] or "", d["name"] or ""))


def score_order(predicted, final):
    """Points for one division order. predicted and final are {team: divisionRank}.

    A team scores POINTS_PER_PLACE when its predicted rank is exactly its final rank.
    """
    return sum(POINTS_PER_PLACE for team, rank in predicted.items() if final.get(team) == rank)
