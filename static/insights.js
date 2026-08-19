"use strict";
// Collection insights — value over time, distributions, set completion.
const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}
function imgUrl(u) {
  return u ? "/api/img?u=" + encodeURIComponent(u) : "";
}

function fmtDate(iso) {
  const d = new Date(iso);
  let s = (d.getMonth() + 1) + "/" + d.getDate();
  if (d.getFullYear() !== new Date().getFullYear()) s += "/" + String(d.getFullYear()).slice(2);
  return s;
}

const RARITY_ORDER = ["common", "uncommon", "rare", "mythic", "special"];
const RARITY_COLORS = { common: "#8b8f9c", uncommon: "#9db4c7", rare: "#c9a86a",
                        mythic: "#c8724e", special: "#c8724e" };
const COLOR_COLORS = { W: "#e8e2d0", U: "#7fb6d9", B: "#9a92a8", R: "#d98a7f",
                       G: "#86b58a", multicolor: "#c9a86a", colorless: "#8b8f9c" };

// horizontal bar list: rows with value bars
function barList(rows, colorFn, valueKey = "value") {
  const max = Math.max(1, ...rows.map((r) => r[valueKey]));
  return rows.map((r) =>
    `<div class="hbar-row" title="${esc(r.label)}: $${r[valueKey].toFixed(2)}">` +
    `<span class="hbar-label">${esc(r.label)}</span>` +
    `<span class="hbar-track"><i style="width:${(r[valueKey] / max) * 100}%;background:${colorFn(r)}"></i></span>` +
    `<span class="hbar-val">$${r[valueKey] < 100 ? r[valueKey].toFixed(2) : r[valueKey].toFixed(0)}</span>` +
    `<span class="hbar-count">${r.count ?? ""}</span></div>`).join("");
}

