const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const localDate = (d) => {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};
const shiftDate = (ds, n) => {
  const [y, m, d] = ds.split("-").map(Number);
  return localDate(new Date(y, m - 1, d + n));
};
const prettyDate = (ds) => {
  const [y, m, d] = ds.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
};
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : "");
const fmtPct = (v) => (v == null ? "–" : `${v.toFixed(1)}%`);
const draftFrom = (p) => ({ winner: p?.winner ?? null, awayScore: p?.awayScore ?? "", homeScore: p?.homeScore ?? "" });

let date = localDate(new Date());
let tab = "games";
let games = [];
let players = [];
const drafts = {};

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("/api/login") && me) showAuth();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const post = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function flash(text, isError = false) {
  const m = $("#msg");
  m.textContent = text;
  m.className = "msg " + (isError ? "bad" : "good");
  m.hidden = false;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => (m.hidden = true), 4000);
}

// ---------- sign in ----------

let authInfo = { providers: [], pinSignup: true };
let me = null;

// Messages handed back in the URL after Google/GitHub sign-in, plus league invite links.
const params = new URLSearchParams(location.search);
const session = {
  get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch {} },
};
if (params.get("join")) session.set("joinCode", params.get("join")); // survives the sign-in round trip
const notice = params.get("auth_error")
  ? { text: params.get("auth_error"), bad: true }
  : params.get("linked") ? { text: `Linked your ${params.get("linked") === "github" ? "GitHub" : "Google"} account. You'll use it to sign in from now on.` }
  : params.get("welcome") ? { text: "Welcome! Tap your name at the top to change how it shows on leaderboards." }
  : null;
if (location.search) history.replaceState(null, "", location.pathname);

const providerIcon = {
  google: `<svg viewBox="0 0 48 48" width="18" height="18" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.2 0 24 0 14.6 0 6.6 5.4 2.7 13.2l7.9 6.2C12.5 13.6 17.8 9.5 24 9.5z"/><path fill="#4285F4" d="M46.1 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.4 5.8c4.3-4 6.9-9.9 6.9-17.2z"/><path fill="#FBBC05" d="M10.6 28.6c-.5-1.4-.8-3-.8-4.6s.3-3.2.8-4.6l-7.9-6.2C1 16.6 0 20.2 0 24s1 7.4 2.7 10.8l7.9-6.2z"/><path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.8c-2.1 1.4-4.8 2.3-8.5 2.3-6.2 0-11.5-4.1-13.4-9.9l-7.9 6.2C6.6 42.6 14.6 48 24 48z"/></svg>`,
  github: `<svg viewBox="0 0 16 16" width="18" height="18" aria-hidden="true" fill="currentColor"><path d="M8 0C3.6 0 0 3.6 0 8c0 3.5 2.3 6.5 5.5 7.6.4.1.5-.2.5-.4v-1.4c-2.2.5-2.7-1-2.7-1-.4-.9-.9-1.2-.9-1.2-.7-.5.1-.5.1-.5.8.1 1.2.8 1.2.8.7 1.3 1.9.9 2.4.7.1-.5.3-.9.5-1.1-1.8-.2-3.6-.9-3.6-4 0-.9.3-1.6.8-2.1-.1-.2-.4-1 .1-2.1 0 0 .7-.2 2.2.8.6-.2 1.3-.3 2-.3s1.4.1 2 .3c1.5-1 2.2-.8 2.2-.8.4 1.1.2 1.9.1 2.1.5.6.8 1.3.8 2.1 0 3.1-1.9 3.7-3.6 4 .3.3.6.8.6 1.5v2.2c0 .2.1.5.6.4C13.7 14.5 16 11.5 16 8c0-4.4-3.6-8-8-8z"/></svg>`,
};
const providerButton = (p, intent) =>
  `<a class="provider-btn" href="/auth/${p.id}/start?intent=${intent}">${providerIcon[p.id] || ""}<span>${intent === "link" ? "Link" : "Continue with"} ${esc(p.label)}</span></a>`;

function showAuthMessage(text, bad) {
  const m = $("#authMsg");
  m.className = "msg " + (bad ? "bad" : "good");
  m.textContent = text;
  m.hidden = false;
}

function showAuth() {
  me = null;
  $("#app").hidden = true;
  $("#auth").hidden = false;
  const hasProviders = authInfo.providers.length > 0;
  $("#providers").hidden = !hasProviders;
  $("#providers").innerHTML = authInfo.providers.map((p) => providerButton(p, "login")).join("");
  $("#pinBox").open = !hasProviders;
  $("#pinBox").classList.toggle("only", !hasProviders);
  $("#pinSummary").textContent = hasProviders ? "Have an older PIN account? Sign in here" : "Sign in";
  $("#registerBtn").hidden = !authInfo.pinSignup;
  $("#pinHelp").textContent = authInfo.pinSignup
    ? "New here? Choose a name and a 4–6 digit PIN, then press Create account."
    : "After you sign in, link Google or GitHub to keep your account.";
  if (session.get("joinCode") && $("#authMsg").hidden)
    showAuthMessage("You've been invited to a league. Sign in to join.", false);
}

function renderLinkBanner() {
  const banner = $("#linkBanner");
  const show = me && me.hasPin && authInfo.providers.length > 0;
  banner.hidden = !show;
  if (!show) return;
  banner.innerHTML = `<p><b>Secure your account.</b> Link Google or GitHub and use it to sign in. Your PIN stops working once you link.</p>
    <div class="providers-row">${authInfo.providers.map((p) => providerButton(p, "link")).join("")}</div>`;
}

