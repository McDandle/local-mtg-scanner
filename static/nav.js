"use strict";
/*
 * Shared navigation: a ☰ dropdown in .nav-slot with Home / Library /
 * Insights / Decks. The Decks entry only appears when the optional
 * deck-builder module (deckbuilder.py) is present. Loaded on every page.
 */
(function () {
  if (window.__navLoaded) return;
  window.__navLoaded = true;

  const slot = document.querySelector(".nav-slot");
  if (!slot) return;

  const cur = location.pathname === "/" ? "index.html" : location.pathname.slice(1);

  const btn = document.createElement("button");
  btn.id = "nav-btn";
  btn.className = "ghost";
  btn.title = "Menu";
  btn.setAttribute("aria-label", "Menu");
  btn.innerHTML = '<svg class="ic" aria-hidden="true"><use href="#i-menu"/></svg>';

  const menu = document.createElement("div");
  menu.id = "nav-menu";
  menu.className = "nav-menu hidden";

  const items = [
    { href: "/", page: "index.html", label: "Home", icon: "home" },
    { href: "/library.html", page: "library.html", label: "Library", icon: "cards" },
    { href: "/insights.html", page: "insights.html", label: "Insights", icon: "chart" },
    { href: "/decks.html", page: "decks.html", label: "Decks", icon: "decks" },
  ];
  for (const it of items) {
    const a = document.createElement("a");
    a.href = it.href;
    a.innerHTML =
      `<svg class="ic" aria-hidden="true"><use href="#i-${it.icon}"/></svg> ${it.label}`;
    if (it.page === cur) a.classList.add("active");
    if (it.page === "decks.html") a.id = "nav-decks";
    menu.appendChild(a);
  }

  btn.onclick = (e) => {
    e.stopPropagation();
    menu.classList.toggle("hidden");
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#nav-btn, #nav-menu")) menu.classList.add("hidden");
  });
  // close the menu on navigation
  menu.addEventListener("click", () => menu.classList.add("hidden"));

  // Decks is only available when the deck extension is present
  fetch("/api/decks").then((r) => {
    if (!r.ok) {
      const d = menu.querySelector("#nav-decks");
      if (d) d.remove();
    }
  }).catch(() => {
    const d = menu.querySelector("#nav-decks");
    if (d) d.remove();
  });

  slot.appendChild(btn);
  slot.appendChild(menu);
})();
