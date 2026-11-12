"""NHL nightly picks - a small multi-player web app. Python standard library only."""
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import date as date_cls, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_FILE = os.environ.get("NHL_PICKS_DB", os.path.join(BASE_DIR, "nhl-picks.db"))
LEGACY_PICKS_FILE = os.path.join(BASE_DIR, "picks.json")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
SECURE_COOKIES = os.environ.get("SECURE_COOKIES") == "1"  # set when served over HTTPS

NHL = "https://api-web.nhle.com/v1"
CACHE_SECONDS = 60
SLOW_CACHE_SECONDS = 3600  # rosters and season stats

LOCKED_STATES = {"LIVE", "CRIT", "FINAL", "OFF"}
FINAL_STATES = {"FINAL", "OFF"}

# Scoring
WINNER_POINTS, EXACT_SCORE_BONUS = 1, 3
GAME_MAX = WINNER_POINTS + EXACT_SCORE_BONUS
POINT_CALL_POINTS, EXACT_COUNT_BONUS = 1, 2
PLAYER_MAX = POINT_CALL_POINTS + EXACT_COUNT_BONUS
PLAYER_GUESSES = (0, 1, 2, 3)  # 3 means "3 or more"
MIN_NIGHTS = 5  # nights needed to qualify for the leaderboard
MIN_GAMES_PLAYED = 3  # a skater needs this many games this season to be drawn

SESSION_COOKIE = "session"
SESSION_DAYS = 365
MAX_LOGIN_FAILS, LOCKOUT_SECONDS = 5, 300

_cache = {}
_cache_lock = threading.Lock()
_db_lock = threading.Lock()
_login_fails = {}
_login_lock = threading.Lock()


class AppError(Exception):
    status = 400


class BadRequest(AppError):
    status = 400


class Unauthorized(AppError):
    status = 401


class NotFound(AppError):
    status = 404


class Conflict(AppError):
    status = 409


class TooManyAttempts(AppError):
    status = 429