async function showApp(user) {
  me = user;
  $("#auth").hidden = true;
  $("#app").hidden = false;
  $("#whoName").textContent = user.name;
  renderLinkBanner();
  await loadLeagues();
  const code = session.get("joinCode");
  if (code) {
    session.set("joinCode", null);
    tab = "board";
    await joinByCode(code).catch((err) => flash(err.message, true));
  }
  setTab(tab);
}

async function start() {
  try {
    const data = await api("/api/me");
    authInfo = data.auth;
    if (data.user) await showApp(data.user);
    else showAuth();
  } catch {
    showAuth();
  }
  if (notice) {
    if (me) flash(notice.text, notice.bad);
    else showAuthMessage(notice.text, notice.bad);
  }
}

$("#authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = e.submitter?.dataset.mode || "login";
  $("#authMsg").hidden = true;
  try {
    await post(`/api/${mode}`, { name: $("#authName").value.trim(), pin: $("#authPin").value });
    $("#authPin").value = "";
    start();
  } catch (err) {
    showAuthMessage(err.message, true);
  }
});

$("#logout").onclick = async () => {
  await post("/api/logout", {}).catch(() => {});
  showAuth();
};

$("#whoName").onclick = async () => {
  const name = prompt("Your name on leaderboards (2–20 letters, numbers, spaces, . - _):", me?.name || "");
  if (name == null || !name.trim() || name.trim() === me?.name) return;
  try {
    const res = await post("/api/me/name", { name: name.trim() });
    me.name = res.name;
    $("#whoName").textContent = res.name;
    flash("Name changed.");
    loadLeagues();
    loadBoard();
  } catch (err) {
    flash(err.message, true);
  }
};

// ---------- winner picks ----------

function statusText(g) {
  if (g.state === "LIVE" || g.state === "CRIT")
    return `<span class="live">LIVE</span> ${g.away.score ?? 0}–${g.home.score ?? 0}`;
  if (g.state === "FINAL" || g.state === "OFF") {
    const tag = g.periodType === "OT" ? " (OT)" : g.periodType === "SO" ? " (SO)" : "";
    return `Final${tag} · ${g.away.score}–${g.home.score}`;
  }
  return esc(fmtTime(g.startTimeUTC));
}

function teamBtn(g, side, draft) {
  const t = g[side];
  const selected = draft.winner === t.abbrev;
  return `<button class="team${selected ? " sel" : ""}" data-team="${esc(t.abbrev)}" data-id="${g.id}" ${g.locked ? "disabled" : ""}>
      ${t.logo ? `<img src="${esc(t.logo)}" alt="">` : ""}
      <span class="tabbr">${esc(t.abbrev)}</span>
      <span class="tname">${esc(t.name)}</span>
    </button>`;
}

function gameCard(g) {
  const d = drafts[g.id];
  const dis = g.locked ? "disabled" : "";
  let foot;
  if (g.result) {
    const r = g.result;
    foot = `<span class="${r.correctWinner ? "good" : "bad"}">${r.correctWinner ? "✓ Winner" : "✗ Winner"}</span>` +
      (r.exactScore ? ` <span class="star">★ Exact score</span>` : "") +
      ` <b>+${r.points} pts</b>`;
  } else if (g.pick) {
    foot = `Your pick: <b>${esc(g.pick.winner)}</b>`;
  } else {
    foot = g.locked ? "No pick" : "Not picked yet";
  }
  const action = g.locked ? "" : `<button class="primary" data-save="${g.id}">Save</button>`;
  return `
    <article class="card">
      <div class="card-head">${statusText(g)}</div>
      <div class="teams">${teamBtn(g, "away", d)}${teamBtn(g, "home", d)}</div>
      <div class="scores">
        <label>${esc(g.away.abbrev)} <input type="number" min="0" inputmode="numeric" data-score="awayScore" data-id="${g.id}" value="${esc(d.awayScore)}" ${dis}></label>
        <label>${esc(g.home.abbrev)} <input type="number" min="0" inputmode="numeric" data-score="homeScore" data-id="${g.id}" value="${esc(d.homeScore)}" ${dis}></label>
        <span class="muted">optional score</span>
      </div>
      <div class="card-foot"><span>${foot}</span>${action}</div>
    </article>`;
}

function renderGames() {
  $("#games").innerHTML = games.length
    ? games.map(gameCard).join("")
    : `<p class="empty">No games on this date.</p>`;
}

async function loadGames() {
  $("#games").innerHTML = `<p class="empty">Loading…</p>`;
  try {
    const data = await api(`/api/games?date=${date}`);
    games = data.games;
    for (const g of games) drafts[g.id] = draftFrom(g.pick);
    renderGames();
  } catch (e) {
    $("#games").innerHTML = `<p class="empty">Couldn't load games: ${esc(e.message)}</p>`;
  }
}

async function savePick(id) {
  const g = games.find((x) => x.id === id);
  const d = drafts[id];
  if (!d.winner) return flash("Pick a team first.", true);
  const num = (v) => (v === "" || v == null ? null : Number(v));
  try {
    const { pick } = await post("/api/picks", { gameId: id, date, winner: d.winner, awayScore: num(d.awayScore), homeScore: num(d.homeScore) });
    g.pick = pick;
    drafts[id] = draftFrom(pick);
    flash(pick ? "Pick saved." : "Pick cleared.");
    renderGames();
  } catch (e) {
    flash(e.message, true);
  }
}

$("#games").addEventListener("click", (e) => {
  const team = e.target.closest("[data-team]");
  if (team && !team.disabled) {
    drafts[team.dataset.id].winner = team.dataset.team;
    renderGames();
    return;
  }
  const save = e.target.closest("[data-save]");
  if (save) savePick(Number(save.dataset.save));
});

