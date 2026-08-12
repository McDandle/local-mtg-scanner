"use strict";
const $ = (id) => document.getElementById(id);

// Card images are proxied through the server's local cache (/api/img) so
// the library keeps working offline after the first view.
function imgUrl(u) {
  if (!u) return "";
  return "/api/img?u=" + encodeURIComponent(u);
}

// Escape provider/CSV-supplied strings that end up in innerHTML templates.
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function toast(msg, ms = 2400) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), ms);
}

// update a button label without clobbering its svg icon
function setBtnLabel(btn, text) {
  const span = btn && btn.querySelector(".btn-label");
  if (span) span.textContent = text;
  else if (btn) btn.textContent = text;
}

// ------------------------------------------------------------ state
let libraryCards = [];
const PAGE = 100;          // ledger sheet size
const SHELF_PAGE = 12;     // tiles per shelf before the "+N more" tile
let page = 0;              // ledger sheet index (0-based)
let expandedWalls = new Set(); // shelf keys expanded into a full wall
let viewMode = "grid";
try { viewMode = localStorage.getItem("mtg_view") || "grid"; } catch (e) {}
let collapsedGroups = {};
try { collapsedGroups = JSON.parse(localStorage.getItem("mtg_collapsed") || "{}") || {}; } catch (e) {}

let currentCard = null;
let qty = 1;
let detailCard = null;
let pendingCard = null;
let printTimer = null;

// batch select/edit state
let batchMode = false;
const selectedIds = new Set();

// live scanning state
let liveActive = false;
let liveStream = null;
let liveBusy = false;
let lastDetectedId = null;
let stableCount = 0;
let noMatchCount = 0;
let lastAddedId = null;
let sessionAdds = 0;
let liveDetected = null;   // currently detected card (for the confirm button)
let confirmedId = null;    // card already confirmed this pass
let audioCtx = null;

// scan overlay switches (the design's .switch pills, no checkboxes)
function switchOn(id) { return $(id).classList.contains("on"); }
function setSwitch(id, on) { $(id).classList.toggle("on", on); }
$("auto-add-switch").onclick = () => setSwitch("auto-add-switch", !switchOn("auto-add-switch"));
$("scan-foil-switch").onclick = () => setSwitch("scan-foil-switch", !switchOn("scan-foil-switch"));

function sessionNote() {
  const n = $("scan-note");
  if (n) n.textContent = sessionAdds ? `${sessionAdds} added this session` : "";
  $("session-count").textContent = sessionAdds ? `${sessionAdds} this session` : "";
}

// ------------------------------------------------------------ stats
const RARITY_RANK = { common: 0, uncommon: 1, rare: 2, mythic: 3, special: 4 };
const RARITY_ORDER = ["common", "uncommon", "rare", "mythic", "special"];
const COLOR_NAME = { W: "White", U: "Blue", B: "Black", R: "Red", G: "Green" };
const COND_NAME = { NM: "Near Mint", LP: "Lightly Played", MP: "Moderately Played", HP: "Heavily Played", D: "Damaged" };
// matches server COND_MULT — used only to label condition-adjusted prices
const COND_MULT = { NM: 1, LP: .9, MP: .8, HP: .6, D: .4 };
function condMult(c) { return COND_MULT[c.condition || "NM"] ?? 1; }

function updateStats() {
  let total = 0, count = 0, foilVal = 0, mvp = null;
  const rar = {};
  for (const c of libraryCards) {
    const v = c.line_value || 0;
    total += v;
    count += c.quantity;
    if (c.foil) foilVal += v;
    const k = c.rarity || "?";
    rar[k] = rar[k] || { count: 0, value: 0 };
    rar[k].count += c.quantity;
    rar[k].value += v;
    if (!mvp || v > mvp.value) mvp = { name: c.name, value: v, set: c.set_code || "" };
  }
  $("sum-value").textContent = "$" + total.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  $("sum-count").textContent = count.toLocaleString();
  $("foil-value").textContent = "$" + Math.round(foilVal).toLocaleString();
  $("mvp-card").textContent = mvp ? mvp.name : "—";
  const sub = $("mvp-sub");
  if (sub) sub.textContent = mvp ? `$${mvp.value.toFixed(2)} · ${(mvp.set || "").toUpperCase()}` : "";

  const bar = $("rarity-bar");
  bar.innerHTML = "";
  for (const r of RARITY_ORDER) {
    const d = rar[r];
    if (!d || !d.value) continue;
    const seg = document.createElement("span");
    seg.className = r;
    seg.style.width = (d.value / (total || 1)) * 100 + "%";
    seg.title = `${r}: ${d.count} cards · $${d.value.toFixed(0)}`;
    bar.appendChild(seg);
  }
  const parts = RARITY_ORDER.filter((r) => rar[r] && rar[r].value != null)
    .map((r) => `<i class="${r}"></i><b>${r}</b> $${rar[r].value.toFixed(0)}`);
  $("breakdown").innerHTML = parts.length ? parts.join("") : "";
}

// ------------------------------------------------------------ scanner: single photo
$("camera-input").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  e.target.value = "";
  $("scan-result").classList.add("hidden");
  const st = $("scan-status");
  st.textContent = "Reading card…";
  st.classList.remove("hidden");
  try {
    const resized = await downscale(file);
    const resp = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "image/jpeg" },
      body: resized,
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    if (data.match) {
      st.classList.add("hidden");
      showMatch(data.match, data.method);
    } else {
      st.textContent = data.ocr_guess
        ? `Couldn't match "${data.ocr_guess}" — try again or search below.`
        : "Couldn't read the card — try better lighting, or search below.";
      if (data.ocr_guess) {
        $("search-input").value = data.ocr_guess;
        runSearch(data.ocr_guess);
      }
    }
  } catch (err) {
    st.textContent = "Scan failed: " + err.message;
  }
};

// Downscale on-device so uploads over Wi-Fi are fast; OCR doesn't need 12MP.
async function downscale(file) {
  const bmp = await createImageBitmap(file).catch(() => null);
  if (!bmp) return file; // e.g. HEIC in an odd browser — send as-is
  const maxDim = 1600;
  const scale = Math.min(1, maxDim / Math.max(bmp.width, bmp.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bmp.width * scale);
  canvas.height = Math.round(bmp.height * scale);
  canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
  return new Promise((res) => canvas.toBlob((b) => res(b || file), "image/jpeg", 0.85));
}

const METHOD_TEXT = {
  exact: "exact print ✓",
  "local-exact": "exact print ✓ · local",
  number: "exact print ✓",
  "local-number": "exact print ✓ · local",
  fuzzy: "name match",
  "local-name": "name match · local",
};
const METHOD_CLASS = {
  exact: "exact", "local-exact": "exact",
  number: "exact", "local-number": "exact",
  fuzzy: "fuzzy", "local-name": "fuzzy",
};

function showMatch(card, method) {
  currentCard = card;
  qty = 1;
  $("qty-value").textContent = "1";
  $("result-foil").checked = false;
  $("result-img").src = imgUrl(card.image_uri || "");
  $("result-name").innerHTML = "";
  $("result-name").textContent = card.name;
  if (method) {
    const badge = document.createElement("span");
    badge.className = "method-badge " + (METHOD_CLASS[method] || "fuzzy");
    badge.textContent = METHOD_TEXT[method] || "match";
    badge.title = method === "local-exact" || method === "local-name"
      ? "Matched from the offline database" : "Matched via Scryfall API";
    $("result-name").appendChild(badge);
  }
  $("result-set").textContent =
    `${card.set_name} (${(card.set_code || "").toUpperCase()}) · #${card.collector_number} · ${card.rarity}`;
  updatePriceLine();
  $("scan-result").classList.remove("hidden");
  if (!liveActive) $("scan-result").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function updatePriceLine() {
  if (!currentCard) return;
  const foil = $("result-foil").checked;
  const p = foil ? currentCard.price_usd_foil : currentCard.price_usd;
  $("result-price").textContent = p != null ? `$${p.toFixed(2)}${foil ? " (foil)" : ""}` : "no price data";
}
$("result-foil").onchange = updatePriceLine;
$("qty-minus").onclick = () => { qty = Math.max(1, qty - 1); $("qty-value").textContent = qty; };
$("qty-plus").onclick = () => { qty += 1; $("qty-value").textContent = qty; };

$("result-wishlist").onclick = () => {
  if (!currentCard) return;
  addWishlist(currentCard);
};

$("add-btn").onclick = async () => {
  if (!currentCard) return;
  const resp = await fetch("/api/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      card: currentCard,
      foil: $("result-foil").checked,
      quantity: qty,
    }),
  });
  const data = await resp.json();
  if (data.ok) {
    toast(`Added ${qty}× ${data.name}`);
    $("scan-result").classList.add("hidden");
    $("search-results").innerHTML = "";
    $("search-input").value = "";
    maybeWishlistBought(data, qty);
  } else {
    toast("Error: " + (data.error || "add failed"));
  }
};

