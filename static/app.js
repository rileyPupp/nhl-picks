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
  if (res.status === 401 && !path.startsWith("/api/login")) showAuth();
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

function showAuth() {
  $("#app").hidden = true;
  $("#auth").hidden = false;
  $("#authName").focus();
}

function showApp(user) {
  $("#auth").hidden = true;
  $("#app").hidden = false;
  $("#whoName").textContent = user.name;
  setTab(tab);
}

$("#authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = e.submitter?.dataset.mode || "login";
  const msg = $("#authMsg");
  msg.hidden = true;
  try {
    const { user } = await post(`/api/${mode}`, { name: $("#authName").value.trim(), pin: $("#authPin").value });
    $("#authPin").value = "";
    showApp(user);
  } catch (err) {
    msg.textContent = err.message;
    msg.hidden = false;
  }
});

$("#logout").onclick = async () => {
  await post("/api/logout", {}).catch(() => {});
  showAuth();
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

// ---------- leaderboard ----------

async function loadBoard() {
  try {
    const b = await api("/api/leaderboard");
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

// ---------- navigation ----------

function load() {
  $("#dateLabel").textContent = prettyDate(date);
  $("#dateInput").value = date;
  if (tab === "games") loadGames();
  if (tab === "players") loadPlayers();
  loadBoard();
}

function setTab(t) {
  tab = t;
  for (const b of $$("[data-tab]")) b.setAttribute("aria-selected", String(b.dataset.tab === t));
  for (const p of $$("[data-panel]")) p.hidden = p.dataset.panel !== t;
  $("#daybar").hidden = t === "board";
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

api("/api/me")
  .then(({ user }) => (user ? showApp(user) : showAuth()))
  .catch(showAuth);
