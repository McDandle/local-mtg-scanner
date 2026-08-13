"""Optional needle2 assistant — natural-language commands over your library.

needle2 is a 45M-param tool-caller (text in, one JSON call out). We give it a
single grammar-constrained `lookup` tool so it can't pick the wrong function,
then resolve the extracted name against the real decks/cards tables. A small
keyword router is only the fallback when needle refuses or the name is empty.

Optional at runtime: if `cactus-needle` isn't installed the /api/chat route
returns 501 and the bubble stays hidden.
"""

import json
import re
import threading
from typing import Literal

try:
    import needle  # pip install cactus-needle
except ImportError:
    needle = None

_lock = threading.Lock()
_agent = None


def available():
    return needle is not None


def _srv():
    import server
    return server


def _db():
    import deckbuilder
    return deckbuilder


class _Sink:
    payload = None

    def send_json(self, obj, status=200):
        self.payload = obj


# ------------------------------------------------------------- entities
def _deck_rows():
    with _srv().db_lock, _srv().db() as conn:
        return [(r["id"], r["name"]) for r in conn.execute(
            "SELECT id, name FROM decks")]


def _match_named(text, names):
    """Longest full-name or distinctive-word hit in text."""
    low = (text or "").lower()
    if not low:
        return None
    for n in names:
        if n.lower() in low:
            return n
    best = None
    for n in names:
        for w in re.findall(r"[a-z0-9']+", n.lower()):
            if len(w) >= 4 and w in low and (best is None or len(w) > best[0]):
                best = (len(w), n)
    return best[1] if best else None


def _deck_entity(text):
    decks = _deck_rows()
    hit = _match_named(text, [n for _, n in decks])
    if not hit:
        return None, None
    for did, dname in decks:
        if dname == hit:
            return did, dname
    return None, None


def _card_entity(text):
    # Full-name only: word-match would map "Lightning Bolt" → "Lightning Strike".
    low = (text or "").lower()
    if not low:
        return None
    with _srv().db_lock, _srv().db() as conn:
        names = [r["name"] for r in conn.execute(
            "SELECT DISTINCT name FROM cards")]
    best = None
    for n in names:
        if n.lower() in low and (best is None or len(n) > len(best)):
            best = n
    return best


def _commander_entity(text):
    with _srv().db_lock, _srv().db() as conn:
        cmds = [r["name"] for r in conn.execute(
            "SELECT DISTINCT name FROM deck_cards WHERE role='commander'")]
    return _match_named(text, cmds)


def _owned_name(text):
    name = _card_entity(text)
    if name:
        return name
    t = re.sub(r"^(do|does)\s+(i|you)\s+(own|have)\s+(any\s+)?", "",
               text or "", flags=re.I)
    t = re.sub(r"^(have|how many)\s+(i\s+have|you\s+have|any|copies of)?\s*",
               "", t, flags=re.I)
    t = t.strip(" ?!.,\"'\n")
    return t or None


def _set_rows():
    with _srv().db_lock, _srv().db() as conn:
        return [(r["set_code"] or "", r["set_name"] or "") for r in conn.execute(
            "SELECT DISTINCT set_code, set_name FROM cards")]


def _set_entity(text):
    """(set_code, set_name) mentioned in text, else (None, None)."""
    low = (text or "").lower()
    if not low:
        return None, None
    sets = _set_rows()
    for code, name in sets:
        if code and re.search(r"\b%s\b" % re.escape(code.lower()), low):
            return code, name
    best = None
    for code, name in sets:
        if name and name.lower() in low and (best is None or len(name) > best[0]):
            best = (len(name), code, name)
    return (best[1], best[2]) if best else (None, None)


def _fmt_from(text):
    m = re.search(
        r"\bin (commander|modern|standard|pauper|vintage|legacy|brawl|pioneer)\b",
        (text or "").lower())
    return m.group(1) if m else "commander"


