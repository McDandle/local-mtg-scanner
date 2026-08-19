"use strict";
/*
 * Icon system for Local TCG Scanner.
 * Injects an SVG sprite and exposes window.icon(name) so HTML and JS can
 * swap flat emoji for brass-stroke glyphs. Load right after <body>.
 */
(function () {
  if (window.__iconsLoaded) return;
  window.__iconsLoaded = true;

  // stroke-style symbols, 24x24 grid
  const S = {
    spark: '<path d="M12 2.6l2.3 7.1 7.1 2.3-7.1 2.3L12 21.4l-2.3-7.1L2.6 12l7.1-2.3z"/>',
    foil: '<path d="M12 3l2 6.4L20.5 12l-6.5 2.6L12 21l-2-6.4L3.5 12 10 9.4z"/><path d="M18.6 3.4l.7 1.9 1.9.7-1.9.7-.7 1.9-.7-1.9-1.9-.7 1.9-.7z"/>',
    home: '<path d="M4 11.2L12 4l8 7.2V20a1 1 0 0 1-1 1h-4.6v-6.2H9.6V21H5a1 1 0 0 1-1-1z"/>',
    cards: '<rect x="8.2" y="3.4" width="11.8" height="16.6" rx="2"/><path d="M5.6 6.4v11.2a2.6 2.6 0 0 0 2.6 2.6h8.6"/>',
    chart: '<path d="M4 4v16h16"/><path d="M6.6 15.4l3.6-4.2 3 2.6 5-6.4"/>',
    decks: '<rect x="9" y="5" width="10.5" height="15" rx="1.8" transform="rotate(6 14 12)"/><rect x="4.5" y="4.6" width="10.5" height="15" rx="1.8" transform="rotate(-6 10 12)"/>',
    camera: '<rect x="3" y="7.2" width="18" height="12.8" rx="2.4"/><circle cx="12" cy="13.4" r="3.7"/><path d="M8.4 7.2l1.3-2.6h4.6l1.3 2.6"/>',
    video: '<rect x="3" y="6.6" width="12.8" height="10.8" rx="2.4"/><path d="M15.8 10.6l5.2-3v8.8l-5.2-3"/>',
    phone: '<rect x="7" y="2.6" width="10" height="18.8" rx="2.6"/><path d="M10.6 18.4h2.8"/>',
    cart: '<path d="M3 4.4h2.3l2.1 11.2a1.6 1.6 0 0 0 1.6 1.3h7.5a1.6 1.6 0 0 0 1.6-1.3L20 8.2H6"/><circle cx="9.7" cy="20.4" r="1.5"/><circle cx="16.6" cy="20.4" r="1.5"/>',
    clock: '<circle cx="12" cy="12" r="8.6"/><path d="M12 7.2v4.8l3.2 2"/>',
    gem: '<path d="M7.2 3.6h9.6l4 5.6L12 20.8 3.2 9.2z"/><path d="M3.6 9.2h16.8M12 20.4L8.2 9.2l3.8-5.4 3.8 5.4z"/>',
    wizard: '<path d="M4.4 20c4.4-1.1 10.8-1.1 15.2 0"/><path d="M7.4 19.4C8.3 12.2 10.5 6.2 12 3.2c1.5 3 3.7 9 4.6 16.2"/><path d="M9.6 12.4c1.5.8 3.3.8 4.8 0"/>',
    book: '<path d="M12 6.2c-2-1.7-4.9-2.1-8-1.9v13.9c3.1-.2 6 .2 8 1.9 2-1.7 4.9-2.1 8-1.9V4.3c-3.1-.2-6 .2-8 1.9z"/><path d="M12 6.2v13.9"/>',
    palette: '<path d="M12 3.2a8.8 8.8 0 1 0 .4 17.6c1.6 0 2.2-1 1.7-2.2-.6-1.5.4-2.7 2-2.7h1.7a3 3 0 0 0 3-3c0-5.4-4-9.7-8.8-9.7z"/><circle cx="8" cy="9" r="1.15" fill="currentColor" stroke="none"/><circle cx="12.6" cy="6.8" r="1.15" fill="currentColor" stroke="none"/><circle cx="16.6" cy="9.6" r="1.15" fill="currentColor" stroke="none"/><circle cx="7.2" cy="13.6" r="1.15" fill="currentColor" stroke="none"/>',
    tools: '<path d="M14.7 6.3a4.2 4.2 0 0 0-5.6 5L3.2 17.2V20.8h3.6l5.9-5.9a4.2 4.2 0 0 0 5-5.6l-2.9 2.9-2.6-.6-.6-2.6z"/>',
    search: '<circle cx="11" cy="11" r="6.6"/><path d="M20.6 20.6l-4.9-4.9"/>',
    die: '<rect x="4" y="4" width="16" height="16" rx="3"/><circle cx="8.7" cy="8.7" r="1.15" fill="currentColor" stroke="none"/><circle cx="15.3" cy="8.7" r="1.15" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.15" fill="currentColor" stroke="none"/><circle cx="8.7" cy="15.3" r="1.15" fill="currentColor" stroke="none"/><circle cx="15.3" cy="15.3" r="1.15" fill="currentColor" stroke="none"/>',
    clipboard: '<path d="M9.2 4.4V3h5.6v1.4"/><path d="M14.8 4.2h2.7A1.5 1.5 0 0 1 19 5.7v13.8a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 19.5V5.7a1.5 1.5 0 0 1 1.5-1.5h2.7"/><path d="M8.6 10h6.8M8.6 13.4h6.8M8.6 16.8h4"/>',
    doc: '<path d="M7 3h7l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v4h4"/><path d="M9.2 12h5.6M9.2 15.4h5.6"/>',
    download: '<path d="M12 4v10.4m0 0l-4.2-4.2m4.2 4.2l4.2-4.2"/><path d="M4.8 19.6h14.4"/>',
    upload: '<path d="M12 14.4V4m0 0L7.8 8.2M12 4l4.2 4.2"/><path d="M4.8 19.6h14.4"/>',
    scales: '<path d="M12 4.2v15M8 19.8h8M12 5.6l-5.6 1.6M12 5.6l5.6 1.6"/><path d="M3.4 13.4l3-5.6 3 5.6a3 3 0 0 1-6 0zM14.6 13.4l3-5.6 3 5.6a3 3 0 0 1-6 0z"/>',
    bulb: '<path d="M9.4 21h5.2M10.2 18.4h3.6"/><path d="M12 3.2a5.9 5.9 0 0 1 3.5 10.6c-.7.6-1 1.3-1 2.1h-5c0-.8-.3-1.5-1-2.1A5.9 5.9 0 0 1 12 3.2z"/>',
    menu: '<path d="M4.4 6.6h15.2M4.4 12h15.2M4.4 17.4h15.2"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 3.6V8h-4.4"/>',
    grid: '<rect x="4" y="4" width="7" height="7" rx="1.4"/><rect x="13" y="4" width="7" height="7" rx="1.4"/><rect x="4" y="13" width="7" height="7" rx="1.4"/><rect x="13" y="13" width="7" height="7" rx="1.4"/>',
    list: '<path d="M9 6.4h11M9 12h11M9 17.6h11"/><circle cx="5" cy="6.4" r="1.2" fill="currentColor" stroke="none"/><circle cx="5" cy="12" r="1.2" fill="currentColor" stroke="none"/><circle cx="5" cy="17.6" r="1.2" fill="currentColor" stroke="none"/>',
    select: '<rect x="4" y="4" width="16" height="16" rx="3.2"/><path d="M8.2 12.4l2.6 2.7 5-5.6"/>',
    copy: '<rect x="8.6" y="8.6" width="11.4" height="11.4" rx="2"/><path d="M15.4 5.4V5a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8.4a2 2 0 0 0 2 2h.4"/>',
    plus: '<path d="M12 5.2v13.6M5.2 12h13.6"/>',
    check: '<path d="M4.6 12.6l4.8 4.9L19.4 6.4"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    compass: '<circle cx="12" cy="12" r="8.8"/><path d="M15.4 8.6l-2 4.8-4.8 2 2-4.8z"/>',
    cardback: '<rect x="5.4" y="3" width="13.2" height="18" rx="2"/><rect x="7.8" y="5.4" width="8.4" height="13.2" rx="1"/><path d="M12 9l1 2.3 2.3 1-2.3 1-1 2.3-1-2.3-2.3-1 2.3-1z"/>',
    price: '<circle cx="12" cy="12" r="8.8"/><path d="M14.8 8.6c-.5-.9-1.6-1.4-2.8-1.4-1.7 0-3 .9-3 2.3 0 3.1 6 1.6 6 4.6 0 1.4-1.3 2.3-3 2.3-1.4 0-2.6-.6-3-1.7M12 5.8v12.4"/>',
    quill: '<path d="M20.4 3.6c-6.8.4-11.6 3.4-13.8 8.6-.9 2.1-1.3 4.4-1.4 6.6 2.4-.1 4.8-.5 6.9-1.5 5.1-2.3 8-7.2 8.3-13.7z"/><path d="M3.6 20.4C7 14 11 9.6 16 6.6"/>',
  };

  const sprite = document.createElement("div");
  sprite.style.display = "none";
  sprite.innerHTML =
    '<svg xmlns="http://www.w3.org/2000/svg" id="icon-sprite" style="position:absolute;width:0;height:0;overflow:hidden">' +
    Object.entries(S)
      .map(
        ([id, body]) =>
          `<symbol id="i-${id}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${body}</symbol>`
      )
      .join("") +
    "</svg>";

  const inject = () => document.body.insertBefore(sprite.firstChild, document.body.firstChild);
  if (document.body) inject();
  else document.addEventListener("DOMContentLoaded", inject);

  // icon(name) -> svg markup string, for JS-built HTML
  window.icon = (name) => `<svg class="ic" aria-hidden="true"><use href="#i-${name}"/></svg>`;

  // upgrade any [data-icon] placeholder to its glyph
  const upgrade = () =>
    document.querySelectorAll("[data-icon]").forEach((el) => {
      el.innerHTML = window.icon(el.dataset.icon);
      el.removeAttribute("data-icon");
    });
  if (document.readyState !== "loading") upgrade();
  document.addEventListener("DOMContentLoaded", upgrade);
})();
