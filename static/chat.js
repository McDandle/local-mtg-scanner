"use strict";
/* Floating assistant bubble — injected on every page when needle2 is available. */
(function () {
  if (window.__chatBubble) return;
  window.__chatBubble = true;

  const css = `
    .chat-fab { position: fixed; right: 18px; bottom: 18px; z-index: 200;
      width: 52px; height: 52px; border-radius: 50%; border: 0;
      background: var(--accent, #6b5fd3); color: #fff; cursor: pointer;
      box-shadow: 0 8px 24px rgba(0,0,0,.35); font: inherit; font-size: 22px;
      display: grid; place-items: center; }
    .chat-fab:hover { filter: brightness(1.08); }
    .chat-panel { position: fixed; right: 18px; bottom: 80px; z-index: 200;
      width: min(380px, calc(100vw - 24px)); height: min(520px, calc(100vh - 110px));
      display: none; flex-direction: column; overflow: hidden;
      background: var(--surface, #1f2233); color: var(--text, #e8e9ec);
      border: 1px solid var(--line, #2e3247); border-radius: 16px;
      box-shadow: 0 16px 48px rgba(0,0,0,.45); }
    .chat-panel.open { display: flex; }
    .chat-head { display: flex; align-items: center; justify-content: space-between;
      padding: 10px 12px; border-bottom: 1px solid var(--line, #2e3247);
      font-weight: 600; }
    .chat-head button { background: none; border: 0; color: var(--text-dim, #8b8fa3);
      cursor: pointer; font-size: 18px; line-height: 1; padding: 4px 6px; }
    .chat-log { flex: 1; overflow-y: auto; padding: 10px; display: flex;
      flex-direction: column; gap: 8px; }
    .chat-msg { max-width: 88%; padding: 8px 11px; border-radius: 12px;
      line-height: 1.4; white-space: pre-wrap; word-break: break-word; font-size: 13.5px; }
    .chat-msg.user { align-self: flex-end; background: var(--accent, #6b5fd3);
      color: #fff; border-bottom-right-radius: 4px; }
    .chat-msg.bot { align-self: flex-start; background: var(--bg, #161826);
      border: 1px solid var(--line, #2e3247); border-bottom-left-radius: 4px; }
    .chat-msg .meta { display: block; margin-top: 5px; font-size: 10px;
      color: var(--text-dim, #8b8fa3); }
    .chat-chips { display: flex; flex-wrap: wrap; gap: 5px; padding: 0 10px 8px; }
    .chat-chips button { font-size: 11px; padding: 3px 8px; border-radius: 999px;
      border: 1px solid var(--line, #2e3247); background: transparent;
      color: var(--text-dim, #8b8fa3); cursor: pointer; }
    .chat-row { display: flex; gap: 6px; padding: 8px 10px 10px;
      border-top: 1px solid var(--line, #2e3247); }
    .chat-row input { flex: 1; min-width: 0; }
    .chat-msg button.cart { display: inline-block; margin-top: 8px; padding: 6px 10px;
      border-radius: 8px; border: 0; background: var(--accent, #6b5fd3); color: #fff;
      cursor: pointer; font-size: 12.5px; }
  `;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  const wrap = document.createElement("div");
  wrap.innerHTML = `
    <button class="chat-fab" id="chat-fab" title="Assistant" aria-label="Open assistant">✦</button>
    <div class="chat-panel" id="chat-panel" role="dialog" aria-label="Assistant">
      <div class="chat-head"><span>Assistant</span><button id="chat-close" aria-label="Close">×</button></div>
      <div class="chat-log" id="chat-log"></div>
      <div class="chat-chips" id="chat-chips">
        <button data-q="what decks do I have?">my decks</button>
        <button data-q="what am I short on across my decks?">short across decks</button>
        <button data-q="export my collection">export collection</button>
        <button data-q="show my wishlist">wishlist</button>
      </div>
      <div class="chat-row">
        <input id="chat-input" type="text" placeholder="Ask about decks, cards, a commander…" autocomplete="off">
        <button id="chat-send">Send</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);

  const fab = document.getElementById("chat-fab");
  const panel = document.getElementById("chat-panel");
  const log = document.getElementById("chat-log");
  const input = document.getElementById("chat-input");
  const send = document.getElementById("chat-send");

  function toggle(on) {
    const open = on === undefined ? !panel.classList.contains("open") : on;
    panel.classList.toggle("open", open);
    fab.textContent = open ? "–" : "✦";
    fab.setAttribute("aria-label", open ? "Close assistant" : "Open assistant");
    if (open) input.focus();
  }
  fab.addEventListener("click", () => toggle());
  document.getElementById("chat-close").addEventListener("click", () => toggle(false));

  function linkify(el, text) {
    const url = /(https?:\/\/\S+)/g;
    let last = 0, m;
    while ((m = url.exec(text))) {
      if (m.index > last) el.appendChild(document.createTextNode(text.slice(last, m.index)));
      const a = document.createElement("a");
      a.href = m[1]; a.target = "_blank"; a.rel = "noopener";
      a.textContent = /tcgplayer\.com\/massentry/.test(m[1]) ? "Open TCGplayer cart" : m[1];
      el.appendChild(a);
      last = m.index + m[1].length;
    }
    if (last < text.length) el.appendChild(document.createTextNode(text.slice(last)));
  }

  function addMsg(text, who) {
    const m = document.createElement("div");
    m.className = "chat-msg " + who;
    if (who === "bot") linkify(m, text);
    else m.textContent = text;
    log.appendChild(m);
    log.scrollTop = log.scrollHeight;
    return m;
  }

  async function ask(text) {
    addMsg(text, "user");
    input.value = "";
    const wait = addMsg("…", "bot");
    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const data = await r.json();
      wait.textContent = "";
      linkify(wait, data.answer || data.error || "no answer");
      const payload = (data.calls || []).find((c) => c && c.list);
      if (payload && payload.list) {
        wait.appendChild(document.createTextNode("\n"));
        const b = document.createElement("button");
        b.className = "cart";
        const isCart = !!payload.cart;
        b.textContent = isCart ? "Copy list & open TCGplayer" : "Copy list";
        b.onclick = async () => {
          try { await navigator.clipboard.writeText(payload.list); }
          catch (e) {
            const ta = document.createElement("textarea");
            ta.value = payload.list; document.body.appendChild(ta);
            ta.select(); document.execCommand("copy"); ta.remove();
          }
          if (isCart) {
            b.textContent = "Copied — paste on TCGplayer";
            window.open("https://www.tcgplayer.com/massentry?productline=Magic&catalogId=1", "_blank");
          } else {
            b.textContent = "Copied";
          }
        };
        wait.appendChild(b);
      }
    } catch (e) {
      wait.textContent = "chat failed: " + e;
    }
  }

  send.addEventListener("click", () => input.value.trim() && ask(input.value.trim()));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") input.value.trim() && ask(input.value.trim());
  });
  document.getElementById("chat-chips").addEventListener("click", (e) => {
    if (e.target.dataset && e.target.dataset.q) {
      toggle(true);
      ask(e.target.dataset.q);
    }
  });
})();