// simple line chart
function lineChart(pts, W = 700) {
  const H = 210, padL = 52, padR = 24, padT = 22, padB = 36;
  const vs = pts.map((p) => p.v);
  const min = Math.min(...vs), max = Math.max(...vs), span = max - min || 1;
  const x = (i) => padL + (i / (pts.length - 1)) * (W - padL - padR);
  const y = (v) => H - padB - ((v - min) / span) * (H - padT - padB);
  const path = pts.map((p, i) =>
    `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const area = `${path} L${x(pts.length - 1).toFixed(1)},${H - padB} L${x(0).toFixed(1)},${H - padB} Z`;
  const dots = pts.length <= 90 ? pts.map((p, i) =>
    `<circle class="chart-dot" cx="${x(i).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="3">` +
    `<title>${fmtDate(p.t)} · $${p.v.toFixed(2)}</title></circle>`).join("") : "";
  const last = pts[pts.length - 1];
  const step = Math.max(1, Math.floor(pts.length / 6));
  const ticks = pts.map((p, i) => (i % step === 0)
    ? `<text x="${x(i).toFixed(1)}" y="${H - 14}" text-anchor="middle" class="chart-label">${fmtDate(p.t)}</text>` : "").join("");
  return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
    <defs><linearGradient id="vg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" style="stop-color:var(--accent-soft);stop-opacity:.35"/>
      <stop offset="100%" style="stop-color:var(--accent-soft);stop-opacity:0"/>
    </linearGradient></defs>
    <path d="${area}" class="chart-area" fill="url(#vg)"/>
    <path d="${path}" class="chart-line"/>
    ${dots}
    <circle class="chart-dot" cx="${x(pts.length - 1)}" cy="${y(last.v)}" r="4"><title>now: $${last.v.toFixed(2)}</title></circle>
    <text x="${padL}" y="16" class="chart-label">high $${max.toFixed(2)}</text>
    <text x="${W - padR}" y="16" class="chart-now" text-anchor="end">now $${last.v.toFixed(2)}</text>
    ${ticks}
  </svg>`;
}

let data = null;
let insightsGame = "all";
function withGame(url) {
  return url + (url.includes("?") ? "&" : "?") + "game=" + encodeURIComponent(insightsGame);
}

async function load() {
  try {
    data = await fetch(withGame("/api/insights")).then((r) => r.json());
  } catch (err) {
    document.querySelectorAll(".chart-box, .card-list, .progress-list, .commander-list")
      .forEach((el) => { el.innerHTML = `<p class="dim">Failed to load: ${esc(err.message)}</p>`; });
    return;
  }
  render();
}

function render() {
  $("sum-value").textContent = "$" + data.total_value.toFixed(0);
  $("sum-count").textContent = data.total_cards;
  $("sum-wish").textContent = data.wishlist_count;
  $("sum-decks").textContent = data.deck_count || 0;

  // color chart is MTG-only (other games have no mana colors)
  const colorPanel = $("color-chart").closest(".panel");
  if (colorPanel) colorPanel.classList.toggle("hidden",
    insightsGame !== "all" && insightsGame !== "mtg");

  // value over time
  const pts = (data.value_history || []).map((h) => ({ t: h.date, v: h.value }));
  const vc = $("value-chart");
  const renderValueChart = () => {
    if (pts.length < 2) {
      vc.innerHTML =
        `<p class="dim">Not enough price snapshots yet — hit “Prices” on the library page a few times over the coming days.</p>`;
      return;
    }
    // render at the container's real width so chart text keeps its px size
    vc.innerHTML = lineChart(pts, Math.max(300, vc.clientWidth));
    const costNote = data.total_paid
      ? ` · paid $${data.total_paid.toFixed(2)} · ` +
        (data.gain >= 0 ? "+$" : "−$") + Math.abs(data.gain).toFixed(2)
      : "";
    $("value-note").textContent =
      `First snapshot ${fmtDate(pts[0].t)} · ${pts.length} snapshots · ` +
      `current $${data.total_value.toFixed(2)}${costNote}`;
  };
  renderValueChart();
  if (window.ResizeObserver) new ResizeObserver(renderValueChart).observe(vc);

  // gainers & losers
  const movers = data.movers || [];
  $("movers-note").textContent = movers.length
    ? "(price change since the previous snapshot)"
    : "(shows up after two price refreshes)";
  $("movers").innerHTML = movers.length
    ? movers.map((m) => {
        const up = m.pct >= 0;
        return `<div class="mover-row" data-sid="${esc(m.scryfall_id)}">` +
          `<img loading="lazy" src="${imgUrl(m.image_uri || "")}">` +
          `<div class="mini-main"><b>${esc(m.name)}</b>` +
          `<small>${esc(m.set_name || "?")} · $${m.old.toFixed(2)} → $${m.new.toFixed(2)}</small></div>` +
          `<span class="mover-pct ${up ? "up" : "down"}">${up ? "+" : ""}${m.pct}%</span></div>`;
      }).join("")
    : `<p class="dim">No price movement yet — refresh prices a couple of times to build history.</p>`;
  wireCardClicks($("movers"), movers);

  // rarity
  const rarity = (data.rarity || []).map((r) => ({
    label: r.rarity, value: r.value, count: r.count,
  }));
  rarity.sort((a, b) => {
    const ia = RARITY_ORDER.indexOf(a.label), ib = RARITY_ORDER.indexOf(b.label);
    return (ia >= 0 ? ia : 99) - (ib >= 0 ? ib : 99);
  });
  $("rarity-chart").innerHTML = rarity.length
    ? barList(rarity, (r) => RARITY_COLORS[r.label] || "#9aa3b2")
    : `<p class="dim">No data yet.</p>`;

  // colors
  const colors = (data.colors || []).map(([k, count]) => ({
    label: k === "multicolor" ? "Multicolor" : k === "colorless" ? "Colorless" : ({ W: "White", U: "Blue", B: "Black", R: "Red", G: "Green" }[k] || k),
    value: count, count: "", isColor: true, key: k,
  }));
  const maxCol = Math.max(1, ...colors.map((c) => c.value));
  $("color-chart").innerHTML = colors.length
    ? colors.map((c) =>
        `<div class="hbar-row" title="${c.label}">` +
        `<span class="hbar-label">${esc(c.label)}</span>` +
        `<span class="hbar-track"><i style="width:${(c.value / maxCol) * 100}%;background:${COLOR_COLORS[c.key] || "#8b8f9c"}"></i></span>` +
        `<span class="hbar-val">${c.value}</span><span></span></div>`).join("")
    : `<p class="dim">No data yet.</p>`;

  // top sets
  const sets = (data.top_sets || []).map((s) => ({
    label: s.name, value: s.value, count: s.count,
  }));
  $("sets-chart").innerHTML = sets.length
    ? barList(sets, () => "#c9a86a")
    : `<p class="dim">No data yet.</p>`;

  // set completion
  const prog = data.set_progress || [];
  $("progress-note").textContent = prog.length
    ? "(of printings in the offline index — approximations for sets with reprints)"
    : "(download the offline database to see completion)";
  $("progress-panel").classList.toggle("hidden", !prog.length);
  $("set-progress").innerHTML = prog.map((s) =>
    `<div class="progress-row" title="${s.name}: ${s.owned}/${s.total} printings">` +
    `<span class="progress-name">${esc(s.name)}</span>` +
    `<span class="progress-track"><i style="width:${Math.min(100, s.pct)}%;${s.pct >= 99 ? "background:var(--green)" : ""}"></i></span>` +
    `<span class="progress-val">${s.owned}/${s.total} · ${s.pct}%</span></div>`).join("");

  // top cards
  const topCards = data.top_cards || [];
  $("top-cards").innerHTML = topCards.map((c) =>
    `<div class="mini-row" data-sid="${esc(c.scryfall_id)}">` +
    `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
    `<div class="mini-main"><b>${esc(c.name)}</b><small>${esc(c.set_name || "?")} · ${c.quantity}×</small></div>` +
    `<span class="mini-val">$${(c.line_value || 0).toFixed(2)}</span></div>`).join("") ||
    `<p class="dim">No data yet.</p>`;
  wireCardClicks($("top-cards"), topCards);

  // commanders
  $("commanders").innerHTML = (data.commanders || []).length
    ? data.commanders.map((c) =>
        `<div class="mini-row"><span class="mini-rank">${c.qty}</span>` +
        `<div class="mini-main"><b>${esc(c.name)}</b><small>decks</small></div></div>`).join("")
    : `<p class="dim">No decks yet — build or import one and your commanders show up here.</p>`;

  // recent
  const recent = data.recent || [];
  $("recent").innerHTML = recent.map((c) =>
    `<div class="mini-row" data-sid="${esc(c.scryfall_id)}">` +
    `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
    `<div class="mini-main"><b>${esc(c.name)}</b><small>${esc(c.set_name || "?")}</small></div>` +
    `<span class="mini-date">${fmtDate(c.added_at || "")}</span></div>`).join("") ||
    `<p class="dim">No cards yet.</p>`;
  wireCardClicks($("recent"), recent);

  // deck win/loss records + losses by commander
  const mu = data.matchups || { records: [], matchups_by: [] };
  $("deck-records").innerHTML = mu.records.length
    ? mu.records.map((r) =>
        `<div class="mover-row"><div class="mini-main"><b>${esc(r.name)}</b>` +
        `<small>${r.wins}W – ${r.losses}L</small></div>` +
        `<span class="mover-pct ${r.winrate != null && r.winrate >= 50 ? "up" : "down"}">` +
        `${r.winrate != null ? r.winrate + "%" : "—"}</span></div>`).join("")
    : `<p class="dim">No games recorded yet — open a deck and log wins and losses.</p>`;
  $("losses-by").innerHTML = mu.matchups_by.length
    ? mu.matchups_by.map((o) => {
        const wr = o.winrate != null ? o.winrate + "%" : "—";
        return `<div class="mini-row"><span class="mini-rank">${wr}</span>` +
          `<div class="mini-main"><b>${esc(o.commander)}</b>` +
          `<small>${o.wins}W – ${o.losses}L</small></div></div>`;
      }).join("")
    : `<p class="dim">No games recorded yet.</p>`;
}

function wireCardClicks(box, cards) {
  box.querySelectorAll(".mini-row").forEach((row) => {
    const c = cards.find((x) => x.scryfall_id === row.dataset.sid);
    row.style.cursor = "pointer";
    row.onclick = () => { if (window.openCardModal) window.openCardModal(c); };
  });
}

$("insights-game").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-game]");
  if (!b) return;
  insightsGame = b.dataset.game;
  document.querySelectorAll("#insights-game button").forEach((x) =>
    x.classList.toggle("active", x === b));
  load();
});

load();
