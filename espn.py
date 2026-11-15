"""NFL and NBA data from ESPN's public site API (no key needed).

Everything comes from each team's schedule. Standings, playoff odds and grading all read the
same game list, so they always agree with each other.
"""
import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta

import sports

BASE = "https://site.api.espn.com/apis/site/v2/sports"
CACHE_SECONDS = 900
_cache = {}
_lock = threading.Lock()


def _get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "nhl-picks"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _cached(key, loader):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = loader()
    with _lock:
        _cache[key] = (now, value)
    return value


def teams(sport):
    """{abbrev: {team, id, name, short, logo}} for every team in the sport."""
    def load():
        data = _get(f"/{sports.get(sport)['espn']}/teams")
        out = {}
        for entry in data["sports"][0]["leagues"][0]["teams"]:
            t = entry["team"]
            logos = t.get("logos") or [{}]
            out[t["abbreviation"]] = {
                "team": t["abbreviation"], "id": t["id"], "name": t["displayName"],
                "short": t.get("shortDisplayName") or t["displayName"], "logo": logos[0].get("href"),
            }
        return out
    return _cached(("teams", sport), load)


def _score(value):
    if isinstance(value, dict):
        value = value.get("value")
    return None if value is None else float(value)


def _local_date(iso):
    """The game's date in US Eastern time (a fixed -5 hours is close enough for grouping days)."""
    when = datetime.strptime(iso, "%Y-%m-%dT%H:%MZ") - timedelta(hours=5)
    return when.date().isoformat()


def games(sport, year):
    """Every regular-season game in a season (given by its start year), each once."""
    cfg = sports.get(sport)
    espn_year = year + cfg.get("espnYearOffset", 0)

    def load():
        seen = {}
        for meta in teams(sport).values():
            data = _get(f"/{cfg['espn']}/teams/{meta['id']}/schedule?season={espn_year}")
            for ev in data.get("events", []):
                if (ev.get("seasonType") or {}).get("type") != 2:
                    continue
                comp = ev["competitions"][0]
                status = comp["status"]["type"]
                home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
                away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
                seen[ev["id"]] = {
                    "id": ev["id"], "localDate": _local_date(ev["date"]),
                    "completed": bool(status.get("completed")), "state": status.get("state"),
                    "home": home["team"]["abbreviation"], "away": away["team"]["abbreviation"],
                    "homeScore": _score(home.get("score")), "awayScore": _score(away.get("score")),
                }
        return sorted(seen.values(), key=lambda g: (g["localDate"], g["id"]))
    return _cached(("games", sport, espn_year), load)


def _streak(results):
    """results: outcomes oldest first, 'W' / 'L' / 'T'. Returns e.g. 'W3' or 'L1'."""
    if not results:
        return ""
    last, count = results[-1], 0
    for r in reversed(results):
        if r != last:
            break
        count += 1
    return f"{last}{count}"


def standings_rows(sport, year, as_of):
    """Team rows shaped like the NHL standings, using games completed on or before as_of ('YYYY-MM-DD')."""
    cfg = sports.get(sport)
    meta = teams(sport)
    record = {t: {"w": 0, "l": 0, "t": 0, "results": []} for t in meta}
    for g in games(sport, year):
        if not g["completed"] or g["localDate"] > as_of or g["homeScore"] is None:
            continue
        if g["homeScore"] == g["awayScore"]:
            record[g["home"]]["t"] += 1
            record[g["away"]]["t"] += 1
            record[g["home"]]["results"].append("T")
            record[g["away"]]["results"].append("T")
            continue
        winner, loser = (g["home"], g["away"]) if g["homeScore"] > g["awayScore"] else (g["away"], g["home"])
        record[winner]["w"] += 1
        record[winner]["results"].append("W")
        record[loser]["l"] += 1
        record[loser]["results"].append("L")

    division_of, conference_of = {}, {}
    for code, (name, conference, members) in cfg["divisions"].items():
        for abbrev in members:
            division_of[abbrev] = (code, name, conference)

    rows = []
    for abbrev, info in meta.items():
        rec = record.get(abbrev) or {"w": 0, "l": 0, "t": 0, "results": []}
        gp = rec["w"] + rec["l"] + rec["t"]
        pct = (rec["w"] + 0.5 * rec["t"]) / gp if gp else 0.0
        code, division_name, conference = division_of.get(abbrev, ("", "", ""))
        rows.append({
            "team": abbrev, "name": info["name"], "place": info["short"], "logo": info["logo"],
            "division": code, "divisionName": division_name,
            "conference": conference, "conferenceName": conference,
            "gp": gp, "w": rec["w"], "l": rec["l"], "otl": 0, "t": rec["t"],
            "pts": rec["w"], "pct": round(pct, 4),
            "streak": _streak(rec["results"]),
        })

    def order(group):
        group.sort(key=lambda r: (-r["pct"], -r["w"], r["name"]))
        for i, r in enumerate(group, 1):
            yield i, r

    for r in rows:
        r["divisionRank"] = r["conferenceRank"] = r["leagueRank"] = 0
    for code in {r["division"] for r in rows if r["division"]}:
        for i, r in order([r for r in rows if r["division"] == code]):
            r["divisionRank"] = i
    for conf in {r["conference"] for r in rows if r["conference"]}:
        for i, r in order([r for r in rows if r["conference"] == conf]):
            r["conferenceRank"] = i
    for i, r in order(list(rows)):
        r["leagueRank"] = i
    return rows