// If the card just added was on the wishlist, offer to mark it bought
// (decrements the wishlist entry without double-adding to the library).
async function maybeWishlistBought(res, qty) {
  if (!res || !res.ok || !res.wishlist_match) return;
  const wm = res.wishlist_match;
  if (!confirm(`"${wm.name}" was on your wishlist — mark it as bought? (removes it from the wishlist)`)) return;
  const resp = await fetch("/api/wishlist/bought", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: wm.id, qty: qty || 1, to_collection: false }),
  });
  const d = await resp.json();
  if (d.ok) {
    toast(`Removed ${d.name} from your wishlist`);
    if (wishlistOpen) loadWishlist();
  }
}

// ------------------------------------------------------------ live scanning
$("live-btn").onclick = startLive;
$("live-stop").onclick = stopLive;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") stopLive(); });
// mobile bottom dock mirrors the scan-bar actions
$("dock-live").onclick = () => $("live-btn").click();
$("dock-photo").onclick = () => document.getElementById("camera-input").click();

async function startLive() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    toast("Live camera needs the https:// link — scan the Pair Phone QR", 4000);
    return;
  }
  try {
    liveStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment", width: { ideal: 1920 } },
      audio: false,
    });
  } catch (err) {
    toast("Camera blocked: " + err.message, 4000);
    return;
  }
  $("scan-video").srcObject = liveStream;
  $("scan-overlay").classList.remove("hidden");
  document.body.classList.add("scanning");
  liveActive = true;
  lastDetectedId = lastAddedId = null;
  liveDetected = confirmedId = null;
  stableCount = noMatchCount = 0;
  sessionAdds = 0;
  sessionNote();
  // iOS only allows audio started from a user gesture — prime the beep now.
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch (e) { /* no audio */ }
  setupCameraControls();
  liveLoop();
}

// Tap-to-focus + zoom. Zoom is the big win for small collector-line text
// (basic land variations especially); focus points-of-interest is applied
// where the browser supports it and ignored elsewhere.
function setupCameraControls() {
  const track = liveStream.getVideoTracks()[0];
  const caps = track.getCapabilities ? track.getCapabilities() : {};

  const zoomRow = $("zoom-row");
  if (caps.zoom && caps.zoom.max > caps.zoom.min) {
    const slider = $("zoom-slider");
    slider.min = caps.zoom.min;
    slider.max = Math.min(caps.zoom.max, caps.zoom.min * 5);
    slider.step = caps.zoom.step || 0.1;
    slider.value = track.getSettings().zoom || caps.zoom.min;
    slider.oninput = () =>
      track.applyConstraints({ advanced: [{ zoom: parseFloat(slider.value) }] })
        .catch(() => {});
    zoomRow.classList.remove("hidden");
  } else {
    zoomRow.classList.add("hidden");
  }

  $("scan-video").onclick = async (e) => {
    const rect = e.target.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    const ring = $("focus-ring");
    ring.style.left = (e.clientX - rect.left) + "px";
    ring.style.top = (e.clientY - rect.top) + "px";
    ring.classList.remove("hidden");
    clearTimeout(ring._t);
    ring._t = setTimeout(() => ring.classList.add("hidden"), 900);

    const advanced = [];
    if (caps.focusMode && caps.focusMode.includes("single-shot"))
      advanced.push({ focusMode: "single-shot" });
    if ("pointsOfInterest" in caps)
      advanced.push({ pointsOfInterest: [{ x, y }] });
    if (!advanced.length) return; // iOS: continuous AF only — ring still cues user
    try {
      await track.applyConstraints({ advanced });
      setTimeout(() => {
        if (caps.focusMode && caps.focusMode.includes("continuous"))
          track.applyConstraints({ advanced: [{ focusMode: "continuous" }] })
            .catch(() => {});
      }, 2500);
    } catch (err) { /* unsupported combination — ignore */ }
  };
}

function stopLive() {
  if (!liveActive) return;
  liveActive = false;
  if (liveStream) for (const t of liveStream.getTracks()) t.stop();
  liveStream = null;
  $("scan-overlay").classList.add("hidden");
  document.body.classList.remove("scanning");
}

function liveLoop() {
  if (!liveActive) return;
  captureAndIdentify().finally(() => setTimeout(liveLoop, 400));
}

async function captureAndIdentify() {
  if (liveBusy) return;
  const video = $("scan-video");
  if (!video.videoWidth) return;
  liveBusy = true;
  try {
    const maxDim = 1400;
    const scale = Math.min(1, maxDim / Math.max(video.videoWidth, video.videoHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.8));
    if (!blob) return;
    const data = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "image/jpeg" },
      body: blob,
    }).then((r) => r.json());
    handleLiveResult(data);
  } catch (err) {
    /* transient network/frame errors: keep scanning */
  } finally {
    liveBusy = false;
  }
}

