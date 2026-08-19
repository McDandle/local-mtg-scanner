"""Deck Builder module for Local TCG Scanner (MIT licensed, see LICENSE).

Loaded optionally by server.py — removing this file (plus static/decks.*)
cleanly drops every deck feature with no code changes elsewhere.

Decks are stored in SQLite (decks / deck_cards). Every deck card is
compared against the indexed collection so a deck shows what you own vs.
what you need to order, including a priced buy list. See the README
section "Deck Builder" for usage.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))

_NORM = re.compile(r"[^a-z0-9]")
_NUM = re.compile(r"0*(\d+)")


def _norm_name(s):
    return _NORM.sub("", (s or "").lower())


def _norm_num(n):
    m = _NUM.match(str(n or ""))
    return m.group(1) if m else str(n or "")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _server():
    import server  # lazy import: avoids a cycle at module load time
    return server


TABLES = """
CREATE TABLE IF NOT EXISTS decks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    format TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deck_cards (
    id INTEGER PRIMARY KEY,
    deck_id INTEGER NOT NULL,
    scryfall_id TEXT NOT NULL,
    name TEXT NOT NULL,
    set_code TEXT,
    set_name TEXT,
    collector_number TEXT,
    rarity TEXT,
    mana_cost TEXT,
    type_line TEXT,
    colors TEXT,
    image_uri TEXT,
    back_image_uri TEXT,
    foil INTEGER NOT NULL DEFAULT 0,
    quantity INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL DEFAULT 'main',
    price_usd REAL,
    price_usd_foil REAL,
    UNIQUE (deck_id, scryfall_id, foil, role)
);
CREATE INDEX IF NOT EXISTS idx_deck_cards_deck ON deck_cards (deck_id);
CREATE TABLE IF NOT EXISTS deck_value_history (
    id INTEGER PRIMARY KEY,
    deck_id INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    total_value REAL,
    missing_value REAL
);
CREATE INDEX IF NOT EXISTS idx_deck_value ON deck_value_history (deck_id, recorded_at);
CREATE TABLE IF NOT EXISTS deck_matches (
    id INTEGER PRIMARY KEY,
    deck_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    opponent TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deck_matches_deck ON deck_matches (deck_id);
"""


def init_tables(conn):
    """Create deck tables (called from the tracker's init_db)."""
    conn.executescript(TABLES)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(deck_cards)")]
    if "legalities" not in cols:
        conn.execute("ALTER TABLE deck_cards ADD COLUMN legalities TEXT")


# ---------------------------------------------------------------- Archidekt
# Deck import from Archidekt (https://archidekt.com). Deck *detail* is
# fetched through their public API: GET /api/decks/<id>/ returns the full
# card list (names, quantities, Commander/Sideboard categories, set codes,
# collector numbers). Deck *search* is a best-effort feature: Archidekt
# removed their public search endpoint, so we first try the old API route
# and then their server-rendered search page (__NEXT_DATA__ JSON). If both
# are down we report it gracefully — importing by URL/ID always works.

ARCHIDEKT_BASE = "https://archidekt.com"
ARCHIDEKT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# deckFormat is a numeric id in the API; names come from Archidekt's client.
ARCHIDEKT_FORMATS = {
    1: "Standard", 2: "Modern", 3: "Commander", 4: "Legacy", 5: "Vintage",
    6: "Pauper", 7: "Custom", 8: "Frontier", 9: "Future Standard",
    10: "Penny Dreadful", 11: "1v1 Commander", 12: "Duel Commander",
    13: "Standard Brawl", 14: "Oathbreaker", 15: "Pioneer", 16: "Historic",
    17: "Pauper EDH", 18: "Alchemy", 20: "Brawl", 21: "Gladiator",
    22: "Premodern", 23: "PreDH", 24: "Timeless", 25: "Canadian Highlander",
    26: "Competitive Brawl",
}
_arch_lock = threading.Lock()
_arch_last = [0.0]


def _archidekt_get(path, params=None, html=False):
    """GET an Archidekt endpoint, throttled, returns parsed JSON or text."""
    with _arch_lock:
        wait = 0.6 - (time.time() - _arch_last[0])
        if wait > 0:
            time.sleep(wait)
        _arch_last[0] = time.time()
    url = ARCHIDEKT_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": ARCHIDEKT_UA,
        "Accept": "text/html,application/json,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return None
        raise
    if html:
        return body.decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:
        return None


def _find_deck_lists(obj, depth=0):
    """Tolerant dig: find the first list of dicts that look like decks."""
    if depth > 7 or obj is None:
        return None
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "name" in obj[0] and "id" in obj[0]:
            return obj
        for x in obj:
            got = _find_deck_lists(x, depth + 1)
            if got:
                return got
    elif isinstance(obj, dict):
        for v in obj.values():
            got = _find_deck_lists(v, depth + 1)
            if got:
                return got
    return None


def _archidekt_search_page(q):
    """Search via Archidekt's server-rendered search page (__NEXT_DATA__)."""
    html = _archidekt_get("/decks/search", {"q": q}, html=True)
    if not html:
        return None
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return _find_deck_lists(data.get("props", {}).get("pageProps", {}))


def _arch_role(categories):
    """Map Archidekt categories to a deck role; None = skip (maybeboard…)."""
    s = " ".join(str(x) for x in (categories or [])).lower()
    if "commander" in s:
        return "commander"
    if "sideboard" in s:
        return "sideboard"
    if any(k in s for k in ("maybeboard", "considering", "wishlist",
                            "acquireboard", "signature")):
        return None
    return "main"


def _insert_deck_card(conn, deck_id, s, role, qty, foil):
    """Insert (or merge into) a deck_cards row from a card summary."""
    conn.execute(
        "INSERT INTO deck_cards (deck_id, scryfall_id, name, set_code, "
        "set_name, collector_number, rarity, mana_cost, type_line, colors, "
        "image_uri, back_image_uri, foil, quantity, role, price_usd, "
        "price_usd_foil) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (deck_id, s["scryfall_id"], s["name"], s.get("set_code"),
         s.get("set_name"), s.get("collector_number"), s.get("rarity"),
         s.get("mana_cost") or "", s.get("type_line") or "",
         s.get("colors") or "", s.get("image_uri"),
         s.get("back_image_uri"), 1 if foil else 0, max(1, int(qty)), role,
         s.get("price_usd"), s.get("price_usd_foil")))


def api_archidekt_search(self, q):
    """Best-effort popular-deck search. Returns [] + error when unavailable."""
    q = (q or "").strip()
    if not q:
        self.send_json({"decks": []})
        return
    decks = None
    try:
        d = _archidekt_get("/api/decks/search/",
                           {"q": q, "pageSize": 12, "ordering": "-updatedAt"})
        if d and isinstance(d.get("results"), list):
            decks = d["results"]
    except Exception:
        decks = None
    if not decks:
        try:
            decks = _archidekt_search_page(q)
        except Exception:
            decks = None
    if not decks:
        self.send_json({"decks": [],
                        "error": "Archidekt search is currently unavailable "
                                  "(their public search API is down) — paste a "
                                  "deck URL instead."})
        return
    out = []
    for r in decks[:12]:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        user = r.get("user") if isinstance(r.get("user"), dict) else {}
        out.append({
            "id": r.get("id"),
            "name": r["name"],
            "format": r.get("format") or r.get("deckFormat") or "",
            "owner": user.get("username") or "",
            "card_count": r.get("cardCount") or r.get("card_count"),
            "updated_at": r.get("updatedAt") or "",
        })
    self.send_json({"decks": out})


def api_archidekt_import(self):
    """Create a deck from an Archidekt deck URL or numeric ID."""
    body = json.loads(self.read_body())
    url = (body.get("url") or "").strip()
    m = re.search(r"archidekt\.com/decks/(\d+)", url)
    if not m:
        m = re.fullmatch(r"(\d{3,})", url)
    if not m:
        self.send_json({"error": "Paste an Archidekt deck URL "
                                  "(archidekt.com/decks/12345) or just the ID."}, 400)
        return
    _import_archidekt_id(self, m.group(1))


def _import_archidekt_id(self, arch_id):
    """Fetch an Archidekt deck by numeric id and create it — shared by
    api_archidekt_import and api_deck_import_url so read_body (single-use
    per request) is only consumed by the URL-parsing endpoints."""
    try:
        data = _archidekt_get("/api/decks/%s/" % arch_id)
    except Exception as e:
        self.send_json({"error": "Could not reach Archidekt: %s" % e}, 502)
        return
    if not data or data.get("error") or data.get("detail"):
        self.send_json({"error": "Archidekt deck not found (id %s) — check the "
                                  "URL and try again." % arch_id}, 404)
        return
    name = (data.get("name") or "").strip() or ("Archidekt deck " + arch_id)
    fmt = data.get("deckFormat")
    if isinstance(fmt, int):
        fmt = ARCHIDEKT_FORMATS.get(fmt, "Archidekt format %d" % fmt)
    fmt = str(fmt or "").strip() or None
    srv = _server()
    ts = _now_iso()
    with srv.db_lock, srv.db() as conn:
        cur = conn.execute(
            "INSERT INTO decks (name, format, created_at, updated_at) "
            "VALUES (?,?,?,?)", (name, fmt, ts, ts))
        new_deck_id = cur.lastrowid
        skipped = []
        added = 0
        for e in (data.get("cards") or []):
            try:
                qty = max(1, int(e.get("quantity") or 1))
            except (TypeError, ValueError):
                qty = 1
            role = _arch_role(e.get("categories"))
            if role is None:
                continue
            card = e.get("card") or {}
            oc = card.get("oracleCard") or {}
            cname = (oc.get("name") or card.get("displayName") or "").strip()
            if not cname:
                continue
            s = None
            ed = card.get("edition") or {}
            if isinstance(ed, dict):
                set_code = str(ed.get("editioncode") or "").strip()
            else:
                set_code = str(ed or "").strip()
            number = str(card.get("collectorNumber") or "").strip()
            if set_code and number:
                s = srv.local_exact_match(set_code, number)
                if s is None and srv.P.has_api:
                    try:
                        s = srv.P.exact_lookup(set_code, number)
                    except Exception:
                        s = None
            if s is None:
                s = srv.local_name_match(cname)
                if s is None and srv.P.has_api:
                    try:
                        s = srv.P.name_lookup(cname)
                    except Exception:
                        s = None
            if s is None:
                skipped.append(cname)
                continue
            _insert_deck_card(conn, new_deck_id, s, role, qty, False)
            added += qty
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True, "id": new_deck_id, "name": name,
                    "added": added, "skipped": skipped})


