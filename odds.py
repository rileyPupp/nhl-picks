"""Playoff odds: a Monte Carlo simulation of the rest of the regular season, for any sport.

This is our own estimate, built from each sport's schedule and current standings. It is not a
sportsbook price. Each unplayed game is a weighted coin flip, where the weight comes from each
team's current win percentage (points percentage for the NHL). Home-ice advantage, injuries
and schedule strength are ignored.
"""
import math
import random

OT_LOSS_POINT_SHARE = 0.23  # NHL: share of losses that end in overtime or a shootout
SIMS = 2000
FINAL_STATES = {"FINAL", "OFF"}


def nhl_remaining_games(schedules):
    """schedules: {team: NHL club-schedule-season payload}. Unplayed regular-season games, each once."""
    seen = {}
    for payload in schedules.values():
        for g in payload.get("games", []):
            if g.get("gameType") != 2 or g.get("gameState") in FINAL_STATES:
                continue
            seen.setdefault(g["id"], (g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"]))
    return list(seen.values())


def _rating(row):
    """Strength on a log-odds scale. Uses row['pct'] when set, else points per game (NHL)."""
    pct = row.get("pct")
    if pct is None:
        pct = row["pts"] / (2 * row["gp"]) if row["gp"] else 0.5
    pct = min(max(pct, 0.05), 0.95)
    return math.log(pct / (1 - pct))


def _qualifiers(divisions, conferences, pts, tiebreak, per_division, wildcards):
    chosen = set()
    if per_division:
        for members in divisions.values():
            ranked = sorted(members, key=lambda i: (pts[i], tiebreak[i]), reverse=True)
            chosen.update(ranked[:per_division])
    for members in conferences.values():
        rest = sorted((i for i in members if i not in chosen), key=lambda i: (pts[i], tiebreak[i]), reverse=True)
        chosen.update(rest[:wildcards])
    return chosen


def playoff_odds(rows, games, *, win_points, ot_point, per_division, wildcards,
                 sims=SIMS, seed="playoff-odds"):
    """{team: percent} chance of making the playoffs.

    rows: standings rows with team, division, conference, pts and gp (and pct, optional).
    games: [(away, home)] still to be played.
    """
    if not rows:
        return {}
    rng = random.Random(seed)
    teams = [r["team"] for r in rows]
    index = {t: i for i, t in enumerate(teams)}
    rating = {r["team"]: _rating(r) for r in rows}

    sim_games = []
    for away, home in games:
        if away not in index or home not in index:
            continue
        p_home = 1 / (1 + math.exp(-(rating[home] - rating[away])))
        sim_games.append((index[away], index[home], p_home))

    divisions, conferences = {}, {}
    for i, r in enumerate(rows):
        divisions.setdefault(r["division"], []).append(i)
        conferences.setdefault(r["conference"], []).append(i)

    base = [r["pts"] for r in rows]
    counts = [0] * len(teams)
    for _ in range(sims):
        pts = base[:]
        for away, home, p_home in sim_games:
            if rng.random() < p_home:
                winner, loser = home, away
            else:
                winner, loser = away, home
            pts[winner] += win_points
            if ot_point and rng.random() < OT_LOSS_POINT_SHARE:
                pts[loser] += 1
        tiebreak = [rng.random() for _ in teams]
        for i in _qualifiers(divisions, conferences, pts, tiebreak, per_division, wildcards):
            counts[i] += 1
    return {t: round(100 * counts[i] / sims, 1) for i, t in enumerate(teams)}