function handleLiveResult(data) {
  const overlay = $("scan-overlay-text");
  if (!liveActive) return;
  if (data.match) {
    noMatchCount = 0;
    const id = data.match.scryfall_id;
    if (id === lastDetectedId) {
      stableCount++;
    } else {
      lastDetectedId = id;
      stableCount = 1;
      confirmedId = null;  // new card → confirmable again
      showMatch(data.match, data.method);
    }
    liveDetected = data.match;
    updateScanConfirm();
    const price = data.match.price_usd != null ? ` · $${data.match.price_usd.toFixed(2)}` : "";
    let note = "";
    const isExact = !!data.exact;
    if (isExact) {
      note = ` — ${(data.match.set_code || "?").toUpperCase()} #${data.match.collector_number} ✓`;
    } else if (switchOn("auto-add-switch")) {
      note = " — auto-add needs the exact print; zoom in on the bottom number";
    } else if (/^Basic Land/.test(data.match.type_line || "")) {
      note = " — zoom for the bottom-left number, or search name + number above";
    }
    const thumb = $("scan-thumb");
    thumb.src = imgUrl(data.match.image_uri || "");
    thumb.classList.remove("hidden");
    $("scan-meta").textContent =
      `${data.match.price_usd != null ? "$" + data.match.price_usd.toFixed(2) : "—"} · ${(data.match.set_code || "?").toUpperCase()} #${data.match.collector_number || "?"}`;
    $("scan-match").classList.remove("hidden");
    overlay.textContent = `${data.match.name}${note}`;
    overlay.classList.add("matched");
    // auto-add once per stable, newly-seen card — but only when the exact
    // printing is known (set + collector number), so partial name-only
    // matches never get added silently.
    if (switchOn("auto-add-switch") && isExact && stableCount === 2 && id !== lastAddedId) {
      lastAddedId = id;
      fetch("/api/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ card: data.match, foil: switchOn("scan-foil-switch"), quantity: 1 }),
      }).then((r) => r.json()).then((res) => {
        if (res.ok) {
          confirmedId = id;
          updateScanConfirm();
          sessionAdds++;
          sessionNote();
          beep();
          toast("Auto-added " + res.name);
          maybeWishlistBought(res, 1);
        }
      });
    }
  } else {
    noMatchCount++;
    // Two empty frames = card slid out (rig workflow). Re-arm immediately so
    // consecutive identical cards (e.g. a playset of the same Plains) each
    // get added.
    if (noMatchCount >= 2) {
      lastDetectedId = null;
      stableCount = 0;
      lastAddedId = null;
      liveDetected = null;
      confirmedId = null;
      $("scan-thumb").classList.add("hidden");
      $("scan-meta").textContent = "";
      $("scan-match").classList.add("hidden");
      overlay.textContent = "Point at a card…";
      overlay.classList.remove("matched");
      updateScanConfirm();
    }
  }
}

// The confirm button in the live scanner adds the currently detected card
// to the library on the same screen (no need to stop scanning).
function updateScanConfirm() {
  const btn = $("scan-confirm");
  const card = liveDetected;
  if (!card) { btn.classList.add("hidden"); return; }
  btn.classList.remove("hidden");
  const added = confirmedId === card.scryfall_id;
  btn.disabled = added;
  btn.classList.toggle("added", added);
  btn.querySelector(".scan-label").textContent = added ? "Added — next card?" : "Add to library";
}

$("scan-confirm").onclick = async () => {
  const card = liveDetected;
  if (!card || confirmedId === card.scryfall_id) return;
  const foil = switchOn("scan-foil-switch");
  try {
    const resp = await fetch("/api/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card: card, foil: foil, quantity: 1 }),
    });
    const res = await resp.json();
    if (res.ok) {
      confirmedId = card.scryfall_id;
      sessionAdds++;
      sessionNote();
      beep();
      updateScanConfirm();
      toast("Added " + res.name + (foil ? " ✦" : ""));
      maybeWishlistBought(res, 1);
    } else {
      toast("Error: " + (res.error || "add failed"));
    }
  } catch (err) {
    toast("Add failed: " + err.message);
  }
};

// Short confirmation beep + haptic for heads-down batch scanning.
function beep() {
  try {
    if (navigator.vibrate) navigator.vibrate(30);
  } catch (e) {}
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.connect(gain).connect(audioCtx.destination);
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.15);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.15);
  } catch (e) { /* audio unavailable — non-essential */ }
}

// ------------------------------------------------------------ search
let searchTimer = null;
$("search-input").oninput = (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (q.length < 3) { $("search-results").innerHTML = ""; return; }
  searchTimer = setTimeout(() => runSearch(q), 350);
};

async function runSearch(q) {
  const resp = await fetch("/api/search?q=" + encodeURIComponent(q));
  const data = await resp.json();
  const cards = data.cards || [];
  const box = $("search-results");
  box.innerHTML = "";
  // A single unambiguous hit (e.g. a name + collector number) → show it now.
  if (cards.length === 1) {
    showMatch(cards[0]);
    return;
  }
  for (const c of cards) {
    const div = document.createElement("div");
    div.className = "search-card";
    div.innerHTML = `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
      `<button class="search-wl" title="Add to wishlist">${icon("cart")}</button>` +
      `<small>${esc(c.set_name)} · #${esc(c.collector_number)} · ` +
      `${c.price_usd != null ? "$" + c.price_usd.toFixed(2) : "—"}</small>`;
    div.onclick = () => showMatch(c);
    div.querySelector(".search-wl").onclick = (e) => {
      e.stopPropagation();
      addWishlist(c, e.currentTarget);
    };
    box.appendChild(div);
  }
}

// add a card to the wishlist (server dedupes by printing)
async function addWishlist(card, btn) {
  try {
    await fetch("/api/wishlist/add", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card }),
    });
    toast(`Added ${card.name} to wishlist`);
    if (btn) { btn.classList.add("added"); btn.title = "On wishlist"; }
  } catch (err) {
    toast("Wishlist failed: " + err.message);
  }
}

// ------------------------------------------------------------ library
async function loadLibrary() {
  const resp = await fetch("/api/collection");
  const data = await resp.json();
  libraryCards = data.cards;
  updateStats();
  renderLibrary();
}

function groupKey(group, c) {
  if (group === "set") return c.set_name || "Unknown set";
  if (group === "rarity") return c.rarity || "Other";
  if (group === "color") {
    const cols = c.colors || "";
    if (!cols.length) return "Colorless";
    if (cols.length > 1) return "Multicolor";
    return COLOR_NAME[cols] || "Other";
  }
  if (group === "letter") {
    const ch = (c.name || "")[0] || "#";
    return /[a-z]/i.test(ch) ? ch.toUpperCase() : "#";
  }
  return "All";
}

function groupOrder(group, key) {
  if (group === "rarity") {
    const i = RARITY_ORDER.indexOf(key);
    return i >= 0 ? i : 99;
  }
  if (group === "color") {
    const i = ["White", "Blue", "Black", "Red", "Green", "Multicolor", "Colorless"].indexOf(key);
    return i >= 0 ? i : 99;
  }
  return 0;
}

function filteredLibrary() {
  const filter = $("filter-input").value.trim().toLowerCase();
  const rarity = $("filter-rarity").value;
  const color = $("filter-color").value;
  const foilF = $("filter-foil").value;
  const condF = $("filter-condition").value;
  const statusF = $("filter-status").value;
  return libraryCards.filter((c) => {
    if (filter && !(c.name.toLowerCase().includes(filter) ||
                    (c.set_name || "").toLowerCase().includes(filter) ||
                    (c.type_line || "").toLowerCase().includes(filter))) return false;
    if (rarity !== "all" && c.rarity !== rarity) return false;
    const cols = c.colors || "";
    if (color === "multi" && cols.length < 2) return false;
    if (color === "colorless" && cols.length > 0) return false;
    if (color !== "all" && color !== "multi" && color !== "colorless" && !cols.includes(color)) return false;
    if (foilF === "foil" && !c.foil) return false;
    if (foilF === "nonfoil" && c.foil) return false;
    if (condF !== "all" && (c.condition || "NM") !== condF) return false;
    if (statusF === "trade" && !c.for_trade) return false;
    if (statusF === "sale" && !c.for_sale) return false;
    if (statusF === "trade-sale" && !c.for_trade && !c.for_sale) return false;
    return true;
  });
}