# ---------------------------------------------------------------- owned maps
# "Owned" is computed from the collection index: how many copies of the
# exact printing, plus how many of any other printing of the same name
# (a deck slot doesn't care which printing you actually own).

def _owned_maps():
    srv = _server()
    by_id, by_name = {}, {}
    with srv.db_lock, srv.db() as conn:
        for r in conn.execute(
                "SELECT scryfall_id, name, SUM(quantity) AS q FROM cards "
                "GROUP BY scryfall_id, name"):
            by_id[r["scryfall_id"]] = r["q"]
            key = _norm_name(r["name"])
            by_name[key] = by_name.get(key, 0) + r["q"]
    return by_id, by_name


def _card_need(d, owned_by_id, owned_by_name):
    """Given a deck-card row, return (owned, need) with other-print credit."""
    qty = d["quantity"]
    oid = owned_by_id.get(d["scryfall_id"], 0)
    oname = owned_by_name.get(_norm_name(d["name"]), 0)
    exact = min(qty, oid)
    other = min(qty - exact, max(0, oname - oid))
    return exact + other, qty - (exact + other)


def _deck_stats(conn, deck_id, owned_by_id, owned_by_name):
    rows = conn.execute(
        "SELECT * FROM deck_cards WHERE deck_id=? ORDER BY role, name COLLATE NOCASE",
        (deck_id,)).fetchall()
    total = owned = missing = 0
    deck_value = missing_value = 0.0
    cards = []
    for r in rows:
        d = dict(r)
        own, need = _card_need(d, owned_by_id, owned_by_name)
        price = d["price_usd_foil"] if d["foil"] else d["price_usd"]
        d["owned"] = own
        d["owned_exact"] = min(d["quantity"], owned_by_id.get(d["scryfall_id"], 0))
        d["owned_other_printing"] = max(0, own - d["owned_exact"])
        d["need"] = need
        d["unit_price"] = price
        d["line_value"] = round((price or 0) * d["quantity"], 2)
        d["need_value"] = round((price or 0) * need, 2)
        total += d["quantity"]
        owned += own
        missing += need
        deck_value += (price or 0) * d["quantity"]
        missing_value += (price or 0) * need
        cards.append(d)
    stats = {
        "total": total, "owned": owned, "missing": missing,
        "deck_value": round(deck_value, 2),
        "missing_value": round(missing_value, 2),
    }
    return stats, cards


def _deck_summary(conn, deck, owned_by_id, owned_by_name):
    stats, cards = _deck_stats(conn, deck["id"], owned_by_id, owned_by_name)
    return {
        "id": deck["id"], "name": deck["name"], "format": deck["format"],
        "created_at": deck["created_at"], "updated_at": deck["updated_at"],
        "card_count": stats["total"], "owned": stats["owned"],
        "missing": stats["missing"], "missing_value": stats["missing_value"],
        "deck_value": stats["deck_value"],
        # first cards for the index shelf (missing first, so the shelf
        # reads as "what do I still need" at a glance)
        "cards": [{
            "id": c["id"], "name": c["name"], "image_uri": c["image_uri"],
            "foil": c["foil"], "rarity": c["rarity"],
            "quantity": c["quantity"], "unit_price": c["unit_price"],
            "need": c["need"],
        } for c in sorted(cards, key=lambda x: (x["need"] == 0, x["role"]))[:16]],
    }


def _get_deck_or_404(self, conn, deck_id):
    row = conn.execute("SELECT * FROM decks WHERE id=?", (deck_id,)).fetchone()
    if row is None:
        self.send_json({"error": "deck not found"}, 404)
        return None
    return row


# ---------------------------------------------------------------- handlers

def api_decks(self):
    srv = _server()
    owned_by_id, owned_by_name = _owned_maps()
    with srv.db_lock, srv.db() as conn:
        decks = [dict(r) for r in conn.execute(
            "SELECT * FROM decks ORDER BY updated_at DESC")]
        out = [_deck_summary(conn, d, owned_by_id, owned_by_name) for d in decks]
    self.send_json({"decks": out})


def api_decks_create(self):
    body = json.loads(self.read_body())
    name = (body.get("name") or "").strip()
    if not name:
        self.send_json({"error": "deck name required"}, 400)
        return
    fmt = (body.get("format") or "").strip() or None
    ts = _now_iso()
    srv = _server()
    with srv.db_lock, srv.db() as conn:
        cur = conn.execute(
            "INSERT INTO decks (name, format, created_at, updated_at) "
            "VALUES (?,?,?,?)", (name, fmt, ts, ts))
        deck_id = cur.lastrowid
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True, "id": deck_id})


def api_deck_detail(self, deck_id):
    srv = _server()
    owned_by_id, owned_by_name = _owned_maps()
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        stats, cards = _deck_stats(conn, deck_id, owned_by_id, owned_by_name)
    self.send_json({"deck": dict(deck), "stats": stats, "cards": cards})


def api_deck_update(self, deck_id):
    body = json.loads(self.read_body())
    srv = _server()
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        name = deck["name"]
        fmt = deck["format"]
        if "name" in body:
            name = (body.get("name") or "").strip() or deck["name"]
        if "format" in body:
            fmt = (body.get("format") or "").strip() or None
        conn.execute(
            "UPDATE decks SET name=?, format=?, updated_at=? WHERE id=?",
            (name, fmt, _now_iso(), deck_id))
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True})