# ------------------------------------------------------------- operations
def _op_list_decks():
    sink = _Sink()
    _db().api_decks(sink)
    decks = (sink.payload or {}).get("decks", [])
    return {"decks": [{"name": d["name"], "format": d.get("format"),
                       "cards": d.get("card_count"), "owned": d.get("owned"),
                       "missing": d.get("missing")} for d in decks]}


def _op_status(deck_id, deck_name):
    sink = _Sink()
    _db().api_deck_missing(sink, deck_id)
    p = sink.payload or {}
    stats = p.get("stats", {})
    items = p.get("items", [])
    return {"deck": deck_name, "total": stats.get("total"),
            "owned": stats.get("owned"), "missing": stats.get("missing"),
            "missing_value": stats.get("missing_value"),
            "buy": [{"name": i["name"], "qty": i["qty"]} for i in items[:10]]}


def _op_legality(deck_id, deck_name, fmt):
    sink = _Sink()
    _db().api_deck_legality(sink, deck_id, fmt)
    p = sink.payload or {}
    return {"deck": deck_name, "format": p.get("format"), "ok": p.get("ok"),
            "checked": p.get("checked"),
            "issues": [i.get("issue") for i in (p.get("issues") or [])[:8]]}


def _op_owned(card_name):
    key = _db()._norm_name(card_name)
    with _srv().db_lock, _srv().db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT name, set_code, set_name, collector_number, quantity, "
            "foil, price_usd, price_usd_foil FROM cards")]
    matches = [r for r in rows if key in _db()._norm_name(r["name"])]
    if not matches:
        return {"owned": False, "card": card_name}
    return {"owned": True, "card": card_name,
            "total_copies": sum(r["quantity"] for r in matches),
            "copies": [{"name": r["name"], "qty": r["quantity"],
                        "foil": bool(r["foil"]),
                        "set": (r["set_code"] or "").upper(),
                        "set_name": r["set_name"] or "",
                        "num": r["collector_number"] or "",
                        "price": round((r["price_usd_foil"] if r["foil"]
                                        else r["price_usd"]) or 0, 2)}
                       for r in matches[:8]]}


def _op_set(set_code, set_name):
    with _srv().db_lock, _srv().db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT name, set_code, collector_number, quantity, foil, "
            "price_usd, price_usd_foil FROM cards WHERE lower(set_code)=? "
            "ORDER BY name COLLATE NOCASE", (set_code.lower(),))]
    items = [{"name": r["name"], "qty": r["quantity"],
              "set": (r["set_code"] or "").upper(),
              "num": r["collector_number"] or "",
              "foil": bool(r["foil"]),
              "price": round((r["price_usd_foil"] if r["foil"]
                              else r["price_usd"]) or 0, 2)}
             for r in rows]
    lines = ["%s %s (%s) #%s" % (i["qty"], i["name"], i["set"], i["num"])
             for i in items]
    return {"inset": True, "set": set_code.upper(), "set_name": set_name,
            "count": sum(i["qty"] for i in items), "items": items[:20],
            "list": "\n".join(lines)}


def _tcg_name(name):
    # Mass Entry matches on name. Set/number dumps empty carts for UB
    # printings (TMC/TMT), and "Face // Face" splits need the front face.
    n = (name or "").split(" // ", 1)[0].strip()
    return n


def _tcg_list(items):
    return "\n".join("%s %s" % (i["qty"], _tcg_name(i["name"])) for i in items)


def _op_cart(deck_id, deck_name):
    sink = _Sink()
    _db().api_deck_missing(sink, deck_id, cheapest=True)
    p = sink.payload or {}
    items = p.get("items") or []
    return {"cart": True, "deck": deck_name,
            "total": p.get("total") or 0,
            "count": len(items),
            "items": [{"name": i["name"], "qty": i["qty"],
                        "set": i.get("set_code"),
                        "price": i.get("total")}
                       for i in items[:20]],
            "list": _tcg_list(items)}