function renderLibrary() {
  const scrollY = window.scrollY;
  const list = $("library-list");

  let rows = filteredLibrary();

  const sort = $("sort-select").value;
  if (sort === "price-low") rows.sort((a, b) => (a.unit_price ?? 0) - (b.unit_price ?? 0));
  else if (sort === "price-high") rows.sort((a, b) => (b.unit_price ?? 0) - (a.unit_price ?? 0));
  else if (sort === "recent") rows.sort((a, b) => (b.added_at || "").localeCompare(a.added_at || ""));
  else if (sort === "set") rows.sort((a, b) =>
    (a.set_name || "").localeCompare(b.set_name || "") ||
    (a.collector_number || "").localeCompare(b.collector_number || "", undefined, { numeric: true }));
  else if (sort === "rarity") rows.sort((a, b) =>
    (RARITY_RANK[a.rarity] ?? 9) - (RARITY_RANK[b.rarity] ?? 9) ||
    (a.name || "").localeCompare(b.name || ""));
  // "collection": server order, name A→Z

  list.innerHTML = "";
  if (!rows.length) {
    list.innerHTML = `<p style="color:var(--text-dim);text-align:center;margin:34px 0">` +
      (libraryCards.length ? "No matches — try clearing the filters." : "Library is empty — scan your first card!") + `</p>`;
    return;
  }

  const group = $("group-select").value;

  if (viewMode === "list") {
    // ledger + sheet pager — there is no bottom of the page to reach
    const totalSheets = Math.max(1, Math.ceil(rows.length / PAGE));
    if (page >= totalSheets) page = totalSheets - 1;
    const visible = rows.slice(page * PAGE, (page + 1) * PAGE);
    renderLedger(visible, list);
    const pager = document.createElement("div");
    pager.className = "pager";
    const from = rows.length ? page * PAGE + 1 : 0;
    const to = Math.min(rows.length, (page + 1) * PAGE);
    pager.innerHTML = `<span class="count">Sheet ${page + 1} of ${totalSheets} · ${from}–${to} of ${rows.length.toLocaleString()}</span>` +
      `<button class="ghost small" id="pager-prev" ${page === 0 ? "disabled" : ""}>Previous sheet</button>` +
      `<button class="ghost small" id="pager-next" ${page >= totalSheets - 1 ? "disabled" : ""}>Next sheet</button>`;
    list.appendChild(pager);
    const prev = pager.querySelector("#pager-prev");
    prev.onclick = () => { page = Math.max(0, page - 1); renderLibrary(); };
    pager.querySelector("#pager-next").onclick = () => { page = Math.min(totalSheets - 1, page + 1); renderLibrary(); };
    requestAnimationFrame(() => window.scrollTo(0, scrollY));
    return;
  }

  if (group === "none") {
    // no grouping → the whole library as one wall, no shelf paging
    renderShelf(rows, list, null, true);
  } else {
    const groups = new Map();
    for (const c of rows) {
      const key = groupKey(group, c);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(c);
    }
    const entries = [...groups.entries()].sort((a, b) => {
      const o = groupOrder(group, a[0]) - groupOrder(group, b[0]);
      return o !== 0 ? o : a[0].localeCompare(b[0]);
    });
    for (const [key, cards] of entries) {
      const collapsed = !!collapsedGroups[key];
      const head = document.createElement("button");
      head.className = "group-head";
      head.setAttribute("aria-expanded", collapsed ? "false" : "true");
      const code = group === "set" ? ((cards[0] && cards[0].set_code) || "").toUpperCase() : "";
      const val = cards.reduce((s, c) => s + (c.line_value || 0), 0);
      head.innerHTML =
        `<svg class="caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 9l7 7 7-7"/></svg>` +
        `<span class="name"></span>` +
        (code ? `<span class="code">${esc(code)}</span>` : "") +
        `<span class="rule"></span>` +
        `<span class="group-meta">${cards.length} · $${val.toFixed(2)}</span>`;
      head.querySelector(".name").textContent = key;
      head.onclick = () => {
        collapsedGroups[key] = !collapsedGroups[key];
        try { localStorage.setItem("mtg_collapsed", JSON.stringify(collapsedGroups)); } catch (e) {}
        renderLibrary();
      };
      list.appendChild(head);
      if (!collapsed) renderShelf(cards, list, key);
    }
  }
  requestAnimationFrame(() => window.scrollTo(0, scrollY));
}

function tileMarkup(c) {
  const check = batchMode
    ? `<input type="checkbox" class="tile-check" ${selectedIds.has(c.id) ? "checked" : ""}>`
    : "";
  const img = c.image_uri
    ? `<img loading="lazy" src="${imgUrl(c.image_uri)}" alt="">`
    : `<div class="art-fallback"></div>`;
  const badges = [];
  if (c.foil) badges.push(`<span class="badge foil">FOIL</span>`);
  if (c.quantity > 1) badges.push(`<span class="badge">×${c.quantity}</span>`);
  if (c.condition && c.condition !== "NM")
    badges.push(`<span class="cond" title="${COND_NAME[c.condition] || c.condition}">${c.condition}</span>`);
  if (c.for_trade) badges.push(`<span class="badge trade">TRADE</span>`);
  if (c.for_sale) badges.push(`<span class="badge sale">SELL</span>`);
  return `<div class="card-tile${c.foil ? " is-foil" : ""}" data-cid="${c.id}" title="${esc(c.name)}">` +
    check + img +
    `<div class="scrim">` +
    (c.unit_price != null ? `<span class="price">$${c.unit_price.toFixed(2)}</span>` : `<span class="price">—</span>`) +
    badges.join("") +
    `<span class="gem ${c.rarity || "common"}"></span>` +
    `</div></div>`;
}

function renderShelf(cards, parent, key, alwaysWall = false) {
  const wall = alwaysWall || expandedWalls.has(key);
  const container = document.createElement("div");
  container.className = wall ? "group-wall" : "shelf";
  const visible = wall ? cards : cards.slice(0, SHELF_PAGE);
  for (const c of visible) {
    const t = document.createElement("div");
    t.innerHTML = tileMarkup(c);
    const tile = t.firstChild;
    const cb = tile.querySelector(".tile-check");
    if (cb) cb.onclick = (e) => { e.stopPropagation(); toggleSelect(c); };
    tile.onclick = () => { if (batchMode) toggleSelect(c); else openDetail(c); };
    container.appendChild(tile);
  }
  if (!alwaysWall) {
    const more = document.createElement("button");
    more.className = "more";
    if (wall) {
      more.textContent = "▴ collapse";
      more.onclick = () => { expandedWalls.delete(key); renderLibrary(); };
    } else if (cards.length > SHELF_PAGE) {
      more.textContent = `+${cards.length - SHELF_PAGE} more`;
      more.onclick = () => { expandedWalls.add(key); renderLibrary(); };
    } else {
      more.style.display = "none";
    }
    container.appendChild(more);
  }
  parent.appendChild(container);
}