def api_deck_delete(self, deck_id):
    srv = _server()
    with srv.db_lock, srv.db() as conn:
        conn.execute("DELETE FROM deck_cards WHERE deck_id=?", (deck_id,))
        conn.execute("DELETE FROM decks WHERE id=?", (deck_id,))
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True})


def api_deck_duplicate(self, deck_id):
    srv = _server()
    with srv.db_lock, srv.db() as conn:
        src = _get_deck_or_404(self, conn, deck_id)
        if src is None:
            return
        ts = _now_iso()
        cur = conn.execute(
            "INSERT INTO decks (name, format, created_at, updated_at) "
            "VALUES (?,?,?,?)", (src["name"] + " (copy)", src["format"], ts, ts))
        new_id = cur.lastrowid
        cols = ("scryfall_id,name,set_code,set_name,collector_number,rarity,"
                "mana_cost,type_line,colors,image_uri,back_image_uri,foil,"
                "quantity,role,price_usd,price_usd_foil")
        for c in conn.execute(
                "SELECT %s FROM deck_cards WHERE deck_id=?" % cols, (deck_id,)):
            conn.execute(
                "INSERT INTO deck_cards (deck_id,%s) VALUES (%s)" % (
                    cols, ",".join("?" * (len(c) + 1))),
                (new_id,) + tuple(c))
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True, "id": new_id})


def _resolve_summary(body, self):
    """Card summary from the request body: prefer the full summary the
    frontend already has, otherwise look it up (local index, then API)."""
    card = body.get("card")
    if isinstance(card, dict) and card.get("scryfall_id") and card.get("name"):
        return card
    sid = (body.get("scryfall_id") or "").strip()
    if not sid:
        return None
    srv = _server()
    s = srv.local_by_sid(sid)
    if s is None and srv.P.has_api:
        try:
            raw = srv.P.get_card(sid)
            if raw:
                s = srv.P.summary(raw)
        except Exception:
            s = None
    return s


def api_deck_add_card(self, deck_id):
    body = json.loads(self.read_body())
    s = _resolve_summary(body, self)
    if s is None:
        self.send_json({"error": "card not found"}, 404)
        return
    qty = int(body.get("quantity", 1))
    foil = 1 if body.get("foil") else 0
    role = (body.get("role") or "main").strip() or "main"
    if role not in ("commander", "main", "sideboard"):
        role = "main"
    replace = bool(body.get("replace"))
    if qty < 1 and not replace:
        qty = 1
    srv = _server()
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        cur = conn.execute(
            "SELECT id, quantity FROM deck_cards WHERE deck_id=? AND "
            "scryfall_id=? AND foil=? AND role=?",
            (deck_id, s["scryfall_id"], foil, role)).fetchone()
        if replace:
            if qty <= 0:
                if cur:
                    conn.execute("DELETE FROM deck_cards WHERE id=?", (cur["id"],))
                final_qty = 0
            elif cur:
                conn.execute("UPDATE deck_cards SET quantity=?, price_usd=?, "
                             "price_usd_foil=?, image_uri=?, back_image_uri=? "
                             "WHERE id=?",
                             (qty, s.get("price_usd"), s.get("price_usd_foil"),
                              s.get("image_uri"), s.get("back_image_uri"),
                              cur["id"]))
                final_qty = qty
            else:
                _insert_deck_card(conn, deck_id, s, role, qty, foil)
                final_qty = qty
        elif cur:
            conn.execute("UPDATE deck_cards SET quantity=quantity+? WHERE id=?",
                         (qty, cur["id"]))
            final_qty = cur["quantity"] + qty
        else:
            _insert_deck_card(conn, deck_id, s, role, qty, foil)
            final_qty = qty
        conn.execute("UPDATE decks SET updated_at=? WHERE id=?",
                     (_now_iso(), deck_id))
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True, "quantity": final_qty})


def api_deck_remove_card(self, deck_id):
    body = json.loads(self.read_body())
    srv = _server()
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        if body.get("id"):
            conn.execute(
                "DELETE FROM deck_cards WHERE id=? AND deck_id=?",
                (int(body["id"]), deck_id))
        else:
            conn.execute(
                "DELETE FROM deck_cards WHERE deck_id=? AND scryfall_id=? "
                "AND foil=? AND role=?",
                (deck_id, body.get("scryfall_id", ""),
                 1 if body.get("foil") else 0,
                 body.get("role") or "main"))
        conn.execute("UPDATE decks SET updated_at=? WHERE id=?",
                     (_now_iso(), deck_id))
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True})


def api_deck_missing(self, deck_id, cheapest=False):
    """The buy list: every printing still needed, aggregated and priced.

    cheapest=True swaps each missing printing for the cheapest printing of
    that card name in the offline index (when one is available).
    """
    srv = _server()
    owned_by_id, owned_by_name = _owned_maps()
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        stats, cards = _deck_stats(conn, deck_id, owned_by_id, owned_by_name)
    agg = {}
    for c in cards:
        if c["need"] <= 0:
            continue
        key = (c["scryfall_id"], c["foil"])
        a = agg.get(key)
        if a is None:
            a = {
                "scryfall_id": c["scryfall_id"],
                "name": c["name"],
                "set_code": c["set_code"],
                "set_name": c["set_name"],
                "collector_number": c["collector_number"],
                "rarity": c["rarity"],
                "foil": c["foil"],
                "image_uri": c["image_uri"],
                "qty": 0,
                "unit_price": c["unit_price"],
                "total": 0.0,
            }
            agg[key] = a
        a["qty"] += c["need"]
        a["total"] = round(a["qty"] * (a["unit_price"] or 0), 2)
    if cheapest:
        _apply_cheapest(srv, agg)
    items = sorted(agg.values(), key=lambda x: (
        (x["set_name"] or "").lower(), _norm_num(x["collector_number"]),
        x["name"].lower()))
    self.send_json({
        "items": items,
        "total": round(sum(i["total"] for i in items), 2),
        "stats": stats,
        "cheapest": bool(cheapest),
    })


def _apply_cheapest(srv, agg):
    """Swap each aggregated missing printing for its cheapest printing."""
    byname = srv.local_by_name_index()
    if not byname:
        return
    swapped = {}
    for key, a in list(agg.items()):
        cands = byname.get(_norm_name(a["name"]), [])
        best = None
        for c in cands:
            p = c.get("price_usd_foil") if a["foil"] else c.get("price_usd")
            if p is None:
                continue
            if best is None or p < best[1]:
                best = (c, p)
        if best is None:
            continue
        c, p = best
        new_key = (c["scryfall_id"], a["foil"])
        if new_key in swapped:
            swapped[new_key]["qty"] += a["qty"]
            swapped[new_key]["total"] = round(
                swapped[new_key]["qty"] * swapped[new_key]["unit_price"], 2)
        else:
            swapped[new_key] = {
                "scryfall_id": c["scryfall_id"],
                "name": c["name"],
                "set_code": c.get("set_code"),
                "set_name": c.get("set_name"),
                "collector_number": c.get("collector_number"),
                "rarity": c.get("rarity"),
                "foil": a["foil"],
                "image_uri": c.get("image_uri"),
                "qty": a["qty"],
                "unit_price": p,
                "total": round(a["qty"] * p, 2),
            }
    if swapped:
        agg.clear()
        agg.update(swapped)


def api_decks_resolve(self):
    """Resolve a pasted decklist's card names to summaries (newest printing).

    Used by "Paste decklist": local index first (fast/offline), then the
    game API. Unmatched names come back with an error so the UI can report.
    """
    body = json.loads(self.read_body())
    srv = _server()
    out = []
    for line in body.get("lines", []):
        name = (line.get("name") or "").strip()
        if not name:
            out.append({"card": None, "error": "empty line"})
            continue
        card = srv.local_name_match(name)
        if card is None and srv.P.has_api:
            try:
                card = srv.P.name_lookup(name)
            except Exception:
                card = None
        if card is None and srv.P.has_api:
            try:
                hits = srv.P.search(name, limit=3)
                if hits:
                    card = hits[0]
            except Exception:
                card = None
        out.append({"card": card,
                    "error": None if card else "not found: " + name})
    self.send_json({"results": out})