$("#games").addEventListener("input", (e) => {
  const el = e.target;
  if (el.dataset.score) drafts[el.dataset.id][el.dataset.score] = el.value;
});

// ---------- player points ----------

const guessLabel = (n) => (n >= 3 ? "3+" : String(n));

function playerStatus(p) {
  if (p.state === "final") return `Final · <b>${p.actual}</b> pt${p.actual === 1 ? "" : "s"}`;
  if (p.state === "live") return `<span class="live">LIVE</span> ${p.actual} pt${p.actual === 1 ? "" : "s"} so far`;
  if (p.state === "void") return `Didn't play`;
  return esc(fmtTime(p.startTimeUTC));
}

function playerCard(p) {
  let foot;
  if (p.state === "void") foot = `<span class="muted">Doesn't count</span>`;
  else if (p.result) {
    const r = p.result;
    foot = `<span class="${r.correctCall ? "good" : "bad"}">${r.correctCall ? "✓ Call" : "✗ Call"}</span>` +
      (r.exactCount ? ` <span class="star">★ Exact</span>` : "") + ` <b>+${r.points} pts</b>`;
  } else if (p.guess != null) foot = `Your call: <b>${guessLabel(p.guess)}</b>`;
  else foot = p.locked ? "No pick" : "Not picked yet";

  const ppg = p.seasonGP ? (p.seasonPoints / p.seasonGP).toFixed(2) : "0.00";
  const buttons = [0, 1, 2, 3].map((n) =>
    `<button class="guess${p.guess === n ? " sel" : ""}" data-player="${p.playerId}" data-guess="${n}" ${p.locked ? "disabled" : ""}>${guessLabel(n)}</button>`).join("");
  return `
    <article class="card player">
      <div class="card-head"><span class="tier tier-${esc(p.tier.toLowerCase())}">${esc(p.tier)}</span> ${playerStatus(p)}</div>
      <div class="pinfo">
        ${p.headshot ? `<img src="${esc(p.headshot)}" alt="">` : ""}
        <div>
          <div class="pname">${esc(p.name)}</div>
          <div class="muted small">${esc(p.position)} · ${esc(p.team)} vs ${esc(p.opponent)}</div>
          <div class="muted small">${p.seasonPoints} pts in ${p.seasonGP} GP (${ppg}/game)</div>
        </div>
      </div>
      <div class="guesses" role="group" aria-label="Points for ${esc(p.name)}">${buttons}</div>
      <div class="card-foot"><span>${foot}</span></div>
    </article>`;
}

function renderPlayers(message) {
  $("#players").innerHTML = players.length
    ? players.map(playerCard).join("")
    : `<p class="empty">${esc(message || "No player pick'em for this night.")}</p>`;
}

async function loadPlayers() {
  $("#players").innerHTML = `<p class="empty">Loading…</p>`;
  try {
    const data = await api(`/api/players?date=${date}`);
    players = data.players;
    renderPlayers(data.message);
  } catch (e) {
    $("#players").innerHTML = `<p class="empty">Couldn't load players: ${esc(e.message)}</p>`;
  }
}

$("#players").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-guess]");
  if (!btn || btn.disabled) return;
  const p = players.find((x) => x.playerId === Number(btn.dataset.player));
  const n = Number(btn.dataset.guess);
  const guess = p.guess === n ? null : n; // tap the selected one again to clear
  try {
    const res = await post("/api/player-picks", { date, playerId: p.playerId, guess });
    p.guess = res.guess;
    flash(guess == null ? "Pick cleared." : `Saved: ${p.name}, ${guessLabel(guess)}.`);
    renderPlayers();
  } catch (err) {
    flash(err.message, true);
  }
});

// ---------- leagues ----------

const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
};
let leagues = [];
let board = store.get("board") || ""; // "" = not chosen yet, "all" = everyone, else league id
let leagueFormMode = null;

const currentLeague = () => leagues.find((l) => String(l.id) === board) || null;
const inviteLink = (code) => `${location.origin}/?join=${encodeURIComponent(code)}`;

function setBoard(value) {
  board = String(value);
  store.set("board", board);
  renderLeagues();
  loadBoard();
}

async function loadLeagues() {
  try {
    leagues = (await api("/api/leagues")).leagues;
  } catch {
    leagues = [];
  }
  if (board !== "all" && !currentLeague()) board = leagues.length ? String(leagues[0].id) : "all";
  renderLeagues();
}

function renderLeagues() {
  $("#boardSelect").innerHTML =
    `<option value="all">Everyone</option>` +
    leagues.map((l) => `<option value="${l.id}">${esc(l.name)}</option>`).join("");
  $("#boardSelect").value = currentLeague() ? board : "all";

  const l = currentLeague();
  const info = $("#leagueInfo");
  info.hidden = !l;
  if (!l) return;
  const members = l.members.map((m) => `<li>${esc(m.name)}${m.id === l.ownerId ? ` <span class="muted small">(owner)</span>` : ""}
      ${l.isOwner && m.id !== l.ownerId ? `<button class="link" data-remove="${m.id}" data-name="${esc(m.name)}">Remove</button>` : ""}</li>`).join("");
  info.innerHTML = `
    <div class="league-head">
      <div><b>${esc(l.name)}</b> <span class="muted small">· ${l.members.length} player${l.members.length === 1 ? "" : "s"}</span></div>
      <div class="invite">Invite code <code>${esc(l.code)}</code>
        <button id="copyInvite">Copy invite link</button>
        <button id="leaveLeague">Leave</button>
      </div>
    </div>
    <details><summary class="small">Members</summary><ul class="members">${members}</ul></details>`;
}