function renderLedger(cards, parent) {
  const table = document.createElement("div");
  table.className = "ledger";
  const head = document.createElement("div");
  head.className = "ledger-head";
  head.innerHTML = `<span></span><span>Card</span><span>Set #</span><span>Qty</span><span>Finish</span><span>Cond</span><span>Each</span>`;
  table.appendChild(head);
  for (const c of cards) {
    const row = document.createElement("div");
    row.className = "ledger-row";
    row.dataset.cid = c.id;
    const check = batchMode
      ? `<input type="checkbox" class="row-check" ${selectedIds.has(c.id) ? "checked" : ""}>`
      : "";
    const unit = c.unit_price != null ? "$" + c.unit_price.toFixed(2) : "—";
    row.innerHTML =
      `<span class="thumb">${check}${c.image_uri ? `<img loading="lazy" src="${imgUrl(c.image_uri)}" alt="">` : ""}</span>` +
      `<span class="card-cell"><span class="dot-color" style="background:${colorDot(c)}"></span>` +
      `<span class="name"></span>` +
      (c.foil ? `<span class="foil-tag">FOIL</span>` : "") +
      (c.for_trade ? `<span class="flag-tag trade">TRADE</span>` : "") +
      (c.for_sale ? `<span class="flag-tag sale">SELL</span>` : "") +
      `<span class="gem ${c.rarity || "common"}"></span></span>` +
      `<span class="num">${esc((c.set_code || "").toUpperCase())} #${esc(c.collector_number || "?")}</span>` +
      `<span class="qty-cell">×${c.quantity}</span>` +
      `<span class="finish-cell">${c.foil ? "Foil" : "—"}</span>` +
      `<span class="cond-cell">${c.condition || "NM"}</span>` +
      `<span class="ext">${unit}<small style="display:block;color:var(--text-faint);font-size:10px">${c.unit_price != null ? "= $" + (c.line_value || 0).toFixed(2) : ""}</small></span>`;
    row.querySelector(".name").textContent = c.name;
    const cb = row.querySelector(".row-check");
    if (cb) cb.onclick = (e) => { e.stopPropagation(); toggleSelect(c); };
    row.onclick = (e) => {
      if (e.target.closest("input, button, a")) return;
      if (batchMode) toggleSelect(c); else openDetail(c);
    };
    table.appendChild(row);
  }
  parent.appendChild(table);
}

function colorDot(c) {
  const cols = c.colors || "";
  if (!cols.length) return "var(--mtg-c)";
  if (cols.length > 1) return "var(--mtg-m)";
  return "var(--mtg-" + cols.toLowerCase() + ")";
}

function resetPaging() { page = 0; }
$("filter-input").oninput = () => { resetPaging(); renderLibrary(); };
$("sort-select").onchange = () => { resetPaging(); renderLibrary(); };
$("group-select").onchange = () => { resetPaging(); updateCollapseBtns(); renderLibrary(); };
$("filter-rarity").onchange = () => { resetPaging(); renderLibrary(); };
$("filter-color").onchange = () => { resetPaging(); renderLibrary(); };
$("filter-foil").onchange = () => { resetPaging(); renderLibrary(); };
$("filter-condition").onchange = () => { resetPaging(); renderLibrary(); };
$("filter-status").onchange = () => { resetPaging(); renderLibrary(); };

// Expand / collapse every group at once (library grouping only).
function updateCollapseBtns() {
  $("collapse-seg").classList.toggle("hidden", $("group-select").value === "none");
}

function setAllGroups(collapsed) {
  const group = $("group-select").value;
  if (group === "none") return;
  const keys = new Set(filteredLibrary().map((c) => groupKey(group, c)));
  for (const k of keys) collapsedGroups[k] = collapsed;
  try { localStorage.setItem("mtg_collapsed", JSON.stringify(collapsedGroups)); } catch (e) {}
  renderLibrary();
}
$("expand-all").onclick = () => setAllGroups(false);
$("collapse-all").onclick = () => setAllGroups(true);

$("view-toggle").onclick = (e) => {
  const btn = e.target.closest("button[data-view]");
  if (!btn) return;
  viewMode = btn.dataset.view;
  try { localStorage.setItem("mtg_view", viewMode); } catch (err) {}
  $("view-toggle").querySelectorAll("button")
    .forEach((b) => b.classList.toggle("active", b === btn));
  resetPaging();
  renderLibrary();
};

// ------------------------------------------------------------ batch select
function toggleSelect(c) {
  if (selectedIds.has(c.id)) selectedIds.delete(c.id);
  else selectedIds.add(c.id);
  const el = document.querySelector(`[data-cid="${c.id}"]`);
  const cb = el && el.querySelector(".tile-check, .row-check");
  if (cb) cb.checked = selectedIds.has(c.id);
  updateBatchCount();
}

function updateBatchCount() {
  $("batch-count").textContent = selectedIds.size + " selected";
}

function setBatchMode(on) {
  batchMode = on;
  document.body.classList.toggle("batch", on);
  setBtnLabel($("batch-btn"), on ? "Exit" : "Select");
  $("batch-bar").classList.toggle("hidden", !on);
  if (!on) selectedIds.clear();
  updateBatchCount();
  resetPaging();
  renderLibrary();
}

$("batch-btn").onclick = () => setBatchMode(!batchMode);
$("batch-cancel").onclick = () => setBatchMode(false);
$("batch-select-all").onclick = () => {
  filteredLibrary().forEach((c) => selectedIds.add(c.id));
  renderLibrary();
  updateBatchCount();
};
$("batch-qty").onclick = async () => {
  if (!selectedIds.size) { toast("Select cards first"); return; }
  const q = prompt(`Set quantity for ${selectedIds.size} card(s) to:`, "1");
  if (q == null) return;
  const n = parseInt(q, 10);
  if (isNaN(n) || n < 1) { toast("Invalid quantity"); return; }
  const resp = await fetch("/api/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: [...selectedIds], quantity: n }),
  });
  const d = await resp.json();
  if (d.ok) { toast(`Set ${d.count} card(s) to ×${n}`); }
  else toast("Error: " + (d.error || "failed"));
  selectedIds.clear();
  loadLibrary();
};
$("batch-delete").onclick = async () => {
  if (!selectedIds.size) { toast("Select cards first"); return; }
  if (!confirm(`Remove ${selectedIds.size} card(s) from library?`)) return;
  const resp = await fetch("/api/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: [...selectedIds], delete: true }),
  });
  const d = await resp.json();
  if (d.ok) { toast(`Removed ${d.count} card(s)`); }
  else toast("Error: " + (d.error || "failed"));
  selectedIds.clear();
  loadLibrary();
};

// ------------------------------------------------------------ export / import
$("export-btn").onclick = async () => {
  const resp = await fetch("/api/export");
  if (!resp.ok) { toast("Export failed"); return; }
  const blob = await resp.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "mtg-collection.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  toast("Exported library CSV");
};

$("import-input").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  e.target.value = "";
  const text = await f.text();
  try {
    const resp = await fetch("/api/import", {
      method: "POST",
      headers: { "Content-Type": "text/csv" },
      body: text,
    });
    const d = await resp.json();
    if (d.ok) {
      toast(`Imported: ${d.added} added, ${d.updated} merged` +
        (d.skipped ? `, ${d.skipped} skipped` : ""));
      if (d.skipped) console.warn("Import skipped:", d.errors);
      loadLibrary();
    } else {
      toast("Import error: " + (d.error || "failed"));
    }
  } catch (err) {
    toast("Import error: " + err.message);
  }
};