# ------------------------------------------------- resource planner
# One card owned, many decks wanting it: the planner aggregates demand
# across every deck so shortages are visible at the collection level.

def api_decks_planner(self):
    srv = _server()
    owned_by_id, owned_by_name = _owned_maps()
    with srv.db_lock, srv.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT dc.*, d.name AS deck_name FROM deck_cards dc "
            "JOIN decks d ON d.id = dc.deck_id "
            "ORDER BY dc.name COLLATE NOCASE")]
    agg, deck_names = {}, set()
    for r in rows:
        key = _norm_name(r["name"])
        a = agg.get(key)
        if a is None:
            a = {"name": r["name"], "demand": 0, "decks": {},
                 "image_uri": r["image_uri"], "owned": 0,
                 "scryfall_id": r["scryfall_id"],
                 "set_code": r["set_code"], "set_name": r["set_name"],
                 "collector_number": r["collector_number"],
                 "rarity": r["rarity"], "price_usd": r["price_usd"],
                 "price_usd_foil": r["price_usd_foil"]}
            agg[key] = a
        a["demand"] += r["quantity"]
        a["decks"][r["deck_name"]] = a["decks"].get(r["deck_name"], 0) + r["quantity"]
        deck_names.add(r["deck_name"])
    out = []
    for a in agg.values():
        a["owned"] = owned_by_name.get(_norm_name(a["name"]), 0)
        a["deficit"] = max(0, a["demand"] - a["owned"])
        a["deck_list"] = sorted(
            ({"deck": k, "qty": v} for k, v in a["decks"].items()),
            key=lambda x: (-x["qty"], x["deck"]))
        out.append(a)
    out.sort(key=lambda x: (-x["deficit"], x["name"].lower()))
    self.send_json({
        "cards": out,
        "total_demand": sum(a["demand"] for a in out),
        "total_deficit": sum(a["deficit"] for a in out),
        "deck_count": len(deck_names),
    })


# ---------------------------------------------------------------- legality
# Format checks powered by Scryfall's per-card legalities (fetched lazily
# and cached on the deck card rows so repeat checks are instant/offline).

LEGAL_FORMATS = {
    "standard": "standard", "modern": "modern", "pioneer": "pioneer",
    "legacy": "legacy", "vintage": "vintage", "pauper": "pauper",
    "commander": "commander", "brawl": "brawl", "duel": "commander",
}
COMMANDER_LIKE = ("commander", "brawl")


def api_deck_legality(self, deck_id, fmt):
    fmt = (fmt or "commander").strip().lower()
    key = LEGAL_FORMATS.get(fmt, "commander")
    srv = _server()
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        cards = [dict(r) for r in conn.execute(
            "SELECT * FROM deck_cards WHERE deck_id=?", (deck_id,))]
    # lazy legalities enrichment (network; no db lock held)
    missing = [c for c in cards if not c.get("legalities")]
    fetched = {}
    if missing and srv.P.has_api:
        for c in missing:
            try:
                raw = srv.P.get_card(c["scryfall_id"])
            except Exception:
                raw = None
            if raw and raw.get("legalities"):
                fetched[c["id"]] = raw["legalities"]
    if fetched:
        with srv.db_lock, srv.db() as conn:
            for cid, leg in fetched.items():
                conn.execute(
                    "UPDATE deck_cards SET legalities=? WHERE id=?",
                    (json.dumps(leg), cid))
    for c in cards:
        if c["id"] in fetched:
            c["legalities"] = fetched[c["id"]]
        else:
            try:
                c["legalities"] = json.loads(c.get("legalities") or "{}")
            except Exception:
                c["legalities"] = {}

    issues = []
    total = sum(c["quantity"] for c in cards)
    main_cards = [c for c in cards if c["role"] in ("main", "commander")]

    if key in COMMANDER_LIKE:
        want = 100 if key == "commander" else 60
        if total != want:
            issues.append({"card": None, "issue": "Deck has %d cards — "
                          "%s needs exactly %d." % (total, fmt.title(), want),
                          "severity": "error"})
    else:
        main_qty = sum(c["quantity"] for c in main_cards)
        if main_qty < 60:
            issues.append({"card": None, "issue": "Main deck has %d cards — "
                          "60-card formats need at least 60." % main_qty,
                          "severity": "error"})
        sb_qty = sum(c["quantity"] for c in cards if c["role"] == "sideboard")
        if sb_qty > 15:
            issues.append({"card": None, "issue": "Sideboard has %d cards — "
                          "max 15." % sb_qty, "severity": "error"})

    checked = 0
    for c in main_cards:
        checked += 1
        name = c["name"]
        leg = c.get("legalities") or {}
        status = leg.get(key, "")
        if key in COMMANDER_LIKE:
            if c["role"] == "commander":
                if status == "banned":
                    issues.append({"card": name, "issue": "Banned as a "
                                  "commander.", "severity": "error"})
                elif status not in ("legal", "restricted"):
                    issues.append({"card": name, "issue": "Not legal as a "
                                  "commander (or legality unknown).",
                                  "severity": "warning"})
            else:
                if status == "banned":
                    issues.append({"card": name, "issue": "Banned in %s."
                                  % fmt.title(), "severity": "error"})
                elif not leg:
                    issues.append({"card": name, "issue": "Legality unknown "
                                  "(offline) — verify manually.",
                                  "severity": "warning"})
        else:
            if status == "banned":
                issues.append({"card": name, "issue": "Banned in %s."
                              % fmt.title(), "severity": "error"})
            elif status == "restricted" and key == "vintage":
                issues.append({"card": name, "issue": "Restricted (max 1) "
                              "in Vintage.", "severity": "warning"})
            elif not leg:
                issues.append({"card": name, "issue": "Legality unknown "
                              "(offline) — verify manually.",
                              "severity": "warning"})

    def _dupes(limit, label):
        by_name = {}
        for c in main_cards:
            if "Basic Land" in (c.get("type_line") or ""):
                continue
            by_name.setdefault(_norm_name(c["name"]), []).append(c)
        for cs in by_name.values():
            q = sum(c["quantity"] for c in cs)
            if q > limit:
                issues.append({"card": cs[0]["name"], "qty": q,
                               "issue": label, "severity": "error"})

    if key == "commander":
        _dupes(1, "Commander allows only one copy of each card "
                   "(except basic lands).")
        cmd = [c for c in cards if c["role"] == "commander"]
        identity = set()
        for c in cmd:
            identity.update(c.get("colors") or "")
        for c in main_cards:
            if c["role"] == "commander":
                continue
            cols = set(c.get("colors") or "")
            if cols - identity:
                issues.append({"card": c["name"], "issue": "Color identity "
                              "%s outside commander identity %s." % (
                                  "".join(sorted(cols)) or "colorless",
                                  "".join(sorted(identity)) or "colorless"),
                              "severity": "error"})
    elif key == "brawl":
        _dupes(1, "Brawl allows only one copy of each card "
                  "(except basic lands).")
    else:
        if key == "pauper":
            for c in main_cards:
                if (c.get("rarity") or "").lower() != "common":
                    issues.append({"card": c["name"], "issue": "Not "
                                  "common — illegal in Pauper.",
                                  "severity": "error"})
        _dupes(4, "More than 4 copies of a card in a 4-of format.")
        if key == "vintage":
            by_name = {}
            for c in main_cards:
                by_name.setdefault(_norm_name(c["name"]), []).append(c)
            for cs in by_name.values():
                if (sum(c["quantity"] for c in cs) > 1 and
                        any((c.get("legalities") or {}).get("vintage") == "restricted"
                            for c in cs)):
                    issues.append({"card": cs[0]["name"], "qty":
                                  sum(c["quantity"] for c in cs),
                                  "issue": "Restricted card — max 1 copy.",
                                  "severity": "error"})

    self.send_json({"format": fmt, "key": key, "issues": issues,
                    "checked": checked, "total_cards": total,
                    "ok": not any(i["severity"] == "error" for i in issues)})


