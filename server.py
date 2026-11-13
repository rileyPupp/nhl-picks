"""NHL nightly picks - a small multi-player web app. Python standard library only."""
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import threading
import time
import urllib.request
from datetime import date as date_cls, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import db
import news
import oauth
import standings
from db import Duplicate, placeholders

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
LEGACY_PICKS_FILE = os.path.join(BASE_DIR, "picks.json")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
SECURE_COOKIES = os.environ.get("SECURE_COOKIES") == "1"  # set when served over HTTPS
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://localhost:{PORT}")  # where OAuth sends people back

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
STATE_COOKIE = "oauth_state"
OAUTH_STATE_SECONDS = 600
SESSION_DAYS = 365
MAX_LOGIN_FAILS, LOCKOUT_SECONDS = 5, 300

_cache = {}
_cache_lock = threading.Lock()
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


# ---------- accounts ----------

NAME_RE = re.compile(r"^[A-Za-z0-9 _.-]{2,20}$")
PIN_RE = re.compile(r"^\d{4,6}$")


def _now():
    return datetime.now(timezone.utc).isoformat()


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
    conn.run("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    conn.run("INSERT INTO sessions (token, user_id, expires) VALUES (?, ?, ?)",
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
    conn.many("INSERT INTO picks (user_id, game_id, date, winner, away_score, home_score) "
              "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING", rows)
    return len(rows)


def pin_signup_allowed():
    """PIN accounts are for local play. Once Google/GitHub is set up, new players use those."""
    return not oauth.enabled() or os.environ.get("ALLOW_PIN_SIGNUP") == "1"


def _create_user(conn, name, salt="", pin_hash=""):
    """Insert a user (no PIN when salt/hash are empty). Returns (id, imported legacy pick count)."""
    first_user = conn.value("SELECT COUNT(*) AS n FROM users") == 0
    user_id = conn.value("INSERT INTO users (name, pin_salt, pin_hash, created) VALUES (?, ?, ?, ?) "
                         "RETURNING id", (name, salt, pin_hash, _now()))
    return user_id, (_import_legacy_picks(conn, user_id) if first_user else 0)


def _finish_import(imported):
    if imported and os.path.exists(LEGACY_PICKS_FILE):
        os.replace(LEGACY_PICKS_FILE, LEGACY_PICKS_FILE + ".imported")


def register(body):
    if not pin_signup_allowed():
        raise Conflict("New accounts sign up with Google or GitHub.")
    name, pin = _clean_credentials(body)
    salt = secrets.token_hex(16)
    try:
        with db.connect() as conn:
            user_id, imported = _create_user(conn, name, salt, _hash_pin(pin, salt))
            token = _new_session(conn, user_id)
    except Duplicate:
        raise Conflict("That name is taken. Pick another, or sign in.")
    _finish_import(imported)
    return {"id": user_id, "name": name}, token


def login(body):
    name, pin = _clean_credentials(body)
    key = name.lower()
    with _login_lock:
        fails, until = _login_fails.get(key, (0, 0))
        if until > time.time():
            raise TooManyAttempts("Too many wrong PINs. Try again in a few minutes.")
    with db.connect() as conn:
        row = conn.one("SELECT * FROM users WHERE lower(name) = lower(?)", (name,))
        if row is not None and not row["pin_hash"]:
            raise Unauthorized("This account signs in with Google or GitHub.")
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
    with db.connect() as conn:
        return conn.one(
            "SELECT users.id, users.name FROM sessions JOIN users ON users.id = sessions.user_id "
            "WHERE token = ? AND expires > ?", (token, time.time()))


def logout(token):
    with db.connect() as conn:
        conn.run("DELETE FROM sessions WHERE token = ?", (token,))


def me(token):
    user = user_for_token(token)
    auth = {"providers": oauth.enabled(), "pinSignup": pin_signup_allowed()}
    if user is None:
        return {"user": None, "auth": auth}
    with db.connect() as conn:
        linked = [r["provider"] for r in conn.all(
            "SELECT provider FROM identities WHERE user_id = ? ORDER BY provider", (user["id"],))]
        has_pin = bool(conn.value("SELECT pin_hash FROM users WHERE id = ?", (user["id"],)))
    return {"user": {**user, "linked": linked, "hasPin": has_pin}, "auth": auth}


def rename(user, body):
    name = str(body.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise BadRequest("Names are 2-20 letters, numbers, spaces, dots, dashes or underscores.")
    try:
        with db.connect() as conn:
            conn.run("UPDATE users SET name = ? WHERE id = ?", (name, user["id"]))
    except Duplicate:
        raise Conflict("That name is taken.")
    return {"name": name}


# ---------- Google / GitHub sign-in ----------

def _unique_name(conn, wanted):
    base = re.sub(r"[^A-Za-z0-9 _.-]", "", wanted or "").strip()[:16] or "Player"
    if len(base) < 2:
        base = f"{base}Player"[:16]
    for n in range(0, 1000):
        name = base if n == 0 else f"{base[:16]}{n + 1}"
        if not conn.one("SELECT 1 AS x FROM users WHERE lower(name) = lower(?)", (name,)):
            return name
    return f"Player{secrets.randbelow(10 ** 6)}"


def oauth_start(provider, intent, user):
    """Make a one-time state and return (provider URL, state). intent is login or link."""
    if not oauth.is_enabled(provider):
        raise NotFound("That sign-in option isn't set up.")
    if intent == "link" and user is None:
        raise Unauthorized("Sign in first, then link an account.")
    state = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.run("DELETE FROM oauth_states WHERE expires < ?", (time.time(),))
        conn.run("INSERT INTO oauth_states (state, provider, intent, user_id, expires) VALUES (?, ?, ?, ?, ?)",
                 (state, provider, intent, user["id"] if intent == "link" else None,
                  time.time() + OAUTH_STATE_SECONDS))
    return oauth.authorize_url(provider, PUBLIC_URL, state), state


def oauth_callback(provider, query, state_cookie):
    """Finish sign-in. Returns (where to send the browser, new session token or None)."""
    def fail(message):
        return "/?" + urlencode({"auth_error": message}), None

    if not oauth.is_enabled(provider):
        return fail("That sign-in option isn't set up.")
    if "error" in query:
        return fail("Sign-in was cancelled.")
    state, code = query.get("state", [""])[0], query.get("code", [""])[0]
    if not state or not code or not state_cookie or not hmac.compare_digest(state, state_cookie):
        return fail("Sign-in expired. Please try again.")
    with db.connect() as conn:
        saved = conn.one("SELECT * FROM oauth_states WHERE state = ? AND provider = ? AND expires > ?",
                         (state, provider, time.time()))
        conn.run("DELETE FROM oauth_states WHERE state = ?", (state,))
    if saved is None:
        return fail("Sign-in expired. Please try again.")
    try:
        ident = oauth.fetch_identity(provider, PUBLIC_URL, code)
    except oauth.OAuthError as e:
        return fail(str(e))

    label = oauth.PROVIDERS[provider]["label"]
    imported = 0
    with db.connect() as conn:
        owner = conn.value("SELECT user_id FROM identities WHERE provider = ? AND subject = ?",
                           (provider, ident["subject"]))
        if saved["intent"] == "link":
            if owner is not None and owner != saved["user_id"]:
                return fail(f"That {label} account is already used by another player.")
            conn.run("INSERT INTO identities (provider, subject, user_id, email, created) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT DO NOTHING", (provider, ident["subject"], saved["user_id"], ident["email"], _now()))
            # Linked accounts stop using the PIN, which is much easier to guess.
            conn.run("UPDATE users SET pin_salt = '', pin_hash = '' WHERE id = ?", (saved["user_id"],))
            return "/?" + urlencode({"linked": provider}), None
        if owner is None:
            owner, imported = _create_user(conn, _unique_name(conn, ident["name"]))
            conn.run("INSERT INTO identities (provider, subject, user_id, email, created) VALUES (?, ?, ?, ?, ?)",
                     (provider, ident["subject"], owner, ident["email"], _now()))
            where = "/?welcome=1"
        else:
            where = "/"
        token = _new_session(conn, owner)
    _finish_import(imported)
    return where, token


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


def fetch_standings(when):
    """Team standings for a YYYY-MM-DD date, or live ('now'). A past date never changes, so it's kept."""
    path = "/standings/now" if when == "now" else f"/standings/{when}"
    return _cached(("standings", when), lambda: standings.parse_standings(_get_json(path)),
                   done=lambda rows: when != "now" and bool(rows))


def fetch_player(player_id):
    return _cached(("player", player_id), lambda: _get_json(f"/player/{player_id}/landing"),
                   ttl=SLOW_CACHE_SECONDS)


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
    with db.connect() as conn:
        if not winner:  # clearing a pick
            conn.run("DELETE FROM picks WHERE user_id = ? AND game_id = ?", (user["id"], game_id))
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
        conn.run("INSERT INTO picks (user_id, game_id, date, winner, away_score, home_score) "
                 "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, game_id) DO UPDATE SET "
                 "date = excluded.date, winner = excluded.winner, "
                 "away_score = excluded.away_score, home_score = excluded.home_score",
                     (user["id"], game_id, day, winner, away, home))
    return {"date": day, "winner": winner, "awayScore": away, "homeScore": home}


def api_games(user, query):
    day = _parse_day(query.get("date", [date_cls.today().isoformat()])[0])
    with db.connect() as conn:
        rows = conn.all("SELECT * FROM picks WHERE user_id = ? AND date = ?", (user["id"], day))
    picks = {r["game_id"]: _pick_row(r) for r in rows}
    games = []
    for g in fetch_day(day):
        pick = picks.get(str(g["id"]))
        games.append({**g, "locked": is_locked(g), "pick": pick, "result": grade(pick, g)})
    return {"date": day, "games": games}


# ---------- player points pick'em ----------

def _slate_rows(day):
    with db.connect() as conn:
        return conn.all("SELECT * FROM slates WHERE date = ? ORDER BY slot", (day,))


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
    cols = ("player_id", "name", "position", "team", "opponent", "game_id", "headshot",
            "tier", "season_gp", "season_points")
    with db.connect() as conn:  # if two requests race, the first draw wins
        conn.many(f"INSERT INTO slates (date, slot, {', '.join(cols)}) VALUES ({placeholders(len(cols) + 2)}) "
                  "ON CONFLICT DO NOTHING",
                  [(day, i, *(p[c] for c in cols)) for i, p in enumerate(chosen)])
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
    with db.connect() as conn:
        guesses = {r["player_id"]: r["guess"] for r in conn.all(
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
    with db.connect() as conn:
        if guess is None:
            conn.run("DELETE FROM player_picks WHERE user_id = ? AND date = ? AND player_id = ?",
                     (user["id"], day, player_id))
            return None
        if guess not in PLAYER_GUESSES or isinstance(guess, bool):
            raise BadRequest("Guess 0, 1, 2 or 3+ points.")
        conn.run("INSERT INTO player_picks (user_id, date, player_id, guess) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT (user_id, date, player_id) DO UPDATE SET guess = excluded.guess",
                 (user["id"], day, player_id, guess))
    return guess


# ---------- scoring & leaderboard ----------

def _pct(earned, possible):
    return round(100 * earned / possible, 1) if possible else None


def leaderboard(user, query):
    """Pick % = points earned / points possible, over the nights each player played.

    On a night you play, every finished game (and every drawn skater who played) counts,
    so skipping the hard ones doesn't help. Nights you sit out don't count against you.
    With ?league=<id>, only that league's members are ranked.
    """
    league_id = _league_id(query.get("league", [None])[0])
    with db.connect() as conn:
        if league_id is None:
            league = None
            users = {r["id"]: r["name"] for r in conn.all("SELECT id, name FROM users")}
        else:
            league = _league_for_member(conn, league_id, user)
            users = {r["id"]: r["name"] for r in conn.all(
                "SELECT users.id, users.name FROM league_members JOIN users ON users.id = league_members.user_id "
                "WHERE league_id = ?", (league_id,))}
        ids = list(users)
        where = f"WHERE user_id IN ({placeholders(len(ids))})"
        picks = conn.all(f"SELECT * FROM picks {where}", ids) if ids else []
        guesses = conn.all(f"SELECT * FROM player_picks {where}", ids) if ids else []

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
            "id": uid, "name": name, "me": uid == user["id"], "nights": len(own),
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
    return {"minNights": MIN_NIGHTS, "league": league, "rows": rows, "myNights": mine}


# ---------- leagues ----------

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I mix-ups
CODE_LENGTH = 6
MAX_LEAGUES_PER_USER = 20


def _league_id(value):
    if value in (None, "", "all"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequest("Unknown league.")


def _league_for_member(conn, league_id, user):
    league = conn.one(
        "SELECT leagues.id, leagues.name, leagues.code, leagues.owner_id FROM leagues "
        "JOIN league_members ON league_members.league_id = leagues.id "
        "WHERE leagues.id = ? AND league_members.user_id = ?", (league_id, user["id"]))
    if league is None:
        raise NotFound("You're not in that league.")
    return {"id": league["id"], "name": league["name"], "code": league["code"],
            "isOwner": league["owner_id"] == user["id"]}


def my_leagues(user):
    with db.connect() as conn:
        leagues = conn.all(
            "SELECT leagues.id, leagues.name, leagues.code, leagues.owner_id FROM leagues "
            "JOIN league_members ON league_members.league_id = leagues.id "
            "WHERE league_members.user_id = ? ORDER BY lower(leagues.name)", (user["id"],))
        out = []
        for lg in leagues:
            members = conn.all(
                "SELECT users.id, users.name FROM league_members JOIN users ON users.id = league_members.user_id "
                "WHERE league_id = ? ORDER BY league_members.joined", (lg["id"],))
            out.append({"id": lg["id"], "name": lg["name"], "code": lg["code"],
                        "isOwner": lg["owner_id"] == user["id"], "ownerId": lg["owner_id"],
                        "members": members})
    return {"leagues": out}


def _check_league_limit(conn, user):
    n = conn.value("SELECT COUNT(*) AS n FROM league_members WHERE user_id = ?", (user["id"],))
    if n >= MAX_LEAGUES_PER_USER:
        raise Conflict(f"You can be in at most {MAX_LEAGUES_PER_USER} leagues.")


def create_league(user, body):
    name = str(body.get("name") or "").strip()
    if not 2 <= len(name) <= 40:
        raise BadRequest("League names are 2-40 characters.")
    for _ in range(10):  # retry on the rare invite-code collision
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        try:
            with db.connect() as conn:
                _check_league_limit(conn, user)
                league_id = conn.value("INSERT INTO leagues (name, code, owner_id, created) VALUES (?, ?, ?, ?) "
                                       "RETURNING id", (name, code, user["id"], _now()))
                conn.run("INSERT INTO league_members (league_id, user_id, joined) VALUES (?, ?, ?)",
                         (league_id, user["id"], _now()))
            return {"league": {"id": league_id, "name": name, "code": code, "isOwner": True}}
        except Duplicate:
            continue
    raise Conflict("Couldn't make an invite code. Try again.")


def join_league(user, body):
    code = re.sub(r"[^A-Za-z0-9]", "", str(body.get("code") or "")).upper()
    with db.connect() as conn:
        league = conn.one("SELECT id, name, code, owner_id FROM leagues WHERE code = ?", (code,))
        if league is None:
            raise NotFound("No league has that invite code.")
        already = conn.one("SELECT 1 AS x FROM league_members WHERE league_id = ? AND user_id = ?",
                           (league["id"], user["id"]))
        if not already:
            _check_league_limit(conn, user)
            conn.run("INSERT INTO league_members (league_id, user_id, joined) VALUES (?, ?, ?) "
                     "ON CONFLICT DO NOTHING", (league["id"], user["id"], _now()))
    return {"league": {"id": league["id"], "name": league["name"], "code": league["code"],
                       "isOwner": league["owner_id"] == user["id"]}, "alreadyIn": bool(already)}


def _remove_member(conn, league_id, user_id):
    """Take someone out; hand ownership to the longest-standing member, or delete an empty league."""
    conn.run("DELETE FROM league_members WHERE league_id = ? AND user_id = ?", (league_id, user_id))
    owner = conn.value("SELECT owner_id FROM leagues WHERE id = ?", (league_id,))
    if owner != user_id:
        return
    heir = conn.value("SELECT user_id FROM league_members WHERE league_id = ? ORDER BY joined LIMIT 1",
                      (league_id,))
    if heir is None:
        conn.run("DELETE FROM leagues WHERE id = ?", (league_id,))
    else:
        conn.run("UPDATE leagues SET owner_id = ? WHERE id = ?", (heir, league_id))


def leave_league(user, body):
    league_id = _league_id(body.get("leagueId"))
    with db.connect() as conn:
        _league_for_member(conn, league_id, user)
        _remove_member(conn, league_id, user["id"])
    return {"ok": True}


def remove_member(user, body):
    league_id = _league_id(body.get("leagueId"))
    target = body.get("userId")
    with db.connect() as conn:
        if not _league_for_member(conn, league_id, user)["isOwner"]:
            raise Conflict("Only the league owner can remove players.")
        if target == user["id"]:
            raise BadRequest("Use Leave to leave your own league.")
        _remove_member(conn, league_id, target)
    return {"ok": True}


# ---------- standings orders ----------

TEAM_RE = re.compile(r"^[A-Z]{3}$")


def _default(value):
    """NHL API localized text comes as {'default': ..., 'fr': ...}."""
    return value.get("default") if isinstance(value, dict) else value


def _status(period, key, today):
    if period not in standings.PERIODS:
        raise BadRequest("Period must be week, month or season.")
    try:
        return standings.describe(period, key or standings.default_key(period, today), today)
    except standings.PeriodError as e:
        raise BadRequest(str(e))


def _final_ranks(status):
    """{team: {division, division_rank, points}} from the period's last-day standings.

    Stored the first time anyone asks after the period ends, so later NHL data can't change grades.
    """
    with db.connect() as conn:
        rows = conn.all("SELECT team, division, division_rank, points FROM standings_results "
                        "WHERE period_type = ? AND period_key = ?", (status["type"], status["key"]))
    if not rows:
        snapshot = fetch_standings(status["end"])
        if not snapshot:
            return {}
        rows = [{"team": t["team"], "division": t["division"], "division_rank": t["divisionRank"],
                 "points": t["pts"]} for t in snapshot]
        with db.connect() as conn:
            conn.many("INSERT INTO standings_results (period_type, period_key, team, division, division_rank, points) "
                      "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                      [(status["type"], status["key"], r["team"], r["division"], r["division_rank"], r["points"])
                       for r in rows])
    return {r["team"]: r for r in rows}


def api_standings(user, query):
    today = date_cls.today()
    period = query.get("period", ["week"])[0]
    status = _status(period, query.get("key", [None])[0], today)
    final = _final_ranks(status) if status["final"] else {}
    ranks = {team: r["division_rank"] for team, r in final.items()}
    divisions = standings.group_divisions(fetch_standings(standings.snapshot_date(status)))

    with db.connect() as conn:
        rows = conn.all("SELECT o.user_id, u.name, o.division, o.team, o.predicted_rank FROM standings_orders o "
                        "JOIN users u ON u.id = o.user_id WHERE o.period_type = ? AND o.period_key = ?",
                        (period, status["key"]))
    by_user = {}  # user_id -> {"name", "orders": {division: {team: rank}}}
    for r in rows:
        entry = by_user.setdefault(r["user_id"], {"name": r["name"], "orders": {}})
        entry["orders"].setdefault(r["division"], {})[r["team"]] = r["predicted_rank"]

    def ordered(order):
        return [team for team, _ in sorted(order.items(), key=lambda kv: kv[1])]

    mine = by_user.get(user["id"], {"orders": {}})["orders"]
    # Everyone's orders stay hidden until the period locks, so nobody can copy one.
    picks = {}
    for uid, entry in by_user.items():
        if uid != user["id"] and not status["locked"]:
            continue
        for div, order in entry["orders"].items():
            picks.setdefault(div, []).append({"name": entry["name"], "mine": uid == user["id"],
                                              "order": ordered(order)})

    scores = []
    if final:
        for entry in by_user.values():
            points = sum(standings.score_order(order, ranks) for order in entry["orders"].values())
            placed = sum(len(order) for order in entry["orders"].values())
            scores.append({"name": entry["name"], "points": points, "placed": placed,
                           "possible": placed})
        scores.sort(key=lambda s: (-s["points"], s["name"].lower()))

    for div in divisions:
        div["mine"] = ordered(mine[div["abbrev"]]) if div["abbrev"] in mine else None
        div["picks"] = sorted(picks.get(div["abbrev"], []), key=lambda p: p["name"].lower())
    return {
        "period": {**status, "prevKey": standings.prev_key(period, status["key"]),
                   "nextKey": standings.next_key(period, status["key"])},
        "asOf": standings.snapshot_date(status),
        "final": final and True or False,
        "divisions": divisions,
        "scores": scores,
    }


def save_standings_order(user, body):
    today = date_cls.today()
    status = _status(body.get("period"), body.get("key"), today)
    if status["locked"]:
        raise Conflict("This period has started, so its picks are locked.")
    division = body.get("division")
    members = {t["team"] for t in fetch_standings("now") if t["division"] == division}
    if not members:
        raise BadRequest("Pick a division from the standings.")
    order = body.get("teams") or []  # an empty list clears the pick
    if not isinstance(order, list) or not all(isinstance(t, str) for t in order):
        raise BadRequest("Send the teams in order.")
    if order and (len(order) != len(members) or set(order) != members):
        raise BadRequest(f"Rank all {len(members)} teams in that division, each once.")
    with db.connect() as conn:
        conn.run("DELETE FROM standings_orders WHERE user_id = ? AND period_type = ? "
                 "AND period_key = ? AND division = ?",
                 (user["id"], status["type"], status["key"], division))
        if order:
            conn.many("INSERT INTO standings_orders (user_id, period_type, period_key, division, team, predicted_rank) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      [(user["id"], status["type"], status["key"], division, t, i + 1)
                       for i, t in enumerate(order)])
    return {"period": status["type"], "key": status["key"], "division": division, "teams": order}


# ---------- teams, players, news ----------

def api_teams(user):
    return {"teams": sorted(fetch_standings("now"), key=lambda t: t["name"])}


def api_team(user, code):
    code = (code or "").upper()
    row = next((t for t in fetch_standings("now") if t["team"] == code), None) if TEAM_RE.match(code) else None
    if row is None:
        raise NotFound("No team with that code.")
    skaters = []
    for s in fetch_team_skaters(code):
        skaters.append({
            "id": s["playerId"], "name": f'{_default(s["firstName"])} {_default(s["lastName"])}',
            "position": s.get("positionCode"), "gp": s.get("gamesPlayed") or 0,
            "goals": s.get("goals") or 0, "assists": s.get("assists") or 0,
            "points": s.get("points") or 0, "plusMinus": s.get("plusMinus") or 0,
            "headshot": s.get("headshot"),
        })
    skaters.sort(key=lambda s: (-s["points"], s["name"]))
    return {"team": row, "skaters": skaters}


def api_player(user, player_id):
    if not str(player_id).isdigit():
        raise NotFound("No player with that id.")
    data = fetch_player(player_id)
    seasons = [{
        "season": s.get("season"), "team": _default(s.get("teamCommonName")),
        "gp": s.get("gamesPlayed") or 0, "goals": s.get("goals") or 0,
        "assists": s.get("assists") or 0, "points": s.get("points") or 0,
        "plusMinus": s.get("plusMinus") or 0, "pim": s.get("pim") or 0,
        "shots": s.get("shots") or 0, "avgToi": s.get("avgToi"),
        "powerPlayPoints": s.get("powerPlayPoints") or 0,
    } for s in data.get("seasonTotals", []) if s.get("gameTypeId") == 2 and s.get("leagueAbbrev") == "NHL"]
    return {
        "player": {
            "id": data.get("playerId"),
            "name": f'{_default(data.get("firstName"))} {_default(data.get("lastName"))}',
            "position": data.get("position"), "number": data.get("sweaterNumber"),
            "team": data.get("currentTeamAbbrev"), "teamLogo": data.get("teamLogo"),
            "headshot": data.get("headshot"), "birthDate": data.get("birthDate"),
            "birthCountry": data.get("birthCountry"), "shoots": data.get("shootsCatches"),
            "heightInInches": data.get("heightInInches"), "weightInPounds": data.get("weightInPounds"),
            "active": data.get("isActive"),
        },
        "seasons": seasons,
    }


def api_news(user, query):
    team = (query.get("team", [""])[0] or "").upper() or None
    if team and not TEAM_RE.match(team):
        raise BadRequest("Team must be a 3-letter code.")
    try:
        limit = min(max(int(query.get("limit", ["40"])[0]), 1), 100)
    except ValueError:
        raise BadRequest("Limit must be a number.")
    return {"items": news.list_items(team, limit)}


# ---------- HTTP ----------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def log_message(self, format, *args):
        pass  # keep the console quiet

    def send_header(self, keyword, value):
        if keyword.lower() == "cache-control":
            self._sent_cache_control = True
        super().send_header(keyword, value)

    def end_headers(self):
        # Static files: make browsers (phones especially) check for a newer copy each load,
        # so a deploy's CSS/JS shows up right away. Unchanged files come back as a 304.
        if not getattr(self, "_sent_cache_control", False):
            super().send_header("Cache-Control", "no-cache")
        self._sent_cache_control = False
        super().end_headers()

    def cookie(self, name):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        return cookie[name].value if name in cookie else None

    def session_token(self):
        return self.cookie(SESSION_COOKIE)

    def _set_cookie(self, name, value, max_age):
        secure = "; Secure" if SECURE_COOKIES else ""
        self.send_header("Set-Cookie", f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}")

    def send_json(self, status, payload, cookie=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self._set_cookie(SESSION_COOKIE, *cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location, cookies=()):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        for name, value, max_age in cookies:
            self._set_cookie(name, value, max_age)
        self.end_headers()

    def handle_auth(self, url):
        """GET /auth/<provider>/start?intent=login|link and /auth/<provider>/callback."""
        parts = url.path.strip("/").split("/")
        if len(parts) != 3 or parts[2] not in ("start", "callback"):
            return self.send_json(404, {"error": "Not found."})
        provider, step, query = parts[1], parts[2], parse_qs(url.query)
        try:
            if step == "start":
                intent = query.get("intent", ["login"])[0]
                intent = intent if intent in ("login", "link") else "login"
                location, state = oauth_start(provider, intent, user_for_token(self.session_token()))
                return self.send_redirect(location, [(STATE_COOKIE, state, OAUTH_STATE_SECONDS)])
            location, token = oauth_callback(provider, query, self.cookie(STATE_COOKIE))
        except AppError as e:
            return self.send_redirect("/?" + urlencode({"auth_error": str(e)}))
        cookies = [(STATE_COOKIE, "", 0)]
        if token:
            cookies.append((SESSION_COOKIE, token, SESSION_DAYS * 86400))
        self.send_redirect(location, cookies)

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
        if url.path.startswith("/auth/"):
            return self.handle_auth(url)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        query = parse_qs(url.query)
        routes = {
            "/api/me": lambda: me(self.session_token()),
            "/api/games": self._signed_in(lambda u: api_games(u, query)),
            "/api/players": self._signed_in(lambda u: api_players(u, query)),
            "/api/leaderboard": self._signed_in(lambda u: leaderboard(u, query)),
            "/api/leagues": self._signed_in(my_leagues),
            "/api/standings": self._signed_in(lambda u: api_standings(u, query)),
            "/api/teams": self._signed_in(api_teams),
            "/api/news": self._signed_in(lambda u: api_news(u, query)),
        }
        if url.path.startswith("/api/teams/"):
            code = url.path.removeprefix("/api/teams/")
            return self._respond(self._signed_in(lambda u: api_team(u, code)))
        if url.path.startswith("/api/players/"):
            player_id = url.path.removeprefix("/api/players/")
            return self._respond(self._signed_in(lambda u: api_player(u, player_id)))
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
            "/api/me/name": self._signed_in(lambda u: rename(u, body())),
            "/api/picks": self._signed_in(lambda u: {"pick": save_pick(u, body())}),
            "/api/player-picks": self._signed_in(lambda u: {"guess": save_player_pick(u, body())}),
            "/api/leagues": self._signed_in(lambda u: create_league(u, body())),
            "/api/leagues/join": self._signed_in(lambda u: join_league(u, body())),
            "/api/leagues/leave": self._signed_in(lambda u: leave_league(u, body())),
            "/api/leagues/remove": self._signed_in(lambda u: remove_member(u, body())),
            "/api/standings-order": self._signed_in(lambda u: {"order": save_standings_order(u, body())}),
        }
        route = routes.get(url.path)
        if route is None:
            return self.send_json(404, {"error": "Not found."})
        self._respond(route)


def main():
    db.init()
    news.start_background_refresh()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    where = f"http://localhost:{PORT}" if HOST in ("127.0.0.1", "localhost") else f"http://<this computer's IP>:{PORT}"
    print(f"NHL Picks running at {where}  using {db.describe()}  (Ctrl+C to stop)")
    providers = ", ".join(p["label"] for p in oauth.enabled()) or "none (PIN accounts only)"
    print(f"Sign-in: {providers}" + (f"  callbacks at {PUBLIC_URL}/auth/<provider>/callback" if oauth.enabled() else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