// ------------------------------------------------------------ offline database
async function refreshLocaldb() {
  const box = $("localdb-box");
  const chip = $("offline-chip");
  try {
    const st = await fetch("/api/localdb").then((r) => r.json());
    const btn = $("localdb-btn");
    btn.disabled = !!st.downloading;
    if (st.downloading) {
      // downloading → keep the full box visible with progress
      box.classList.remove("hidden");
      chip.classList.add("hidden");
      btn.textContent = "…";
      $("localdb-status").textContent =
        st.phase === "build" ? "Building local index…" : "Downloading…";
      updateLocaldbBar(st);
    } else if (st.available) {
      // downloaded → collapse into the small chip next to Prices
      box.classList.add("hidden");
      chip.classList.remove("hidden");
      btn.textContent = "Update";
      const label = chip.querySelector(".btn-label");
      if (label) label.textContent = "Offline";
      chip.title = `${(st.card_count || 0).toLocaleString()} cards offline` +
        (st.downloaded_at ? ` · ${st.downloaded_at.slice(0, 10)} — click to update` : " — click to update");
    } else {
      // not downloaded yet → full prompt stays visible
      box.classList.remove("hidden");
      chip.classList.add("hidden");
      btn.textContent = "Download";
      $("localdb-status").textContent = "Not downloaded yet — enables offline scanning";
    }
  } catch (err) {
    box.classList.add("hidden");
    chip.classList.add("hidden");
  }
}

function updateLocaldbBar(st) {
  const wrap = $("localdb-bar-wrap");
  wrap.classList.remove("hidden");
  const bar = $("localdb-bar");
  if (st.total_mb) {
    bar.classList.remove("indeterminate");
    bar.style.width = Math.min(100, ((st.done_mb || 0) / st.total_mb) * 100) + "%";
  } else {
    bar.classList.add("indeterminate");
  }
  if (st.phase === "build") $("localdb-status").textContent = "Building local index…";
  else if (st.total_mb) $("localdb-status").textContent = `Downloading ${st.done_mb}/${st.total_mb} MB`;
  else if (st.cards) $("localdb-status").textContent = `Downloaded ${st.cards.toLocaleString()} cards…`;
}

$("localdb-btn").onclick = async () => {
  const resp = await fetch("/api/localdb/download", { method: "POST" });
  if (!resp.ok) toast("Download already running", 3000);
  refreshLocaldb();
};

// collapsed offline status — click to surface the full update box
$("offline-chip").onclick = () => {
  $("localdb-box").classList.toggle("hidden");
};

// ------------------------------------------------------------ 3D flip card
// Drag the card in the detail modal to spin it front-to-back. Horizontal
// drag rotates around Y; vertical drag adds a little tilt. On release it
// snaps to the nearest face (0° = front, 180° = back).
let flipRotY = 0, flipRotX = -8, flipDrag = null;
const flip3d = $("flip3d");
const flipInner = $("flip3d-inner");

function flipApply() {
  flipInner.style.transform = `rotateX(${flipRotX}deg) rotateY(${flipRotY}deg)`;
}

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
function flipRelease() {
  if (!flipDrag) return;
  flipDrag = null;
  flipInner.classList.remove("dragging");
  flipRotY = Math.round(flipRotY / 180) * 180; // snap to nearest face
  flipRotX = -8;
  flipApply();
}
flip3d.addEventListener("pointerup", flipRelease);
flip3d.addEventListener("pointercancel", flipRelease);
flip3d.addEventListener("dblclick", () => {
  flipRotY = flipRotY % 360 < 90 || flipRotY % 360 > 270 ? 180 : 0;
  flipApply();
});

function flipReset() {
  flipRotY = 0; flipRotX = -8; flipDrag = null;
  flipInner.classList.remove("dragging");
  flipApply();
}

// ------------------------------------------------------------ card detail modal
function openDetail(card) {
  detailCard = card;
  pendingCard = null;
  $("detail-img").src = imgUrl(card.image_uri || "");
  $("detail-back").src = imgUrl(card.back_image_uri) || "/cardback.jpg";
  flipReset();
  $("detail-name").innerHTML = esc(card.name) + (card.foil ? " " + icon("foil") : "");
  $("detail-set").textContent =
    `${card.set_name || "?"} (${(card.set_code || "").toUpperCase()}) · #${card.collector_number || "?"} · ${card.rarity || ""}`;
  $("detail-prices").textContent =
    `Non-foil ${card.price_usd != null ? "$" + (card.price_usd * condMult(card)).toFixed(2) : "—"}` +
    ` · Foil ${card.price_usd_foil != null ? "$" + (card.price_usd_foil * condMult(card)).toFixed(2) : "—"}` +
    ((card.condition || "NM") !== "NM" ? ` · ${COND_NAME[card.condition] || card.condition}` : "");
  $("detail-foil").checked = !!card.foil;
  $("detail-condition").value = card.condition || "NM";
  $("detail-qty").textContent = card.quantity || 1;
  $("detail-paid").value = card.purchase_price != null ? card.purchase_price : "";
  $("detail-trade").checked = !!card.for_trade;
  $("detail-sale").checked = !!card.for_sale;
  const gainEl = $("detail-gain");
  if (card.purchase_price != null && card.unit_price != null) {
    const diff = card.unit_price - card.purchase_price;
    const pct = card.purchase_price ? (diff / card.purchase_price) * 100 : 0;
    gainEl.textContent =
      `Paid $${card.purchase_price.toFixed(2)} each · now $${card.unit_price.toFixed(2)} · ` +
      (diff >= 0 ? "+$" : "−$") + Math.abs(diff).toFixed(2) +
      ` (${pct >= 0 ? "+" : ""}${pct.toFixed(0)}%)`;
  } else {
    gainEl.textContent = "";
  }
  $("detail-scryfall").href = card.scryfall_uri || "#";
  // cards without a library row id (e.g. from the wishlist) are view-only
  const canEdit = card.id != null;
  const dc = document.querySelector(".detail-controls");
  if (dc) dc.classList.toggle("hidden", !canEdit);
  const saveBtn = $("detail-save");
  if (saveBtn) saveBtn.classList.toggle("hidden", !canEdit);
  $("print-search").value = card.name;
  $("print-results").innerHTML = "";
  $("detail-history").innerHTML = `<p>Loading price history…</p>`;
  $("card-modal").classList.remove("hidden");
  showHistoryChart(card);
  showOracle(card);
  runPrintSearch(card.name);
}

$("detail-close").onclick = () => $("card-modal").classList.add("hidden");
$("card-modal").onclick = (e) => {
  if (e.target === $("card-modal")) $("card-modal").classList.add("hidden");
};

async function changeDetailQty(d) {
  if (!detailCard) return;
  const newQty = detailCard.quantity + d;
  if (newQty <= 0 && !confirm(`Remove ${detailCard.name} from library?`)) return;
  const resp = await fetch("/api/update", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: detailCard.id, quantity: Math.max(0, newQty) }),
  });
  if (resp.ok) {
    detailCard.quantity = Math.max(0, newQty);
    $("detail-qty").textContent = Math.max(0, newQty);
    loadLibrary();
  }
}
$("detail-qty-minus").onclick = () => changeDetailQty(-1);
$("detail-qty-plus").onclick = () => changeDetailQty(1);

$("detail-save").onclick = async () => {
  if (!detailCard) return;
  const paid = $("detail-paid").value;
  const body = { id: detailCard.id, foil: $("detail-foil").checked,
                 condition: $("detail-condition").value,
                 purchase_price: paid === "" ? null : paid,
                 for_trade: $("detail-trade").checked,
                 for_sale: $("detail-sale").checked };
  if (pendingCard) body.card = pendingCard;
  const resp = await fetch("/api/update", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await resp.json();
  if (d.ok) {
    toast("Saved" + (pendingCard ? " — printing changed" : ""));
    $("card-modal").classList.add("hidden");
    loadLibrary();
  } else {
    toast("Error: " + (d.error || "save failed"));
  }
};

$("detail-delete").onclick = async () => {
  if (!detailCard) return;
  if (!confirm(`Remove ${detailCard.name} from library?`)) return;
  await fetch("/api/update", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: detailCard.id, delete: true }),
  });
  $("card-modal").classList.add("hidden");
  loadLibrary();
};

