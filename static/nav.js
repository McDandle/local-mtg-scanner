"use strict";
/*
 * Shared navigation: page tabs (Home / Library / Insights / Decks) in the
 * top bar. The Decks tab only appears when the optional deck-builder module
 * (deckbuilder.py) is present. Loaded on every page.
 */
(function () {
  if (window.__navLoaded) return;
  window.__navLoaded = true;

  const tabs = document.querySelector("#nav-tabs");
  const cur = location.pathname === "/" ? "index.html" : location.pathname.slice(1);

  const items = [
    { href: "/", page: "index.html", label: "Home", icon: "home" },
    { href: "/library.html", page: "library.html", label: "Library", icon: "cards" },
    { href: "/insights.html", page: "insights.html", label: "Insights", icon: "chart" },
    { href: "/decks.html", page: "decks.html", label: "Decks", icon: "decks" },
  ];
  for (const it of items) {
    const a = document.createElement("a");
    a.href = it.href;
    a.innerHTML = `${it.label}`;
    if (it.page === cur) a.classList.add("active");
    if (it.page === "decks.html") a.id = "nav-decks";
    tabs && tabs.appendChild(a);
  }

  // Decks is only available when the deck extension is present
  fetch("/api/decks").then((r) => {
    if (!r.ok) {
      const d = document.getElementById("nav-decks");
      if (d) d.remove();
    }
  }).catch(() => {
    const d = document.getElementById("nav-decks");
    if (d) d.remove();
  });

  fetch("/api/info").then((r) => r.json()).then((info) => {
    if (info.assistant && !document.querySelector('script[src="/chat.js"]')) {
      const s = document.createElement("script");
      s.src = "/chat.js";
      document.body.appendChild(s);
    }
  }).catch(() => {});

  // legacy dropdown slot (pair/wishlist pages no longer use it)
  const slot = document.querySelector(".nav-slot");
  if (slot) slot.remove();
})();