# ------------------------------------------------------ deck value history

def _deck_value_from_cards(cards):
    total = 0.0
    for c in cards:
        p = c.get("price_usd_foil") if c.get("foil") else c.get("price_usd")
        total += (p or 0) * c["quantity"]
    return round(total, 2)


def _record_value(conn, deck_id, cards, owned_by_id, owned_by_name):
    stats, _ = _deck_stats(conn, deck_id, owned_by_id, owned_by_name)
    conn.execute(
        "INSERT INTO deck_value_history (deck_id, recorded_at, total_value, "
        "missing_value) VALUES (?,?,?,?)",
        (deck_id, _now_iso(), stats["deck_value"], stats["missing_value"]))


def api_deck_value(self, deck_id):
    srv = _server()
    owned_by_id, owned_by_name = _owned_maps()
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        cards = [dict(r) for r in conn.execute(
            "SELECT * FROM deck_cards WHERE deck_id=?", (deck_id,))]
        last = conn.execute(
            "SELECT recorded_at FROM deck_value_history WHERE deck_id=? "
            "ORDER BY recorded_at DESC LIMIT 1", (deck_id,)).fetchone()
        stale = True
        if last:
            try:
                ts = datetime.strptime(last["recorded_at"], "%Y-%m-%dT%H:%M:%SZ")
                stale = (datetime.now(timezone.utc) - ts).total_seconds() > 3600
            except Exception:
                stale = True
        if last is None or stale:
            _record_value(conn, deck_id, cards, owned_by_id, owned_by_name)
        history = [dict(r) for r in conn.execute(
            "SELECT recorded_at, total_value, missing_value FROM "
            "deck_value_history WHERE deck_id=? ORDER BY recorded_at",
            (deck_id,))]
    self.send_json({"history": history,
                    "current": _deck_value_from_cards(cards)})


def api_deck_value_record(self, deck_id):
    srv = _server()
    owned_by_id, owned_by_name = _owned_maps()
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        cards = [dict(r) for r in conn.execute(
            "SELECT * FROM deck_cards WHERE deck_id=?", (deck_id,))]
        _record_value(conn, deck_id, cards, owned_by_id, owned_by_name)
    self.send_json({"ok": True,
                    "current": _deck_value_from_cards(cards)})


# -------------------------------------------------------- win/loss tracker

def api_deck_matches(self, deck_id):
    """Win/loss record for a deck, plus per-commander head-to-head."""
    srv = _server()
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        rows = [dict(r) for r in conn.execute(
            "SELECT id, result, opponent, recorded_at FROM deck_matches "
            "WHERE deck_id=? ORDER BY id DESC LIMIT 50", (deck_id,))]
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")
    total = wins + losses
    opp = {}
    for r in rows:
        key = ((r.get("opponent") or "").strip().lower()) or "unknown"
        a = opp.get(key)
        if a is None:
            a = {"opponent": (r.get("opponent") or "").strip() or "Unknown",
                 "wins": 0, "losses": 0}
            opp[key] = a
        if r["result"] == "win":
            a["wins"] += 1
        else:
            a["losses"] += 1
    matchups = sorted(opp.values(), key=lambda a: -(a["wins"] + a["losses"]))
    for a in matchups:
        total_a = a["wins"] + a["losses"]
        a["winrate"] = round(a["wins"] / total_a * 100) if total_a else None
    self.send_json({
        "deck_id": deck_id, "wins": wins, "losses": losses, "total": total,
        "winrate": round(wins / total * 100) if total else None,
        "matchups": matchups, "recent": rows[:12],
    })


def api_deck_match_add(self, deck_id):
    srv = _server()
    body = json.loads(self.read_body())
    result = "win" if body.get("result") == "win" else "loss"
    opponent = (body.get("opponent") or "").strip()
    if not opponent:
        self.send_json({"error": "commander name required"}, 400)
        return
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        conn.execute(
            "INSERT INTO deck_matches (deck_id, result, opponent, recorded_at) "
            "VALUES (?,?,?,?)", (deck_id, result, opponent, _now_iso()))
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True})


def api_deck_match_delete(self):
    srv = _server()
    body = json.loads(self.read_body())
    with srv.db_lock, srv.db() as conn:
        conn.execute("DELETE FROM deck_matches WHERE id=?",
                     (int(body.get("id", 0)),))
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True})


# ------------------------------------------------- add deck to collection

def api_deck_add_to_collection(self, deck_id):
    """Merge every card in a deck into the scanned collection (precons!)."""
    srv = _server()
    with srv.db_lock, srv.db() as conn:
        deck = _get_deck_or_404(self, conn, deck_id)
        if deck is None:
            return
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM deck_cards WHERE deck_id=?", (deck_id,))]
    if not rows:
        self.send_json({"ok": True, "added": 0, "updated": 0,
                        "total_cards": 0})
        return
    srv.backup_db()
    ts = srv.now_iso()
    added = updated = total_qty = 0
    with srv.db_lock, srv.db() as conn:
        for r in rows:
            sid = r["scryfall_id"]
            foil = r["foil"]
            qty = r["quantity"]
            existing = conn.execute(
                "SELECT id FROM cards WHERE scryfall_id=? AND foil=?",
                (sid, foil)).fetchone()
            if existing:
                conn.execute("UPDATE cards SET quantity=quantity+? WHERE id=?",
                             (qty, existing["id"]))
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO cards (scryfall_id, name, set_code, set_name, "
                    "collector_number, rarity, mana_cost, type_line, colors, "
                    "image_uri, scryfall_uri, back_image_uri, foil, quantity, "
                    "price_usd, price_usd_foil, price_updated_at, added_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, r["name"], r["set_code"], r["set_name"],
                     r["collector_number"], r["rarity"], r["mana_cost"] or "",
                     r["type_line"] or "", r["colors"] or "",
                     r["image_uri"], "", r["back_image_uri"], foil, qty,
                     r["price_usd"], r["price_usd_foil"], ts, ts))
                added += 1
            conn.execute(
                "INSERT INTO price_history (scryfall_id, recorded_at, usd, "
                "usd_foil) VALUES (?,?,?,?)",
                (sid, ts, r["price_usd"], r["price_usd_foil"]))
            total_qty += qty
    srv.broadcast({"type": "library-changed"})
    self.send_json({"ok": True, "added": added, "updated": updated,
                    "total_cards": total_qty})