$("print-search").oninput = (e) => {
  clearTimeout(printTimer);
  const q = e.target.value.trim();
  if (q.length < 2) { $("print-results").innerHTML = ""; return; }
  printTimer = setTimeout(() => runPrintSearch(q), 300);
};

async function runPrintSearch(q) {
  const resp = await fetch("/api/search?q=" + encodeURIComponent(q));
  const data = await resp.json();
  const box = $("print-results");
  box.innerHTML = "";
  for (const c of data.cards || []) {
    const div = document.createElement("div");
    div.className = "print-card";
    if (c.scryfall_id === (detailCard && detailCard.scryfall_id)) div.classList.add("selected");
    div.innerHTML = `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
      `<small>${esc(c.set_name)} · #${esc(c.collector_number)} · ${esc(c.rarity)}</small>`;
    div.onclick = () => {
      pendingCard = c;
      box.querySelectorAll(".print-card").forEach((el) => el.classList.remove("selected"));
      div.classList.add("selected");
    };
    box.appendChild(div);
  }
}

async function showOracle(card) {
  const body = $("oracle-body");
  body.innerHTML = `<p class="dim">Loading…</p>`;
  try {
    const resp = await fetch("/api/card/" + card.scryfall_id);
    const d = await resp.json();
    let html = "";
    if (d.oracle_text) {
      html += `<div class="oracle-text">${esc(d.oracle_text)}</div>`;
    }
    if (d.rulings && d.rulings.length) {
      html += `<div class="rulings">` + d.rulings.map((r) =>
        `<p><b>${esc((r.date || "").slice(0, 10))}</b> ${esc(r.text)}</p>`).join("") + `</div>`;
    }
    body.innerHTML = html || `<p class="dim">No oracle text or rulings available.</p>`;
  } catch (err) {
    body.innerHTML = `<p class="dim">Couldn't load: ${esc(err.message)}</p>`;
  }
}

// ------------------------------------------------------------ price history
function fmtDate(iso) {
  const d = new Date(iso);
  let s = (d.getMonth() + 1) + "/" + d.getDate();
  if (d.getFullYear() !== new Date().getFullYear()) s += "/" + String(d.getFullYear()).slice(2);
  return s;
}

async function showHistoryChart(card) {
  const resp = await fetch("/api/history/" + card.scryfall_id);
  const data = await resp.json();
  const pts = (data.history || [])
    .map((h) => ({ t: h.recorded_at, v: card.foil ? h.usd_foil : h.usd }))
    .filter((p) => p.v != null);
  const box = $("detail-history");
  const render = () => {
    if (pts.length < 2) {
      box.innerHTML = `<p>Not enough price history yet — prices are snapshotted ` +
        `each time you refresh. Current: ` +
        (card.unit_price != null ? "$" + card.unit_price.toFixed(2) : "—") + `</p>`;
      return;
    }
    // render at the container's real width so chart text keeps its px size
    box.innerHTML = sparkline(pts, Math.max(300, box.clientWidth));
  };
  render();
  if (window.ResizeObserver && !box._ro) {
    box._ro = new ResizeObserver(render);
    box._ro.observe(box);
  }
}

function sparkline(pts, W = 460) {
  const H = 170, padL = 30, padR = 30, padT = 24, padB = 40;
  const vs = pts.map((p) => p.v);
  const min = Math.min(...vs), max = Math.max(...vs), span = max - min || 1;
  const x = (i) => padL + (i / (pts.length - 1)) * (W - padL - padR);
  const y = (v) => H - padB - ((v - min) / span) * (H - padT - padB);
  const path = pts.map((p, i) =>
    `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const first = pts[0], last = pts[pts.length - 1];
  const dots = pts.length <= 60 ? pts.map((p, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="2.5" fill="#6fce8a">` +
    `<title>${fmtDate(p.t)} · $${p.v.toFixed(2)}</title></circle>`).join("") : "";
  return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
    <path d="${path}" fill="none" stroke="#6fce8a" stroke-width="2"/>
    ${dots}
    <circle cx="${x(pts.length - 1)}" cy="${y(last.v)}" r="4" fill="#6fce8a"><title>now: $${last.v.toFixed(2)}</title></circle>
    <text x="${padL}" y="16" fill="#98a0b0" font-size="11">high $${max.toFixed(2)}</text>
    <text x="${padL}" y="${H - padB + 16}" fill="#98a0b0" font-size="11">low $${min.toFixed(2)}</text>
    <text x="${padL}" y="${H - 8}" fill="#98a0b0" font-size="10">${fmtDate(first.t)}</text>
    <text x="${W - padR}" y="${H - 8}" fill="#98a0b0" font-size="10" text-anchor="end">${fmtDate(last.t)}</text>
    <text x="${W - padR}" y="16" fill="#eef0f4" font-size="12" text-anchor="end">now $${last.v.toFixed(2)}</text>
  </svg>`;
}

// ------------------------------------------------------------ wishlist
let wishlistOpen = false;
let wishlistSearchTimer = null;

$("wishlist-btn").onclick = () => {
  wishlistOpen = true;
  $("wishlist-modal").classList.remove("hidden");
  loadWishlist();
};
$("wishlist-close").onclick = () => {
  wishlistOpen = false;
  $("wishlist-modal").classList.add("hidden");
};
$("wishlist-modal").onclick = (e) => {
  if (e.target === $("wishlist-modal")) { wishlistOpen = false; $("wishlist-modal").classList.add("hidden"); }
};

async function loadWishlist() {
  const data = await fetch("/api/wishlist").then((r) => r.json());
  const box = $("wishlist-items");
  const items = data.items || [];
  updateWishlistBadge(data.alert_count);
  if (!items.length) {
    box.innerHTML = `<p style="color:var(--dim);text-align:center;margin:18px 0">` +
      `Wishlist is empty — search above, or hit Wishlist in any card's details.</p>`;
    return;
  }
  box.innerHTML = "";
  for (const it of items) {
    const row = document.createElement("div");
    row.className = "wl-row" + (it.target_price != null && it.price != null && it.price <= it.target_price ? " alert" : "");
    row.innerHTML =
      `<img loading="lazy" src="${imgUrl(it.image_uri || "")}">` +
      `<div class="wl-main"><b>${esc(it.name)}</b>` +
      `<small>${esc(it.set_name || "?")} · #${esc(it.collector_number || "?")} · ×${it.quantity}</small></div>` +
      `<div class="wl-price"><small>now</small><b>${it.price != null ? "$" + it.price.toFixed(2) : "—"}</b></div>` +
      `<div class="wl-target"><small>target</small>` +
      `<input type="number" step="0.01" min="0" data-id="${it.id}" value="${it.target_price != null ? it.target_price : ""}" placeholder="—"></div>` +
      (it.target_price != null && it.price != null && it.price <= it.target_price
        ? `<span class="wl-alert">⚠ target met</span>` : "") +
      `<button class="wl-rm" data-id="${it.id}" title="Remove">✕</button>` +
      `<button class="wl-bought" data-id="${it.id}" title="Add to collection and remove from wishlist">✔ Bought</button>`;
    row.querySelector("img").onclick = () => openDetail({ ...it, id: null, foil: 0 });
    row.querySelector(".wl-main").onclick = () => openDetail({ ...it, id: null, foil: 0 });
    box.appendChild(row);
  }
  box.querySelectorAll(".wl-rm").forEach((b) => {
    b.onclick = async () => {
      await fetch("/api/wishlist/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: parseInt(b.dataset.id, 10) }),
      });
      loadWishlist();
    };
  });
  box.querySelectorAll(".wl-bought").forEach((b) => {
    b.onclick = async () => {
      const id = parseInt(b.dataset.id, 10);
      b.disabled = true;
      try {
        const resp = await fetch("/api/wishlist/bought", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id, to_collection: true }),
        });
        const d = await resp.json();
        if (d.ok) {
          toast(`Added ${d.name} to your library`);
          loadWishlist();
          loadLibrary();
        } else {
          toast("Error: " + (d.error || "failed"));
          b.disabled = false;
        }
      } catch (err) {
        toast("Bought failed: " + err.message);
        b.disabled = false;
      }
    };
  });
  box.querySelectorAll(".wl-target input").forEach((inp) => {
    inp.onchange = async () => {
      await fetch("/api/wishlist/update", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: parseInt(inp.dataset.id, 10), target_price: inp.value }),
      });
      loadWishlist();
      toast("Target price saved");
    };
  });
}

