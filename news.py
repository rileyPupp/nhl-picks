"""News aggregate: public sports RSS feeds, tagged by team and stored for the News tab.

A background thread refreshes every sport's feeds every REFRESH_SECONDS and writes new items to
the news_items table. Page requests only read that table, so they never wait on a feed, and
the last good copy keeps showing if a feed goes down.
"""
import html
import re
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import db
import sports

REFRESH_SECONDS = 15 * 60
KEEP_DAYS = 45
PER_FEED = 60
SUMMARY_CHARS = 400

# Nickname and city for each NHL team, matched case-sensitively as whole words.
NHL_TEAM_NAMES = {
    "ANA": ("Ducks", "Anaheim"), "BOS": ("Bruins", "Boston"), "BUF": ("Sabres", "Buffalo"),
    "CGY": ("Flames", "Calgary"), "CAR": ("Hurricanes", "Carolina"), "CHI": ("Blackhawks", "Chicago"),
    "COL": ("Avalanche", "Colorado"), "CBJ": ("Blue Jackets", "Columbus"), "DAL": ("Stars", "Dallas"),
    "DET": ("Red Wings", "Detroit"), "EDM": ("Oilers", "Edmonton"), "FLA": ("Panthers", "Florida"),
    "LAK": ("Kings", "Los Angeles"), "MIN": ("Wild", "Minnesota"), "MTL": ("Canadiens", "Montreal"),
    "NSH": ("Predators", "Nashville"), "NJD": ("Devils", "New Jersey"), "NYI": ("Islanders", "New York Islanders"),
    "NYR": ("Rangers", "New York Rangers"), "OTT": ("Senators", "Ottawa"), "PHI": ("Flyers", "Philadelphia"),
    "PIT": ("Penguins", "Pittsburgh"), "SJS": ("Sharks", "San Jose"), "SEA": ("Kraken", "Seattle"),
    "STL": ("Blues", "St. Louis"), "TBL": ("Lightning", "Tampa Bay"), "TOR": ("Maple Leafs", "Toronto"),
    "UTA": ("Mammoth", "Utah"), "VAN": ("Canucks", "Vancouver"), "VGK": ("Golden Knights", "Vegas"),
    "WSH": ("Capitals", "Washington"), "WPG": ("Jets", "Winnipeg"),
}


def _patterns(names_by_team):
    return [
        (abbrev, re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b"))
        for abbrev, names in names_by_team.items()
    ]


_TEAM_PATTERNS = {
    "nhl": _patterns(NHL_TEAM_NAMES),
    "nfl": _patterns(sports.SPORTS["nfl"]["newsTeams"]),
    "nba": _patterns(sports.SPORTS["nba"]["newsTeams"]),
}


def tag_teams(text, sport="nhl"):
    """Team abbreviations mentioned in the text, sorted."""
    return sorted(abbrev for abbrev, pattern in _TEAM_PATTERNS[sport] if pattern.search(text))


def _clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _iso(pub_date):
    try:
        return parsedate_to_datetime(pub_date).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def parse_feed(xml_bytes, source, sport="nhl"):
    """RSS 2.0 items as dicts. Items without a title or link are skipped."""
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        summary = _clean(item.findtext("description"))[:SUMMARY_CHARS]
        items.append({
            "link": link, "source": source, "title": title, "summary": summary,
            "published": _iso(item.findtext("pubDate")),
            "teams": tag_teams(f"{title} {summary}", sport),
        })
        if len(items) >= PER_FEED:
            break
    return items


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nhl-picks"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def refresh(sport="nhl"):
    """Pull one sport's feeds and store new items. Returns how many items were read."""
    read = 0
    for source, url in sports.get(sport)["feeds"]:
        try:
            items = parse_feed(_fetch(url), source, sport)
        except (OSError, ValueError, ET.ParseError) as e:
            print(f"news: {sport} {source} feed failed, keeping stored items ({e})", file=sys.stderr)
            continue
        fetched = datetime.now(timezone.utc).isoformat()
        rows = [(i["link"], sport, i["source"], i["title"], i["summary"], i["published"],
                 f",{','.join(i['teams'])}," if i["teams"] else ",", fetched) for i in items]
        with db.connect() as conn:
            conn.many("INSERT INTO news_items (link, sport, source, title, summary, published, teams, fetched) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (link) DO NOTHING", rows)
        read += len(items)
    return read


def refresh_all():
    for sport in sports.SPORTS:
        refresh(sport)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    with db.connect() as conn:
        conn.run("DELETE FROM news_items WHERE published < ?", (cutoff,))


def list_items(sport="nhl", team=None, limit=40):
    with db.connect() as conn:
        if team:
            rows = conn.all("SELECT * FROM news_items WHERE sport = ? AND teams LIKE ? "
                            "ORDER BY published DESC LIMIT ?", (sport, f"%,{team},%", limit))
        else:
            rows = conn.all("SELECT * FROM news_items WHERE sport = ? ORDER BY published DESC LIMIT ?",
                            (sport, limit))
    return [{
        "title": r["title"], "summary": r["summary"], "link": r["link"], "source": r["source"],
        "published": r["published"], "teams": [t for t in r["teams"].split(",") if t],
    } for r in rows]


def start_background_refresh():
    def loop():
        while True:
            try:
                refresh_all()
            except Exception as e:  # keep the thread alive whatever goes wrong
                print(f"news: refresh failed ({e})", file=sys.stderr)
            time.sleep(REFRESH_SECONDS)

    threading.Thread(target=loop, daemon=True, name="news-refresh").start()