def api_collection_import_decklist(self):
    """Resolve a pasted decklist and add the cards straight to the collection
    (no deck created) — the quick way to index pre-con lists."""
    body = json.loads(self.read_body())
    lines = body.get("lines", [])
    srv = _server()
    resolved, skipped = [], []
    for line in lines:
        name = (line.get("name") or "").strip()
        if not name:
            continue
        try:
            qty = max(1, int(line.get("quantity") or 1))
        except (TypeError, ValueError):
            qty = 1
        card = srv.local_name_match(name)
        if card is None and srv.P.has_api:
            try:
                card = srv.P.name_lookup(name)
            except Exception:
                card = None
        if card is None and srv.P.has_api:
            try:
                hits = srv.P.search(name, limit=3)
                if hits:
                    card = hits[0]
            except Exception:
                card = None
        if card is None:
            skipped.append(name)
        else:
            resolved.append((card, qty))
    if not resolved:
        self.send_json({"ok": True, "added": 0, "updated": 0,
                        "skipped": skipped, "total_cards": 0})
        return
    srv.backup_db()
    ts = srv.now_iso()
    added = updated = total = 0
    with srv.db_lock, srv.db() as conn:
        for s, qty in resolved:
            sid = s["scryfall_id"]
            existing = conn.execute(
                "SELECT id FROM cards WHERE scryfall_id=? AND foil=0",
                (sid,)).fetchone()
            if existing:
                conn.execute("UPDATE cards SET quantity=quantity+? WHERE id=?",
                             (qty, existing["id"]))
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO cards (scryfall_id, name, set_code, set_name, "
                    "collector_number, rarity, mana_cost, type_line, colors, "
                    "image_uri, scryfall_uri, back_image_uri, foil, quantity, "
                    "price_usd, price_usd_foil, price_updated_at, added_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, s["name"], s.get("set_code"), s.get("set_name"),
                     s.get("collector_number"), s.get("rarity"),
                     s.get("mana_cost") or "", s.get("type_line") or "",
                     s.get("colors") or "", s.get("image_uri"), "",
                     s.get("back_image_uri"), 0, qty, s.get("price_usd"),
                     s.get("price_usd_foil"), ts, ts))
                added += 1
            conn.execute(
                "INSERT INTO price_history (scryfall_id, recorded_at, usd, "
                "usd_foil) VALUES (?,?,?,?)",
                (sid, ts, s.get("price_usd"), s.get("price_usd_foil")))
            total += qty
    srv.broadcast({"type": "library-changed"})
    self.send_json({"ok": True, "added": added, "updated": updated,
                    "skipped": skipped, "total_cards": total})


# ------------------------------------------------------ precon deck index
# Searchable index of official preconstructed decks (MTGJSON). A background
# sync pulls the set list + every precon-bearing set + MTGJSON's card
# identifiers, resolves every deck card to its exact printing, and stores a
# small searchable index in precon_index.json. Searching and importing are
# then instant/offline. Reprints are resolved through the same identifier
# map, so even Sol Ring in a precon gets its exact printing.

PRECON_DIR = os.path.join(ROOT, "precon_cache")
PRECON_INDEX_FILE = os.path.join(ROOT, "precon_index.json")
PRECON_SET_TYPES = ("commander", "duel_deck", "planechase", "archenemy",
                    "vanguard")
MTGJSON_BASE = "https://mtgjson.com/api/v5"
MTGJSON_UA = "LocalCardTracker/1.0 (personal collection tool)"

_precon_index = None
_precon_state = {"active": False, "phase": "", "done": 0, "total": 0,
                 "decks": 0, "error": ""}
_precon_lock = threading.Lock()
_mtg_lock = threading.Lock()
_mtg_last = [0.0]


def _mtgjson_get(path, cache=False):
    """Fetch a MTGJSON gz file, throttled; returns parsed JSON."""
    with _mtg_lock:
        wait = 0.25 - (time.time() - _mtg_last[0])
        if wait > 0:
            time.sleep(wait)
        _mtg_last[0] = time.time()
    fname = path.split("/")[-1]
    cache_path = os.path.join(PRECON_DIR, fname) if cache else None
    if cache and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return _decompress_mtgjson(f.read())
    req = urllib.request.Request(MTGJSON_BASE + path, headers={
        "User-Agent": MTGJSON_UA})
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read()
    if cache:
        os.makedirs(PRECON_DIR, exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(raw)
    return _decompress_mtgjson(raw)


def _decompress_mtgjson(raw):
    if raw[:2] == b"\x1f\x8b":
        import gzip as _gzip
        return json.loads(_gzip.decompress(raw))
    return json.loads(raw)


def _extract_precon_deck(set_code, set_name, deck, set_cards, idents):
    """Build an index entry for one MTGJSON deck (exact printings)."""
    cards_by_uuid = {c["uuid"]: c for c in set_cards}
    cmd_uuids = [e["uuid"] for e in (deck.get("commander") or [])]
    out = []
    for e in deck.get("mainBoard", []):
        c = cards_by_uuid.get(e["uuid"]) or idents.get(e["uuid"])
        if not c:
            continue
        sid = (c.get("identifiers") or {}).get("scryfallId") or ""
        out.append({
            "name": c.get("name", ""),
            "quantity": e.get("count", 1),
            "role": "commander" if e["uuid"] in cmd_uuids else "main",
            "scryfall_id": sid,
            "set_code": (c.get("setCode") or set_code).lower(),
            "collector_number": str(c.get("number") or ""),
        })
    if not any(x["role"] == "commander" for x in out):
        for u in cmd_uuids:
            c = cards_by_uuid.get(u) or idents.get(u)
            if not c:
                continue
            out.append({
                "name": c.get("name", ""), "quantity": 1,
                "role": "commander",
                "scryfall_id": (c.get("identifiers") or {}).get("scryfallId") or "",
                "set_code": (c.get("setCode") or set_code).lower(),
                "collector_number": str(c.get("number") or ""),
            })
    if not out:
        return None
    commander = next((x["name"] for x in out if x["role"] == "commander"), "")
    return {
        "set_code": set_code, "set_name": set_name,
        "name": deck.get("name", ""), "commander": commander,
        "card_count": sum(x["quantity"] for x in out),
        "cards": out,
    }


def load_precon_index():
    global _precon_index
    with _precon_lock:
        if _precon_index is not None:
            return _precon_index
        try:
            with open(PRECON_INDEX_FILE) as f:
                _precon_index = json.load(f).get("decks", [])
        except Exception:
            _precon_index = []
        return _precon_index


def sync_precons():
    """Background: build/refresh the precon deck index."""
    def work():
        global _precon_index
        with _precon_lock:
            if _precon_state["active"]:
                return
            _precon_state.update(active=True, phase="list", done=0, total=0,
                                 decks=0, error="")
        srv = _server()
        try:
            os.makedirs(PRECON_DIR, exist_ok=True)
            sets = _mtgjson_get("/SetList.json.gz", cache=True).get("data", [])
            cand = [s for s in sets
                    if s.get("type") in PRECON_SET_TYPES and s.get("code")]
            cand.sort(key=lambda s: s["code"])
            total = len(cand)
            _precon_state["total"] = total
            idents = _mtgjson_get("/AllIdentifiers.json.gz", cache=True)
            idents = idents.get("data", {})
            decks = []
            for i, s in enumerate(cand, 1):
                code = s["code"].lower()
                _precon_state.update(done=i, phase="%s (%s)" % (
                    s.get("name", ""), code))
                srv.broadcast({"type": "precon-progress",
                               "done": i, "total": total,
                               "phase": _precon_state["phase"],
                               "decks": len(decks)})
                try:
                    d = _mtgjson_get("/%s.json.gz" % s["code"], cache=True)
                except Exception:
                    continue
                data = d.get("data") if isinstance(d, dict) else None
                if not data or "decks" not in data:
                    continue
                set_cards = data.get("cards", [])
                for deck in data.get("decks", []):
                    entry = _extract_precon_deck(
                        code, s.get("name", ""), deck, set_cards, idents)
                    if entry:
                        decks.append(entry)
            with _precon_lock:
                _precon_index = decks
                _precon_state["decks"] = len(decks)
            tmp = PRECON_INDEX_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"synced_at": _now_iso(), "decks": decks}, f)
            os.replace(tmp, PRECON_INDEX_FILE)
            srv.broadcast({"type": "precon-done", "decks": len(decks)})
        except Exception as e:
            _precon_state["error"] = str(e)
            srv.broadcast({"type": "precon-error", "error": str(e)})
        finally:
            with _precon_lock:
                _precon_state.update(active=False, phase="", done=0,
                                     total=0, decks=len(_precon_index or []))
    threading.Thread(target=work, daemon=True).start()


def api_precon_status(self):
    with _precon_lock:
        st = dict(_precon_state)
    decks = load_precon_index()
    st["available"] = bool(decks)
    st["decks"] = max(st.get("decks", 0), len(decks))
    self.send_json(st)


def api_precon_sync(self):
    with _precon_lock:
        if _precon_state["active"]:
            self.send_json({"error": "sync already running"}, 409)
            return
    sync_precons()
    self.send_json({"ok": True, "started": True})