async function updateWishlistBadge(count) {
  const badge = $("wishlist-badge");
  if (count > 0) {
    badge.textContent = count;
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }
}

$("wishlist-search").oninput = (e) => {
  clearTimeout(wishlistSearchTimer);
  const q = e.target.value.trim();
  if (q.length < 2) { $("wishlist-results").innerHTML = ""; return; }
  wishlistSearchTimer = setTimeout(() => runWishlistSearch(q), 300);
};

async function runWishlistSearch(q) {
  const data = await fetch("/api/search?q=" + encodeURIComponent(q)).then((r) => r.json());
  const box = $("wishlist-results");
  box.innerHTML = "";
  for (const c of (data.cards || [])) {
    const div = document.createElement("div");
    div.className = "search-card";
    div.innerHTML = `<img loading="lazy" src="${imgUrl(c.image_uri || "")}">` +
      `<small>${esc(c.set_name)} · #${esc(c.collector_number)} · ` +
      `${c.price_usd != null ? "$" + c.price_usd.toFixed(2) : "—"}</small>`;
    div.onclick = async () => {
      await fetch("/api/wishlist/add", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ card: c }),
      });
      toast(`Added ${c.name} to wishlist`);
      $("wishlist-search").value = "";
      $("wishlist-results").innerHTML = "";
      loadWishlist();
    };
    box.appendChild(div);
  }
}

$("wishlist-refresh").onclick = async () => {
  const resp = await fetch("/api/wishlist/refresh", { method: "POST" });
  const d = await resp.json();
  if (!resp.ok) { toast(d.error || "refresh already running", 3000); return; }
  const btn = $("wishlist-refresh");
  if (d.started) {
    btn.disabled = true;
    setBtnLabel(btn, "…");
  }
};

$("detail-wishlist").onclick = async () => {
  if (!detailCard) return;
  await fetch("/api/wishlist/add", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ card: detailCard }),
  });
  toast(`Added ${detailCard.name} to wishlist`);
};

// ------------------------------------------------------------ phone pairing
$("pair-btn").onclick = async () => {
  const info = await fetch("/api/info").then((r) => r.json());
  $("pair-url").textContent = info.url;
  if (info.qr_available) {
    $("pair-qr").innerHTML = await fetch("/api/qr").then((r) => r.text());
  } else {
    $("pair-qr").innerHTML =
      `<p style="color:var(--dim)">QR unavailable — open the URL below on your phone.</p>`;
  }
  $("pair-modal").classList.remove("hidden");
};
$("pair-close").onclick = () => $("pair-modal").classList.add("hidden");
$("pair-modal").onclick = (e) => {
  if (e.target === $("pair-modal")) $("pair-modal").classList.add("hidden");
};

// ------------------------------------------------------------ live events
let feedTimer = null;
function showFeed(title, sub, img) {
  $("live-img").src = imgUrl(img || "");
  $("live-title").textContent = title;
  $("live-sub").textContent = sub;
  $("live-feed").classList.remove("hidden");
  clearTimeout(feedTimer);
  feedTimer = setTimeout(() => $("live-feed").classList.add("hidden"), 12000);
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    const evt = JSON.parse(e.data);
    if (evt.type === "scan" && evt.card) {
      showFeed(`Scanning: ${evt.card.name}`,
        `${evt.card.set_name} · ` +
        (evt.card.price_usd != null ? "$" + evt.card.price_usd.toFixed(2) : "no price"),
        evt.card.image_uri);
    } else if (evt.type === "add") {
      showFeed(`Added ${evt.quantity}× ${evt.name}` + (evt.foil ? " ✦" : ""),
        evt.unit_price != null ? "$" + evt.unit_price.toFixed(2) + " each" : "",
        evt.image_uri);
      loadLibrary();
      window.dispatchEvent(new CustomEvent("mtg-library-changed"));
    } else if (evt.type === "library-changed") {
      loadLibrary();
      window.dispatchEvent(new CustomEvent("mtg-library-changed"));
    } else if (evt.type === "price-progress") {
      const btn = $("refresh-btn");
      btn.disabled = true;
      setBtnLabel(btn, `${evt.done}/${evt.total}`);
    } else if (evt.type === "price-done") {
      const btn = $("refresh-btn");
      btn.disabled = false;
      setBtnLabel(btn, "Prices");
      toast(`Updated ${evt.updated} card prices`);
      loadLibrary();
    } else if (evt.type === "localdb-progress") {
      updateLocaldbBar(evt);
    } else if (evt.type === "localdb-done") {
      refreshLocaldb();
      toast("Offline database ready — " + (evt.card_count || 0).toLocaleString() + " cards");
    } else if (evt.type === "localdb-error") {
      refreshLocaldb();
      toast("Offline DB error: " + evt.error, 4000);
    } else if (evt.type === "wishlist-changed") {
      updateWishlistBadge(0);
      fetch("/api/wishlist/alerts").then((r) => r.json()).then((d) => updateWishlistBadge(d.count));
      if (wishlistOpen) loadWishlist();
    } else if (evt.type === "wishlist-progress") {
      const btn = $("wishlist-refresh");
      if (btn) setBtnLabel(btn, `${evt.done}/${evt.total}`);
    } else if (evt.type === "wishlist-done") {
      const btn = $("wishlist-refresh");
      if (btn) { btn.disabled = false; setBtnLabel(btn, "Prices"); }
      toast((evt.alerts && evt.alerts.length)
        ? "Price alert: " + evt.alerts.join(", ") + " hit your target! ✦"
        : "Wishlist prices refreshed");
      loadWishlist();
    }
  };
  es.onerror = () => {
    es.close();
    setTimeout(connectEvents, 3000); // auto-reconnect
  };
}

$("refresh-btn").onclick = async () => {
  const resp = await fetch("/api/refresh-prices", { method: "POST" });
  const data = await resp.json();
  if (!resp.ok) {
    toast(data.error || "Refresh already running", 3000);
    return;
  }
  if (data.started) {
    const btn = $("refresh-btn");
    btn.disabled = true;
    setBtnLabel(btn, data.total ? `0/${data.total}` : "…");
  }
};

connectEvents();
refreshLocaldb();
loadLibrary();

// wishlist alert badge
fetch("/api/wishlist/alerts").then((r) => r.json()).then((d) => updateWishlistBadge(d.count)).catch(() => {});
updateCollapseBtns();