# ---------- database ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pin_salt TEXT NOT NULL,
    pin_hash TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS picks (
    user_id INTEGER NOT NULL REFERENCES users(id),
    game_id TEXT NOT NULL,
    date TEXT NOT NULL,
    winner TEXT NOT NULL,
    away_score INTEGER,
    home_score INTEGER,
    PRIMARY KEY (user_id, game_id)
);
CREATE TABLE IF NOT EXISTS slates (
    date TEXT NOT NULL,
    slot INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    position TEXT,
    team TEXT NOT NULL,
    opponent TEXT NOT NULL,
    game_id TEXT NOT NULL,
    headshot TEXT,
    tier TEXT NOT NULL,
    season_gp INTEGER NOT NULL,
    season_points INTEGER NOT NULL,
    PRIMARY KEY (date, slot)
);
CREATE TABLE IF NOT EXISTS player_picks (
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    player_id INTEGER NOT NULL,
    guess INTEGER NOT NULL,
    PRIMARY KEY (user_id, date, player_id)
);
"""


def db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)


# ---------- accounts ----------

NAME_RE = re.compile(r"^[A-Za-z0-9 _.-]{2,20}$")
PIN_RE = re.compile(r"^\d{4,6}$")


def _hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()


def _clean_credentials(body):
    name = str(body.get("name") or "").strip()
    pin = str(body.get("pin") or "")
    if not NAME_RE.match(name):
        raise BadRequest("Names are 2-20 letters, numbers, spaces, dots, dashes or underscores.")
    if not PIN_RE.match(pin):
        raise BadRequest("PINs are 4 to 6 digits.")
    return name, pin


def _new_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    conn.execute("INSERT INTO sessions (token, user_id, expires) VALUES (?, ?, ?)",
                 (token, user_id, time.time() + SESSION_DAYS * 86400))
    return token


def _import_legacy_picks(conn, user_id):
    """The first account to register inherits picks saved by the old single-player version."""
    if not os.path.exists(LEGACY_PICKS_FILE):
        return 0
    with open(LEGACY_PICKS_FILE, encoding="utf-8") as f:
        legacy = json.load(f)
    rows = [(user_id, str(gid), p["date"], p["winner"], p.get("awayScore"), p.get("homeScore"))
            for gid, p in legacy.items() if p.get("date") and p.get("winner")]
    conn.executemany("INSERT OR IGNORE INTO picks VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def register(body):
    name, pin = _clean_credentials(body)
    salt = secrets.token_hex(16)
    with _db_lock, db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
            raise Conflict("That name is taken. Pick another, or sign in.")
        first_user = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        cur = conn.execute("INSERT INTO users (name, pin_salt, pin_hash, created) VALUES (?, ?, ?, ?)",
                           (name, salt, _hash_pin(pin, salt), datetime.now(timezone.utc).isoformat()))
        user_id = cur.lastrowid
        imported = _import_legacy_picks(conn, user_id) if first_user else 0
        token = _new_session(conn, user_id)
    if imported:
        os.replace(LEGACY_PICKS_FILE, LEGACY_PICKS_FILE + ".imported")
    return {"id": user_id, "name": name}, token


def login(body):
    name, pin = _clean_credentials(body)
    key = name.lower()
    with _login_lock:
        fails, until = _login_fails.get(key, (0, 0))
        if until > time.time():
            raise TooManyAttempts("Too many wrong PINs. Try again in a few minutes.")
    with _db_lock, db() as conn:
        row = conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
        ok = row is not None and hmac.compare_digest(row["pin_hash"], _hash_pin(pin, row["pin_salt"]))
        if ok:
            token = _new_session(conn, row["id"])
    with _login_lock:
        if ok:
            _login_fails.pop(key, None)
        else:
            fails += 1
            _login_fails[key] = (0, time.time() + LOCKOUT_SECONDS) if fails >= MAX_LOGIN_FAILS else (fails, 0)
    if not ok:
        raise Unauthorized("Wrong name or PIN.")
    return {"id": row["id"], "name": row["name"]}, token


def user_for_token(token):
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT users.id, users.name FROM sessions JOIN users ON users.id = sessions.user_id "
            "WHERE token = ? AND expires > ?", (token, time.time())).fetchone()
    return dict(row) if row else None


def logout(token):
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---------- NHL data ----------

def _get_json(path):
    req = urllib.request.Request(NHL + path, headers={"User-Agent": "nhl-picks"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _cached(key, loader, ttl=CACHE_SECONDS, done=lambda value: False):
    """Memoize loader(); results for which done() is true (finished games) never expire."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (hit[2] or now - hit[0] < ttl):
            return hit[1]
    value = loader()
    with _cache_lock:
        _cache[key] = (now, value, done(value))
    return value


def _team(t):
    return {
        "abbrev": t.get("abbrev"),
        "name": (t.get("placeName") or {}).get("default") or t.get("abbrev"),
        "logo": t.get("logo"),
        "score": t.get("score"),
    }


def trim_game(g):
    return {
        "id": g["id"],
        "startTimeUTC": g.get("startTimeUTC"),
        "state": g.get("gameState"),
        "periodType": (g.get("periodDescriptor") or {}).get("periodType"),
        "away": _team(g["awayTeam"]),
        "home": _team(g["homeTeam"]),
    }


def fetch_day(day):
    """Games for a YYYY-MM-DD date."""
    def load():
        return [trim_game(g) for g in _get_json(f"/score/{day}").get("games", [])]
    return _cached(("day", day), load,
                   done=lambda games: bool(games) and all(g["state"] in FINAL_STATES for g in games))


def fetch_boxscore(game_id):
    """{state, players: {playerId: {goals, assists, points, toi}}} for one game."""
    def load():
        data = _get_json(f"/gamecenter/{game_id}/boxscore")
        players = {}
        for side in (data.get("playerByGameStats") or {}).values():
            for group in ("forwards", "defense"):
                for p in side.get(group, []):
                    players[p["playerId"]] = {k: p.get(k) for k in ("goals", "assists", "points", "toi")}
        return {"state": data.get("gameState"), "players": players}
    return _cached(("box", str(game_id)), load, done=lambda b: b["state"] in FINAL_STATES)