def _op_planner():
    sink = _Sink()
    _db().api_decks_planner(sink)
    p = sink.payload or {}
    cards = [c for c in (p.get("cards") or []) if c.get("deficit")][:12]
    return {"planner": True,
            "deck_count": p.get("deck_count"),
            "total_deficit": p.get("total_deficit"),
            "short": [{"name": c["name"], "need": c["deficit"],
                        "owned": c["owned"],
                        "decks": [d["deck"] for d in (c.get("deck_list") or [])]}
                       for c in cards]}


def _op_wishlist():
    with _srv().db_lock, _srv().db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT name, quantity, target_price, price_usd, price_usd_foil "
            "FROM wishlist ORDER BY added_at DESC")]
    items, alerts = [], 0
    for r in rows:
        price = r["price_usd_foil"] if r.get("foil") else r["price_usd"]
        hit = (r.get("target_price") is not None and price is not None
               and price <= r["target_price"])
        if hit:
            alerts += 1
        items.append({"name": r["name"], "qty": r["quantity"],
                      "price": price, "target": r.get("target_price"),
                      "alert": hit})
    return {"wishlist": True, "alerts": alerts, "items": items[:20]}


def _op_recs(commander):
    sink = _Sink()
    _db().api_edhrec(sink, commander)
    p = sink.payload or {}
    if p.get("unavailable") or p.get("error"):
        return {"error": p.get("error") or "recommendations unavailable"}
    cards = p.get("cards", [])
    return {"commander": commander,
            "cards": [{"name": c["name"], "synergy": c.get("synergy")}
                      for c in cards[:10]]}


def _op_export_deck(deck_id, deck_name):
    with _srv().db_lock, _srv().db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT name, quantity, role FROM deck_cards WHERE deck_id=? "
            "ORDER BY role, name COLLATE NOCASE", (deck_id,))]
    lines = []
    for role in ("commander", "main"):
        for r in rows:
            if r["role"] == role:
                lines.append("%s %s" % (r["quantity"], r["name"]))
    sb = [r for r in rows if r["role"] == "sideboard"]
    if sb:
        lines += ["", "Sideboard:"]
        lines += ["%s %s" % (r["quantity"], r["name"]) for r in sb]
    return {"export": True, "title": deck_name, "kind": "deck",
            "count": sum(r["quantity"] for r in rows),
            "list": "\n".join(lines)}


def _op_export_collection(set_code=None, set_name=None):
    with _srv().db_lock, _srv().db() as conn:
        if set_code:
            rows = [dict(r) for r in conn.execute(
                "SELECT name, set_code, collector_number, quantity FROM cards "
                "WHERE lower(set_code)=? ORDER BY name COLLATE NOCASE",
                (set_code.lower(),))]
            lines = ["%s %s (%s) #%s" % (
                r["quantity"], r["name"],
                (r["set_code"] or "").upper(), r["collector_number"] or "")
                     for r in rows]
            title = "%s (%s)" % (set_name or set_code, set_code.upper())
            count = sum(r["quantity"] for r in rows)
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT name, SUM(quantity) AS q FROM cards GROUP BY name "
                "ORDER BY name COLLATE NOCASE")]
            lines = ["%s %s" % (r["q"], r["name"]) for r in rows]
            title, count = "Collection", sum(r["q"] for r in rows)
    return {"export": True, "title": title, "kind": "set" if set_code else "collection",
            "count": count, "list": "\n".join(lines)}


