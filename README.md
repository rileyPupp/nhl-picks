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

## Run it
Double-click `start.bat` (or run `python server.py`), then open http://localhost:8000. Requires Python 3 only; no packages to install.

Everyone signs in with a name and a 4-6 digit PIN. Data is saved in `nhl-picks.db` (SQLite) in this folder. Picks from the old single-player version (`picks.json`) are moved to the first account created, and the file is renamed `picks.json.imported`.

### Letting friends play
- **Same Wi-Fi:** run with `HOST=0.0.0.0` (in PowerShell: `$env:HOST="0.0.0.0"; python server.py`) and have them open `http://<your computer's IP>:8000`. Windows may ask to allow Python through the firewall.
- **Anywhere:** host it on a small server (any VPS, Render, Fly.io, ...). Set `HOST=0.0.0.0`, `PORT` as the host requires, and `SECURE_COOKIES=1` when it's served over HTTPS. Keep `nhl-picks.db` on persistent storage, or set `NHL_PICKS_DB` to a path that is.
