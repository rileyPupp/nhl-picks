# NHL Picks

A nightly NHL prediction game for you and your friends.

## Games
Pick the winner of every NHL game each night. **1 point** for the right winner, **+3 bonus** for the exact final score (4 total). Games are graded on the NHL's official final score, so a shootout win counts as e.g. 4-3. Picks lock when a game goes live or its start time passes.

## Player points
Each night three skaters are drawn, the same three for everyone: one **star**, one **middle**-of-the-lineup player and one **depth** player (ranked by season points per game). Guess how many points each gets: 0, 1, 2 or 3+.
**1 point** for calling point / no point right, **+2 bonus** for the exact number (3 total). A player who doesn't dress doesn't count. The draw happens the first time anyone opens that night (from the day before game day) and is then fixed.

## Leaderboard: Pick %
**Pick % = points earned ÷ points possible, over the nights you played.**
- Once you make any pick on a night, every finished game that night counts (unpicked = 0), so skipping hard games doesn't help. The same goes for the three drawn skaters.
- Nights you sit out don't count against you, so joining late or missing a night is fine.
- You need 5 nights to get a rank.

## Leagues
Make a league from the Leaderboard tab and share its invite link (or 6-letter code). The Board picker switches between a league and Everyone. Anyone can leave; the owner can remove players. If the owner leaves, the longest-standing member becomes owner, and an empty league is deleted.

## Signing in
- **With Google or GitHub** once their keys are set (see below). New players can only sign up this way.
- **With a name + PIN** when no Google/GitHub keys are set (local play). Older PIN accounts can still sign in by PIN; the app then asks them to link Google or GitHub, and the PIN stops working once linked. `ALLOW_PIN_SIGNUP=1` keeps PIN sign-up open.
- Tap your name at the top to change how it shows on leaderboards.

## Run it locally
Double-click `start.bat` (or run `python server.py`), then open http://localhost:8000. Locally it needs Python 3 only: data goes in `nhl-picks.db` (SQLite) in this folder, which is never committed.

## Put it online (Render + Neon, free)
The web app runs on [Render](https://render.com), the database on [Neon](https://neon.tech) (Render's own free Postgres is deleted after 30 days; Neon's isn't). Render's free plan sleeps after 15 idle minutes, so the first visit after that takes about a minute.

1. **Database.** This folder is linked to the Neon project `nhl-picks` (see `.neon` and `neon.ts`). `neon deploy` (or `neon env pull`) writes its connection strings into `.env.local`, which is gitignored. The app reads `.env.local` on start, so locally it now uses Neon instead of the SQLite file — that is the live database. Two strings:
   - `DATABASE_URL` (pooled, host has `-pooler`): used for the app's queries.
   - `DATABASE_URL_UNPOOLED` (direct): used for creating tables and for `copy_to_postgres.py`.
2. **Copy your current data** (optional, once, before anyone signs up on the site):
   ```
   pip install -r requirements.txt
   python copy_to_postgres.py
   ```
3. **Web app.** Push this repo to GitHub. On Render: New → Blueprint → pick the repo. It reads `render.yaml` and asks for the secret values. Copy `DATABASE_URL` and `DATABASE_URL_UNPOOLED` from `.env.local` (or the Neon console's **Connect** dialog) and leave the rest blank for now. After it deploys, note the site address (e.g. `https://nhl-picks.onrender.com`) and set `PUBLIC_URL` to it.
4. **Google sign-in.** In [Google Cloud Console](https://console.cloud.google.com/apis/credentials): create a project → OAuth consent screen (External, app name, your email; Publish it so anyone can sign in) → Credentials → Create OAuth client ID → Web application. Authorized redirect URI: `<PUBLIC_URL>/auth/google/callback`. Copy the client ID and secret into Render as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
5. **GitHub sign-in.** GitHub → Settings → Developer settings → OAuth Apps → New. Homepage: `<PUBLIC_URL>`. Callback URL: `<PUBLIC_URL>/auth/github/callback`. Generate a client secret, then set `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` on Render.
6. Render redeploys when env vars change. Open the site, sign in with your old PIN, and link Google or GitHub.

To try Google/GitHub sign-in on your own computer, make a second OAuth app with callback `http://localhost:8000/auth/<provider>/callback` and set the same variables locally (a GitHub OAuth app allows only one callback URL).

### All settings
| Variable | What it does |
|---|---|
| `DATABASE_URL` | Postgres connection string (pooled on Neon). Unset = local SQLite file. Also read from `.env.local` / `.env`. |
| `DATABASE_URL_UNPOOLED` | Direct Postgres connection for creating tables and bulk copies. Falls back to `DATABASE_URL`. |
| `PUBLIC_URL` | The site's address, used for sign-in callbacks. Default `http://localhost:<PORT>`. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Turn on Google sign-in. |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | Turn on GitHub sign-in. |
| `HOST`, `PORT` | Where the server listens. Use `HOST=0.0.0.0` online (Render sets `PORT`). |
| `SECURE_COOKIES` | `1` when served over HTTPS. |
| `ALLOW_PIN_SIGNUP` | `1` keeps PIN sign-up open even with Google/GitHub on. |