def api_precon_search(self, q):
    q = _norm_name(q)
    if not q:
        self.send_json({"decks": []})
        return
    decks = load_precon_index()
    if not decks:
        self.send_json({"decks": [], "unavailable": True})
        return
    out = []
    for d in decks:
        hay = _norm_name(d["name"] + " " + d.get("set_name", "") + " " +
                        d.get("commander", ""))
        if q in hay:
            out.append({"set_code": d["set_code"], "set_name": d["set_name"],
                        "name": d["name"], "commander": d.get("commander", ""),
                        "card_count": d["card_count"]})
    out.sort(key=lambda x: x["name"].lower())
    self.send_json({"decks": out[:24]})


def api_precon_import(self):
    body = json.loads(self.read_body())
    set_code = (body.get("set_code") or "").strip().lower()
    deck_name = (body.get("deck_name") or "").strip()
    srv = _server()
    spec = None
    for d in load_precon_index():
        if d["set_code"] == set_code and d["name"] == deck_name:
            spec = d
            break
    if spec is None:
        self.send_json({"error": "precon deck not found in the local index — "
                                  "run a sync first."}, 404)
        return
    ts = _now_iso()
    with srv.db_lock, srv.db() as conn:
        cur = conn.execute(
            "INSERT INTO decks (name, format, created_at, updated_at) "
            "VALUES (?,?,?,?)", (spec["name"], "Commander", ts, ts))
        deck_id = cur.lastrowid
        skipped = []
        added = 0
        for c in spec["cards"]:
            s = None
            if c.get("scryfall_id"):
                s = srv.local_by_sid(c["scryfall_id"])
                if s is None and srv.P.has_api:
                    try:
                        raw = srv.P.get_card(c["scryfall_id"])
                        if raw:
                            s = srv.P.summary(raw)
                    except Exception:
                        s = None
            if s is None and c.get("set_code") and c.get("collector_number"):
                s = srv.local_exact_match(c["set_code"], c["collector_number"])
            if s is None and c.get("name"):
                s = srv.local_name_match(c["name"])
                if s is None and srv.P.has_api:
                    try:
                        s = srv.P.name_lookup(c["name"])
                    except Exception:
                        s = None
            if s is None:
                skipped.append(c.get("name") or c.get("scryfall_id"))
                continue
            _insert_deck_card(conn, deck_id, s, c.get("role") or "main",
                              c.get("quantity", 1), False)
            added += c.get("quantity", 1)
        conn.execute("UPDATE decks SET updated_at=? WHERE id=?",
                     (ts, deck_id))
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True, "id": deck_id, "name": spec["name"],
                    "added": added, "skipped": skipped})


# ------------------------------------------------- deck URL import
# Import a deck by URL from Archidekt (via the importer above), Moxfield
# (public JSON API) or TappedOut (plain-text decklist export). Fetches go
# through a shared throttled browser-UA helper so we stay polite to all
# three hosts.

_web_lock = threading.Lock()
_web_last = [0.0]


def _web_get(url, timeout=25):
    """GET a URL with a browser User-Agent, throttled; text or None."""
    with _web_lock:
        wait = 0.25 - (time.time() - _web_last[0])
        if wait > 0:
            time.sleep(wait)
        _web_last[0] = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": ARCHIDEKT_UA,
        "Accept": "text/html,application/json,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def _resolve_card(srv, name, set_code=None, number=None):
    """Resolve a card to a summary: exact set/number first, then the name
    ladder (local index, then the game API: exact, fuzzy name, search)."""
    s = None
    if set_code and number:
        s = srv.local_exact_match(set_code, number)
        if s is None and srv.P.has_api:
            try:
                s = srv.P.exact_lookup(set_code, number)
            except Exception:
                s = None
    if s is None:
        s = srv.local_name_match(name)
        if s is None and srv.P.has_api:
            try:
                s = srv.P.name_lookup(name)
            except Exception:
                s = None
    if s is None and srv.P.has_api:
        try:
            hits = srv.P.search(name, limit=3)
            if hits:
                s = hits[0]
        except Exception:
            s = None
    return s


MOXFIELD_FORMATS = ("Commander", "Standard", "Modern", "Pioneer", "Legacy",
                    "Vintage", "Pauper", "Brawl")


def _norm_format(fmt):
    """Normalize a Moxfield format string against the known list; else None."""
    low = (fmt or "").strip().lower()
    for f in MOXFIELD_FORMATS:
        if low == f.lower():
            return f
    return None


def _parse_decklist(text):
    """Plain-text decklist lines -> [(name, qty, role)]; mirrors the
    parseDecklist rules in static/decks.js (comments, Sideboard: sections,
    "SB: " prefixes, leading quantities)."""
    out = []
    role = "main"
    for raw in re.split(r"\r?\n", text):
        t = raw.strip()
        if not t or t.startswith("#"):
            continue
        if re.match(r"^sideboard\b", t, re.I):
            role = "sideboard"
            continue
        line_role = role
        m = re.match(r"^sb\s*:\s*", t, re.I)
        if m:
            line_role = "sideboard"
            t = t[m.end():].strip()
        qty = 1
        m = re.match(r"^(\d{1,3})\s*(?:x|×)?\s*(.+)$", t, re.I)
        if m:
            qty = max(1, int(m.group(1)))
            t = m.group(2).strip()
        if not t:
            continue
        out.append((t, qty, line_role))
    return out


def _finish_deck(self, name, fmt, resolved, skipped):
    """Create the deck row, insert resolved (summary, role, qty) cards,
    broadcast, and reply — shared by every URL importer."""
    if not resolved:
        # Never leave an empty deck behind: either the source deck really is
        # empty/private, or its response shape changed and nothing parsed.
        self.send_json({"error": "No cards could be imported from that deck — "
                                 "check it is public and not empty."
                                 + (" Unmatched: %s" % ", ".join(skipped[:5])
                                    if skipped else "")}, 502)
        return
    srv = _server()
    ts = _now_iso()
    with srv.db_lock, srv.db() as conn:
        cur = conn.execute(
            "INSERT INTO decks (name, format, created_at, updated_at) "
            "VALUES (?,?,?,?)", (name, fmt, ts, ts))
        deck_id = cur.lastrowid
        added = 0
        for s, role, qty in resolved:
            _insert_deck_card(conn, deck_id, s, role, qty, False)
            added += qty
    srv.broadcast({"type": "decks-changed"})
    self.send_json({"ok": True, "id": deck_id, "name": name,
                    "added": added, "skipped": skipped})


def _import_moxfield(self, mox_id):
    """Create a deck from a Moxfield deck id (public JSON API, v3 then v2)."""
    text = _web_get("https://api2.moxfield.com/v3/decks/all/%s" % mox_id)
    if not text:
        text = _web_get("https://api2.moxfield.com/v2/decks/all/%s" % mox_id)
    if not text:
        self.send_json({"error": "Could not reach Moxfield — check the URL and "
                                 "try again."}, 502)
        return
    try:
        data = json.loads(text)
    except ValueError:
        self.send_json({"error": "Moxfield returned an unreadable response — "
                                 "check the deck is public and try again."}, 502)
        return
    name = (data.get("name") or "").strip() or ("Moxfield deck " + mox_id)
    fmt = _norm_format(data.get("format"))
    srv = _server()
    resolved, skipped = [], []
    for board, role in (("commanders", "commander"),
                        ("mainboard", "main"),
                        ("sideboard", "sideboard")):
        # v3 nests each board under "boards" as {count, cards}; v2 puts the
        # card map at the top level.
        entries = ((data.get("boards") or {}).get(board) or {}).get("cards")
        if not isinstance(entries, dict):
            entries = data.get(board)
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            try:
                qty = max(1, int(entry.get("quantity") or 1))
            except (TypeError, ValueError):
                qty = 1
            card = entry.get("card") or {}
            cname = (card.get("name") or "").strip()
            if not cname:
                continue
            set_code = str(card.get("set") or "").strip()
            # collector number: "cn" in v3, "number" in v2
            number = str(card.get("cn") or card.get("number") or "").strip()
            s = _resolve_card(srv, cname, set_code, number)
            if s is None:
                skipped.append(cname)
                continue
            resolved.append((s, role, qty))
    _finish_deck(self, name, fmt, resolved, skipped)


