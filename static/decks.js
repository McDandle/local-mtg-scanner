/*
 * Deck Builder UI for Local MTG Scanner (MIT licensed, see LICENSE).
 *
 * Runs on the dedicated /decks.html page (self-contained — no dependency
 * on app.js). Deck import from Archidekt by URL/ID, plus best-effort
 * popular-deck search; every deck is indexed against the scanned
 * collection to show owned vs. to-order with a priced buy list.
 */
(function () {
  "use strict";
  if (window.__decksLoaded) return;
  if (!document.body || document.body.dataset.page !== "decks") return;
  window.__decksLoaded = true;

  // ------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }
  function imgUrl(u) {
    return u ? "/api/img?u=" + encodeURIComponent(u) : "";
  }
  function toast(msg, ms = 2400) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.add("hidden"), ms);
  }

  const ROLES = ["commander", "main", "sideboard"];
  const ROLE_LABEL = { commander: "Commander", main: "Main deck", sideboard: "Sideboard" };
  const FORMATS = ["", "Commander", "Standard", "Modern", "Pioneer",
                   "Legacy", "Vintage", "Pauper", "Brawl", "Other"];

  let decks = [];
  let currentDeck = null;   // {deck, stats, cards} from /api/decks/<id>
  let deckSearchTimer = null;
  let buylist = null;

  // ------------------------------------------------------------ deck list
  async function loadDecks(initial) {
    let data;
    try {
      data = await fetch("/api/decks").then((r) => r.json());
    } catch (err) {
      if (initial) {
        $("decks-list").innerHTML =
          `<p style="color:var(--dim);text-align:center;margin:14px 0">Deck builder unavailable.</p>`;
      }
      return;
    }
    decks = data.decks || [];
    renderDecks();
    updateHero();
  }

  function renderDecks() {
    const box = $("decks-list");
    if (!decks.length) {
      box.innerHTML =
        `<p style="color:var(--text-dim);text-align:center;margin:14px 0">No decks yet — ` +
        `import one above, or create a deck and add cards by search.</p>`;
      return;
    }
    box.innerHTML = "";
    for (const d of decks) {
      const shelf = document.createElement("div");
      shelf.className = "deck-shelf";
      const complete = d.missing <= 0;
      const head = document.createElement("button");
      head.className = "deck-head";
      head.innerHTML =
        `<span class="dot-color" style="background:var(--accent)"></span>` +
        `<span class="name"></span>` +
        `<span class="meta">${esc(d.format || "No format")} · ${d.card_count} cards</span>` +
        `<span class="rule"></span>` +
        `<span class="state ${complete ? "" : "pending"}">${complete ? "✓ complete" : d.missing + " to order"}</span>` +
        `<span class="open btn small">open editor →</span>`;
      head.querySelector(".name").textContent = d.name;
      head.onclick = () => openDeck(d.id);
      shelf.appendChild(head);
      if (d.cards && d.cards.length) {
        const row = document.createElement("div");
        row.className = "shelf";
        for (const c of d.cards) {
          const wrap = document.createElement("div");
          wrap.innerHTML = deckTile(c);
          const tile = wrap.firstChild;
          tile.onclick = () => openDeck(d.id);
          row.appendChild(tile);
        }
        // "View all" — expands the shelf into the full deck, grouped by type
        const viewAll = document.createElement("button");
        viewAll.className = "more";
        viewAll.textContent = d.card_count > d.cards.length ? `View all ${d.card_count} →` : "View all →";
        viewAll.onclick = async () => {
          const wall = shelf.querySelector(".deck-wall");
          if (wall) { wall.remove(); viewAll.textContent = "View all →"; return; }
          viewAll.disabled = true;
          viewAll.textContent = "…";
          try {
            const resp = await fetch("/api/decks/" + d.id);
            const data = await resp.json();
            if (data.deck && data.cards) {
              const w = document.createElement("div");
              w.className = "deck-wall";
              renderDeckWall(data.cards, w);
              shelf.appendChild(w);
              viewAll.textContent = "▴ collapse";
            } else {
              viewAll.textContent = "View all →";
            }
          } catch (err) {
            viewAll.textContent = "View all →";
          }
          viewAll.disabled = false;
        };
        row.appendChild(viewAll);
        shelf.appendChild(row);
      }
      box.appendChild(shelf);
    }
  }

  // full deck grouped by classification — the "View all" wall under a shelf
  function renderDeckWall(cards, container) {
    const groups = new Map();
    for (const c of cards) {
      const t = deckType(c.type_line);
      if (!groups.has(t)) groups.set(t, []);
      groups.get(t).push(c);
    }
    for (const t of DECKVIEW_TYPES) {
      const list = groups.get(t);
      if (!list) continue;
      list.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
      const q = list.reduce((s, c) => s + c.quantity, 0);
      const v = list.reduce((s, c) => s + (c.unit_price || 0) * c.quantity, 0);
      const head = document.createElement("div");
      head.className = "deckview-group";
      head.innerHTML = `<b>${t}</b><small>${q} · $${v.toFixed(2)}</small>`;
      container.appendChild(head);
      const grid = document.createElement("div");
      grid.className = "deckview-grid";
      for (const c of list) grid.appendChild(deckviewCell(c));
      container.appendChild(grid);
    }
  }

  function deckTile(c) {
    const badges = [];
    if (c.foil) badges.push(`<span class="badge foil">FOIL</span>`);
    if (c.quantity > 1) badges.push(`<span class="badge">×${c.quantity}</span>`);
    if (c.need > 0) badges.push(`<span class="badge order">+${c.need}</span>`);
    return `<div class="card-tile${c.foil ? " is-foil" : ""}${c.need > 0 ? " is-missing" : ""}" title="${esc(c.name)}">` +
      (c.image_uri ? `<img loading="lazy" src="${imgUrl(c.image_uri)}" alt="">` : `<div class="art-fallback"></div>`) +
      `<div class="scrim">` +
      (c.unit_price != null ? `<span class="price">$${c.unit_price.toFixed(2)}</span>` : `<span class="price">—</span>`) +
      badges.join("") +
      `<span class="gem ${c.rarity || "common"}"></span>` +
      `</div></div>`;
  }

  function updateHero() {
    let cards = 0, owned = 0, missing = 0;
    for (const d of decks) {
      cards += d.card_count || 0;
      owned += d.owned || 0;
      missing += d.missing || 0;
    }
    $("sum-decks").textContent = decks.length;
    $("sum-cards").textContent = cards;
    $("sum-owned").textContent = owned;
    $("sum-missing").textContent = missing;
  }

  async function newDeck() {
    const resp = await fetch("/api/decks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Untitled deck" }),
    });
    const d = await resp.json();
    if (d.ok) {
      toast("Deck created");
      openDeck(d.id);
    } else toast("Error: " + (d.error || "create failed"));
  }

  // ------------------------------------------------------------ deck editor
  async function openDeck(id) {
    const data = await fetch("/api/decks/" + id).then((r) => r.json());
    if (data.error) { toast("Error: " + data.error); return; }
    currentDeck = data;
    $("deck-name").value = data.deck.name;
    $("deck-format").value = data.deck.format || "";
    renderDeck();
    $("deck-modal").classList.remove("hidden");
  }

  function closeDeckModal() {
    $("deck-modal").classList.add("hidden");
    currentDeck = null;
    $("deck-search").value = "";
    $("deck-search-results").innerHTML = "";
    $("deck-cards").innerHTML = "";
  }

  async function refreshDeck(id) {
    const data = await fetch("/api/decks/" + id).then((r) => r.json());
    if (data.error) return;
    currentDeck = data;
    renderDeck();
  }

  function renderDeck() {
    if (!currentDeck) return;
    const { deck, stats, cards } = currentDeck;
    const box = $("deck-cards");
    const pct = stats.total ? Math.round((stats.owned / stats.total) * 100) : 0;

    $("deck-stat-total").textContent = stats.total;
    $("deck-stat-owned").textContent = stats.owned;
    $("deck-stat-missing").textContent = stats.missing;
    $("deck-stat-cost").textContent = "$" + stats.missing_value.toFixed(2);
    const bar = $("deck-progress-bar");
    bar.style.width = pct + "%";
    bar.style.background = stats.missing > 0 ? "var(--accent)" : "var(--green)";

    box.innerHTML = "";
    if (!cards.length) {
      box.innerHTML = `<p style="color:var(--dim);text-align:center;margin:16px 0">` +
        `No cards yet — search above to add some, or paste a decklist.</p>`;
      return;
    }
    for (const role of ROLES) {
      const group = cards.filter((c) => c.role === role);
      if (!group.length) continue;
      const head = document.createElement("div");
      head.className = "deck-group-head";
      const gOwned = group.reduce((s, c) => s + c.owned, 0);
      const gMissing = group.reduce((s, c) => s + c.need, 0);
      head.innerHTML = `<b>${ROLE_LABEL[role]}</b>` +
        (gMissing
          ? `<small>${group.length} lines · own ${gOwned} · <span class="miss">need ${gMissing}</span></small>`
          : `<small>${group.length} lines · <span class="full">✓ complete</span></small>`);
      box.appendChild(head);
      for (const c of group) box.appendChild(deckCardRow(c));
    }
    renderDeckStats();
  }

  function deckCardRow(c) {
    const row = document.createElement("div");
    row.className = "deck-card" + (c.need > 0 ? " missing" : "");
    const status = c.need > 0
      ? `<span class="own">own ${c.owned}</span><span class="miss">need ${c.need}</span>`
      : `<span class="full">✓</span>`;
    row.innerHTML =
      `<img loading="lazy" src="${imgUrl(c.image_uri || "")}" alt="">` +
      `<div class="deck-card-main"><b>${esc(c.name)}${c.foil ? " <span class=\"foil-tag\">" + icon("foil") + "</span>" : ""}</b>` +
      `<small>${esc(c.set_name || "?")} · #${esc(c.collector_number || "?")} · ${esc(c.rarity || "")}</small></div>` +
      `<div class="deck-card-status">${status}</div>` +
      `<div class="deck-card-qty"><button data-d="-1">−</button><span>${c.quantity}</span><button data-d="1">+</button></div>` +
      `<select class="deck-card-role" title="Move between Commander / Main / Sideboard">` +
      ROLES.map((r) => `<option value="${r}" ${r === c.role ? "selected" : ""}>${r === "commander" ? "C" : r === "main" ? "M" : "S"}</option>`).join("") +
      `</select>` +
      `<button class="deck-card-rm" title="Remove from deck">✕</button>`;

    row.querySelector("img").onclick = (e) => { e.stopPropagation(); openCardModal(c); };
    row.querySelector(".deck-card-main").onclick = () => openCardModal(c);

    row.querySelector(".deck-card-qty button[data-d='1']").onclick = async () => {
      await postCard(c, { quantity: 1 });
      refreshDeck(currentDeck.deck.id);
    };
    row.querySelector(".deck-card-qty button[data-d='-1']").onclick = async () => {
      await postCard(c, { quantity: c.quantity - 1, replace: true });
      refreshDeck(currentDeck.deck.id);
    };
    row.querySelector(".deck-card-rm").onclick = async () => {
      await fetch(`/api/decks/${currentDeck.deck.id}/cards/remove`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: c.id }),
      });
      toast("Removed " + c.name);
      refreshDeck(currentDeck.deck.id);
    };
    row.querySelector(".deck-card-role").onchange = async (e) => {
      const newRole = e.target.value;
      if (newRole === c.role) return;
      await fetch(`/api/decks/${currentDeck.deck.id}/cards/remove`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: c.id }),
      });
      await postCard(c, { quantity: c.quantity, role: newRole });
      refreshDeck(currentDeck.deck.id);
    };
    return row;
  }

  // c is a deck card row or a search summary; opts override quantity/role/foil
  function postCard(c, opts) {
    const card = {
      scryfall_id: c.scryfall_id, name: c.name, set_code: c.set_code,
      set_name: c.set_name, collector_number: c.collector_number,
      rarity: c.rarity, mana_cost: c.mana_cost, type_line: c.type_line,
      colors: c.colors, image_uri: c.image_uri, back_image_uri: c.back_image_uri,
      price_usd: c.price_usd, price_usd_foil: c.price_usd_foil,
    };
    return fetch(`/api/decks/${currentDeck.deck.id}/cards`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        card, quantity: opts.quantity, role: opts.role || c.role,
        foil: opts.foil != null ? opts.foil : !!c.foil, replace: !!opts.replace,
      }),
    }).then((r) => r.json());
  }

  async function saveDeckMeta() {
    if (!currentDeck) return;
    const name = $("deck-name").value.trim();
    const format = $("deck-format").value;
    await fetch(`/api/decks/${currentDeck.deck.id}/update`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name || "Untitled deck", format }),
    });
    currentDeck.deck.name = name || "Untitled deck";
    currentDeck.deck.format = format;
    loadDecks(false);
  }

  async function deleteDeck() {
    if (!currentDeck) return;
    if (!confirm(`Delete “${currentDeck.deck.name}”?`)) return;
    await fetch(`/api/decks/${currentDeck.deck.id}/delete`, { method: "POST" });
    toast("Deleted deck");
    closeDeckModal();
    loadDecks(false);
  }

  async function duplicateDeck() {
    if (!currentDeck) return;
    const resp = await fetch(`/api/decks/${currentDeck.deck.id}/duplicate`, { method: "POST" });
    const d = await resp.json();
    if (d.ok) {
      toast("Duplicated");
      closeDeckModal();
      openDeck(d.id);
      loadDecks(false);
    } else toast("Error: " + (d.error || "duplicate failed"));
  }

  async function addToCollection() {
    if (!currentDeck) return;
    if (!confirm(`Add every card in “${currentDeck.deck.name}” to your collection?\nQuantities merge with what you already own.`)) return;
    toast("Adding to collection…", 6000);
    try {
      const resp = await fetch(`/api/decks/${currentDeck.deck.id}/add-to-collection`, { method: "POST" });
      const d = await resp.json();
      if (d.ok) {
        toast(`Added ${d.added} new, merged ${d.updated} — ${d.total_cards} cards`, 5000);
      } else toast("Error: " + (d.error || "failed"), 5000);
    } catch (err) {
      toast("Failed: " + err.message, 5000);
    }
  }

  // ------------------------------------------------------------ add by search
  function onDeckSearch(e) {
    clearTimeout(deckSearchTimer);
    const q = e.target.value.trim();
    if (q.length < 2) { $("deck-search-results").innerHTML = ""; return; }
    deckSearchTimer = setTimeout(() => runDeckSearch(q), 300);
  }

  async function runDeckSearch(q) {
    const resp = await fetch("/api/search?q=" + encodeURIComponent(q));
    const data = await resp.json();
    const box = $("deck-search-results");
    box.innerHTML = "";
    for (const c of (data.cards || [])) {
      const div = document.createElement("div");
      div.className = "deck-search-card";
      div.innerHTML = `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
        `<small>${esc(c.set_name)} · #${esc(c.collector_number)}</small>`;
      div.onclick = async () => {
        const role = $("deck-role").value;
        const foil = $("deck-foil").checked;
        await postCard(c, { quantity: 1, role, foil });
        toast(`Added ${c.name}`);
        refreshDeck(currentDeck.deck.id);
      };
      box.appendChild(div);
    }
  }

  // ------------------------------------------------------------ buy list
  async function openBuylist() {
    if (!currentDeck) return;
    const cheapest = $("buylist-cheapest").checked ? "1" : "";
    const data = await fetch(
      `/api/decks/${currentDeck.deck.id}/missing?cheapest=${cheapest}`).then((r) => r.json());
    buylist = data;
    const box = $("buylist-items");
    box.innerHTML = "";
    if (!data.items.length) {
      box.innerHTML = `<p style="color:var(--green);text-align:center;margin:20px 0">` +
        `Nothing to order — you own every card in this deck. ✦</p>`;
    }
    for (const i of data.items) {
      const row = document.createElement("div");
      row.className = "buylist-row";
      const unit = i.unit_price != null ? "$" + i.unit_price.toFixed(2) : "—";
      row.innerHTML =
        `<img loading="lazy" src="${imgUrl(i.image_uri || "")}">` +
        `<div class="buylist-main"><b>${esc(i.name)}${i.foil ? " " + icon("foil") : ""}</b>` +
        `<small>${esc(i.set_name || "?")} · #${esc(i.collector_number || "?")} · ${esc(i.rarity || "")}</small></div>` +
        `<div class="buylist-qty">×${i.qty}</div>` +
        `<div class="buylist-price"><small>${unit} each</small><b>$${i.total.toFixed(2)}</b></div>`;
      row.querySelector("img").onclick = () => openCardModal(i);
      row.querySelector(".buylist-main").onclick = () => openCardModal(i);
      box.appendChild(row);
    }
    $("buylist-note").textContent =
      `${data.items.length} printing${data.items.length === 1 ? "" : "s"} to order · ` +
      `${data.stats.missing} cards`;
    $("buylist-total").textContent = "$" + data.total.toFixed(2);
    $("buylist-modal").classList.remove("hidden");
  }

  function closeBuylist() {
    $("buylist-modal").classList.add("hidden");
    buylist = null;
  }

  function buylistText() {
    if (!buylist) return "";
    const lines = [`${currentDeck.deck.name} — buy list`];
    for (const i of buylist.items) {
      const set = (i.set_code || "").toUpperCase();
      const unit = i.unit_price != null ? "$" + i.unit_price.toFixed(2) : "—";
      lines.push(`${i.qty}× ${i.name} (${set} #${i.collector_number || "?"})${i.foil ? " ✦" : ""} — ${unit} each — $${i.total.toFixed(2)}`);
    }
    lines.push(`Total: $${buylist.total.toFixed(2)}`);
    return lines.join("\n");
  }

  async function copyText(text, toastMsg) {
    try {
      await navigator.clipboard.writeText(text);
      toast(toastMsg);
    } catch (err) {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); toast(toastMsg); } catch (e) { toast("Copy failed"); }
      ta.remove();
    }
  }

  async function copyBuylist() {
    copyText(buylistText(), "Buy list copied");
  }

  function openTcgplayer() {
    if (!buylist) return;
    // No foil marker: TCGplayer Mass Entry has no reliable foil syntax.
    const lines = buylist.items.map((i) =>
      `${i.qty} ${i.name} ${(i.set_code || "").toUpperCase()} ${i.collector_number || ""}`);
    const url = "https://www.tcgplayer.com/massentry?productline=Magic&catalogId=1&q=" +
      encodeURIComponent(lines.join("\n"));
    window.open(url, "_blank");
  }

  function exportBuylistCsv() {
    if (!buylist) return;
    const rows = [["name", "set_code", "set_name", "collector_number", "rarity",
                   "foil", "quantity", "unit_price", "total"]];
    for (const i of buylist.items) {
      rows.push([i.name, i.set_code || "", i.set_name || "", i.collector_number || "",
                 i.rarity || "", i.foil ? "foil" : "nonfoil", i.qty,
                 i.unit_price != null ? i.unit_price.toFixed(2) : "", i.total.toFixed(2)]);
    }
    const csv = rows.map((r) => r.map((v) => /[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : v).join(",")).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = "buy-list-" + (currentDeck.deck.name || "deck").replace(/[^\w]+/g, "-").toLowerCase() + ".csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    toast("Buy list CSV downloaded");
  }

  function exportDeckText() {
    if (!currentDeck) return;
    const lines = [];
    for (const role of ["commander", "main"]) {
      const group = currentDeck.cards.filter((c) => c.role === role);
      if (!group.length) continue;
      for (const c of group) lines.push(`${c.quantity} ${c.name}`);
    }
    const sb = currentDeck.cards.filter((c) => c.role === "sideboard");
    if (sb.length) {
      lines.push("", "Sideboard:");
      for (const c of sb) lines.push(`${c.quantity} ${c.name}`);
    }
    copyText(lines.join("\n"), "Decklist copied");
  }

  // ------------------------------------------------------------ paste decklist
  function openImport() {
    $("importdeck-text").value = "";
    $("importdeck-status").classList.add("hidden");
    $("importdeck-modal").classList.remove("hidden");
    $("importdeck-text").focus();
  }

  function closeImport() {
    $("importdeck-modal").classList.add("hidden");
  }

  function parseDecklist(text) {
    const lines = [];
    let role = "main";
    for (const raw of text.split(/\r?\n/)) {
      let t = raw.trim();
      if (!t || t.startsWith("#")) continue;
      if (/^sideboard\b/i.test(t)) { role = "sideboard"; continue; }
      let lineRole = role;
      let m = t.match(/^sb\s*:\s*/i);
      if (m) { lineRole = "sideboard"; t = t.slice(m[0].length).trim(); }
      let qty = 1;
      m = t.match(/^(\d{1,3})\s*(?:x|×)?\s*(.+)$/i);
      if (m) { qty = Math.max(1, parseInt(m[1], 10)); t = m[2].trim(); }
      if (!t) continue;
      lines.push({ name: t, qty, role: lineRole });
    }
    return lines;
  }

  async function runImport() {
    const text = $("importdeck-text").value;
    const lines = parseDecklist(text);
    const status = $("importdeck-status");
    status.classList.remove("hidden");
    if (!lines.length) {
      status.textContent = "No cards found in that text.";
      return;
    }
    status.textContent = `Resolving ${lines.length} lines…`;
    $("importdeck-go").disabled = true;
    try {
      const resp = await fetch("/api/decks/resolve", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lines }),
      });
      const data = await resp.json();
      const results = data.results || [];
      let added = 0;
      const skipped = [];
      for (let i = 0; i < results.length; i++) {
        const r = results[i];
        if (!r.card) { skipped.push(lines[i].name); continue; }
        status.textContent = `Adding ${added + lines[i].qty} cards so far…`;
        await postCard(r.card, { quantity: lines[i].qty, role: lines[i].role, foil: false });
        added += lines[i].qty;
      }
      status.textContent = `Added ${added} cards.` +
        (skipped.length ? ` Skipped ${skipped.length}: ${skipped.join(", ")}` : "");
      if (currentDeck) refreshDeck(currentDeck.deck.id);
      loadDecks(false);
      setTimeout(closeImport, 2600);
    } catch (err) {
      status.textContent = "Import failed: " + err.message;
    } finally {
      $("importdeck-go").disabled = false;
    }
  }

  // ------------------------------------------------------------ deck stats
  // Mana curve / color pips / avg CMC, computed client-side from mana_cost.
  function parseCmc(manaCost) {
    if (!manaCost) return 0;
    let total = 0;
    const syms = manaCost.match(/\{[^{}]+\}/g) || [];
    for (const s of syms) {
      const inner = s.slice(1, -1);
      if (/^\d+$/.test(inner)) total += parseInt(inner, 10);
      else if (inner !== "X") total += 1; // colored, hybrid, {C}, {S}…
    }
    return total;
  }

  function colorPips(manaCost) {
    const pips = { W: 0, U: 0, B: 0, R: 0, G: 0 };
    if (!manaCost) return pips;
    const syms = manaCost.match(/\{[^{}]+\}/g) || [];
    for (const s of syms) {
      const inner = s.slice(1, -1);
      if (/^[WUBRG]\/[WUBRG]$/.test(inner)) {      // hybrid {W/U}
        pips[inner[0]]++; pips[inner[2]]++;
      } else if (/^[WUBRG]$/.test(inner)) {          // {R}
        pips[inner]++;
      } else if (/^[WUBRG]{2,5}$/.test(inner)) {     // {RGW}
        for (const ch of inner) pips[ch]++;
      }
    }
    return pips;
  }

  function renderDeckStats() {
    const box = $("deck-stats-body");
    if (!currentDeck) { box.innerHTML = ""; return; }
    const main = currentDeck.cards.filter((c) => c.role !== "sideboard");
    const curve = {};
    let lands = 0, creatures = 0, nonlands = 0, avgSum = 0;
    const pips = { W: 0, U: 0, B: 0, R: 0, G: 0 };
    for (const c of main) {
      const q = c.quantity;
      const isLand = /Land/.test(c.type_line || "");
      if (isLand) { lands += q; continue; }
      nonlands += q;
      if (/Creature/.test(c.type_line || "")) creatures += q;
      const cmc = parseCmc(c.mana_cost);
      const bucket = cmc >= 8 ? 8 : cmc;
      curve[bucket] = (curve[bucket] || 0) + q;
      avgSum += cmc * q;
      const pp = colorPips(c.mana_cost);
      for (const k in pp) pips[k] += pp[k] * q;
    }
    const avg = nonlands ? (avgSum / nonlands) : 0;
    const max = Math.max(1, ...Object.values(curve));
    let bars = "";
    for (let i = 0; i <= 8; i++) {
      const v = curve[i] || 0;
      const h = (v / max) * 80;
      bars += `<rect x="${i * 36 + 4}" y="${90 - h}" width="28" height="${h}" rx="3" fill="${i === 8 ? "#e0704e" : "#6fce8a"}"/>` +
        `<text x="${i * 36 + 18}" y="${103}" text-anchor="middle" font-size="9" fill="#98a0b0">${i === 8 ? "8+" : i}</text>`;
    }
    const pipColors = { W: "#f7f3d9", U: "#9bb9e0", B: "#a5a5a5", R: "#e07a5f", G: "#6fce8a" };
    const pipHtml = ["W", "U", "B", "R", "G"].map((k) =>
      `<span class="pip"><i style="background:${pipColors[k]}"></i>${k} × ${pips[k]}</span>`).join("");
    box.innerHTML =
      `<div class="statline"><span>Avg CMC <b>${avg.toFixed(2)}</b></span>` +
      `<span>Lands <b>${lands}</b></span><span>Non-lands <b>${nonlands}</b></span>` +
      `<span>Creatures <b>${creatures}</b></span></div>` +
      `<svg viewBox="0 0 ${36 * 9} 108" xmlns="http://www.w3.org/2000/svg">${bars}</svg>` +
      `<div class="pips">${pipHtml}</div>`;
  }

  // ------------------------------------------------------------ planner
  async function openPlanner() {
    const box = $("planner-items");
    box.innerHTML = `<p class="status">Computing…</p>`;
    $("planner-modal").classList.remove("hidden");
    let data;
    try {
      data = await fetch("/api/decks/planner").then((r) => r.json());
    } catch (err) {
      box.innerHTML = `<p class="status">Planner failed: ${esc(err.message)}</p>`;
      return;
    }
    $("planner-summary").textContent =
      `${data.deck_count} decks · ${data.cards.length} unique cards · ${data.total_deficit} card(s) short`;
    box.innerHTML = "";
    if (!data.cards.length) {
      box.innerHTML = `<p class="status">No cards in any deck yet.</p>`;
      return;
    }
    for (const c of data.cards) {
      const row = document.createElement("div");
      row.className = "planner-row" + (c.deficit > 0 ? " short" : "");
      const deckHtml = c.deck_list.map((d) =>
        `<span class="decktag">${esc(d.deck)} ×${d.qty}</span>`).join("");
      row.innerHTML =
        `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
        `<div class="planner-main"><b>${esc(c.name)}</b><small>${deckHtml}</small></div>` +
        `<div class="planner-nums"><span>want ${c.demand}</span>` +
        `<span class="own">own ${c.owned}</span>` +
        (c.deficit > 0 ? `<span class="miss">need ${c.deficit}</span>` : `<span class="full">✓</span>`) +
        `</div>`;
      row.querySelector("img").onclick = () => openCardModal(c);
      row.querySelector(".planner-main").onclick = () => openCardModal(c);
      box.appendChild(row);
    }
  }

  // ------------------------------------------------------------ legality
  async function openLegality() {
    if (!currentDeck) return;
    const fmt = $("deck-format").value || "commander";
    const box = $("legality-issues");
    $("legality-result").textContent = "Checking…";
    $("legality-result").className = "legality-result";
    box.innerHTML = `<p class="status">Checking ${esc(fmt)} legality… (first check downloads card legalities)</p>`;
    $("legality-modal").classList.remove("hidden");
    let data;
    try {
      data = await fetch(`/api/decks/${currentDeck.deck.id}/legality?format=${encodeURIComponent(fmt)}`)
        .then((r) => r.json());
    } catch (err) {
      box.innerHTML = `<p class="status">Legality check failed: ${esc(err.message)}</p>`;
      return;
    }
    $("legality-result").textContent = data.ok ? "✓ Legal" : "✗ Issues found";
    $("legality-result").className = "legality-result " + (data.ok ? "ok" : "bad");
    box.innerHTML = "";
    if (!data.issues.length) {
      box.innerHTML = `<p class="status" style="color:var(--green)">No issues — this deck looks legal for ${esc(fmt)}.</p>`;
      return;
    }
    for (const i of data.issues) {
      const row = document.createElement("div");
      row.className = "legality-issue " + i.severity;
      const label = (i.card ? esc(i.card) + (i.qty ? ` ×${i.qty}` : "") + " — " : "");
      row.innerHTML = `<b>${i.severity === "error" ? "✗" : "⚠"}</b><span>${label}${esc(i.issue)}</span>`;
      box.appendChild(row);
    }
  }

  // ------------------------------------------------------------ EDHREC recs
  async function openRecs() {
    if (!currentDeck) return;
    const commanders = currentDeck.cards
      .filter((c) => c.role === "commander").map((c) => c.name);
    if (!commanders.length) {
      toast("Add a commander first — set a card's role to Commander in the deck.");
      return;
    }
    const note = $("recs-note");
    const box = $("recs-items");
    note.classList.add("hidden");
    box.innerHTML = `<p class="status">Loading recommendations…</p>`;
    $("recs-modal").classList.remove("hidden");
    let lists;
    try {
      lists = await Promise.all(commanders.map((name) =>
        fetch("/api/decks/edhrec?name=" + encodeURIComponent(name)).then((r) => r.json())));
    } catch (err) {
      note.textContent = "Recommendations failed: " + err.message;
      note.classList.remove("hidden");
      box.innerHTML = "";
      return;
    }
    const err = lists.find((d) => d.error);
    if (err) {
      note.textContent = err.error;
      note.classList.remove("hidden");
      box.innerHTML = "";
      return;
    }
    if (lists.every((d) => d.unavailable)) {
      note.textContent = "No recommendations available for this commander.";
      note.classList.remove("hidden");
      box.innerHTML = "";
      return;
    }
    const seen = new Set();
    const cards = [];
    for (const d of lists) {
      for (const c of (d.cards || [])) {
        if (seen.has(c.scryfall_id)) continue;
        seen.add(c.scryfall_id);
        cards.push(c);
      }
    }
    box.innerHTML = "";
    const inDeck = new Set(
      currentDeck.cards.map((c) => (c.name || "").trim().toLowerCase()));
    for (const c of cards) {
      const row = document.createElement("div");
      row.className = "buylist-row";
      const pct = Math.round((c.synergy || 0) * 100);
      const already = inDeck.has((c.name || "").trim().toLowerCase());
      row.innerHTML =
        `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
        `<div class="buylist-main"><b>${esc(c.name)}</b>` +
        `<small>${esc(c.set_name || "")} · #${esc(c.collector_number || "?")} · ` +
        `${esc(c.rarity || "")}</small>` +
        `<small>synergy ${pct}% · in ${c.num_decks || 0} decks</small></div>` +
        (already
          ? `<span class="recs-in-deck">in deck</span>`
          : `<button class="recs-add">＋ Add</button>`);
      row.querySelector("img").onclick = () => openCardModal(c);
      row.querySelector(".buylist-main").onclick = () => openCardModal(c);
      const add = row.querySelector(".recs-add");
      if (add) {
        add.onclick = async () => {
          await postCard(c, { quantity: 1, role: "main" });
          toast(`Added ${c.name}`);
          refreshDeck(currentDeck.deck.id);
        };
      }
      box.appendChild(row);
    }
  }

  // ------------------------------------------------------------ deck value
  async function openValue() {
    if (!currentDeck) return;
    $("value-chart").innerHTML = `<p class="status">Loading…</p>`;
    $("value-modal").classList.remove("hidden");
    const data = await fetch(`/api/decks/${currentDeck.deck.id}/value`).then((r) => r.json());
    const pts = (data.history || []).map((h) => ({ t: h.recorded_at, v: h.total_value }));
    if (pts.length < 2) {
      $("value-chart").innerHTML =
        `<p class="dim">Not enough snapshots yet — value is recorded automatically ` +
        `(about once an hour), or tap <b>Record now</b>. Current: <b>$${data.current.toFixed(2)}</b></p>`;
    } else {
      const vc = $("value-chart");
      const render = () => { vc.innerHTML = lineChart(pts, Math.max(300, vc.clientWidth)); };
      render();
      if (window.ResizeObserver && !vc._ro) { vc._ro = new ResizeObserver(render); vc._ro.observe(vc); }
    }
    $("value-note").textContent =
      `Current deck value: $${data.current.toFixed(2)} · ${pts.length} snapshot${pts.length === 1 ? "" : "s"}`;
  }

  async function recordValue() {
    if (!currentDeck) return;
    await fetch(`/api/decks/${currentDeck.deck.id}/value/record`, { method: "POST" });
    toast("Value recorded");
    openValue();
  }

  function fmtDate(iso) {
    const d = new Date(iso);
    let s = (d.getMonth() + 1) + "/" + d.getDate();
    if (d.getFullYear() !== new Date().getFullYear()) s += "/" + String(d.getFullYear()).slice(2);
    return s;
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

  // ------------------------------------------------------------ card modal
  // View-only card details come from the shared /card-modal.js (injected
  // into the page); this thin wrapper keeps all call sites unchanged.
  function openCardModal(c) {
    if (window.openCardModal) window.openCardModal(c);
  }

  // ------------------------------------------------------------ playtest
  function openPlaytest() {
    if (!currentDeck) return;
    const sel = $("playtest-card");
    sel.innerHTML = "";
    const main = currentDeck.cards.filter((c) => c.role === "main");
    for (const c of main) {
      const o = document.createElement("option");
      o.value = c.scryfall_id;
      o.textContent = c.name + (c.quantity > 1 ? ` ×${c.quantity}` : "");
      sel.appendChild(o);
    }
    $("playtest-hand").innerHTML = "";
    $("playtest-odds").innerHTML = "";
    $("playtest-modal").classList.remove("hidden");
    drawHand();
    updateOdds();
  }

  function drawHand() {
    const hand = $("playtest-hand");
    const main = currentDeck.cards.filter((c) => c.role === "main");
    const pool = [];
    for (const c of main) for (let i = 0; i < c.quantity; i++) pool.push(c);
    if (pool.length < 7) {
      hand.innerHTML = `<p class="dim">Not enough main-deck cards to draw 7.</p>`;
      return;
    }
    const tmp = pool.slice();
    const picked = [];
    for (let i = 0; i < 7; i++) {
      picked.push(tmp.splice(Math.floor(Math.random() * tmp.length), 1)[0]);
    }
    hand.innerHTML = picked.map((c) =>
      `<div class="hand-card" title="${esc(c.name)}">` +
      `<img loading="lazy" src="${imgUrl(c.image_uri || "")}"><small>${esc(c.name)}</small></div>`).join("");
    hand.querySelectorAll(".hand-card").forEach((el, idx) => {
      el.onclick = () => openCardModal(picked[idx]);
    });
  }

  function comb(n, k) {
    if (k < 0 || k > n) return 0;
    k = Math.min(k, n - k);
    let r = 1;
    for (let i = 0; i < k; i++) r = r * (n - i) / (i + 1);
    return r;
  }

  function updateOdds() {
    const main = currentDeck.cards.filter((c) => c.role === "main");
    const deckSize = main.reduce((s, c) => s + c.quantity, 0);
    const box = $("playtest-odds");
    if (!deckSize) { box.innerHTML = ""; return; }
    const sel = $("playtest-card");
    const c = main.find((x) => x.scryfall_id === sel.value);
    const v = $("playtest-turn").value;
    const onDraw = $("playtest-playdraw").value === "draw";
    let n;
    if (v === "oh") n = 7;
    else n = parseInt(v, 10) + (onDraw ? 7 : 6); // cards seen by turn N
    if (!c) { box.innerHTML = `<p class="dim">No main-deck cards to compute odds for.</p>`; return; }
    const p = hypergeo(deckSize, c.quantity, n);
    box.innerHTML =
      `<p>In a ${deckSize}-card deck, drawing <b>${esc(c.name)}</b> ` +
      `(${c.quantity} copy${c.quantity === 1 ? "" : "s"}) within ${n} cards seen: ` +
      `<b class="big">${(p * 100).toFixed(1)}%</b></p>`;
  }

  function hypergeo(N, K, n) {
    if (K <= 0 || n <= 0) return 0;
    if (K >= N) return 1;
    const fail = comb(N - K, n) / comb(N, n);
    return Math.max(0, 1 - fail);
  }

  async function runImportToCollection() {
    const text = $("importdeck-text").value;
    const lines = parseDecklist(text);
    const status = $("importdeck-status");
    status.classList.remove("hidden");
    if (!lines.length) {
      status.textContent = "No cards found in that text.";
      return;
    }
    const qty = lines.reduce((s, l) => s + l.qty, 0);
    status.textContent = `Adding ${qty} cards to the collection…`;
    $("importdeck-collection").disabled = true;
    try {
      const resp = await fetch("/api/collection/import-decklist", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lines }),
      });
      const d = await resp.json();
      status.textContent = `Added ${d.added} new, merged ${d.updated} (${d.total_cards} cards)` +
        (d.skipped.length ? ` — skipped ${d.skipped.length}: ${d.skipped.join(", ")}` : "");
      setTimeout(closeImport, 2600);
    } catch (err) {
      status.textContent = "Import failed: " + err.message;
    } finally {
      $("importdeck-collection").disabled = false;
    }
  }

  // ------------------------------------------------------------ precon search
  async function loadPreconStatus() {
    let st;
    try {
      st = await fetch("/api/decks/precon/status").then((r) => r.json());
    } catch (err) { return; }
    renderPreconStatus(st);
    if (st.available) return;
    if (st.active) return;
    // no index yet — kick off the one-time sync automatically
    const status = $("precon-status");
    status.textContent = "Building the precon index (one-time download of the " +
      "official MTGJSON decklists)…";
    fetch("/api/decks/precon/sync", { method: "POST" }).catch(() => {});
  }

  function renderPreconStatus(st) {
    const status = $("precon-status");
    if (st.active) {
      status.textContent = st.phase === "list"
        ? "Downloading set list…"
        : `Indexing ${st.phase} (${st.done}/${st.total}) — ${st.decks} decks found…`;
    } else if (st.available) {
      status.textContent =
        `${st.decks.toLocaleString()} preconstructed decks indexed — search above, ` +
        `or press ⇧⌘R / refresh the page if you recently bought a new precon.`;
    } else if (st.error) {
      status.textContent = "Index failed: " + st.error;
    } else {
      status.textContent = "Building the precon index (one-time download)…";
    }
  }

  async function preconSearch() {
    const q = $("precon-q").value.trim();
    if (q.length < 2) return;
    const box = $("precon-results");
    const status = $("precon-status");
    box.innerHTML = `<p class="status">Searching…</p>`;
    let data;
    try {
      data = await fetch("/api/decks/precon/search?q=" + encodeURIComponent(q))
        .then((r) => r.json());
    } catch (err) {
      box.innerHTML = `<p class="status">Search failed: ${esc(err.message)}</p>`;
      return;
    }
    if (data.unavailable) {
      box.innerHTML = `<p class="status">The precon index isn't built yet — syncing now…</p>`;
      status.textContent = "Building the precon index (one-time download)…";
      fetch("/api/decks/precon/sync", { method: "POST" }).catch(() => {});
      return;
    }
    if (!data.decks.length) {
      box.innerHTML = `<p class="status">No preconstructed decks found for “${esc(q)}”.</p>`;
      return;
    }
    box.innerHTML = "";
    for (const d of data.decks) {
      const row = document.createElement("div");
      row.className = "arch-deck";
      row.innerHTML =
        `<div class="arch-deck-main"><b>${esc(d.name)}</b>` +
        `<small>${esc(d.set_name)} · ${d.card_count} cards` +
        (d.commander ? ` · Commander: ${esc(d.commander)}` : "") + `</small></div>` +
        `<span class="arch-import-btn">Import</span>`;
      row.onclick = async () => {
        row.classList.add("busy");
        toast("Importing deck…", 8000);
        try {
          const resp = await fetch("/api/decks/precon/import", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ set_code: d.set_code, deck_name: d.name }),
          });
          const res = await resp.json();
          if (res.ok) {
            toast(`Imported “${res.name}” — ${res.added} cards` +
              (res.skipped && res.skipped.length ? `, ${res.skipped.length} skipped` : ""));
            loadDecks(false);
            openDeck(res.id);
          } else {
            toast("Import error: " + (res.error || "failed"), 6000);
          }
        } catch (err) {
          toast("Import failed: " + err.message, 6000);
        } finally {
          row.classList.remove("busy");
        }
      };
      box.appendChild(row);
    }
  }

  // ------------------------------------------------------------ import by URL
  async function importDeckUrl(urlOrId) {
    toast("Importing deck…", 8000);
    try {
      const resp = await fetch("/api/decks/import-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: urlOrId }),
      });
      const d = await resp.json();
      if (d.ok) {
        toast(`Imported “${d.name}” — ${d.added} cards` +
          (d.skipped && d.skipped.length ? `, ${d.skipped.length} skipped` : ""), 5000);
        loadDecks(false);
        openDeck(d.id);
      } else {
        toast("Import error: " + (d.error || "failed"), 6000);
      }
    } catch (err) {
      toast("Import failed: " + err.message, 6000);
    }
  }

  // ------------------------------------------------------------ events
  function connectEvents() {
    const es = new EventSource("/api/events");
    es.onmessage = (e) => {
      let evt;
      try { evt = JSON.parse(e.data); } catch (err) { return; }
      if (evt.type === "add" || evt.type === "library-changed") {
        // the collection changed (e.g. a phone scan) → refresh owned counts
        if (currentDeck) refreshDeck(currentDeck.deck.id);
        loadDecks(false);
      } else if (evt.type === "decks-changed") {
        loadDecks(false);
      } else if (evt.type === "precon-progress") {
        renderPreconStatus({ active: true, phase: evt.phase || "",
                            done: evt.done, total: evt.total, decks: evt.decks });
      } else if (evt.type === "precon-done") {
        toast("Precon index ready — " + (evt.decks || 0).toLocaleString() + " decks");
        loadPreconStatus();
      } else if (evt.type === "precon-error") {
        const st = $("precon-status");
        if (st) st.textContent = "Index failed: " + (evt.error || "unknown");
        toast("Precon index failed: " + (evt.error || "unknown"), 6000);
      }
    };
    es.onerror = () => {
      es.close();
      setTimeout(connectEvents, 3000);
    };
  }

  // ------------------------------------------------------------ deck viewer
  const DECKVIEW_TYPES = ["Creatures", "Planeswalkers", "Instants", "Sorceries",
                          "Enchantments", "Artifacts", "Battles", "Lands", "Other"];
  let deckViewMode = "list";

  function deckType(tl) {
    tl = tl || "";
    if (/Creature/.test(tl)) return "Creatures";
    if (/Planeswalker/.test(tl)) return "Planeswalkers";
    if (/Instant/.test(tl)) return "Instants";
    if (/Sorcery/.test(tl)) return "Sorceries";
    if (/Enchantment/.test(tl)) return "Enchantments";
    if (/Artifact/.test(tl)) return "Artifacts";
    if (/Battle/.test(tl)) return "Battles";
    if (/Land/.test(tl)) return "Lands";
    return "Other";
  }

  function openDeckView() {
    if (!currentDeck) return;
    $("deckview-title").textContent = currentDeck.deck.name;
    $("deckview-modal").classList.remove("hidden");
    renderDeckView();
  }

  function renderDeckView() {
    if (!currentDeck) return;
    const { stats, cards } = currentDeck;
    let totalVal = 0, landQty = 0, creatureQty = 0;
    const byType = {};
    for (const c of cards) {
      const t = deckType(c.type_line);
      byType[t] = byType[t] || { qty: 0, value: 0 };
      byType[t].qty += c.quantity;
      byType[t].value += (c.unit_price || 0) * c.quantity;
      totalVal += (c.unit_price || 0) * c.quantity;
      if (t === "Lands") landQty += c.quantity;
      if (t === "Creatures") creatureQty += c.quantity;
    }
    const typeCounts = DECKVIEW_TYPES.filter((t) => byType[t])
      .map((t) => `${byType[t].qty} ${t.toLowerCase()}`).join(" · ");
    $("deckview-stats").innerHTML =
      `<div class="statline"><span>Cards <b>${stats.total}</b></span>` +
      `<span>Value <b>$${totalVal.toFixed(2)}</b></span>` +
      `<span>Lands <b>${landQty}</b></span>` +
      `<span>Creatures <b>${creatureQty}</b></span></div>` +
      `<div class="deckview-types">${esc(typeCounts)}</div>`;
    const body = $("deckview-body");
    if (deckViewMode === "visual") renderDeckVisual(cards, body);
    else renderDeckList(cards, body);
  }

  function renderDeckList(cards, body) {
    body.innerHTML = "";
    const groups = new Map();
    for (const c of cards) {
      const t = deckType(c.type_line);
      if (!groups.has(t)) groups.set(t, []);
      groups.get(t).push(c);
    }
    for (const t of DECKVIEW_TYPES) {
      const list = groups.get(t);
      if (!list) continue;
      list.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
      const head = document.createElement("div");
      head.className = "deckview-group";
      const q = list.reduce((s, c) => s + c.quantity, 0);
      const v = list.reduce((s, c) => s + (c.unit_price || 0) * c.quantity, 0);
      head.innerHTML = `<b>${t}</b><small>${q} · $${v.toFixed(2)}</small>`;
      body.appendChild(head);
      for (const c of list) {
        const row = document.createElement("div");
        row.className = "deckview-row" + (c.role === "commander" ? " commander" : "");
        const price = c.unit_price != null ? "$" + c.unit_price.toFixed(2) : "—";
        const status = c.need > 0
          ? `<span class="own">own ${c.owned}</span><span class="miss">need ${c.need}</span>`
          : `<span class="full">✓</span>`;
        row.innerHTML =
          `<span class="dv-qty">${c.quantity}</span>` +
          `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
          `<div class="dv-main"><b>${esc(c.name)}${c.foil ? " " + icon("foil") : ""}` +
          (c.role === "commander" ? ` <span class="dv-cmd">commander</span>` : "") + `</b>` +
          `<small>${esc(c.set_name || "?")} · #${esc(c.collector_number || "?")}</small></div>` +
          `<div class="dv-status">${status}</div>` +
          `<div class="dv-price"><small>${price} each</small><b>$${((c.unit_price || 0) * c.quantity).toFixed(2)}</b></div>`;
        row.querySelector("img").onclick = () => openCardModal(c);
        row.querySelector(".dv-main").onclick = () => openCardModal(c);
        body.appendChild(row);
      }
    }
  }

  // one visual-grid cell: the zooming card + fixed-size name/price below
  function deckviewCell(c) {
    const cell = document.createElement("div");
    cell.className = "deckview-cell";
    const tile = document.createElement("div");
    tile.className = "deckview-tile" + (c.role === "commander" ? " commander" : "");
    tile.innerHTML =
      `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
      `<span class="dv-qty-badge">×${c.quantity}</span>` +
      (c.need > 0 ? `<span class="dv-need-badge">need ${c.need}</span>` : "");
    cell.appendChild(tile);
    const name = document.createElement("span");
    name.className = "dv-name";
    name.textContent = c.name;
    cell.appendChild(name);
    const price = document.createElement("span");
    price.className = "dv-tile-price";
    price.textContent = c.unit_price != null ? "$" + c.unit_price.toFixed(2) : "";
    cell.appendChild(price);
    cell.title = `${c.name} · ${c.set_name}`;
    cell.onclick = () => openCardModal(c);
    return cell;
  }

  function renderDeckVisual(cards, body) {
    body.innerHTML = "";
    const grid = document.createElement("div");
    grid.className = "deckview-grid";
    const ordered = cards.slice().sort((a, b) => {
      const ta = DECKVIEW_TYPES.indexOf(deckType(a.type_line));
      const tb = DECKVIEW_TYPES.indexOf(deckType(b.type_line));
      return ta - tb || (a.name || "").localeCompare(b.name || "");
    });
    for (const c of ordered) grid.appendChild(deckviewCell(c));
    body.appendChild(grid);
  }

  // ------------------------------------------------------------ wire up
  function init() {
    const newBtn = document.getElementById("deck-new-btn");
    newBtn.onclick = newDeck;
    $("deck-close-btn").onclick = closeDeckModal;
    $("deck-name").addEventListener("change", saveDeckMeta);
    $("deck-format").addEventListener("change", saveDeckMeta);
    $("deck-delete-btn").onclick = deleteDeck;
    $("deck-duplicate-btn").onclick = duplicateDeck;
    $("deck-export-btn").onclick = exportDeckText;
    $("deck-buylist-btn").onclick = openBuylist;
    $("deck-view-btn").onclick = openDeckView;
    $("deck-legal-btn").onclick = openLegality;
    $("deck-recs-btn").onclick = openRecs;
    $("deck-value-btn").onclick = openValue;
    $("deck-playtest-btn").onclick = openPlaytest;
    $("deck-add-collection-btn").onclick = addToCollection;
    $("deck-import-btn").onclick = openImport;
    $("deck-import-btn2").onclick = openImport;
    $("deck-search").addEventListener("input", onDeckSearch);
    $("buylist-close").onclick = closeBuylist;
    $("buylist-tcg").onclick = openTcgplayer;
    $("buylist-copy").onclick = copyBuylist;
    $("buylist-csv").onclick = exportBuylistCsv;
    $("buylist-cheapest").onchange = () => { if (buylist) openBuylist(); };
    $("importdeck-go").onclick = runImport;
    $("importdeck-collection").onclick = runImportToCollection;
    $("importdeck-cancel").onclick = closeImport;
    $("planner-btn").onclick = openPlanner;
    $("planner-close").onclick = () => $("planner-modal").classList.add("hidden");
    $("legality-close").onclick = () => $("legality-modal").classList.add("hidden");
    $("recs-close").onclick = () => $("recs-modal").classList.add("hidden");
    $("playtest-close").onclick = () => $("playtest-modal").classList.add("hidden");
    $("playtest-draw").onclick = drawHand;
    $("playtest-redraw").onclick = drawHand;
    $("playtest-card").onchange = updateOdds;
    $("playtest-turn").onchange = updateOdds;
    $("playtest-playdraw").onchange = updateOdds;
    $("value-close").onclick = () => $("value-modal").classList.add("hidden");
    $("value-record").onclick = recordValue;
    $("deckview-close").onclick = () => $("deckview-modal").classList.add("hidden");
    $("deckview-toggle").onclick = (e) => {
      const btn = e.target.closest("button[data-v]");
      if (!btn) return;
      deckViewMode = btn.dataset.v;
      $("deckview-toggle").querySelectorAll("button")
        .forEach((b) => b.classList.toggle("active", b === btn));
      renderDeckView();
    };

    // import by URL (Archidekt / Moxfield / TappedOut)
    $("arch-import").onclick = () => importDeckUrl($("arch-url").value.trim());
    $("arch-url").addEventListener("keydown", (e) => {
      if (e.key === "Enter") importDeckUrl($("arch-url").value.trim());
    });

    // preconstructed deck search
    $("precon-go").onclick = preconSearch;
    $("precon-q").addEventListener("keydown", (e) => {
      if (e.key === "Enter") preconSearch();
    });

    // modal chrome
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const order = ["importdeck-modal", "playtest-modal", "value-modal",
                     "legality-modal", "recs-modal", "planner-modal",
                     "buylist-modal", "deckview-modal", "deck-modal"];
      for (const id of order) {
        const el = $(id);
        if (!el || el.classList.contains("hidden")) continue;
        if (id === "importdeck-modal") closeImport();
        else if (id === "deck-modal") closeDeckModal();
        else if (id === "buylist-modal") closeBuylist();
        else el.classList.add("hidden");
        break;
      }
    });
    for (const id of ["deck-modal", "buylist-modal", "importdeck-modal",
                      "planner-modal", "legality-modal", "recs-modal",
                      "playtest-modal", "value-modal", "deckview-modal"]) {
      $(id).addEventListener("click", (e) => {
        if (e.target !== $(id)) return;
        if (id === "deck-modal") closeDeckModal();
        else if (id === "buylist-modal") closeBuylist();
        else if (id === "importdeck-modal") closeImport();
        else $(id).classList.add("hidden");
      });
    }

    loadDecks(true);
    loadPreconStatus();
    connectEvents();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