function openLeagueForm(mode) {
  leagueFormMode = mode;
  $("#leagueFormLabel").textContent = mode === "new" ? "League name" : "Invite code";
  $("#leagueSubmit").textContent = mode === "new" ? "Create" : "Join";
  $("#leagueInput").value = "";
  $("#leagueForm").hidden = false;
  $("#leagueInput").focus();
}

async function joinByCode(code) {
  const res = await post("/api/leagues/join", { code });
  flash(res.alreadyIn ? `You're already in ${res.league.name}.` : `Joined ${res.league.name}!`);
  await loadLeagues();
  setBoard(res.league.id);
}

$("#newLeague").onclick = () => openLeagueForm("new");
$("#joinLeague").onclick = () => openLeagueForm("join");
$("#leagueCancel").onclick = () => ($("#leagueForm").hidden = true);
$("#boardSelect").onchange = (e) => setBoard(e.target.value);

$("#leagueForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const value = $("#leagueInput").value.trim();
  try {
    if (leagueFormMode === "new") {
      const { league } = await post("/api/leagues", { name: value });
      flash(`Made ${league.name}. Share the invite link with your friends.`);
      await loadLeagues();
      setBoard(league.id);
    } else {
      await joinByCode(value);
    }
    $("#leagueForm").hidden = true;
  } catch (err) {
    flash(err.message, true);
  }
});

$("#leagueInfo").addEventListener("click", async (e) => {
  const l = currentLeague();
  if (!l) return;
  try {
    if (e.target.id === "copyInvite") {
      await navigator.clipboard.writeText(inviteLink(l.code));
      flash("Invite link copied.");
    } else if (e.target.id === "leaveLeague") {
      if (!confirm(`Leave ${l.name}?`)) return;
      await post("/api/leagues/leave", { leagueId: l.id });
      flash(`You left ${l.name}.`);
      board = "";
      await loadLeagues();
      loadBoard();
    } else if (e.target.dataset.remove) {
      if (!confirm(`Remove ${e.target.dataset.name} from ${l.name}?`)) return;
      await post("/api/leagues/remove", { leagueId: l.id, userId: Number(e.target.dataset.remove) });
      await loadLeagues();
      loadBoard();
    }
  } catch (err) {
    flash(err.message, true);
  }
});


// ---------- leaderboard ----------