def _import_tappedout(self, slug):
    """Create a deck from a TappedOut slug (plain-text decklist export)."""
    text = _web_get("https://tappedout.net/mtg-decks/%s/?fmt=txt" % slug)
    if not text:
        self.send_json({"error": "Could not reach TappedOut — check the URL and "
                                 "try again."}, 502)
        return
    if re.search(r"<!DOCTYPE|Just a moment|Attention", text, re.I):
        self.send_json({"error": "TappedOut is currently unavailable (their bot "
                                 "protection blocked the request) — try the "
                                 "decklist paste instead."}, 502)
        return
    srv = _server()
    resolved, skipped = [], []
    for cname, qty, role in _parse_decklist(text):
        s = _resolve_card(srv, cname)
        if s is None:
            skipped.append(cname)
            continue
        resolved.append((s, role, qty))
    _finish_deck(self, "TappedOut deck %s" % slug, None, resolved, skipped)


def api_deck_import_url(self):
    """Create a deck from an Archidekt, Moxfield, or TappedOut deck URL."""
    body = json.loads(self.read_body())
    url = (body.get("url") or "").strip()
    m = re.search(r"archidekt\.com/decks/(\d+)", url)
    if not m:
        m = re.fullmatch(r"(\d{3,})", url)
    if m:
        _import_archidekt_id(self, m.group(1))
        return
    m = re.search(r"moxfield\.com/decks/([A-Za-z0-9_-]+)", url)
    if m:
        _import_moxfield(self, m.group(1))
        return
    m = re.search(r"tappedout\.net/mtg-decks/([^/?#]+)", url)
    if m:
        _import_tappedout(self, m.group(1))
        return
    self.send_json({"error": "Paste a deck URL — archidekt.com/decks/12345, "
                             "moxfield.com/decks/abc123, or "
                             "tappedout.net/mtg-decks/<deck>/"}, 400)


# ---------------------------------------------------------------- edhrec
# Commander "top cards" recommendations from EDHREC's public JSON endpoint
# (json.edhrec.com, unofficial but open). Fetches are throttled through
# _web_get and cached in memory for an hour so repeat lookups are instant.

EDHREC_TTL = 3600
_edhrec_cache = {}
_edhrec_lock = threading.Lock()


def _edhrec_slug(name):
    """'Uril, the Miststalker' -> 'uril-the-miststalker'."""
    s = re.sub(r"[^a-z0-9 -]", "", (name or "").lower())
    return re.sub(r" +", "-", s.strip())


def api_edhrec(self, name):
    """Top cards for a commander, from EDHREC's JSON endpoint (cached 1h)."""
    slug = _edhrec_slug(name)
    if not slug:
        self.send_json({"cards": [], "unavailable": True})
        return
    with _edhrec_lock:
        hit = _edhrec_cache.get(slug)
        if hit and time.time() - hit[0] < EDHREC_TTL:
            self.send_json(hit[1])
            return
    try:
        text = _web_get("https://json.edhrec.com/pages/commanders/%s.json" % slug)
        if not text:
            raise ValueError("empty response")
        data = json.loads(text)
        groups = (data.get("container") or {}).get("json_dict") or {}
        groups = groups.get("cardlists") or []
    except Exception as e:
        self.send_json({"error": "EDHREC is currently unavailable — %s" % e})
        return
    picked, saw_top = {}, False
    for tag in ("topcards", "highsynergycards"):
        for g in groups:
            if not isinstance(g, dict) or g.get("tag") != tag:
                continue
            if tag == "topcards":
                saw_top = True
            for cv in (g.get("cardviews") or []):
                cvname = (cv.get("name") or "").strip()
                if not cvname or cvname in picked:
                    continue
                picked[cvname] = cv
                if len(picked) >= 24:
                    break
        if len(picked) >= 24:
            break
    if not saw_top:
        self.send_json({"cards": [], "unavailable": True})
        return
    srv = _server()
    cards = []
    for cvname, cv in picked.items():
        s = _resolve_card(srv, cvname)
        if s is None:
            continue
        s = dict(s)
        s["synergy"] = cv.get("synergy")
        s["num_decks"] = cv.get("num_decks")
        cards.append(s)
    resp = {"commander": name, "cards": cards}
    with _edhrec_lock:
        _edhrec_cache[slug] = (time.time(), resp)
    self.send_json(resp)


# ---------------------------------------------------------------- routing

def _serve_static(handler, relpath, ctype):
    path = os.path.join(ROOT, "static", relpath)
    if not os.path.isfile(path):
        handler.send_json({"error": "not found"}, 404)
        return
    with open(path, "rb") as f:
        body = f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_get(handler, path, query):
    if path == "/decks.html":
        _serve_static(handler, "decks.html", "text/html; charset=utf-8")
        return True
    if path == "/decks.js":
        _serve_static(handler, "decks.js", "application/javascript")
        return True
    if path == "/decks.css":
        _serve_static(handler, "decks.css", "text/css")
        return True
    if path == "/api/decks":
        api_decks(handler)
        return True
    if path == "/api/decks/planner":
        api_decks_planner(handler)
        return True
    if path == "/api/decks/precon/status":
        api_precon_status(handler)
        return True
    if path == "/api/decks/precon/search":
        api_precon_search(handler, query.get("q", ""))
        return True
    if path == "/api/decks/archidekt/search":
        api_archidekt_search(handler, query.get("q", ""))
        return True
    if path == "/api/decks/edhrec":
        api_edhrec(handler, query.get("name", ""))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)", path)
    if m:
        api_deck_detail(handler, int(m.group(1)))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/missing", path)
    if m:
        api_deck_missing(handler, int(m.group(1)),
                         cheapest=query.get("cheapest", "") in ("1", "true"))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/legality", path)
    if m:
        api_deck_legality(handler, int(m.group(1)), query.get("format", ""))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/value", path)
    if m:
        api_deck_value(handler, int(m.group(1)))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/matches", path)
    if m:
        api_deck_matches(handler, int(m.group(1)))
        return True
    return False


def handle_post(handler, path):
    if path == "/api/decks":
        api_decks_create(handler)
        return True
    if path == "/api/decks/resolve":
        api_decks_resolve(handler)
        return True
    if path == "/api/decks/precon/sync":
        api_precon_sync(handler)
        return True
    if path == "/api/decks/precon/import":
        api_precon_import(handler)
        return True
    if path == "/api/decks/archidekt/import":
        api_archidekt_import(handler)
        return True
    if path == "/api/decks/import-url":
        api_deck_import_url(handler)
        return True
    if path == "/api/collection/import-decklist":
        api_collection_import_decklist(handler)
        return True
    if path == "/api/decks/matches/delete":
        api_deck_match_delete(handler)
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/matches", path)
    if m:
        api_deck_match_add(handler, int(m.group(1)))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/update", path)
    if m:
        api_deck_update(handler, int(m.group(1)))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/delete", path)
    if m:
        api_deck_delete(handler, int(m.group(1)))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/duplicate", path)
    if m:
        api_deck_duplicate(handler, int(m.group(1)))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/cards", path)
    if m:
        api_deck_add_card(handler, int(m.group(1)))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/cards/remove", path)
    if m:
        api_deck_remove_card(handler, int(m.group(1)))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/value/record", path)
    if m:
        api_deck_value_record(handler, int(m.group(1)))
        return True
    m = re.fullmatch(r"/api/decks/(\d+)/add-to-collection", path)
    if m:
        api_deck_add_to_collection(handler, int(m.group(1)))
        return True
    return False