def fetch_team_skaters(team):
    return _cached(("skaters", team), lambda: _get_json(f"/club-stats/{team}/now").get("skaters", []),
                   ttl=SLOW_CACHE_SECONDS)


def fetch_roster_ids(team):
    def load():
        data = _get_json(f"/roster/{team}/current")
        return {p["id"] for group in ("forwards", "defensemen") for p in data.get(group, [])}
    return _cached(("roster", team), load, ttl=SLOW_CACHE_SECONDS)


def is_locked(game):
    """Picks lock at puck drop: once the game is live/final or its start time has passed."""
    if game["state"] in LOCKED_STATES:
        return True
    try:
        start = datetime.strptime(game["startTimeUTC"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) >= start


def _parse_day(value):
    try:
        date_cls.fromisoformat(value)
    except (TypeError, ValueError):
        raise BadRequest("Date must be YYYY-MM-DD.")
    return value


# ---------- winner picks ----------

def grade(pick, game):
    """Points for one winner pick once the game is final; None while still in play."""
    if not pick or game["state"] not in FINAL_STATES:
        return None
    a, h = game["away"]["score"], game["home"]["score"]
    if a is None or h is None:
        return None
    actual_winner = game["home"]["abbrev"] if h > a else game["away"]["abbrev"]
    correct = pick.get("winner") == actual_winner
    exact = correct and pick.get("awayScore") == a and pick.get("homeScore") == h
    return {
        "correctWinner": correct,
        "exactScore": exact,
        "points": (WINNER_POINTS if correct else 0) + (EXACT_SCORE_BONUS if exact else 0),
    }


def _pick_row(row):
    return {"date": row["date"], "winner": row["winner"],
            "awayScore": row["away_score"], "homeScore": row["home_score"]}


def _score(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BadRequest("Scores must be whole numbers, 0 or more.")
    return value


def save_pick(user, body):
    day = _parse_day(body.get("date"))
    game_id = str(body.get("gameId", ""))
    game = next((g for g in fetch_day(day) if str(g["id"]) == game_id), None)
    if game is None:
        raise NotFound("That game is not on this date.")
    if is_locked(game):
        raise Conflict("This game has started, so picks are locked.")

    winner = body.get("winner")
    with _db_lock, db() as conn:
        if not winner:  # clearing a pick
            conn.execute("DELETE FROM picks WHERE user_id = ? AND game_id = ?", (user["id"], game_id))
            return None
        if winner not in (game["away"]["abbrev"], game["home"]["abbrev"]):
            raise BadRequest("Pick one of the two teams.")
        away, home = _score(body.get("awayScore")), _score(body.get("homeScore"))
        if (away is None) != (home is None):
            raise BadRequest("Enter both scores, or neither.")
        if away is not None:
            if away == home:
                raise BadRequest("A predicted score can't be a tie.")
            predicted = game["home"]["abbrev"] if home > away else game["away"]["abbrev"]
            if predicted != winner:
                raise BadRequest("That score doesn't match the team you picked.")
        conn.execute("INSERT OR REPLACE INTO picks VALUES (?, ?, ?, ?, ?, ?)",
                     (user["id"], game_id, day, winner, away, home))
    return {"date": day, "winner": winner, "awayScore": away, "homeScore": home}


def api_games(user, query):
    day = _parse_day(query.get("date", [date_cls.today().isoformat()])[0])
    with db() as conn:
        rows = conn.execute("SELECT * FROM picks WHERE user_id = ? AND date = ?", (user["id"], day)).fetchall()
    picks = {r["game_id"]: _pick_row(r) for r in rows}
    games = []
    for g in fetch_day(day):
        pick = picks.get(str(g["id"]))
        games.append({**g, "locked": is_locked(g), "pick": pick, "result": grade(pick, g)})
    return {"date": day, "games": games}


# ---------- player points pick'em ----------

def _slate_rows(day):
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM slates WHERE date = ? ORDER BY slot", (day,))]


def build_slate(day):
    """Draw tonight's three skaters: one star, one middle-of-the-lineup, one depth player.

    Everyone gets the same three: the draw is seeded by the date and stored the first time
    anyone opens the night, so later roster or stat changes can't reshuffle it.
    """
    pool = []
    for g in fetch_day(day):
        for side, other in (("away", "home"), ("home", "away")):
            team, opponent = g[side]["abbrev"], g[other]["abbrev"]
            roster = fetch_roster_ids(team)
            for s in fetch_team_skaters(team):
                gp = s.get("gamesPlayed") or 0
                if s["playerId"] not in roster or gp < MIN_GAMES_PLAYED:
                    continue
                pool.append({
                    "player_id": s["playerId"],
                    "name": f'{s["firstName"]["default"]} {s["lastName"]["default"]}',
                    "position": s.get("positionCode"),
                    "team": team, "opponent": opponent, "game_id": str(g["id"]),
                    "headshot": s.get("headshot"),
                    "season_gp": gp, "season_points": s.get("points") or 0,
                })
    if len(pool) < 3:
        return []
    pool.sort(key=lambda p: (-p["season_points"] / p["season_gp"], p["player_id"]))
    third = len(pool) // 3
    tiers = [("Star", pool[:third]), ("Middle", pool[third:2 * third]), ("Depth", pool[2 * third:])]
    rng = random.Random(f"nhl-picks-slate-{day}")
    chosen = []
    for tier, players in tiers:
        used_teams = {c["team"] for c in chosen}
        options = [p for p in players if p["team"] not in used_teams] or players
        chosen.append({**rng.choice(options), "tier": tier})
    return chosen


def get_slate(day):
    rows = _slate_rows(day)
    if rows:
        return rows
    today = date_cls.today()
    target = date_cls.fromisoformat(day)
    if not today - timedelta(days=1) <= target <= today + timedelta(days=1):
        return []  # only draw near game day, so injuries and call-ups are reflected
    chosen = build_slate(day)
    with _db_lock, db() as conn:
        if conn.execute("SELECT 1 FROM slates WHERE date = ?", (day,)).fetchone():
            return _slate_rows(day)  # another request drew it first
        conn.executemany(
            "INSERT INTO slates VALUES (:date, :slot, :player_id, :name, :position, :team, :opponent, "
            ":game_id, :headshot, :tier, :season_gp, :season_points)",
            [{**p, "date": day, "slot": i} for i, p in enumerate(chosen)])
    return _slate_rows(day)


def player_outcome(slot, games_by_id):
    """(state, actualPoints) where state is pending, live, final or void (didn't play)."""
    game = games_by_id.get(slot["game_id"])
    if game is None or game["state"] not in LOCKED_STATES:
        return "pending", None
    box = fetch_boxscore(slot["game_id"])
    stats = box["players"].get(slot["player_id"])
    played = stats is not None and stats.get("toi") not in (None, "", "00:00")
    if game["state"] in FINAL_STATES and box["state"] in FINAL_STATES:
        return ("final", stats["points"] or 0) if played else ("void", None)
    return "live", (stats["points"] or 0) if stats else 0


def grade_player(guess, actual):
    got_point, called_point = actual > 0, guess > 0
    correct = got_point == called_point
    exact = min(actual, 3) == guess
    return {"correctCall": correct, "exactCount": exact,
            "points": (POINT_CALL_POINTS if correct else 0) + (EXACT_COUNT_BONUS if exact else 0)}


def api_players(user, query):
    day = _parse_day(query.get("date", [date_cls.today().isoformat()])[0])
    slate = get_slate(day)
    if not slate:
        message = ("Tonight's players are drawn the day before game day. Check back closer to puck drop."
                   if date_cls.fromisoformat(day) > date_cls.today()
                   else "No player pick'em for this night.")
        return {"date": day, "players": [], "message": message}
    games = {str(g["id"]): g for g in fetch_day(day)}
    with db() as conn:
        guesses = {r["player_id"]: r["guess"] for r in conn.execute(
            "SELECT player_id, guess FROM player_picks WHERE user_id = ? AND date = ?", (user["id"], day))}
    players = []
    for s in slate:
        game = games.get(s["game_id"])
        state, actual = player_outcome(s, games)
        guess = guesses.get(s["player_id"])
        players.append({
            "playerId": s["player_id"], "name": s["name"], "position": s["position"],
            "team": s["team"], "opponent": s["opponent"], "headshot": s["headshot"], "tier": s["tier"],
            "seasonGP": s["season_gp"], "seasonPoints": s["season_points"],
            "startTimeUTC": game and game["startTimeUTC"],
            "locked": game is None or is_locked(game),
            "state": state, "actual": actual, "guess": guess,
            "result": grade_player(guess, actual) if state == "final" and guess is not None else None,
        })
    return {"date": day, "players": players}


def save_player_pick(user, body):
    day = _parse_day(body.get("date"))
    player_id = body.get("playerId")
    slot = next((s for s in _slate_rows(day) if s["player_id"] == player_id), None)
    if slot is None:
        raise NotFound("That player isn't in this night's pick'em.")
    game = next((g for g in fetch_day(day) if str(g["id"]) == slot["game_id"]), None)
    if game is None or is_locked(game):
        raise Conflict("This player's game has started, so the pick is locked.")
    guess = body.get("guess")
    with _db_lock, db() as conn:
        if guess is None:
            conn.execute("DELETE FROM player_picks WHERE user_id = ? AND date = ? AND player_id = ?",
                         (user["id"], day, player_id))
            return None
        if guess not in PLAYER_GUESSES or isinstance(guess, bool):
            raise BadRequest("Guess 0, 1, 2 or 3+ points.")
        conn.execute("INSERT OR REPLACE INTO player_picks VALUES (?, ?, ?, ?)",
                     (user["id"], day, player_id, guess))
    return guess


# ---------- scoring & leaderboard ----------

def _pct(earned, possible):
    return round(100 * earned / possible, 1) if possible else None


def leaderboard(user):
    """Pick % = points earned / points possible, over the nights each player played.

    On a night you play, every finished game (and every drawn skater who played) counts,
    so skipping the hard ones doesn't help. Nights you sit out don't count against you.
    """
    with db() as conn:
        users = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM users")}
        picks = conn.execute("SELECT * FROM picks").fetchall()
        guesses = conn.execute("SELECT * FROM player_picks").fetchall()

    nights = {}  # (user_id, date) -> tallies

    def night(uid, day):
        return nights.setdefault((uid, day), {
            "date": day, "gameEarned": 0, "gamePossible": 0, "winners": 0, "exact": 0, "picked": set(),
            "playerEarned": 0, "playerPossible": 0, "playerExact": 0, "guessed": set()})

    for p in picks:
        night(p["user_id"], p["date"])["picked"].add(p["game_id"])
    for p in guesses:
        night(p["user_id"], p["date"])["guessed"].add(p["player_id"])

    picks_by = {(p["user_id"], p["game_id"]): _pick_row(p) for p in picks}
    guesses_by = {(p["user_id"], p["date"], p["player_id"]): p["guess"] for p in guesses}
    for day in sorted({d for _, d in nights}):
        games = fetch_day(day)
        final_games = [g for g in games if g["state"] in FINAL_STATES
                       and g["away"]["score"] is not None and g["home"]["score"] is not None]
        slate = [(s, player_outcome(s, {str(g["id"]): g for g in games})) for s in _slate_rows(day)]
        for (uid, d), n in nights.items():
            if d != day:
                continue
            if n["picked"]:
                for g in final_games:
                    n["gamePossible"] += GAME_MAX
                    r = grade(picks_by.get((uid, str(g["id"]))), g)
                    if r:
                        n["gameEarned"] += r["points"]
                        n["winners"] += r["correctWinner"]
                        n["exact"] += r["exactScore"]
            if n["guessed"]:
                for s, (state, actual) in slate:
                    if state != "final":
                        continue
                    n["playerPossible"] += PLAYER_MAX
                    guess = guesses_by.get((uid, day, s["player_id"]))
                    if guess is not None:
                        r = grade_player(guess, actual)
                        n["playerEarned"] += r["points"]
                        n["playerExact"] += r["exactCount"]

    rows, mine = [], []
    for uid, name in users.items():
        own = [n for (u, _), n in nights.items() if u == uid and n["gamePossible"] + n["playerPossible"]]
        t = {k: sum(n[k] for n in own) for k in
             ("gameEarned", "gamePossible", "winners", "exact", "playerEarned", "playerPossible", "playerExact")}
        rows.append({
            "name": name, "me": uid == user["id"], "nights": len(own),
            "qualified": len(own) >= MIN_NIGHTS,
            "pickPct": _pct(t["gameEarned"] + t["playerEarned"], t["gamePossible"] + t["playerPossible"]),
            "gamePct": _pct(t["gameEarned"], t["gamePossible"]),
            "playerPct": _pct(t["playerEarned"], t["playerPossible"]),
            "points": t["gameEarned"] + t["playerEarned"],
            "winners": t["winners"], "exact": t["exact"], "playerExact": t["playerExact"],
        })
        if uid == user["id"]:
            for n in sorted(own, key=lambda n: n["date"], reverse=True):
                mine.append({
                    "date": n["date"],
                    "pickPct": _pct(n["gameEarned"] + n["playerEarned"], n["gamePossible"] + n["playerPossible"]),
                    "gamePoints": n["gameEarned"], "gamePossible": n["gamePossible"],
                    "winners": n["winners"], "exact": n["exact"],
                    "playerPoints": n["playerEarned"], "playerPossible": n["playerPossible"],
                })

    rows.sort(key=lambda r: (not r["qualified"], r["pickPct"] is None, -(r["pickPct"] or 0), -r["points"]))
    return {"minNights": MIN_NIGHTS, "rows": rows, "myNights": mine}


# ---------- HTTP ----------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def log_message(self, format, *args):
        pass  # keep the console quiet

    def session_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None

    def send_json(self, status, payload, cookie=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            token, max_age = cookie
            secure = "; Secure" if SECURE_COOKIES else ""
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                                           f"Max-Age={max_age}{secure}")
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, handler):
        try:
            result = handler()
            payload, cookie = result if isinstance(result, tuple) else (result, None)
            self.send_json(200, payload, cookie)
        except AppError as e:
            self.send_json(e.status, {"error": str(e)})
        except (OSError, ValueError):
            self.send_json(502, {"error": "Couldn't reach the NHL API. Check your connection and try again."})

    def _signed_in(self, fn):
        def run():
            user = user_for_token(self.session_token())
            if user is None:
                raise Unauthorized("Please sign in.")
            return fn(user)
        return run

    def do_GET(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        query = parse_qs(url.query)
        routes = {
            "/api/me": lambda: {"user": user_for_token(self.session_token())},
            "/api/games": self._signed_in(lambda u: api_games(u, query)),
            "/api/players": self._signed_in(lambda u: api_players(u, query)),
            "/api/leaderboard": self._signed_in(leaderboard),
        }
        route = routes.get(url.path)
        if route is None:
            return self.send_json(404, {"error": "Not found."})
        self._respond(route)

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)

        def body():
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise BadRequest("Invalid request.")
            if not isinstance(data, dict):
                raise BadRequest("Invalid request.")
            return data

        def signed_in_with_cookie(fn):
            def run():
                user, token = fn(body())
                return {"user": user}, (token, SESSION_DAYS * 86400)
            return run

        def do_logout():
            token = self.session_token()
            if token:
                logout(token)
            return {"user": None}, ("", 0)

        routes = {
            "/api/register": signed_in_with_cookie(register),
            "/api/login": signed_in_with_cookie(login),
            "/api/logout": do_logout,
            "/api/picks": self._signed_in(lambda u: {"pick": save_pick(u, body())}),
            "/api/player-picks": self._signed_in(lambda u: {"guess": save_player_pick(u, body())}),
        }
        route = routes.get(url.path)
        if route is None:
            return self.send_json(404, {"error": "Not found."})
        self._respond(route)


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    where = f"http://localhost:{PORT}" if HOST in ("127.0.0.1", "localhost") else f"http://<this computer's IP>:{PORT}"
    print(f"NHL Picks running at {where}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