async function loadBoard() {
  try {
    const league = currentLeague();
    const b = await api(`/api/leaderboard${league ? `?league=${league.id}` : ""}`);
    const me = b.rows.find((r) => r.me);
    const rank = me?.qualified ? b.rows.filter((r) => r.qualified).indexOf(me) + 1 : null;
    $("#totals").innerHTML = me
      ? `<span><b>${fmtPct(me.pickPct)}</b> pick %</span>
         <span><b>${rank ? `#${rank}` : "–"}</b> rank</span>
         <span><b>${me.nights}</b> nights</span>`
      : "";
    $("#minNights").textContent = `You need ${b.minNights} nights to get a rank.`;

    let qualifiedRank = 0;
    $("#board").innerHTML = b.rows.length
      ? b.rows.map((r) => `
        <tr class="${r.me ? "me" : ""} ${r.qualified ? "" : "unranked"}">
          <td>${r.qualified ? ++qualifiedRank : "–"}</td>
          <td>${esc(r.name)}${r.qualified ? "" : ` <span class="muted small">(${r.nights}/${b.minNights} nights)</span>`}</td>
          <td><b>${fmtPct(r.pickPct)}</b></td><td>${fmtPct(r.gamePct)}</td><td>${fmtPct(r.playerPct)}</td>
          <td>${r.nights}</td><td>${r.points}</td><td>${r.winners}</td><td>${r.exact + r.playerExact}</td>
        </tr>`).join("")
      : `<tr><td colspan="9">No players yet.</td></tr>`;

    $("#nights").innerHTML = b.myNights.length
      ? b.myNights.map((n) => `
        <tr><td>${esc(n.date)}</td><td><b>${fmtPct(n.pickPct)}</b></td>
          <td>${n.gamePossible ? `${n.gamePoints}/${n.gamePossible}` : "–"}</td><td>${n.winners}</td><td>${n.exact}</td>
          <td>${n.playerPossible ? `${n.playerPoints}/${n.playerPossible}` : "–"}</td></tr>`).join("")
      : `<tr><td colspan="6">No finished nights yet.</td></tr>`;
  } catch (e) {
    $("#totals").textContent = "";
  }
}

// ---------- standings ----------

const PERIODS = ["week", "month", "season"];
const GROUPS = ["league", "conference", "division"];
const DAY_TEXT = { month: "short", day: "numeric", year: "numeric" };
let standingsPeriod = PERIODS.includes(store.get("standingsPeriod")) ? store.get("standingsPeriod") : "week";
let standingsView = store.get("standingsView") === "table" ? "table" : "picks";
let standingsGroup = GROUPS.includes(store.get("standingsGroup")) ? store.get("standingsGroup") : "division";
let standingsKey = null; // null = the period whose picks are still open
let standingsData = null;
let orderDrafts = {}; // division abbrev -> team codes in the order being edited
let drag = null;      // {abbrev, team} while a row is being dragged

const dayText = (ds) => {
  const [y, m, d] = ds.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, DAY_TEXT);
};
const shortDay = (ds) => {
  const [y, m, d] = ds.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" });
};
const seasonText = (key) => `${key.slice(0, 4)}–${key.slice(6)}`;

function periodLabel(p) {
  if (p.type === "week") return `Week of ${shortDay(p.start)} – ${shortDay(p.end)}`;
  if (p.type === "month") {
    const [y, m] = p.key.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
  }
  return `${seasonText(p.key)} season`;
}

function periodStatus(p, asOf) {
  if (p.pickOpen) return `Picks are open and lock on ${dayText(p.start)}, when the period starts.`;
  if (p.final) return `Final. Graded on the standings as of ${dayText(asOf)}.`;
  return `Picks are locked. Standings are live and the period is graded on ${dayText(p.end)}.`;
}

const safeHref = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");
const teamLogo = (t, cls = "") => (t.logo ? `<img class="${cls}" src="${esc(t.logo)}" alt="">` : "");
const sameOrder = (a, b) => a.length === b.length && a.every((t, i) => t === b[i]);
const standingOrder = (div) => div.teams.map((t) => t.team); // current division standings, first to last

function divisionCard(div, p) {
  const byTeam = Object.fromEntries(div.teams.map((t) => [t.team, t]));
  const saved = div.mine; // team codes in the saved order, or null
  const mineRows = div.picks.find((x) => x.mine);

  if (p.pickOpen) {
    const draft = orderDrafts[div.abbrev] || standingOrder(div);
    const dirty = !saved || !sameOrder(draft, saved);
    const rows = draft.map((team, i) => {
      const t = byTeam[team];
      const dragged = drag && drag.team === team && drag.abbrev === div.abbrev;
      return `<li class="order-row${dragged ? " is-dragged" : ""}">
          <span class="grip" data-grip data-division="${esc(div.abbrev)}" data-team="${esc(team)}" title="Drag to reorder" aria-label="Drag to reorder">⋮⋮</span>
          <span class="order-no">${i + 1}</span>
          <span class="tcell">${teamLogo(t)}<span>${esc(t.name)}</span></span>
          <span class="move">
            <button data-move="up" data-division="${esc(div.abbrev)}" data-index="${i}" aria-label="Move up" ${i === 0 ? "disabled" : ""}>▲</button>
            <button data-move="down" data-division="${esc(div.abbrev)}" data-index="${i}" aria-label="Move down" ${i === draft.length - 1 ? "disabled" : ""}>▼</button>
          </span>
        </li>`;
    }).join("");
    return `
      <article class="card div-card" data-card="${esc(div.abbrev)}">
        <div class="card-head"><b>${esc(div.name)}</b> · ${esc(div.conference)}</div>
        <div class="order-status small">${saved ? (dirty ? `<span class="muted">Unsaved changes</span>` : `Saved`) : `<span class="muted">Not saved yet</span>`}</div>
        <ol class="order-list">${rows}</ol>
        <div class="order-actions">
          <button class="primary" data-save-order="${esc(div.abbrev)}" ${dirty ? "" : "disabled"}>Save order</button>
          <button data-reset-order="${esc(div.abbrev)}" ${dirty ? "" : "disabled"}>Reset</button>
          ${saved ? `<button data-clear-order="${esc(div.abbrev)}">Clear pick</button>` : ""}
        </div>
      </article>`;
  }

  // Locked or finished: show the saved order read-only, with exact places marked once final.
  const rows = (saved || []).map((team, i) => {
    const t = byTeam[team];
    const exact = p.final ? (i + 1 === t.divisionRank) : null;
    return `<li class="order-row ${exact === true ? "hit" : exact === false ? "miss" : ""}">
        <span class="order-no">${i + 1}</span>
        <span class="tcell">${teamLogo(t)}<span>${esc(t.name)}</span></span>
        ${exact == null ? "" : `<span class="small">${exact ? "✓ exact" : `finished ${t.divisionRank}`}</span>`}
      </li>`;
  }).join("");
  const exactCount = p.final && saved ? saved.filter((team, i) => byTeam[team].divisionRank === i + 1).length : null;
  const everyone = div.picks.filter((x) => !x.mine).map((x) =>
    `<li><b>${esc(x.name)}</b>: <span class="small muted">${x.order.map(esc).join(" › ")}</span></li>`).join("");
  return `
    <article class="card div-card">
      <div class="card-head"><b>${esc(div.name)}</b> · ${esc(div.conference)}</div>
      ${saved
        ? `<ol class="order-list">${rows}</ol>${exactCount != null ? `<div class="small"><b>${exactCount}</b> exact</div>` : ""}`
        : `<p class="muted small">You didn't rank this division.</p>`}
      ${everyone ? `<details class="everyone"><summary class="small">Everyone's orders</summary><ul>${everyone}</ul></details>` : ""}
    </article>`;
}

function renderPicks() {
  const p = standingsData.period;
  $("#standings").innerHTML = standingsData.divisions.map((d) => divisionCard(d, p)).join("");
}

function renderScores() {
  const p = standingsData.period;
  const scores = standingsData.scores;
  $("#scoresBox").hidden = !(p.final && scores.length);
  $("#standingsScores").innerHTML = scores
    .map((s, i) => `<tr class="${s.name === me?.name ? "me" : ""}"><td>${i + 1}</td><td>${esc(s.name)}</td>
        <td>${s.placed} ranked</td><td><b>${s.points}</b></td></tr>`)
    .join("");
}

// Standings viewer: every team, grouped by league, conference, or division.
function groupedTables() {
  const all = standingsData.divisions.flatMap((d) =>
    d.teams.map((t) => ({ ...t, divisionName: d.name, conferenceName: d.conference })));
  const row = (t, rank) => `<tr>
      <td>${rank}</td><td><span class="tcell">${teamLogo(t)}<span>${esc(t.name)}</span></span></td>
      <td class="small muted">${esc(t.divisionName)}</td><td class="small muted">${esc(t.conferenceName)}</td>
      <td>${t.gp}</td><td>${t.w}</td><td>${t.l}</td><td>${t.otl}</td><td><b>${t.pts}</b></td><td>${esc(t.streak)}</td>
    </tr>`;
  const table = (teams, rankKey) => `
    <div class="table-wrap"><table>
      <thead><tr><th>#</th><th>Team</th><th>Division</th><th>Conference</th><th>GP</th><th>W</th><th>L</th><th>OTL</th><th>Pts</th><th>Streak</th></tr></thead>
      <tbody>${[...teams].sort((a, b) => a[rankKey] - b[rankKey]).map((t) => row(t, t[rankKey])).join("")}</tbody>
    </table></div>`;

  if (standingsGroup === "league") {
    return `<h2>League</h2>${table(all, "leagueRank")}`;
  }
  if (standingsGroup === "conference") {
    const conferences = [...new Set(all.map((t) => t.conferenceName))].sort();
    return conferences.map((c) => {
      const teams = all.filter((t) => t.conferenceName === c);
      return `<h2>${esc(c)}</h2>${table(teams, "conferenceRank")}`;
    }).join("");
  }
  return standingsData.divisions.map((d) => `<h2>${esc(d.name)}</h2>${table(d.teams, "divisionRank")}`).join("");
}

function renderView() {
  const isTable = standingsView === "table";
  $("#standings").hidden = isTable;
  $("#standingsTable").hidden = !isTable;
  $("#groupSeg").hidden = !isTable;
  $("#pickRules").hidden = isTable;
  for (const b of $$("[data-view]")) b.setAttribute("aria-pressed", String(b.dataset.view === standingsView));
  for (const b of $$("[data-group]")) b.setAttribute("aria-pressed", String(b.dataset.group === standingsGroup));
  if (isTable) $("#standingsTable").innerHTML = groupedTables();
  else renderPicks();
}

async function loadStandings() {
  $("#standings").innerHTML = `<p class="empty">Loading…</p>`;
  try {
    const query = `period=${standingsPeriod}${standingsKey ? `&key=${encodeURIComponent(standingsKey)}` : ""}`;
    standingsData = await api(`/api/standings?${query}`);
    const p = standingsData.period;
    standingsKey = p.key;
    orderDrafts = {}; // a fresh load starts from what's saved
    for (const d of standingsData.divisions) orderDrafts[d.abbrev] = d.mine ? [...d.mine] : standingOrder(d);
    for (const b of $$("[data-period]")) b.setAttribute("aria-pressed", String(b.dataset.period === standingsPeriod));
    $("#periodLabel").textContent = periodLabel(p);
    $("#periodExplain").textContent = periodStatus(p, standingsData.asOf);
    renderView();
    renderScores();
  } catch (e) {
    $("#standings").innerHTML = `<p class="empty">Couldn't load standings: ${esc(e.message)}</p>`;
  }
}