def _dispatch(intent, name, query):
    """Turn a (intent, name) pair into an op result. name may be messy —
    we re-resolve against the original query and the known tables."""
    blob = " ".join(x for x in (name, query) if x)
    if intent == "decks":
        return _op_list_decks()
    if intent == "owned":
        card = _owned_name(blob) or _owned_name(query) or (name or "").strip()
        if not card:
            return None
        return _op_owned(card)
    if intent == "set":
        code, sname = _set_entity(blob)
        if code is None:
            code, sname = _set_entity(query)
        if code is None:
            return None
        return _op_set(code, sname)
    if intent in ("status", "legal", "cart"):
        did, dname = _deck_entity(blob)
        if did is None:
            did, dname = _deck_entity(query)
        if did is None:
            return None
        if intent == "status":
            return _op_status(did, dname)
        if intent == "cart":
            return _op_cart(did, dname)
        return _op_legality(did, dname, _fmt_from(query))
    if intent == "planner":
        return _op_planner()
    if intent == "wishlist":
        return _op_wishlist()
    if intent == "export":
        code, sname = _set_entity(blob)
        if code is None:
            code, sname = _set_entity(query)
        if code:
            return _op_export_collection(code, sname)
        if re.search(r"collection|library|all cards", query, re.I):
            return _op_export_collection()
        did, dname = _deck_entity(blob)
        if did is None:
            did, dname = _deck_entity(query)
        if did is None:
            return _op_export_collection()
        return _op_export_deck(did, dname)
    if intent == "recs":
        cmd = _commander_entity(blob) or _commander_entity(query) or (name or "").strip()
        if not cmd:
            return None
        return _op_recs(cmd)
    return None


# ------------------------------------------------------- keyword fallback
_REC_RE = re.compile(
    r"(?:build around|around|recommend(?: cards)? for|suggest(?: cards)? for|"
    r"cards (?:for|go well with|to pair with))\s+(.+?)\s*(?:deck)?[?.!]*$",
    re.I)


_CART_RE = re.compile(
    r"\bcart\b|tcgplayer|mass entry|check\s*out|order list|buy list", re.I)
_PLAN_RE = re.compile(
    r"planner|across (my |all )?decks|shortages?|resource plan|short on",
    re.I)
_WISH_RE = re.compile(r"wish\s*list|want list|price alert", re.I)
_EXPORT_RE = re.compile(
    r"\bexport\b|\bdecklist\b|copy (the )?(deck|list|collection)|paste.?list",
    re.I)
_SET_RE = re.compile(
    r"\bfrom\b|\bin set\b|cards? (from|in)|what do i have (from|in)|which set",
    re.I)


def _route(text):
    t = text.lower()
    if _EXPORT_RE.search(t):
        return _dispatch("export", "", text)
    if _SET_RE.search(t) and _set_entity(text)[0]:
        return _dispatch("set", "", text)
    if _CART_RE.search(t):
        return _dispatch("cart", "", text)
    if _PLAN_RE.search(t):
        return _dispatch("planner", "", text)
    if _WISH_RE.search(t):
        return _dispatch("wishlist", "", text)
    if re.search(r"legal|banned|legality|\bformat\b", t):
        return _dispatch("legal", "", text)
    if re.search(r"missing|still need|need to buy|what do i (still )?need", t):
        return _dispatch("status", "", text)
    m = _REC_RE.search(text)
    if m and m.group(1).strip():
        return _dispatch("recs", m.group(1).strip(), text)
    if re.search(r"\bdecks?\b|\blist\b", t) and not re.search(
            r"\bdeck\b.*\b(legal|missing|need|buy)\b", t):
        return _dispatch("decks", "", text)
    if re.search(r"\bown\b|do i have|have any|how many", t):
        return _dispatch("owned", "", text)
    return None


# ------------------------------------------------------------ needle
def _make_agent():
    @needle.tool
    def lookup(intent: Literal["owned", "status", "legal", "recs", "decks",
                               "cart", "planner", "wishlist", "export", "set"],
               name: str = ""):
        """Look up the user's Magic collection or decks.

        Args:
            intent: owned = do I own a card and which set each copy is from; status = owned vs missing for one deck; cart = cheapest buy list and TCGplayer cart for one deck; legal = is a deck legal; recs = suggest cards for a commander; decks = list every deck; planner = cards short across all decks; wishlist = cards on the wishlist; export = pasteable decklist, collection, or one set; set = list cards you own from a set
            name: the card, deck, commander, or set code/name mentioned; empty for decks, planner, wishlist, or a full collection export
        """
        return {"intent": intent, "name": name}

    return needle.Needle(tools=[lookup])


