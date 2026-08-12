"use strict";
/*
 * Shared view-only card detail modal: 3D flip, prices, price history,
 * oracle text & rulings. Self-injects into any page that loads it and
 * exposes window.openCardModal(card). Used by the home, insights and
 * decks pages (the library page keeps its own editable modal).
 */
(function () {
  if (window.__cardModalLoaded) return;
  window.__cardModalLoaded = true;

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

  let modal = null;

  function close() {
    if (modal) modal.classList.add("hidden");
  }

  function inject() {
    if (modal) return modal;
    modal = document.createElement("div");
    modal.id = "card-modal";
    modal.className = "hidden";
    modal.innerHTML =
      `<div class="card-float">
        <div class="flip3d" id="flip3d">
          <div class="flip3d-inner" id="flip3d-inner">
            <div class="flip3d-face flip3d-front"><img id="detail-img" alt=""></div>
            <div class="flip3d-face flip3d-back"><img id="detail-back" alt=""></div>
          </div>
          <p class="flip-hint">↻ drag to flip</p>
        </div>
      </div>
      <div class="modal-box detail-box">
        <div class="detail-facts">
          <div class="detail-info">
            <h3 id="detail-name"></h3>
            <p id="detail-set"></p>
            <p id="detail-prices" class="price"></p>
            <a id="detail-scryfall" target="_blank" rel="noopener">View on Scryfall ↗</a>
          </div>
          <div id="detail-history"></div>
          <details class="oracle-box">
            <summary>Oracle text &amp; rulings</summary>
            <div id="oracle-body"><p class="dim">Loading…</p></div>
          </details>
          <div class="detail-actions">
            <button id="detail-close" class="ghost">Close</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(modal);

    document.getElementById("detail-close").onclick = close;
    modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
    // capture-phase Escape so this modal closes without also closing the
    // page's own modals underneath (decks page has its own Escape handling)
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.classList.contains("hidden")) {
        close();
        e.stopImmediatePropagation();
      }
    }, true);

    // 3D flip
    let flipRotY = 0, flipRotX = -8, flipDrag = null;
    const flip3d = modal.querySelector("#flip3d");
    const flipInner = modal.querySelector("#flip3d-inner");
    const flipApply = () => {
      flipInner.style.transform = `rotateX(${flipRotX}deg) rotateY(${flipRotY}deg)`;
    };
    flip3d.addEventListener("pointerdown", (e) => {
      flipDrag = { x: e.clientX, y: e.clientY, ry: flipRotY, rx: flipRotX };
      flip3d.setPointerCapture(e.pointerId);
      flipInner.classList.add("dragging");
      flipApply();
    });
    flip3d.addEventListener("pointermove", (e) => {
      if (!flipDrag) return;
      flipRotY = flipDrag.ry + (e.clientX - flipDrag.x) * 0.9;
      flipRotX = Math.max(-38, Math.min(38, flipDrag.rx + (e.clientY - flipDrag.y) * 0.35));
      flipApply();
    });
    const flipRelease = () => {
      if (!flipDrag) return;
      flipDrag = null;
      flipInner.classList.remove("dragging");
      flipRotY = Math.round(flipRotY / 180) * 180;
      flipRotX = -8;
      flipApply();
    };
    flip3d.addEventListener("pointerup", flipRelease);
    flip3d.addEventListener("pointercancel", flipRelease);
    flip3d.addEventListener("dblclick", () => {
      flipRotY = flipRotY % 360 < 90 || flipRotY % 360 > 270 ? 180 : 0;
      flipApply();
    });
    return modal;
  }

  function lineChart(pts, W = 460) {
    const H = 170, padL = 34, padR = 30, padT = 24, padB = 40;
    const vs = pts.map((p) => p.v);
    const min = Math.min(...vs), max = Math.max(...vs), span = max - min || 1;
    const x = (i) => padL + (i / (pts.length - 1)) * (W - padL - padR);
    const y = (v) => H - padB - ((v - min) / span) * (H - padT - padB);
    const path = pts.map((p, i) =>
      `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    const dots = pts.length <= 60 ? pts.map((p, i) =>
      `<circle class="chart-dot" cx="${x(i).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="3">` +
      `<title>${fmtDate(p.t)} · $${p.v.toFixed(2)}</title></circle>`).join("") : "";
    const last = pts[pts.length - 1];
    return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
      <path d="${path}" class="chart-line"/>
      ${dots}
      <circle class="chart-dot" cx="${x(pts.length - 1)}" cy="${y(last.v)}" r="4"><title>now: $${last.v.toFixed(2)}</title></circle>
      <text x="${padL}" y="16" class="chart-label">high $${max.toFixed(2)}</text>
      <text x="${padL}" y="${H - padB + 16}" class="chart-label">low $${min.toFixed(2)}</text>
      <text x="${padL}" y="${H - 8}" class="chart-label">${fmtDate(pts[0].t)}</text>
      <text x="${W - padR}" y="${H - 8}" class="chart-label" text-anchor="end">${fmtDate(last.t)}</text>
      <text x="${W - padR}" y="16" class="chart-now" text-anchor="end">now $${last.v.toFixed(2)}</text>
    </svg>`;
  }

  async function showHistory(card) {
    const box = modal.querySelector("#detail-history");
    const resp = await fetch("/api/history/" + card.scryfall_id);
    const data = await resp.json();
    const pts = (data.history || [])
      .map((h) => ({ t: h.recorded_at, v: card.foil ? h.usd_foil : h.usd }))
      .filter((p) => p.v != null);
    const render = () => {
      if (pts.length < 2) {
        const cur = card.price_usd != null ? card.price_usd : card.unit_price;
        box.innerHTML = `<p class="dim">Not enough price history yet — prices are ` +
          `snapshotted each time you refresh. Current: ` +
          (cur != null ? "$" + cur.toFixed(2) : "—") + `</p>`;
        return;
      }
      // render at the container's real width so chart text keeps its px size
      box.innerHTML = lineChart(pts, Math.max(300, box.clientWidth));
    };
    render();
    if (window.ResizeObserver && !box._ro) {
      box._ro = new ResizeObserver(render);
      box._ro.observe(box);
    }
  }

  async function showOracle(card) {
    const body = modal.querySelector("#oracle-body");
    try {
      const resp = await fetch("/api/card/" + card.scryfall_id);
      const d = await resp.json();
      let html = "";
      if (d.oracle_text) html += `<div class="oracle-text">${esc(d.oracle_text)}</div>`;
      if (d.rulings && d.rulings.length) {
        html += `<div class="rulings">` + d.rulings.map((r) =>
          `<p><b>${esc((r.date || "").slice(0, 10))}</b> ${esc(r.text)}</p>`).join("") + `</div>`;
      }
      body.innerHTML = html || `<p class="dim">No oracle text or rulings available.</p>`;
    } catch (err) {
      body.innerHTML = `<p class="dim">Couldn't load: ${esc(err.message)}</p>`;
    }
  }

  function open(card) {
    if (!card) return;
    inject();
    modal.querySelector("#detail-img").src = imgUrl(card.image_uri || "");
    modal.querySelector("#detail-back").src = imgUrl(card.back_image_uri) || "/cardback.jpg";
    modal.querySelector("#detail-name").innerHTML =
      esc(card.name) + (card.foil ? " " + icon("foil") : "");
    modal.querySelector("#detail-set").textContent =
      `${card.set_name || "?"} (${(card.set_code || "").toUpperCase()}) · #${card.collector_number || "?"} · ${card.rarity || ""}`;
    const np = card.price_usd != null ? card.price_usd : (card.foil ? null : card.unit_price);
    const fp = card.price_usd_foil != null ? card.price_usd_foil : (card.foil ? card.unit_price : null);
    modal.querySelector("#detail-prices").textContent =
      `Non-foil ${np != null ? "$" + np.toFixed(2) : "—"} · Foil ${fp != null ? "$" + fp.toFixed(2) : "—"}`;
    modal.querySelector("#detail-scryfall").href = card.scryfall_uri || "#";
    modal.querySelector("#detail-history").innerHTML = `<p class="dim">Loading price history…</p>`;
    modal.querySelector("#oracle-body").innerHTML = `<p class="dim">Loading…</p>`;
    modal.classList.remove("hidden");
    showHistory(card);
    showOracle(card);
  }

  window.openCardModal = open;
})();