async function saveOrder(division, teams) {
  const p = standingsData.period;
  try {
    await post("/api/standings-order", { period: p.type, key: p.key, division, teams });
    flash(teams.length ? `Saved your ${division} order.` : "Pick cleared.");
  } catch (err) {
    flash(err.message, true);
  }
  loadStandings(); // redraw from what the server holds
}

// Drag a row by its grip (mouse or touch). Each move re-slots the dragged team among the others.
$("#standings").addEventListener("pointerdown", (e) => {
  const grip = e.target.closest("[data-grip]");
  if (!grip || e.button > 0) return;
  e.preventDefault();
  drag = { abbrev: grip.dataset.division, team: grip.dataset.team };
  document.body.classList.add("dragging-order");
  renderPicks();
  window.addEventListener("pointermove", onDragMove);
  window.addEventListener("pointerup", onDragEnd);
  window.addEventListener("pointercancel", onDragEnd);
});

function onDragMove(e) {
  if (!drag) return;
  const card = document.querySelector(`[data-card="${drag.abbrev}"]`);
  if (!card) return;
  const others = [...card.querySelectorAll(".order-row:not(.is-dragged)")];
  const above = others.filter((row) => {
    const b = row.getBoundingClientRect();
    return b.top + b.height / 2 < e.clientY;
  }).length;
  const draft = orderDrafts[drag.abbrev];
  const next = draft.filter((t) => t !== drag.team);
  next.splice(above, 0, drag.team);
  if (!sameOrder(next, draft)) {
    orderDrafts[drag.abbrev] = next;
    renderPicks();
  }
}

function onDragEnd() {
  window.removeEventListener("pointermove", onDragMove);
  window.removeEventListener("pointerup", onDragEnd);
  window.removeEventListener("pointercancel", onDragEnd);
  drag = null;
  document.body.classList.remove("dragging-order");
  renderPicks();
}

$("#standings").addEventListener("click", (e) => {
  const move = e.target.closest("[data-move]");
  if (move) {
    const abbrev = move.dataset.division;
    const i = Number(move.dataset.index);
    const j = move.dataset.move === "up" ? i - 1 : i + 1;
    const draft = orderDrafts[abbrev];
    if (j < 0 || j >= draft.length) return;
    [draft[i], draft[j]] = [draft[j], draft[i]];
    renderPicks();
    return;
  }
  const save = e.target.closest("[data-save-order]");
  if (save) return saveOrder(save.dataset.saveOrder, orderDrafts[save.dataset.saveOrder]);
  const reset = e.target.closest("[data-reset-order]");
  if (reset) {
    const d = standingsData.divisions.find((x) => x.abbrev === reset.dataset.resetOrder);
    orderDrafts[d.abbrev] = d.mine ? [...d.mine] : standingOrder(d);
    return renderPicks();
  }
  const clear = e.target.closest("[data-clear-order]");
  if (clear) saveOrder(clear.dataset.clearOrder, []);
});

