"use strict";
// Home page — portfolio value, recent additions, quick access.
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

function lineChart(pts) {
  const W = 700, H = 210, padL = 52, padR = 24, padT = 22, padB = 36;
  const vs = pts.map((p) => p.v);
  const min = Math.min(...vs), max = Math.max(...vs), span = max - min || 1;
  const x = (i) => padL + (i / (pts.length - 1)) * (W - padL - padR);
  const y = (v) => H - padB - ((v - min) / span) * (H - padT - padB);
  const path = pts.map((p, i) =>
    `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const area = `${path} L${x(pts.length - 1).toFixed(1)},${H - padB} L${x(0).toFixed(1)},${H - padB} Z`;
  const dots = pts.length <= 90 ? pts.map((p, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="2" fill="#7aa7e8">` +
    `<title>${fmtDate(p.t)} · $${p.v.toFixed(2)}</title></circle>`).join("") : "";
  const last = pts[pts.length - 1];
  const step = Math.max(1, Math.floor(pts.length / 6));
  const ticks = pts.map((p, i) => (i % step === 0)
    ? `<text x="${x(i).toFixed(1)}" y="${H - 14}" text-anchor="middle" font-size="10" fill="#8d97ab">${fmtDate(p.t)}</text>` : "").join("");
  return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
    <defs><linearGradient id="vg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#7aa7e8" stop-opacity=".35"/>
      <stop offset="100%" stop-color="#7aa7e8" stop-opacity="0"/>
    </linearGradient></defs>
    <path d="${area}" fill="url(#vg)"/>
    <path d="${path}" fill="none" stroke="#7aa7e8" stroke-width="2"/>
    ${dots}
    <circle cx="${x(pts.length - 1)}" cy="${y(last.v)}" r="4" fill="#7aa7e8"><title>now: $${last.v.toFixed(2)}</title></circle>
    <text x="${padL}" y="16" fill="#8d97ab" font-size="11">high $${max.toFixed(2)}</text>
    <text x="${W - padR}" y="16" fill="#eef2f8" font-size="12" text-anchor="end">now $${last.v.toFixed(2)}</text>
    ${ticks}
  </svg>`;
}

async function load() {
  // Decks quick tile only when the deck extension exists
  try {
    const r = await fetch("/api/decks");
    if (r.ok) $("quick-decks").classList.remove("hidden");
  } catch (e) {}
  let data;
  try {
    data = await fetch("/api/insights").then((r) => r.json());
  } catch (err) {
    $("value-chart").innerHTML = `<p class="dim">Failed to load: ${esc(err.message)}</p>`;
    return;
  }

  $("sum-value").textContent = "$" + data.total_value.toFixed(0);
  $("sum-count").textContent = data.total_cards;
  $("sum-wish").textContent = data.wishlist_count;
  $("sum-decks").textContent = data.deck_count || 0;

  const pts = (data.value_history || []).map((h) => ({ t: h.date, v: h.value }));
  if (pts.length < 2) {
    $("value-chart").innerHTML =
      `<p class="dim">Not enough price snapshots yet — hit “↻ Prices” on the ` +
      `library page a few times over the coming days.</p>`;
    $("value-note").textContent = "";
  } else {
    $("value-chart").innerHTML = lineChart(pts);
    $("value-note").textContent =
      `First snapshot ${fmtDate(pts[0].t)} · ${pts.length} snapshots · ` +
      `current $${data.total_value.toFixed(2)}`;
  }

  const recent = (data.recent || []).slice(0, 5);
  const box = $("recent");
  box.innerHTML = recent.length
    ? recent.map((c) =>
        `<div class="mini-row" data-cid="${c.scryfall_id}">` +
        `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
        `<div class="mini-main"><b>${esc(c.name)}</b><small>${esc(c.set_name || "?")} · ${c.quantity}×</small></div>` +
        `<span class="mini-date">${fmtDate(c.added_at || "")}</span></div>`).join("")
    : `<p class="dim">No cards yet — head to the library and scan your first card!</p>`;
  box.querySelectorAll(".mini-row").forEach((row) => {
    const c = recent.find((x) => x.scryfall_id === row.dataset.cid);
    row.style.cursor = "pointer";
    row.onclick = () => { if (window.openCardModal) window.openCardModal(c); };
  });
}

load();
