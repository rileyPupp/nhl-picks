"""Standings picks: periods (week, month, season), standings snapshots, and grading.

Pure logic only. Data fetching lives in server.py (NHL) and espn.py (NFL, NBA).

A pick ranks every team in a division. Each team scores POINTS_PER_PLACE when it finishes
in exactly the rank picked, judged on the standings snapshot for the period's last day.
"""
from datetime import date as date_cls, timedelta

import sports

PERIODS = ("week", "month", "season")
POINTS_PER_PLACE = 1


class PeriodError(ValueError):
    pass


def _next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def period_bounds(period, key, sport="nhl"):
    """First and last day of a period.

    Keys: week = the Monday it starts on (2026-11-09), month = YYYY-MM, season = the sport's season key.
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
        try:
            return sports.season_bounds(sport, key)
        except (TypeError, ValueError, KeyError):
            raise PeriodError("That isn't a season key for this sport.")
    raise PeriodError("Period must be week, month or season.")


def current_key(period, today, sport="nhl"):
    """The period that contains today."""
    if period == "week":
        return (today - timedelta(days=today.weekday())).isoformat()
    if period == "month":
        return today.strftime("%Y-%m")
    if period == "season":
        return sports.current_season_key(sport, today)
    raise PeriodError("Period must be week, month or season.")


def next_key(period, key, sport="nhl"):
    if period == "week":
        return (date_cls.fromisoformat(key) + timedelta(days=7)).isoformat()
    if period == "month":
        return _next_month(date_cls.fromisoformat(f"{key}-01")).strftime("%Y-%m")
    return sports.season_key(sport, sports.season_year(sport, key) + 1)


def prev_key(period, key, sport="nhl"):
    if period == "week":
        return (date_cls.fromisoformat(key) - timedelta(days=7)).isoformat()
    if period == "month":
        return (date_cls.fromisoformat(f"{key}-01") - timedelta(days=1)).strftime("%Y-%m")
    return sports.season_key(sport, sports.season_year(sport, key) - 1)


def default_key(period, today, sport="nhl"):
    """The period whose picks are still open: this one if it hasn't started, otherwise the next."""
    key = current_key(period, today, sport)
    start, _ = period_bounds(period, key, sport)
    return key if today < start else next_key(period, key, sport)


def describe(period, key, today, sport="nhl"):
    """Status of one period on a given day."""
    start, end = period_bounds(period, key, sport)
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
    """Flatten an NHL api-web /standings response into one dict per team."""
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
            "pct": None,
            "divisionRank": r.get("divisionSequence") or 0,
            "conferenceRank": r.get("conferenceSequence") or 0,
            "leagueRank": r.get("leagueSequence") or 0,
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