for (const b of $$("[data-view]")) {
  b.onclick = () => {
    standingsView = b.dataset.view;
    store.set("standingsView", standingsView);
    renderView();
  };
}
for (const b of $$("[data-group]")) {
  b.onclick = () => {
    standingsGroup = b.dataset.group;
    store.set("standingsGroup", standingsGroup);
    renderView();
  };
}
for (const b of $$("[data-period]")) {
  b.onclick = () => {
    standingsPeriod = b.dataset.period;
    store.set("standingsPeriod", standingsPeriod);
    standingsKey = null;
    loadStandings();
  };
}
$("#periodPrev").onclick = () => { standingsKey = standingsData.period.prevKey; loadStandings(); };
$("#periodNext").onclick = () => { standingsKey = standingsData.period.nextKey; loadStandings(); };

// ---------- teams and players ----------

let teamsList = [];
let teamSort = ["name", "conference", "division", "standing"].includes(store.get("teamSort")) ? store.get("teamSort") : "name";

// Sort the team cards. Standing means league-wide rank; division and conference sorts keep standing order inside each group.
function sortTeams(teams, by) {
  const cmp = (...keys) => (a, b) => {
    for (const k of keys) {
      const x = typeof a[k] === "number" ? a[k] : String(a[k] ?? "");
      const y = typeof b[k] === "number" ? b[k] : String(b[k] ?? "");
      if (x < y) return -1;
      if (x > y) return 1;
    }
    return 0;
  };
  const sorters = {
    name: cmp("name"),
    conference: cmp("conferenceName", "divisionName", "divisionRank"),
    division: cmp("divisionName", "divisionRank"),
    standing: cmp("leagueRank"),
  };
  return [...teams].sort(sorters[by] || sorters.name);
}

$("#teamSort").value = teamSort;
$("#teamSort").onchange = (e) => {
  teamSort = e.target.value;
  store.set("teamSort", teamSort);
  loadTeams();
};

async function ensureTeams() {
  if (!teamsList.length) teamsList = (await api("/api/teams")).teams;
  return teamsList;
}

async function loadTeams() {
  $("#teamsGrid").hidden = false;
  $("#teamDetail").hidden = true;
  $("#teamsGrid").innerHTML = `<p class="empty">Loading…</p>`;
  try {
    const teams = sortTeams(await ensureTeams(), teamSort);
    const card = (t) => `
      <button class="card team-card" data-team-code="${esc(t.team)}">
        ${teamLogo(t)}
        <span class="tc-name">${esc(t.name)}</span>
        <span class="muted small">${esc(t.divisionName)} · #${t.divisionRank} · ${t.pts} pts</span>
      </button>`;
    // Conference and division sorts get a heading per group; the cards keep their sorted order inside.
    if (teamSort === "conference") {
      // Conference heading, then a sub-heading and grid for each division in it.
      const conferences = [];
      for (const t of teams) {
        let c = conferences.find((x) => x.key === t.conferenceName);
        if (!c) conferences.push((c = { key: t.conferenceName, divisions: [] }));
        let d = c.divisions.find((x) => x.key === t.divisionName);
        if (!d) c.divisions.push((d = { key: t.divisionName, teams: [] }));
        d.teams.push(t);
      }
      $("#teamsGrid").innerHTML = conferences.map((c) =>
        `<h2>${esc(c.key)}</h2>` + c.divisions.map((d) =>
          `<h3 class="sub">${esc(d.key)}</h3><div class="grid">${d.teams.map(card).join("")}</div>`).join("")).join("");
      return;
    }
    const groupKey = { division: (t) => `${t.divisionName} · ${t.conferenceName}` }[teamSort];
    if (!groupKey) {
      $("#teamsGrid").innerHTML = `<div class="grid">${teams.map(card).join("")}</div>`;
      return;
    }
    const groups = [];
    for (const t of teams) {
      const key = groupKey(t);
      const last = groups[groups.length - 1];
      if (last && last.key === key) last.teams.push(t);
      else groups.push({ key, teams: [t] });
    }
    $("#teamsGrid").innerHTML = groups.map((g) =>
      `<h2>${esc(g.key)}</h2><div class="grid">${g.teams.map(card).join("")}</div>`).join("");
  } catch (e) {
    $("#teamsGrid").innerHTML = `<p class="empty">Couldn't load teams: ${esc(e.message)}</p>`;
  }
}

async function loadTeam(code) {
  $("#teamsGrid").hidden = true;
  $("#teamDetail").hidden = false;
  $("#playerDetail").hidden = true;
  $("#teamHead").innerHTML = `<p class="empty">Loading…</p>`;
  $("#skaters").innerHTML = "";
  try {
    const { team: t, skaters } = await api(`/api/teams/${encodeURIComponent(code)}`);
    $("#teamHead").innerHTML = `
      <div class="pinfo">${teamLogo(t, "logo-lg")}
        <div>
          <div class="pname">${esc(t.name)}</div>
          <div class="muted small">${esc(t.divisionName)} · ${esc(t.conferenceName)} · #${t.divisionRank} in division</div>
          <div class="small">${t.w}-${t.l}-${t.otl} · <b>${t.pts} pts</b> in ${t.gp} GP · streak ${esc(t.streak)}</div>
        </div>
      </div>`;
    $("#skaters").innerHTML = skaters.length
      ? skaters.map((s) => `
          <tr class="clickable" data-player-id="${s.id}">
            <td>${esc(s.name)}</td><td>${esc(s.position)}</td><td>${s.gp}</td><td>${s.goals}</td>
            <td>${s.assists}</td><td><b>${s.points}</b></td><td>${s.plusMinus > 0 ? "+" : ""}${s.plusMinus}</td>
          </tr>`).join("")
      : `<tr><td colspan="7">No skater stats yet.</td></tr>`;
  } catch (e) {
    $("#teamHead").innerHTML = `<p class="empty">Couldn't load team: ${esc(e.message)}</p>`;
  }
}