def _get_agent():
    global _agent
    if _agent is None:
        _agent = _make_agent()
    return _agent


def _needle_call(text):
    """One complete() — we execute the call ourselves so fat tool results
    never re-enter needle's 256-token window."""
    if not available():
        return None, None
    with _lock:
        try:
            agent = _get_agent()
            agent.reset()
            resp = agent.complete(text)
        except Exception as e:
            return None, {"error": "assistant failed: %s" % e}
    calls = resp.get("function_calls") or []
    if not calls or not calls[0].get("name"):
        return resp, None
    args = calls[0].get("arguments") or {}
    result = _dispatch(args.get("intent"), args.get("name") or "", text)
    return resp, result


# ---------------------------------------------------------------- answers
def _format(results):
    if not results:
        return ('I couldn\u2019t match that to anything I can do. Try: '
                '"what decks do I have?", "what am I still missing for my '
                '<deck>?", "is my <deck> legal?", or "do I own <card>?". '
                'For commander recommendations, try "what should I build '
                'around <commander>?".')
    parts = []
    for r in results:
        if not isinstance(r, dict):
            parts.append(str(r))
            continue
        if r.get("error"):
            parts.append(str(r["error"]))
        elif "decks" in r:
            ds = r["decks"]
            if not ds:
                parts.append("You don't have any decks yet.")
            else:
                parts.append("Your decks:\n" + "\n".join(
                    "%s (%s) \u2014 %s cards, %s owned, %s missing" % (
                        d["name"], d.get("format") or "no format",
                        d["cards"], d["owned"], d["missing"]) for d in ds))
        elif r.get("cart"):
            if not r.get("count"):
                parts.append("%s: nothing to buy — you own the whole deck."
                             % r.get("deck"))
            else:
                lines = ["%s buy list (%s cards, ~$%s, cheapest printings):"
                         % (r.get("deck"), r.get("count"), r.get("total"))]
                for i in r.get("items") or []:
                    lines.append("  %s ×%s  $%s" % (
                        i["name"], i["qty"], i.get("price") or 0))
                lines.append("Copy the list, then paste it into TCGplayer Mass Entry.")
                parts.append("\n".join(lines))
        elif r.get("planner"):
            short = r.get("short") or []
            if not short:
                parts.append("No shortages across your %s decks."
                             % r.get("deck_count"))
            else:
                lines = ["Short across %s decks (need %s more copies):"
                         % (r.get("deck_count"), r.get("total_deficit"))]
                for c in short:
                    lines.append("  %s — own %s, need %s more (%s)" % (
                        c["name"], c["owned"], c["need"],
                        ", ".join(c.get("decks") or [])))
                parts.append("\n".join(lines))
        elif r.get("inset"):
            items = r.get("items") or []
            if not items:
                parts.append("Nothing from %s in your collection." % (
                    r.get("set_name") or r.get("set")))
            else:
                more = r.get("count", 0) - sum(i["qty"] for i in items)
                lines = ["%s (%s) \u2014 %s cards:" % (
                    r.get("set_name") or r.get("set"), r.get("set"),
                    r.get("count"))]
                for i in items:
                    lines.append("  %s \u00d7%s  %s #%s  $%s" % (
                        i["name"], i["qty"], i.get("set"), i.get("num"),
                        i.get("price") or 0))
                if more > 0:
                    lines.append("  \u2026and %s more. Copy the list for all." % more)
                parts.append("\n".join(lines))
        elif r.get("export"):
            n = r.get("count") or 0
            preview = (r.get("list") or "").splitlines()[:12]
            extra = "\n  …" if n and len((r.get("list") or "").splitlines()) > 12 else ""
            parts.append("%s — %s cards. Copy the list to paste elsewhere.\n%s%s"
                         % (r.get("title"), n, "\n".join(preview), extra))
        elif r.get("wishlist"):
            items = r.get("items") or []
            if not items:
                parts.append("Wishlist is empty.")
            else:
                lines = ["Wishlist (%s alerts):" % r.get("alerts")]
                for i in items:
                    mark = " ⚡ under target" if i.get("alert") else ""
                    lines.append("  %s ×%s  $%s%s" % (
                        i["name"], i["qty"], i.get("price") or "—", mark))
                parts.append("\n".join(lines))
        elif "buy" in r:
            if r.get("missing"):
                lines = ["%s: %s cards, %s owned, %s missing (~$%s to complete)"
                         % (r.get("deck"), r.get("total"), r.get("owned"),
                            r.get("missing"), r.get("missing_value"))]
                if r.get("buy"):
                    lines.append("Still need:\n" + "\n".join(
                        "  %s \u00d7%s" % (b["name"], b["qty"]) for b in r["buy"]))
                parts.append("\n".join(lines))
            else:
                parts.append("%s: complete \u2014 you own all %s cards."
                             % (r.get("deck"), r.get("total")))
        elif "issues" in r:
            if r.get("ok"):
                parts.append("%s is legal in %s (%s cards checked)."
                             % (r.get("deck"), r.get("format"), r.get("checked")))
            else:
                iss = r.get("issues") or ["has problems"]
                parts.append("%s is not legal in %s:\n" % (
                    r.get("deck"), r.get("format")) + "\n".join(
                        "  \u2022 %s" % i for i in iss))
        elif "commander" in r and "cards" in r:
            cs = r["cards"]
            if not cs:
                parts.append("No recommendations found for %s." % r.get("commander"))
            else:
                parts.append("Top cards for %s:\n" % r.get("commander") + "\n".join(
                    "  %s (%s%% synergy)" % (
                        c["name"], round((c.get("synergy") or 0) * 100))
                    for c in cs))
        elif "owned" in r:
            if not r.get("owned"):
                parts.append("You don't own %r." % r.get("card"))
            else:
                extra = ""
                copies = r.get("copies") or []
                if copies:
                    extra = "\n" + "\n".join(
                        "  %s \u00d7%s%s  %s #%s  $%s" % (
                            c["name"], c["qty"],
                            " foil" if c.get("foil") else "",
                            c.get("set") or "?",
                            c.get("num") or "?",
                            c.get("price") or 0)
                        for c in copies)
                parts.append("You own %s: %s copies.%s"
                             % (r.get("card"), r.get("total_copies"), extra))
        else:
            parts.append(json.dumps(r, ensure_ascii=False)[:400])
    return "\n\n".join(parts)


def ask(text):
    """One turn. Distinctive intents (cart/planner/wishlist) win first;
    needle classifies the rest; keyword router is last resort."""
    t = text.lower()
    if (_CART_RE.search(t) or _PLAN_RE.search(t) or _WISH_RE.search(t)
            or _EXPORT_RE.search(t) or _SET_RE.search(t)):
        result = _route(text)
        if result is not None:
            return {"answer": _format([result]), "reasoning": "",
                    "confidence": 1.0, "source": "fallback",
                    "calls": [result]}
    source = "needle"
    resp, result = _needle_call(text)
    if result is None:
        result = _route(text)
        source = "fallback" if result is not None else "none"
        if result is None and resp and resp.get("error"):
            return {"error": resp["error"], "source": source}
    conf = (resp or {}).get("confidence") if source == "needle" else 1.0
    reasoning = (resp or {}).get("reasoning") or ""
    return {
        "answer": _format([result] if result else []),
        "reasoning": reasoning,
        "confidence": round(conf, 2) if isinstance(conf, (int, float)) else None,
        "source": source,
        "calls": [result] if result else [],
    }