const seasonLabel = (n) => `${String(n).slice(0, 4)}–${String(n).slice(6)}`;

async function loadPlayer(id) {
  const box = $("#playerDetail");
  box.hidden = false;
  box.innerHTML = `<p class="empty">Loading…</p>`;
  try {
    const { player: p, seasons } = await api(`/api/players/${encodeURIComponent(id)}`);
    const height = p.heightInInches ? `${Math.floor(p.heightInInches / 12)}′${p.heightInInches % 12}″` : "";
    const bio = [p.birthCountry && `Born ${p.birthDate || ""} in ${p.birthCountry}`, height,
      p.weightInPounds && `${p.weightInPounds} lb`, p.shoots && `Shoots ${p.shoots}`].filter(Boolean).join(" · ");
    const rows = seasons.slice().reverse().map((s) => `
      <tr><td>${seasonLabel(s.season)}</td><td>${esc(s.team)}</td><td>${s.gp}</td><td>${s.goals}</td>
        <td>${s.assists}</td><td><b>${s.points}</b></td><td>${s.plusMinus > 0 ? "+" : ""}${s.plusMinus}</td>
        <td>${s.pim}</td><td>${s.shots}</td></tr>`).join("");
    box.innerHTML = `
      <div class="pinfo">${p.headshot ? `<img src="${esc(p.headshot)}" alt="">` : ""}
        <div>
          <div class="pname">${esc(p.name)}</div>
          <div class="muted small">${p.number != null ? `#${esc(p.number)} · ` : ""}${esc(p.position)} · ${esc(p.team || "")}</div>
          <div class="muted small">${esc(bio)}</div>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Season</th><th>Team</th><th>GP</th><th>G</th><th>A</th><th>Pts</th><th>+/-</th><th>PIM</th><th>S</th></tr></thead>
          <tbody>${rows || `<tr><td colspan="9">No regular-season stats yet.</td></tr>`}</tbody>
        </table>
      </div>
      <button class="link-plain" data-close-player>Close</button>`;
  } catch (e) {
    box.innerHTML = `<p class="empty">Couldn't load player: ${esc(e.message)}</p>`;
  }
}

$("#teamsGrid").addEventListener("click", (e) => {
  const b = e.target.closest("[data-team-code]");
  if (b) loadTeam(b.dataset.teamCode);
});
$("#teamBack").onclick = () => loadTeams();
$("#skaters").addEventListener("click", (e) => {
  const row = e.target.closest("[data-player-id]");
  if (row) loadPlayer(row.dataset.playerId);
});
$("#playerDetail").addEventListener("click", (e) => {
  if (e.target.closest("[data-close-player]")) $("#playerDetail").hidden = true;
});

// ---------- news ----------

const relTime = (iso) => new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

async function loadNews() {
  const select = $("#newsTeam");
  try {
    if (!select.options.length) {
      const teams = await ensureTeams();
      select.innerHTML = `<option value="">All teams</option>` +
        teams.map((t) => `<option value="${esc(t.team)}">${esc(t.name)}</option>`).join("");
    }
  } catch {}
  $("#news").innerHTML = `<p class="empty">Loading…</p>`;
  try {
    const team = select.value ? `&team=${encodeURIComponent(select.value)}` : "";
    const { items } = await api(`/api/news?limit=40${team}`);
    $("#news").innerHTML = items.length
      ? items.map((n) => `
          <article class="card news-item">
            <div class="card-head">${esc(n.source)} · ${esc(relTime(n.published))}</div>
            <a class="news-title" href="${esc(safeHref(n.link))}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>
            ${n.summary ? `<p class="small muted">${esc(n.summary)}</p>` : ""}
            ${n.teams.length ? `<div class="chips">${n.teams.map((t) => `<span class="chip">${esc(t)}</span>`).join("")}</div>` : ""}
          </article>`).join("")
      : `<p class="empty">No headlines yet. They appear after the first feed refresh.</p>`;
  } catch (e) {
    $("#news").innerHTML = `<p class="empty">Couldn't load news: ${esc(e.message)}</p>`;
  }
}

$("#newsTeam").onchange = () => loadNews();

// ---------- navigation ----------

function load() {
  $("#dateLabel").textContent = prettyDate(date);
  $("#dateInput").value = date;
  if (tab === "games") loadGames();
  if (tab === "players") loadPlayers();
  if (tab === "standings") loadStandings();
  if (tab === "teams") loadTeams();
  if (tab === "news") loadNews();
  loadBoard();
}

function setTab(t) {
  tab = t;
  for (const b of $$("[data-tab]")) b.setAttribute("aria-selected", String(b.dataset.tab === t));
  for (const p of $$("[data-panel]")) p.hidden = p.dataset.panel !== t;
  $("#daybar").hidden = !["games", "players"].includes(t);
  load();
}

function setDate(d) {
  date = d;
  load();
}

for (const b of $$("[data-tab]")) b.onclick = () => setTab(b.dataset.tab);
$("#prev").onclick = () => setDate(shiftDate(date, -1));
$("#next").onclick = () => setDate(shiftDate(date, 1));
$("#today").onclick = () => setDate(localDate(new Date()));
$("#dateInput").onchange = (e) => e.target.value && setDate(e.target.value);

start();
