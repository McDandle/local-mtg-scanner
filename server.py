#!/usr/bin/env python3
"""Local card collection tracker — MTG, Pokémon TCG, and Yu-Gi-Oh!.

Zero-dependency server (Python stdlib only). Serves a web UI on the LAN so a
phone can act as the scanner while the library stays in SQLite on this Mac.
All supported games run simultaneously from a single server process — switch
between them in the UI; no restart needed.

Ports (all optional, defaults shown):
    CARD_TRACKER_PORT=8484                      (plain HTTP)
    CARD_TRACKER_TLS_PORT=8485                  (HTTPS for live camera)
    POKEMON_TCG_API_KEY=...                     (optional — raises the
                                                 pokemontcg.io price limit)

  python3 server.py

Extra features (all optional, stdlib-only):
  • Offline card database — each game provider downloads its own card index
    (Scryfall bulk JSONL for MTG, the Pokémon TCG GitHub bulk data for
    Pokémon — no API rate limit — and the YGOPRODeck bulk endpoint for
    Yu-Gi-Oh!) and matches scans/search locally.
  • Local image cache — card images are proxied through /api/img and cached,
    so the library renders offline after first view.
  • Price refreshes run in the background and stream progress over SSE.
  • CSV export/import.
  • Automatic DB backups into backups/ (kept before refresh/import/startup).
"""

import csv
import gzip
import hashlib
import io
import json
import os
import queue
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

try:
    import segno  # QR code for phone pairing (optional: pip install segno)
except ImportError:
    segno = None

try:
    import deckbuilder  # optional Deck Builder module (drop-in removable)
except ImportError:
    deckbuilder = None

try:
    import assistant  # optional needle2 chat assistant (pip install cactus-needle)
except ImportError:
    assistant = None
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "collection.db")
OCR_BIN = os.path.join(ROOT, "ocr")
STATIC_DIR = os.path.join(ROOT, "static")

GAME = os.environ.get("CARD_TRACKER_GAME", "mtg").strip().lower()
PORT = int(os.environ.get("CARD_TRACKER_PORT", "8484"))
TLS_PORT = int(os.environ.get("CARD_TRACKER_TLS_PORT", "8485"))
CERT_FILE = os.path.join(ROOT, "cert.pem")
KEY_FILE = os.path.join(ROOT, "key.pem")

# Request size caps: 12MB for photos/CSVs, 1MB for JSON.
MAX_UPLOAD = 12 * 1024 * 1024
MAX_JSON_BODY = 1024 * 1024

# Backups: snapshots of collection.db kept in backups/.
BACKUP_DIR = os.path.join(ROOT, "backups")
BACKUP_KEEP = 14

# Offline card database + image cache.
IMG_CACHE_DIR = os.path.join(ROOT, "img_cache")
LOCAL_CARDS_FILE = os.path.join(ROOT, "local_cards.json")  # trimmed index
LOCAL_META_FILE = os.path.join(ROOT, "localdb_meta.json")

db_lock = threading.Lock()

# ------------------------------------------------------- live event stream
# Phones and the Mac all connect to /api/events (SSE). Scans and adds made
# on any device are broadcast so the Mac library updates in real time.
_subscribers = []
_subscribers_lock = threading.Lock()
_last_scan = [None, 0.0]  # card id, timestamp — dedupe for live scanning
_last_scan_lock = threading.Lock()


def broadcast(event):
    data = json.dumps(event)
    with _subscribers_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


# ---------------------------------------------------------------- database

@contextmanager
def db():
    """Connection that commits on success and always closes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY,
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
                scryfall_uri TEXT,
                foil INTEGER NOT NULL DEFAULT 0,
                quantity INTEGER NOT NULL DEFAULT 1,
                price_usd REAL,
                price_usd_foil REAL,
                price_updated_at TEXT,
                added_at TEXT NOT NULL,
                UNIQUE (scryfall_id, foil)
            );
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY,
                scryfall_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                usd REAL,
                usd_foil REAL
            );
            CREATE INDEX IF NOT EXISTS idx_history_card
                ON price_history (scryfall_id, recorded_at);
            CREATE TABLE IF NOT EXISTS wishlist (
                id INTEGER PRIMARY KEY,
                scryfall_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                set_code TEXT,
                set_name TEXT,
                collector_number TEXT,
                rarity TEXT,
                image_uri TEXT,
                price_usd REAL,
                price_usd_foil REAL,
                target_price REAL,
                quantity INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # migration: 3D card flip shows the back face of double-faced cards
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cards)")]
        if "game" not in cols:
            conn.execute(
                "ALTER TABLE cards ADD COLUMN game TEXT NOT NULL DEFAULT 'mtg'")
        if "back_image_uri" not in cols:
            conn.execute("ALTER TABLE cards ADD COLUMN back_image_uri TEXT")
        if "oracle_text" not in cols:
            conn.execute("ALTER TABLE cards ADD COLUMN oracle_text TEXT")
        if "condition" not in cols:
            conn.execute(
                "ALTER TABLE cards ADD COLUMN condition TEXT NOT NULL DEFAULT 'NM'")
        if "purchase_price" not in cols:
            conn.execute("ALTER TABLE cards ADD COLUMN purchase_price REAL")
        if "for_trade" not in cols:
            conn.execute(
                "ALTER TABLE cards ADD COLUMN for_trade INTEGER NOT NULL DEFAULT 0")
        if "for_sale" not in cols:
            conn.execute(
                "ALTER TABLE cards ADD COLUMN for_sale INTEGER NOT NULL DEFAULT 0")
        wcols = [r[1] for r in conn.execute("PRAGMA table_info(wishlist)")]
        if "game" not in wcols:
            conn.execute(
                "ALTER TABLE wishlist ADD COLUMN game TEXT NOT NULL DEFAULT 'mtg'")
        if deckbuilder is not None:
            deckbuilder.init_tables(conn)


def backup_db():
    """Snapshot collection.db into backups/ (keeps the newest BACKUP_KEEP)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, "collection-%s.db" % time.strftime("%Y%m%d-%H%M%S"))
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        backups = sorted(f for f in os.listdir(BACKUP_DIR)
                         if f.startswith("collection-") and f.endswith(".db"))
        for old in backups[:-BACKUP_KEEP]:
            os.unlink(os.path.join(BACKUP_DIR, old))
    except OSError:
        pass


def backup_today_exists():
    today = time.strftime("%Y%m%d")
    if not os.path.isdir(BACKUP_DIR):
        return False
    return any(f.startswith("collection-" + today) for f in os.listdir(BACKUP_DIR))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Condition quality → price multiplier. Scryfall prices are Near Mint;
# collection value scales down for worn cards.
CONDITIONS = ("NM", "LP", "MP", "HP", "D")
COND_MULT = {"NM": 1.0, "LP": 0.9, "MP": 0.8, "HP": 0.6, "D": 0.4}


def cond_mult(condition):
    return COND_MULT.get(condition or "NM", 1.0)


def _oracle_text(c):
    """Oracle text from a raw provider card (joins double-faced faces)."""
    if not isinstance(c, dict):
        return ""
    faces = c.get("card_faces")
    if faces:
        parts = [f.get("oracle_text") for f in faces if f.get("oracle_text")]
        return "\n\n".join(parts)
    return c.get("oracle_text") or ""


# ================================================================ providers
# Each game provides its own card data source: MTG → Scryfall, Pokémon →
# pokemontcg.io, Riftbound → a local CSV the user provides. Providers speak
# in "trimmed summaries" — a small dict shared by the local index, the API
# responses and the database.

SUMMARY_KEYS = ("scryfall_id", "name", "set_code", "set_name",
                "collector_number", "rarity", "mana_cost", "type_line",
                "colors", "image_uri", "scryfall_uri", "price_usd",
                "price_usd_foil", "back_image_uri")


class Provider:
    id = "mtg"
    label = "MTG"
    has_api = True
    can_batch = False
    throttle_sec = 0.11
    image_hosts = ()

    def __init__(self):
        self._lock = threading.Lock()
        self._last_call = [0.0]

    def _throttle(self):
        with self._lock:
            wait = self.throttle_sec - (time.time() - self._last_call[0])
            if wait > 0:
                time.sleep(wait)
            self._last_call[0] = time.time()

    def get_json(self, path, params=None, retries=4):
        """GET a JSON document, retrying transient 5xx/network failures.

        Card APIs (especially pokemontcg.io) can be flaky; a few retries
        with backoff make interactive use feel solid.
        """
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        self._throttle()
        req = urllib.request.Request(url, headers={
            "User-Agent": "LocalCardTracker/1.0 (personal collection tool)",
            "Accept": "application/json"})
        last = None
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                last = e
            except Exception as e:
                last = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
        raise last

    def post_json(self, path, payload):
        raise NotImplementedError

    # -- mapping ----------------------------------------------------------
    def summary(self, c):
        """Trim a raw provider card object (or pass a trimmed summary through)."""
        raise NotImplementedError

    # -- lookups (return trimmed summaries; raise on network failure) -----
    def search(self, q, limit=24):
        raise NotImplementedError

    def search_numbered(self, head, num):
        raise NotImplementedError

    def get_card(self, sid):
        raise NotImplementedError

    def exact_lookup(self, set_code, number):
        raise NotImplementedError

    def name_lookup(self, name):
        raise NotImplementedError

    # -- offline index ------------------------------------------------------
    def download_index(self, out_tmp, progress):
        """Write the trimmed JSON array of every known card to out_tmp."""
        raise NotImplementedError

    def fresh_prices(self, ids):
        """Bulk-refresh prices for many card ids.

        Returns {sid: (price_usd, price_usd_foil)} to short-circuit the
        per-card API lookup (used by Pokémon, where a bulk feed is faster and
        avoids rate limits), or None to use the default per-card path.
        """
        return None

    def image_ok(self, url):
        try:
            u = urllib.parse.urlparse(url)
        except Exception:
            return False
        return u.scheme in ("https", "http") and u.netloc in self.image_hosts


class MTGProvider(Provider):
    id = "mtg"
    label = "MTG"
    can_batch = True
    base = "https://api.scryfall.com"
    image_hosts = ("cards.scryfall.io",)

    def summary(self, c):
        # already-trimmed summary (e.g. from the local DB)
        if "image_uris" not in c and "card_faces" not in c and "image_uri" in c:
            return {k: c.get(k) for k in SUMMARY_KEYS}
        image = None
        if c.get("image_uris"):
            image = c["image_uris"].get("normal") or c["image_uris"].get("large")
        elif c.get("card_faces"):
            face = c["card_faces"][0]
            if face.get("image_uris"):
                image = face["image_uris"].get("normal")
        back = None
        faces = c.get("card_faces") or []
        if len(faces) > 1:
            fu = faces[1].get("image_uris") or {}
            back = fu.get("normal") or fu.get("large")
        prices = c.get("prices") or {}
        return {
            "scryfall_id": c["id"],
            "name": c["name"],
            "set_code": c.get("set"),
            "set_name": c.get("set_name"),
            "collector_number": c.get("collector_number"),
            "rarity": c.get("rarity"),
            "mana_cost": c.get("mana_cost") or "",
            "type_line": c.get("type_line") or "",
            "colors": "".join(c.get("colors") or []),
            "image_uri": image,
            "scryfall_uri": c.get("scryfall_uri"),
            "price_usd": float(prices["usd"]) if prices.get("usd") else None,
            "price_usd_foil": float(prices["usd_foil"]) if prices.get("usd_foil") else None,
            "back_image_uri": back,
        }

    def search(self, q, limit=24):
        d = self.get_json("/cards/search", {"q": q, "unique": "prints",
                                            "order": "released"})
        return [self.summary(c) for c in (d or {}).get("data", [])[:limit]]

    def search_numbered(self, head, num):
        out, seen = [], set()
        def add(card):
            if card and card["scryfall_id"] not in seen:
                seen.add(card["scryfall_id"])
                out.append(card)
        d = self.get_json("/cards/search", {
            "q": 'name:"%s" number:%s' % (head, num),
            "unique": "prints", "order": "released"})
        for c in (d or {}).get("data", [])[:24]:
            add(self.summary(c))
        if not out and re.fullmatch(r"[A-Za-z0-9]{3,5}", head):
            d = self.get_json("/cards/search", {
                "q": "set:%s number:%s" % (head.lower(), num)})
            for c in (d or {}).get("data", [])[:12]:
                add(self.summary(c))
        return out[:24]

    def get_card(self, sid):
        d = self.get_json("/cards/" + sid)
        return d if d and d.get("id") else None

    def exact_lookup(self, set_code, number):
        d = self.get_json("/cards/%s/%s" % (set_code, number))
        return self.summary(d) if d and d.get("object") == "card" else None

    def name_lookup(self, name):
        d = self.get_json("/cards/named", {"fuzzy": name})
        return self.summary(d) if d and d.get("object") == "card" else None

    def post_json(self, path, payload):
        self._throttle()
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, headers={
            "User-Agent": "LocalCardTracker/1.0 (personal collection tool)",
            "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def download_index(self, out_tmp, progress):
        d = self.get_json("/bulk-data")
        uri = None
        for e in (d or {}).get("data", []):
            if e.get("type") == "default_cards":
                uri = e.get("jsonl_download_uri")
                break
        if not uri:
            raise RuntimeError("Scryfall bulk data unavailable")
        req = urllib.request.Request(uri, headers={
            "User-Agent": "LocalCardTracker/1.0 (personal collection tool)"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length") or 0) / 1048576.0

            class Counter:
                def __init__(self, f):
                    self.f = f
                    self.n = 0
                def read(self, size=-1):
                    data = self.f.read(size)
                    self.n += len(data)
                    return data

            class Prepend:
                def __init__(self, prefix, reader):
                    self.prefix = prefix
                    self.reader = reader
                def read(self, size=-1):
                    if self.prefix:
                        if size is None or size < 0:
                            data, self.prefix = self.prefix, b""
                            return data + self.reader.read()
                        if len(self.prefix) >= size:
                            data, self.prefix = self.prefix[:size], self.prefix[size:]
                            return data
                        data, self.prefix = self.prefix, b""
                        return data + self.reader.read(size - len(data))
                    return self.reader.read(size)

            counter = Counter(resp)
            head = counter.read(2)
            src = gzip.GzipFile(fileobj=Prepend(head, counter)) if head == b"\x1f\x8b" \
                else Prepend(head, counter)
            _write_index(_iter_lines(src), out_tmp, lambda n: progress(
                done_mb=round(counter.n / 1048576.0, 1),
                total_mb=round(total, 1), cards=n),
                decode=True)


class PokemonProvider(Provider):
    id = "pokemon"
    label = "Pokémon"
    base = "https://api.pokemontcg.io/v2"
    throttle_sec = 0.05
    image_hosts = ("images.pokemontcg.io", "img.pokemontcg.io")
    api_key = os.environ.get("POKEMON_TCG_API_KEY", "").strip()

    # -- TCGplayer bulk pricing (tcgcsv.com — TCGplayer's official data feed;
    #    no API key and no rate limit, keyed by TCGplayer product id).
    #    Pokémon cards map to products via the set's ptcgoCode + collector
    #    number, and the price map is cached so refreshes don't re-download.
    tcgsv_base = "https://tcgcsv.com/tcgplayer/3"
    PRICE_MAP_FILE = os.path.join(ROOT, "local_prices_pokemon.json")
    PRICE_MAP_TTL = 12 * 3600
    _price_map = None
    _price_map_ts = 0.0

    def _tcgsv_get(self, path, retries=4):
        last = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(
                    self.tcgsv_base + path, headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X "
                                      "10_15_7) AppleWebKit/537.36 "
                                      "LocalCardTracker/1.0"})
                with urllib.request.urlopen(req, timeout=40) as r:
                    return json.loads(r.read())
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise last

    @staticmethod
    def _pnum(n):
        if n is None:
            return ""
        return _norm_num(str(n).split("/")[0])

    @staticmethod
    def _foilish(sub):
        return "holo" in (sub or "") or "foil" in (sub or "")

    def _match_group(self, sc, sig, sname, ptc, abbrev_by, gname, gsig):
        """Best tcgcsv group for a Pokémon set. Prefers an exact ptcgoCode→
        abbreviation match, then the SV/SWSH/...-era prefix rule, then a
        name+overlap heuristic (skips annotated/alternate printings)."""
        if ptc and ptc.upper() in abbrev_by:
            return abbrev_by[ptc.upper()]
        pref = None
        m = re.match(r"^([a-z]+)(\d+)$", (sc or "").lower())
        if m and m.group(1) in ("sv", "swsh", "bw", "xy", "sm", "dp",
                                "pl", "hgss", "em", "ex", "neo", "gym",
                                "ecard", "mc"):
            n = m.group(2)
            pref = m.group(1).upper() + ("0" + n if len(n) == 1 else n)
        best = None
        bs = 0.0
        for gid, gn in gname.items():
            gnl = gn.lower()
            if any(b in gnl for b in ("shadowless", "1st edition", "1st ed",
                                      "first edition", "unlimited",
                                      "black legend", "cracker jack")):
                continue
            ov = len(sig & gsig[gid]) / max(1, len(sig | gsig[gid]))
            nm = _similar(_norm_name(sname), _norm_name(gn))
            if pref and _norm_name(gn).startswith(_norm_name(pref)):
                ov = 1.0
            score = 0.6 * ov + 0.35 * nm + (0.3 if pref and
                    _norm_name(gn).startswith(_norm_name(pref)) else 0)
            if score > bs:
                bs = score
                best = gid
        return best if bs >= 0.6 else None

    def _build_price_map(self, cards, progress=None):
        """Download TCGplayer poke prices (tcgcsv) and return
        {scryfall_id: [price_usd, price_usd_foil]} for the given card
        summaries (which must carry set_code, collector_number, name,
        scryfall_id and ideally ptcgo_code)."""
        groups = self._tcgsv_get("/groups")
        groups = groups.get("results") or []
        abbrev_by = {}
        gname = {}
        for g in groups:
            gid = g["groupId"]
            gname[gid] = g.get("name") or ""
            a = (g.get("abbreviation") or "").strip().upper()
            if a:
                abbrev_by.setdefault(a, gid)
        setsig = {}
        sptc = {}
        sname = {}
        for c in cards:
            sc = (c.get("set_code") or "").lower()
            setsig.setdefault(sc, set()).add(
                (_norm_name(c.get("name")), self._pnum(c.get("collector_number"))))
            if c.get("ptcgo_code"):
                sptc.setdefault(sc, (c.get("ptcgo_code") or "").upper())
            sname.setdefault(sc, c.get("set_name") or "")
        group_prod = {}
        gidlist = list(gname)
        total = len(gidlist)
        for i, gid in enumerate(gidlist, 1):
            try:
                p = self._tcgsv_get("/%s/products" % gid)
                pr = self._tcgsv_get("/%s/prices" % gid)
            except Exception:
                continue
            prmap = {x["productId"]: x for x in (pr.get("results") or [])}
            info = {}
            for x in (p.get("results") or []):
                ext = {e.get("name"): e.get("value")
                       for e in (x.get("extendedData") or [])}
                num = ext.get("Number")
                if not num:
                    continue
                pr = prmap.get(x["productId"]) or {}
                info[x["productId"]] = {
                    "n": _norm_name(x.get("name")), "nn": self._pnum(num),
                    "sub": (pr.get("subTypeName") or "").lower(),
                    "mkt": pr.get("marketPrice")}
            group_prod[gid] = info
            if progress and i % 40 == 0:
                progress(cards=i)
            time.sleep(0.15)
        gsig = {gid: set((v["n"], v["nn"]) for v in info.values())
                for gid, info in group_prod.items()}
        set2group = {}
        for sc in setsig:
            gid = self._match_group(sc, setsig[sc], sname.get(sc, ""),
                                    sptc.get(sc, ""), abbrev_by, gname, gsig)
            if gid is not None:
                set2group[sc] = gid
        out = {}
        for c in cards:
            sc = (c.get("set_code") or "").lower()
            gid = set2group.get(sc)
            if gid is None or gid not in group_prod:
                continue
            nn = self._pnum(c.get("collector_number"))
            nm = _norm_name(c.get("name"))
            cand = [v for v in group_prod[gid].values()
                    if v["nn"] == nn and nm in v["n"]]
            mk = [v["mkt"] for v in cand if isinstance(v["mkt"], (int, float))]
            nf = [v["mkt"] for v in cand
                  if not self._foilish(v["sub"]) and isinstance(v["mkt"], (int, float))]
            fl = [v["mkt"] for v in cand
                  if self._foilish(v["sub"]) and isinstance(v["mkt"], (int, float))]
            usd = max(nf) if nf else (max(mk) if mk else None)
            fld = max(fl) if fl else (max(mk) if mk else None)
            if usd or fld:
                out[c["scryfall_id"]] = [usd, fld]
        return out

    def price_map(self, force=False):
        """Cached full-card TCGplayer price map for the Pokémon offline index."""
        now = time.time()
        if not force and self._price_map is not None and \
                now - self._price_map_ts < self.PRICE_MAP_TTL:
            return self._price_map
        if not force and os.path.exists(self.PRICE_MAP_FILE):
            try:
                if now - os.path.getmtime(self.PRICE_MAP_FILE) < self.PRICE_MAP_TTL:
                    with open(self.PRICE_MAP_FILE) as f:
                        self._price_map = json.load(f)
                    self._price_map_ts = now
                    return self._price_map
            except Exception:
                pass
        load_local("pokemon")
        cards = _state_for("pokemon").get("cards") or []
        if not cards:
            return {}
        m = self._build_price_map(cards)
        self._price_map = m
        self._price_map_ts = now
        try:
            with open(self.PRICE_MAP_FILE, "w") as f:
                json.dump(m, f)
        except OSError:
            pass
        return m

    def fresh_prices(self, ids):
        """Pokémon prices come from the TCGplayer bulk feed (no per-card API
        calls, no rate limits); fall back to pokemontcg.io when unavailable."""
        m = self.price_map()
        if not m:
            return None
        out = {}
        for sid in ids:
            p = m.get(sid)
            if p and (p[0] or p[1]):
                out[sid] = (p[0], p[1])
        return out if out else None

    def _req(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": "LocalCardTracker/1.0 (personal collection tool)",
                   "Accept": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return urllib.request.Request(url, headers=headers)

    def _get(self, path, params=None, retries=4):
        last = None
        for attempt in range(retries + 1):
            self._throttle()
            try:
                with urllib.request.urlopen(self._req(path, params), timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                last = e
            except Exception as e:
                last = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
        raise last

    def summary(self, c):
        if "image_uri" in c and "images" not in c and "image_uris" not in c:
            return {k: c.get(k) for k in SUMMARY_KEYS}
        imgs = c.get("images") or {}
        image = imgs.get("large") or imgs.get("small")
        setinfo = c.get("set") or {}
        tcg = (c.get("tcgplayer") or {}).get("prices") or {}
        def market(keys):
            for k in keys:
                p = tcg.get(k) or {}
                v = p.get("market") or p.get("mid")
                if v:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return None
        types = c.get("types") or []
        return {
            "scryfall_id": c["id"],
            "name": c["name"],
            "set_code": setinfo.get("id"),
            "set_name": setinfo.get("name"),
            "collector_number": c.get("number"),
            "rarity": c.get("rarity") or "",
            "mana_cost": "",
            "type_line": " ".join(x for x in (c.get("supertype"), " ".join(c.get("subtypes") or [])) if x),
            "colors": "",
            "image_uri": image,
            "scryfall_uri": (c.get("tcgplayer") or {}).get("url") or
                ("https://www.tcgplayer.com/search/all/product?q=" +
                 urllib.parse.quote(c.get("name") or "")),
            "price_usd": market(("normal", "reverseHolofoil", "unlimited", "1stEditionNormal", "holofoil")),
            "price_usd_foil": market(("holofoil", "reverseHolofoil", "1stEditionHolofoil", "unlimitedHolofoil")),
            "back_image_uri": None,
            "ptcgo_code": setinfo.get("ptcgoCode") or "",
            "released_at": setinfo.get("releaseDate") or "",
        }

    def search(self, q, limit=24):
        # pokemontcg.io needs structured queries — turn a bare word into a
        # quoted name search, but pass through advanced syntax (set:, etc.).
        pq = q if ":" in q else 'name:"%s"' % q.replace('"', "")
        d = self._get("/cards", {"q": pq, "pageSize": limit,
                                 "orderBy": "-set.releaseDate"})
        return [self.summary(c) for c in (d or {}).get("data", [])[:limit]]

    def search_numbered(self, head, num):
        out = []
        d = self._get("/cards", {"q": 'name:"%s" number:%s' % (head, num),
                                 "pageSize": 24, "orderBy": "-set.releaseDate"})
        out += [self.summary(c) for c in (d or {}).get("data", [])]
        if not out and len(head) <= 8:
            d = self._get("/cards", {"q": "set.id:%s number:%s" % (head.lower(), num),
                                     "pageSize": 12})
            out += [self.summary(c) for c in (d or {}).get("data", [])]
        return out[:24]

    def get_card(self, sid):
        d = self._get("/cards/" + sid)
        data = (d or {}).get("data")
        if isinstance(data, list):
            return data[0] if data else None
        return data if data and data.get("id") else None

    def exact_lookup(self, set_code, number):
        d = self._get("/cards", {"q": "set.id:%s number:%s" % (set_code.lower(), number),
                                 "pageSize": 1})
        data = (d or {}).get("data") or []
        return self.summary(data[0]) if data else None

    def name_lookup(self, name):
        d = self._get("/cards", {"q": "name:%s" % name, "pageSize": 1,
                                 "orderBy": "-set.releaseDate"})
        data = (d or {}).get("data") or []
        return self.summary(data[0]) if data else None

    def download_index(self, out_tmp, progress):
        # Pull the whole Pokémon TCG card index from the pokemon-tcg-data
        # GitHub repo as a single tarball — no API rate limit, one request,
        # fully self-contained. Images come from images.pokemontcg.io and are
        # cached through /api/img as they're viewed.
        import tarfile
        import shutil
        url = ("https://codeload.github.com/PokemonTCG/"
               "pokemon-tcg-data/tar.gz/refs/heads/master")
        req = urllib.request.Request(url, headers={
            "User-Agent": "LocalCardTracker/1.0 (personal collection tool)"})
        tmp_tar = out_tmp + ".tar.gz"
        with urllib.request.urlopen(req, timeout=180) as resp, \
                open(tmp_tar, "wb") as f:
            done = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if done % (8 << 20) < (1 << 20):
                    progress(done_mb=round(done / 1048576.0, 1))
        extract_dir = tempfile.mkdtemp(prefix="pkmn-tcg-data-")
        summaries = []
        try:
            with tarfile.open(tmp_tar, "r:gz") as tf:
                tf.extractall(extract_dir)
            root = os.path.join(extract_dir, os.listdir(extract_dir)[0])
            sets = {}
            sets_path = os.path.join(root, "sets", "en.json")
            if os.path.exists(sets_path):
                with open(sets_path) as f:
                    for s in json.load(f):
                        sets[s.get("id")] = s
            cards_dir = os.path.join(root, "cards", "en")
            for fname in sorted(os.listdir(cards_dir)):
                if not fname.endswith(".json"):
                    continue
                set_id = fname[:-5]
                setinfo = sets.get(set_id) or {}
                with open(os.path.join(cards_dir, fname)) as f:
                    cards = json.load(f)
                for raw in cards:
                    raw["set"] = {"id": set_id,
                                   "name": setinfo.get("name"),
                                   "releaseDate": setinfo.get("releaseDate") or "",
                                   "ptcgoCode": setinfo.get("ptcgoCode") or ""}
                    s = self.summary(raw)
                    if not s or not s["name"]:
                        continue
                    summaries.append(s)
                    if len(summaries) % 5000 == 0:
                        try:
                            mb = os.path.getsize(tmp_tar) / 1048576.0
                        except OSError:
                            mb = 0.0
                        progress(done_mb=round(mb, 1), cards=len(summaries))
        finally:
            try:
                os.unlink(tmp_tar)
            except OSError:
                pass
            shutil.rmtree(extract_dir, ignore_errors=True)

        # Attach TCGplayer market prices from tcgcsv.com (bulk, no rate
        # limit) so indexed cards carry their price offline.
        try:
            pmap = self._build_price_map(
                summaries, progress=lambda **k: progress(cards=len(summaries)))
            for s in summaries:
                p = pmap.get(s["scryfall_id"])
                if p:
                    s["price_usd"], s["price_usd_foil"] = p[0], p[1]
            try:
                with open(self.PRICE_MAP_FILE, "w") as f:
                    json.dump(pmap, f)
            except OSError:
                pass
        except Exception:
            pass

        with open(out_tmp, "w") as out:
            out.write("[")
            wrote = False
            count = 0
            for s in summaries:
                if wrote:
                    out.write(",\n")
                out.write(json.dumps(s))
                wrote = True
                count += 1
            out.write("]")
        progress(cards=count)


class RiftboundProvider(Provider):
    id = "riftbound"
    label = "Riftbound"
    has_api = False
    image_hosts = ()  # empty → any https host allowed (see image_ok)

    def image_ok(self, url):
        try:
            u = urllib.parse.urlparse(url)
        except Exception:
            return False
        return u.scheme in ("https", "http")

    def summary(self, c):
        return {k: c.get(k) for k in SUMMARY_KEYS}

    def row_to_summary(self, row):
        """Build a trimmed summary from a CSV row (name, set_code, ...)."""
        def f(v):
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None
        sid = (row.get("scryfall_id") or "").strip() or hashlib.md5(
            "|".join([(row.get("name") or "").strip(),
                      (row.get("set_code") or "").strip(),
                      str(row.get("collector_number") or "").strip()]).encode()
        ).hexdigest()
        return {
            "scryfall_id": sid,
            "name": (row.get("name") or "").strip(),
            "set_code": (row.get("set_code") or "").strip() or None,
            "set_name": (row.get("set_name") or "").strip() or None,
            "collector_number": str(row.get("collector_number") or "").strip() or None,
            "rarity": (row.get("rarity") or "").strip() or None,
            "mana_cost": "",
            "type_line": (row.get("type_line") or "").strip() or "",
            "colors": "",
            "image_uri": (row.get("image_uri") or "").strip() or None,
            "scryfall_uri": (row.get("scryfall_uri") or "").strip() or "",
            "price_usd": f(row.get("price_usd")),
            "price_usd_foil": f(row.get("price_usd_foil")),
            "back_image_uri": None,
            "released_at": (row.get("released_at") or "").strip(),
        }

    def resolve_row(self, row):
        return self.row_to_summary(row)

    def get_card(self, sid):
        return None  # no live API

    def download_index(self, out_tmp, progress):
        csv_path = os.path.join(ROOT, "riftbound_cards.csv")
        if not os.path.exists(csv_path):
            raise RuntimeError(
                "riftbound_cards.csv not found in the tracker folder — see "
                "README for the expected columns")
        count = 0
        with open(csv_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        with open(out_tmp, "w") as out:
            out.write("[")
            wrote = False
            for row in rows:
                name = (row.get("name") or "").strip()
                if not name or name.startswith("#"):  # allow # comment lines
                    continue
                s = self.row_to_summary(row)
                if wrote:
                    out.write(",\n")
                out.write(json.dumps(s))
                wrote = True
                count += 1
            out.write("]")
        progress(cards=count)


def _ygo_set_parts(full_code):
    """Split a Yu-Gi-Oh! set code (e.g. 'FOTB-EN043', 'SDK-007', 'DB49')
    into (set_prefix, collector_number). Degrades gracefully on the odd
    promo codes (e.g. 'ORCS-ENSE1' → ('ORCS', 'SE1'))."""
    full = (full_code or "").strip()
    if not full:
        return None, None
    head, sep, tail = full.partition("-")
    if sep:
        number = tail or None
        m2 = re.match(r"^([A-Za-z]{2})([A-Za-z0-9]+)$", tail)
        if m2 and m2.group(2) and re.search(r"\d", m2.group(2)):
            number = m2.group(2)  # 2-letter language + number (EN043/ENSE1)
        else:
            m1 = re.match(r"^([A-Za-z])(\d+)$", tail)
            if m1:
                number = m1.group(2)  # 1-letter language + number (E088)
        return head.upper(), number
    m = re.match(r"^([A-Za-z]+)(\d.*)$", full)
    if m:
        return m.group(1).upper(), m.group(2)
    return full.upper(), None


def _ygo_sid(cid, full_code, rarity):
    """Stable, URL-safe unique id for a printing (card + set code + rarity)."""
    slug = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return "%s-%s-%s" % (cid, slug(full_code), slug(rarity))


class YugiohProvider(Provider):
    id = "yugioh"
    label = "Yu-Gi-Oh!"
    base = "https://db.ygoprodeck.com/api/v7"
    throttle_sec = 0.05
    image_hosts = ("images.ygoprodeck.com",)

    def summary(self, c):
        # already-trimmed summary (e.g. from the local DB)
        if "image_uri" in c and "card_sets" not in c and "card_images" not in c:
            return {k: c.get(k) for k in SUMMARY_KEYS}
        cid = c["id"]
        sets = c.get("card_sets") or []
        si = sets[0] if sets else {}
        full_code = si.get("set_code") or ""
        set_code, number = _ygo_set_parts(full_code)
        image = ((c.get("card_images") or [{}])[0].get("image_url") or None)
        price = None
        if si.get("set_price") not in (None, ""):
            price = si.get("set_price")
        else:
            prices = c.get("card_prices") or [{}]
            price = (prices[0].get("tcgplayer_price") if prices else None)
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None
        return {
            "scryfall_id": _ygo_sid(cid, full_code, si.get("set_rarity")),
            "name": c["name"],
            "set_code": set_code,
            "set_name": si.get("set_name") or "",
            "collector_number": number,
            "rarity": si.get("set_rarity") or "",
            "mana_cost": "",
            "type_line": c.get("type") or c.get("humanReadableCardType") or "",
            "colors": "",
            "image_uri": image,
            "scryfall_uri": c.get("ygoprodeck_url") or "",
            "price_usd": price,
            "price_usd_foil": None,
            "back_image_uri": None,
        }

    def search(self, q, limit=24):
        d = self.get_json("/cardinfo.php", {"fname": q})
        return [self.summary(c) for c in (d or {}).get("data", [])[:limit]]

    def search_numbered(self, head, num):
        return []  # local index handles set+number; live fuzzy covers the rest

    def get_card(self, sid):
        cid = sid.split("-")[0] if sid else sid
        d = self.get_json("/cardinfo.php", {"id": cid})
        data = (d or {}).get("data") or []
        return data[0] if data else None

    def exact_lookup(self, set_code, number):
        return None  # set+number lives in the offline index

    def name_lookup(self, name):
        d = self.get_json("/cardinfo.php", {"fname": name})
        data = (d or {}).get("data") or []
        return self.summary(data[0]) if data else None

    def download_index(self, out_tmp, progress):
        # YGOPRODeck's bulk endpoint returns every card + printings + prices
        # in a single request (no pagination, effectively no rate limit).
        url = self.base + "/cardinfo.php"
        req = urllib.request.Request(url, headers={
            "User-Agent": "LocalCardTracker/1.0 (personal collection tool)"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
        doc = json.loads(raw)
        count = 0
        with open(out_tmp, "w") as out:
            out.write("[")
            wrote = False
            for c in (doc or {}).get("data", []):
                for si in (c.get("card_sets") or []):
                    # narrow to this printing for the summary
                    one = dict(c)
                    one["card_sets"] = [si]
                    s = self.summary(one)
                    if not s or not s["name"]:
                        continue
                    if wrote:
                        out.write(",\n")
                    out.write(json.dumps(s))
                    wrote = True
                    count += 1
                    if count % 5000 == 0:
                        progress(cards=count)
            out.write("]")
        progress(cards=count)


PROVIDERS = {p.id: p for p in (MTGProvider(), PokemonProvider(),
                                YugiohProvider(), RiftboundProvider())}
GAMES = ("mtg", "pokemon", "yugioh")
DEFAULT_GAME = GAME if GAME in PROVIDERS else "mtg"
P = PROVIDERS["mtg"]  # MTG stays the deck-builder/assistant provider


def provider_for(game):
    """Resolve a game id to its provider (unknown ids fall back to MTG)."""
    return PROVIDERS.get((game or "").strip().lower()) or PROVIDERS["mtg"]


def image_ok_any(url):
    return any(p.image_ok(url) for p in PROVIDERS.values())

# -------------------------------------------------- offline card database

_local_lock = threading.Lock()
_local_store = {}       # game -> {cards, exact, byname, byid, meta}
_download_state = {}    # game -> {active, phase, done_mb, total_mb, cards}


def _local_files(game):
    game = (game or "mtg").lower()
    if game == "mtg":
        return LOCAL_CARDS_FILE, LOCAL_META_FILE
    return (os.path.join(ROOT, "local_cards_%s.json" % game),
            os.path.join(ROOT, "localdb_meta_%s.json" % game))


def _state_for(game):
    game = (game or "mtg").lower()
    st = _local_store.get(game)
    if st is None:
        st = {}
        _local_store[game] = st
    return st


def _download_state_for(game):
    game = (game or "mtg").lower()
    st = _download_state.get(game)
    if st is None:
        st = {"active": False, "phase": "", "done_mb": 0.0,
              "total_mb": 0.0, "cards": 0}
        _download_state[game] = st
    return st


def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _norm_num(n):
    m = re.match(r"0*(\d+)", str(n))
    return m.group(1) if m else str(n)


def _index_cards(cards):
    exact, byname, byid = {}, {}, {}
    for c in cards:
        key = (str(c.get("set_code", "")).lower(),
               _norm_num(c.get("collector_number", "")))
        exact.setdefault(key, []).append(c)
        byname.setdefault(_norm_name(c.get("name", "")), []).append(c)
        byid[c["scryfall_id"]] = c
    return exact, byname, byid


def load_local(game="mtg"):
    """Lazily parse a game's local index into memory (once per process)."""
    st = _state_for(game)
    if "cards" in st:
        return True
    cards_file, meta_file = _local_files(game)
    if not os.path.exists(cards_file):
        return False
    try:
        with open(cards_file) as f:
            cards = json.load(f)
        st["cards"] = cards
        st["exact"], st["byname"], st["byid"] = _index_cards(cards)
    except Exception:
        return False
    try:
        with open(meta_file) as f:
            st["meta"] = json.load(f)
    except Exception:
        st["meta"] = None
    return True


def local_by_sid(sid, game="mtg"):
    load_local(game)
    byid = _state_for(game).get("byid")
    return byid.get(sid) if byid else None


def fetch_card(sid, game="mtg"):
    """Best-effort card lookup: local DB first, live API to enrich.

    For double-faced cards missing their back image (older index), prefer the
    live API so adds always carry the flip side — and fall back to the local
    copy when offline.
    """
    prov = provider_for(game)
    card = local_by_sid(sid, game)
    if card is not None and not (card.get("has_faces") and not card.get("back_image_uri")):
        return card
    if prov.has_api:
        try:
            api_card = prov.get_card(sid)
            if api_card:
                return api_card
        except Exception:
            pass
    return card


def local_exact_match(set_code, number, game="mtg"):
    load_local(game)
    exact = _state_for(game).get("exact")
    if not exact:
        return None
    got = exact.get((str(set_code).lower(), _norm_num(number)))
    return got[0] if got else None


def local_name_match(name, game="mtg"):
    load_local(game)
    byname = _state_for(game).get("byname")
    if not byname:
        return None
    cands = byname.get(_norm_name(name.split("//")[0].strip()))
    if not cands:
        return None
    cands.sort(key=lambda c: c.get("released_at") or "", reverse=True)
    return cands[0]


def local_by_name_index(game="mtg"):
    """The whole {norm_name: [summary, ...]} index for a game (deck builder)."""
    load_local(game)
    return _state_for(game).get("byname") or {}


def local_numbered_names(name, number, game="mtg"):
    """Every printing of a name with a given collector number (local DB)."""
    load_local(game)
    byname = _state_for(game).get("byname")
    if not byname:
        return []
    nn = _norm_num(number)
    got = [c for c in byname.get(_norm_name(name.split("//")[0].strip()), [])
           if _norm_num(c.get("collector_number", "")) == nn]
    got.sort(key=lambda c: c.get("released_at") or "", reverse=True)
    return got


def local_search(q, game="mtg"):
    load_local(game)
    cards = _state_for(game).get("cards")
    if not cards:
        return []
    qn = _norm_name(q)
    out = []
    for c in cards:
        if qn in _norm_name(c["name"]) or qn in _norm_name(c.get("set_name", "")):
            out.append(c)
        if len(out) >= 24:
            break
    out.sort(key=lambda c: c.get("released_at") or "", reverse=True)
    return out[:24]


def _search_numbered_local(head, num, game="mtg"):
    out, seen = [], set()
    def add(c):
        if c and c["scryfall_id"] not in seen:
            seen.add(c["scryfall_id"])
            out.append(c)
    for c in local_numbered_names(head, num, game):
        add(c)
    if re.fullmatch(r"[A-Za-z0-9]{3,5}", head):
        c = local_exact_match(head, num, game)
        if c:
            add(c)
    return out[:24]


def _trim_local(c, game="mtg"):
    """Trim a raw provider card to summary + release date + face info."""
    try:
        s = provider_for(game).summary(c)
    except Exception:
        return None
    s["released_at"] = c.get("released_at") or s.get("released_at") or ""
    s["has_faces"] = bool(c.get("card_faces") and len(c["card_faces"]) > 1)
    return s


def _iter_lines(f, chunk=65536):
    """Yield lines from a binary stream without loading it all."""
    buf = b""
    while True:
        data = f.read(chunk)
        if not data:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line
    if buf:
        yield buf


def _write_index(lines, out_tmp, progress, decode=False, game="mtg"):
    """Write a trimmed JSON array from text (or bytes) card lines."""
    count = 0
    wrote = False
    with open(out_tmp, "w") as out:
        out.write("[")
        def emit(s):
            nonlocal wrote, count
            if not s:
                return
            if wrote:
                out.write(",\n")
            out.write(json.dumps(s))
            wrote = True
            count += 1
            if count % 5000 == 0:
                progress(count)
        first = None
        for raw in lines:
            s = raw.decode("utf-8", "replace").strip() if decode else raw.strip()
            if not s:
                continue
            if first is None:
                first = s
                if s.startswith("["):  # legacy: whole doc is one JSON array
                    rest = b"".join(_iter_lines(lines)) if decode else "".join(lines)
                    if decode:
                        rest = rest.decode("utf-8", "replace")
                    try:
                        doc = json.loads(first + rest)
                        for c in (doc if isinstance(doc, list) else []):
                            emit(_trim_local(c, game))
                    except ValueError:
                        pass
                    break
            try:
                emit(_trim_local(json.loads(s), game))
            except ValueError:
                continue
        out.write("]")
    progress(count)
    return count


def download_localdb(game="mtg"):
    """Build the local card index for one game (background)."""
    game = (game or "mtg").lower()

    def work():
        prov = provider_for(game)
        st = _state_for(game)
        dstate = _download_state_for(game)
        cards_file, meta_file = _local_files(game)
        with _local_lock:
            if dstate["active"]:
                return
            dstate.update(active=True, phase="download",
                          done_mb=0.0, total_mb=0.0, cards=0)
        out_tmp = cards_file + ".tmp"
        def progress(done_mb=None, total_mb=None, cards=None):
            with _local_lock:
                if done_mb is not None:
                    dstate["done_mb"] = done_mb
                if total_mb is not None:
                    dstate["total_mb"] = total_mb
                if cards is not None:
                    dstate["cards"] = cards
                ev = dict(dstate)
            broadcast({"type": "localdb-progress", "game": game,
                       "phase": "download",
                       "done_mb": round(ev.get("done_mb", 0), 1),
                       "total_mb": round(ev.get("total_mb", 0), 1),
                       "cards": ev.get("cards", 0)})
        try:
            prov.download_index(out_tmp, progress)
            dstate["phase"] = "build"
            broadcast({"type": "localdb-progress", "game": game,
                       "phase": "build"})
            with open(out_tmp) as f:
                cards = json.load(f)
            with _local_lock:
                st["cards"] = cards
                st["exact"], st["byname"], st["byid"] = _index_cards(cards)
                st["meta"] = {"downloaded_at": now_iso(),
                              "card_count": len(cards),
                              "source": prov.id}
            os.replace(out_tmp, cards_file)
            with open(meta_file, "w") as f:
                json.dump(st["meta"], f)
            broadcast({"type": "localdb-done", "game": game,
                       "card_count": len(cards)})
        except Exception as e:
            try:
                os.unlink(out_tmp)
            except OSError:
                pass
            broadcast({"type": "localdb-error", "game": game, "error": str(e)})
        finally:
            with _local_lock:
                dstate.update(active=False, phase="", done_mb=0.0,
                              total_mb=0.0, cards=0)
    threading.Thread(target=work, daemon=True).start()


def local_meta(game="mtg"):
    try:
        with open(_local_files(game)[1]) as f:
            return json.load(f)
    except Exception:
        return None


# ------------------------------------------------------- image cache
# /api/img proxies card images through a local cache so the library renders
# offline after the first view. Each provider allows its own image hosts.

def api_img(self, url):
    if not image_ok_any(url):
        self.send_json({"error": "not allowed"}, 403)
        return
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        self.send_json({"error": "bad url"}, 400)
        return
    ext = os.path.splitext(u.path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
             "png": "image/png", "webp": "image/webp"}[ext.lstrip(".")]
    path = os.path.join(IMG_CACHE_DIR, hashlib.md5(url.encode()).hexdigest() + ext)
    if not os.path.exists(path):
        os.makedirs(IMG_CACHE_DIR, exist_ok=True)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "LocalCardTracker/1.0 (personal collection tool)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                ctype = resp.headers.get("Content-Type") or ctype
                with open(path, "wb") as f:
                    f.write(resp.read())
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            self.send_json({"error": "image fetch failed"}, 502)
            return
    with open(path, "rb") as f:
        body = f.read()
    self.send_response(200)
    self.send_header("Content-Type", ctype)
    self.send_header("Cache-Control", "public, max-age=86400")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)


# ---------------------------------------------------------------- scanning
# OCR backends, in preference order:
#   • Apple Vision (macOS) — the ocr.swift helper, best quality, auto-built
#     with swiftc on first run.
#   • Tesseract (any OS) — `tesseract` on PATH (brew/apt/choco install
#     tesseract). Language packs via CARD_TRACKER_TESS_LANGS (default "eng").
# Both are normalized to the same line format: {text, confidence 0..1,
# x, y, w, h} with normalized coordinates and a bottom-left origin.

TESS_LANGS = os.environ.get("CARD_TRACKER_TESS_LANGS", "eng")
_ocr_backend = None


def detect_ocr_backend():
    global _ocr_backend
    if _ocr_backend:
        return _ocr_backend
    if sys.platform == "darwin":
        if not os.path.exists(OCR_BIN) and os.path.exists(os.path.join(ROOT, "ocr.swift")):
            try:  # first run on a Mac — build the Vision helper
                subprocess.run(
                    ["swiftc", "-O", os.path.join(ROOT, "ocr.swift"), "-o", OCR_BIN],
                    check=True, capture_output=True, timeout=180)
            except Exception:
                pass
        if os.path.exists(OCR_BIN):
            _ocr_backend = "vision"
            return _ocr_backend
    try:
        subprocess.run(["tesseract", "--version"], capture_output=True, timeout=10)
        _ocr_backend = "tesseract"
    except Exception:
        _ocr_backend = "none"
    return _ocr_backend


def _ocr_vision(path):
    out = subprocess.run([OCR_BIN, path], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "OCR failed")
    return json.loads(out.stdout)


def _ocr_tesseract(path):
    """Tesseract TSV → Vision-style lines (normalized, bottom-left origin)."""
    out = subprocess.run(
        ["tesseract", path, "stdout", "-l", TESS_LANGS, "--psm", "3", "tsv"],
        capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "tesseract failed")
    page_w = page_h = 0
    lines = {}
    rows = out.stdout.splitlines()
    header = rows[0].split("\t") if rows else []
    idx = {k: i for i, k in enumerate(header)}
    for row in rows[1:]:
        f = row.split("\t")
        if len(f) < len(header):
            continue
        level = f[idx["level"]]
        if level == "1":  # page row carries the image dimensions
            page_w = int(f[idx["width"]])
            page_h = int(f[idx["height"]])
        elif level == "5":  # word
            conf = float(f[idx["conf"]])
            text = f[idx["text"]].strip()
            if conf < 0 or not text:
                continue
            key = (f[idx["block_num"]], f[idx["par_num"]], f[idx["line_num"]])
            left, top = int(f[idx["left"]]), int(f[idx["top"]])
            w, h = int(f[idx["width"]]), int(f[idx["height"]])
            ln = lines.setdefault(key, {"words": [], "confs": [],
                                        "x0": left, "y0": top,
                                        "x1": left + w, "y1": top + h})
            ln["words"].append(text)
            ln["confs"].append(conf)
            ln["x0"] = min(ln["x0"], left)
            ln["y0"] = min(ln["y0"], top)
            ln["x1"] = max(ln["x1"], left + w)
            ln["y1"] = max(ln["y1"], top + h)
    if not page_w or not page_h:
        return []
    result = []
    for ln in lines.values():
        result.append({
            "text": " ".join(ln["words"]),
            "confidence": (sum(ln["confs"]) / len(ln["confs"])) / 100.0,
            "x": ln["x0"] / page_w,
            "y": 1.0 - ln["y1"] / page_h,  # flip to bottom-left origin
            "w": (ln["x1"] - ln["x0"]) / page_w,
            "h": (ln["y1"] - ln["y0"]) / page_h,
        })
    return result


def ocr_image(path, attempts=2):
    """Run OCR with the best available backend, retrying transient failures."""
    backend = detect_ocr_backend()
    if backend == "none":
        raise RuntimeError(
            "no OCR backend: install Tesseract (brew/apt/choco install "
            "tesseract), or on macOS ensure Xcode command-line tools so the "
            "Vision helper can build")
    fn = _ocr_vision if backend == "vision" else _ocr_tesseract
    last = None
    for i in range(attempts + 1):
        try:
            return fn(path)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            last = e
            if i < attempts:
                time.sleep(0.4)
    raise last


def candidate_names(lines):
    """Pick likely card-name lines: topmost text, mostly letters.

    Vision bounding boxes have a bottom-left origin, so the card title is
    the line with the highest y.
    """
    usable = []
    for ln in lines:
        text = ln["text"].strip()
        letters = sum(ch.isalpha() for ch in text)
        if len(text) < 3 or len(text) > 40:
            continue
        if letters < len(text) * 0.6:
            continue
        # strip mana-cost artifacts sometimes OCR'd after the name
        text = re.sub(r"[\d{}()*]+$", "", text).strip()
        if text:
            usable.append((ln["y"], ln.get("confidence", 0), text))
    usable.sort(key=lambda t: -t[0])  # topmost first
    seen, out = set(), []
    for _, _, text in usable[:6]:
        if text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


# Bottom-of-card collector line, e.g. "M 0026" (rarity + number) and
# "TLE • EN • …" (set code + language), or Pokémon-style "189/165".
NUM_RE = re.compile(r"^[CURMTS]?\s*0*(\d{1,4})(?:\s*/\s*\d+)?[a-z★]?$")
SET_RE = re.compile(r"^([A-Z0-9]{3,5})\s*[•·.\-*+]\s*[A-Z]{2}\b")
# Yu-Gi-Oh! set codes like "FOTB-EN043", "SDK-007", "LON-E088".
YGO_CODE_RE = re.compile(
    r"^([A-Za-z0-9]{2,6})[-\s–—.]*([A-Za-z]{0,3})?[-\s–—.]*0*(\d{1,4})$")


def parse_print_info(lines, game="mtg"):
    """Extract (set_code, collector_number) from the card's bottom edge."""
    number = set_code = None
    for ln in sorted(lines, key=lambda l: l["y"])[:8]:
        text = ln["text"].strip()
        if game == "yugioh":
            m = YGO_CODE_RE.match(text)
            if m and number is None:
                set_code = m.group(1).upper()
                number = m.group(3)
                continue
        m = NUM_RE.match(text)
        if m and number is None:
            number = m.group(1)
        m = SET_RE.match(text)
        if m and set_code is None:
            set_code = m.group(1).lower()
        if number and set_code:
            break
    return set_code, number


def names_agree(a, b):
    """Loose check that an exact-print hit matches the OCR'd title."""
    norm = lambda s: re.sub(r"[^a-z]", "", s.lower())
    na, nb = norm(a), norm(b.split("//")[0])
    return na in nb or nb in na or _similar(na, nb) > 0.7


def _similar(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def identify_card(image_path, game="mtg"):
    """Match an OCR'd card: local DB first (fast/offline), then the API."""
    prov = provider_for(game)
    lines = ocr_image(image_path)
    names = candidate_names(lines)
    set_code, number = parse_print_info(lines, game)

    # 1) Exact printing first: set code + collector number is unambiguous.
    if set_code and number:
        card = local_exact_match(set_code, number, game)
        method, source = "local-exact", "local"
        if card is None and prov.has_api:
            try:
                card = prov.exact_lookup(set_code, number)
            except Exception:
                card = None
            if card:
                method, source = "exact", "api"
        if card and (not names or names_agree(names[0], card["name"])):
            return {"match": card, "method": method, "source": source,
                    "exact": True,
                    "ocr_guess": names[0] if names else card["name"],
                    "ocr_candidates": names}

    # 2) Collector number without a set code (e.g. Pokémon "189/165") —
    #     match name + number across printings. Still an exact printing.
    if number and names and not set_code:
        for name in names[:2]:
            got = local_numbered_names(name, number, game)
            if got and (len(got) == 1 or names_agree(names[0], got[0]["name"])):
                return {"match": got[0], "method": "local-number", "source": "local",
                        "exact": True,
                        "ocr_guess": name, "ocr_candidates": names}
            if prov.has_api:
                try:
                    got = prov.search_numbered(name, number)
                except Exception:
                    got = None
                if got and (len(got) == 1 or names_agree(names[0], got[0]["name"])):
                    return {"match": got[0], "method": "number", "source": "api",
                            "exact": True,
                            "ocr_guess": name, "ocr_candidates": names}

    # 3) Fall back to fuzzy name match (NOT an exact printing).
    for name in names[:4]:
        card = local_name_match(name, game)
        if card:
            return {"match": card, "method": "local-name", "source": "local",
                    "exact": False,
                    "ocr_guess": name, "ocr_candidates": names}
        if prov.has_api:
            try:
                card = prov.name_lookup(name)
            except Exception:
                card = None
            if card:
                return {"match": card, "method": "fuzzy", "source": "api",
                        "exact": False,
                        "ocr_guess": name, "ocr_candidates": names}
    return {"match": None, "method": None, "source": None, "exact": False,
            "ocr_guess": names[0] if names else None, "ocr_candidates": names}


# ---------------------------------------------------------------- handlers

# ----------------------------------------------------- background refreshes
# Collection and wishlist price refreshes run in background threads and
# stream progress over SSE. They're triggered by the "Prices" button and by
# the once-a-day auto-refresh loop started in __main__.
_refresh_state = {"active": False}
_refresh_lock = threading.Lock()
AUTO_REFRESH_SEC = 24 * 3600


def _meta_get(key, default=None):
    with db_lock, db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def _meta_set(key, value):
    with db_lock, db() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _mark_refreshed():
    _meta_set("last_auto_refresh", str(time.time()))


def _refresh_cards(ids, ts, game):
    prov = provider_for(game)
    bulk = None
    try:
        bulk = prov.fresh_prices(ids)
    except Exception:
        bulk = None
    use_bulk = bulk is not None
    total = len(ids)
    updated = 0
    for i, sid in enumerate(ids, 1):
        usd = fld = None
        if use_bulk:
            pv = bulk.get(sid)
            if not pv:
                continue
            usd, fld = pv[0], pv[1]
        else:
            try:
                card = prov.get_card(sid)
            except Exception:
                card = None
            if not card:
                continue
            s = prov.summary(card)
            usd, fld = s["price_usd"], s["price_usd_foil"]
        with db_lock, db() as conn:
            conn.execute(
                "UPDATE cards SET price_usd=?, price_usd_foil=?, "
                "price_updated_at=? WHERE scryfall_id=?",
                (usd, fld, ts, sid))
            conn.execute(
                "INSERT INTO price_history (scryfall_id, recorded_at, "
                "usd, usd_foil) VALUES (?,?,?,?)",
                (sid, ts, usd, fld))
            updated += 1
        if i % 10 == 0 or i == total:
            broadcast({"type": "price-progress", "game": game, "done": i,
                       "total": total, "updated": updated})
    return updated


def _refresh_wishlist(rows, ts):
    alerts = []
    for i, r in enumerate(rows, 1):
        prov = provider_for(r.get("game"))
        try:
            card = prov.get_card(r["scryfall_id"])
        except Exception:
            card = None
        if card:
            s = prov.summary(card)
            price = s.get("price_usd_foil") if r.get("foil") else s.get("price_usd")
            with db_lock, db() as conn:
                conn.execute(
                    "UPDATE wishlist SET price_usd=?, price_usd_foil=?, "
                    "image_uri=? WHERE id=?",
                    (s.get("price_usd"), s.get("price_usd_foil"),
                     s.get("image_uri"), r["id"]))
                conn.execute(
                    "INSERT INTO price_history (scryfall_id, recorded_at, "
                    "usd, usd_foil) VALUES (?,?,?,?)",
                    (r["scryfall_id"], ts, s.get("price_usd"),
                     s.get("price_usd_foil")))
            if (r.get("target_price") is not None and price is not None
                    and price <= r["target_price"]):
                alerts.append(r["name"])
        if i % 5 == 0 or i == len(rows):
            broadcast({"type": "wishlist-progress", "done": i,
                       "total": len(rows)})
    return alerts


def run_price_refresh(game="mtg"):
    """Refresh one game's collection card prices in the background."""
    game = (game or "mtg").lower()
    prov = provider_for(game)
    with _refresh_lock:
        if _refresh_state["active"]:
            return {"error": "refresh already running"}
        with db_lock, db() as conn:
            ids = [r["scryfall_id"] for r in conn.execute(
                "SELECT DISTINCT scryfall_id FROM cards WHERE game=?", (game,))]
        if not ids or not prov.has_api:
            return {"started": False, "total": len(ids)}
        _refresh_state["active"] = True
    _mark_refreshed()

    def work():
        try:
            backup_db()
            updated = _refresh_cards(ids, now_iso(), game)
            broadcast({"type": "price-done", "game": game, "updated": updated})
            broadcast({"type": "library-changed"})
        finally:
            with _refresh_lock:
                _refresh_state["active"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"started": True, "total": len(ids)}


def run_wishlist_refresh(game=None):
    """Refresh wishlist prices in the background; alerts broadcast over SSE."""
    game = (game or "").lower() or None
    with _refresh_lock:
        if _refresh_state["active"]:
            return {"error": "refresh already running"}
        with db_lock, db() as conn:
            if game:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM wishlist WHERE game=?", (game,))]
            else:
                rows = [dict(r) for r in conn.execute("SELECT * FROM wishlist")]
        if not rows:
            return {"started": False}
        _refresh_state["active"] = True

    def work():
        try:
            alerts = _refresh_wishlist(rows, now_iso())
            broadcast({"type": "wishlist-done", "alerts": alerts})
        finally:
            with _refresh_lock:
                _refresh_state["active"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"started": True}


def run_daily_refresh():
    """One-shot refresh of every game's collection + wishlist prices."""
    with _refresh_lock:
        if _refresh_state["active"]:
            return
        _refresh_state["active"] = True

    def work():
        try:
            ts = now_iso()
            for g in GAMES:
                prov = provider_for(g)
                with db_lock, db() as conn:
                    ids = [r["scryfall_id"] for r in conn.execute(
                        "SELECT DISTINCT scryfall_id FROM cards WHERE game=?", (g,))]
                if ids and prov.has_api:
                    updated = _refresh_cards(ids, ts, g)
                    broadcast({"type": "price-done", "game": g,
                               "updated": updated})
            with db_lock, db() as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM wishlist")]
            alerts = _refresh_wishlist(rows, ts) if rows else []
            broadcast({"type": "wishlist-done", "alerts": alerts})
            broadcast({"type": "library-changed"})
        finally:
            with _refresh_lock:
                _refresh_state["active"] = False

    threading.Thread(target=work, daemon=True).start()


def auto_refresh_loop():
    """Once-a-day background price refresh (collection + wishlist)."""
    while True:
        try:
            last = _meta_get("last_auto_refresh")
            due = last is None or time.time() - float(last) >= AUTO_REFRESH_SEC
        except Exception:
            due = False
        if due:
            run_daily_refresh()
            _mark_refreshed()
        time.sleep(3600)  # re-check hourly


# "Plains 189", "spm 65", "The One Ring 001" — name-or-set + collector
# number at the end of the search query.
NUM_Q_RE = re.compile(r"^(.*?)\s*#?\s*(\d{1,4}[a-z]?)\s*$")


class BodyTooLarge(Exception):
    def __init__(self, limit):
        self.limit = limit


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, max_bytes=MAX_JSON_BODY):
        length = int(self.headers.get("Content-Length") or 0)
        if length > max_bytes:
            raise BodyTooLarge(max_bytes)
        return self.rfile.read(length) if length else b""

    def game_param(self):
        """The game a request targets (from ?game=, defaulting to MTG)."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        return (q.get("game") or "").strip().lower() or DEFAULT_GAME

    def send_file(self, relpath, ctype):
        path = os.path.join(STATIC_DIR, relpath)
        if not os.path.isfile(path):
            self.send_json({"error": "not found"}, 404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        try:
            if path == "/":
                self.send_file("home.html", "text/html; charset=utf-8")
            elif path in ("/index.html", "/library.html"):
                self.send_file("index.html", "text/html; charset=utf-8")
            elif path == "/home.js":
                self.send_file("home.js", "application/javascript")
            elif path == "/nav.js":
                self.send_file("nav.js", "application/javascript")
            elif path == "/card-modal.js":
                self.send_file("card-modal.js", "application/javascript")
            elif path == "/app.js":
                self.send_file("app.js", "application/javascript")
            elif path == "/icons.js":
                self.send_file("icons.js", "application/javascript")
            elif path == "/style.css":
                self.send_file("style.css", "text/css")
            elif path == "/cardback.jpg":
                self.send_file("cardback.jpg", "image/jpeg")
            elif path == "/pokemon-back.jpg":
                self.send_file("pokemon-back.jpg", "image/jpeg")
            elif path == "/yugioh-back.jpg":
                self.send_file("yugioh-back.jpg", "image/jpeg")
            elif path == "/insights.html":
                self.send_file("insights.html", "text/html; charset=utf-8")
            elif path == "/insights.js":
                self.send_file("insights.js", "application/javascript")
            elif path == "/chat.html":
                self.send_file("chat.html", "text/html; charset=utf-8")
            elif path == "/chat.js":
                self.send_file("chat.js", "application/javascript")
            elif path == "/api/insights":
                self.api_insights(query.get("game"))
            elif path == "/api/wishlist":
                self.api_wishlist(query.get("game"))
            elif path == "/api/wishlist/alerts":
                self.api_wishlist_alerts(query.get("game"))
            elif path == "/api/collection":
                self.api_collection(query.get("game"))
            elif path == "/api/search":
                self.api_search(query.get("q", ""), query.get("game"))
            elif path.startswith("/api/history/"):
                self.api_history(path.split("/")[-1], query.get("game"))
            elif path.startswith("/api/card/"):
                self.api_card(path.split("/")[-1], query.get("game"))
            elif path == "/api/events":
                self.api_events()
            elif path == "/api/qr":
                self.api_qr()
            elif path == "/api/info":
                self.api_info()
            elif path == "/api/export":
                self.api_export(query.get("game"))
            elif path == "/api/localdb":
                self.api_localdb(query.get("game"))
            elif path == "/api/img":
                api_img(self, query.get("u", ""))
            elif deckbuilder is not None and deckbuilder.handle_get(self, path, query):
                pass
            else:
                self.send_json({"error": "not found"}, 404)
        except BodyTooLarge as e:
            self.send_json({"error": "request too large (max %d bytes)" % e.limit}, 413)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/scan":
                self.api_scan()
            elif path == "/api/add":
                self.api_add()
            elif path == "/api/update":
                self.api_update()
            elif path == "/api/import":
                self.api_import()
            elif path == "/api/refresh-prices":
                self.api_refresh_prices()
            elif path == "/api/batch":
                self.api_batch()
            elif path == "/api/wishlist/add":
                self.api_wishlist_add()
            elif path == "/api/wishlist/update":
                self.api_wishlist_update()
            elif path == "/api/wishlist/remove":
                self.api_wishlist_remove()
            elif path == "/api/wishlist/bought":
                self.api_wishlist_bought()
            elif path == "/api/wishlist/refresh":
                self.api_wishlist_refresh()
            elif path == "/api/localdb/download":
                self.api_localdb_download()
            elif path == "/api/chat":
                self.api_chat()
            elif deckbuilder is not None and deckbuilder.handle_post(self, path):
                pass
            else:
                self.send_json({"error": "not found"}, 404)
        except BodyTooLarge as e:
            self.send_json({"error": "request too large (max %d bytes)" % e.limit}, 413)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    # -- live pairing / events
    def api_info(self):
        self.send_json({"url": phone_url(),
                        "http_url": f"http://{lan_ip()}:{PORT}",
                        "qr_available": segno is not None,
                        "https": TLS_AVAILABLE,
                        "ocr_backend": detect_ocr_backend(),
                        "game": DEFAULT_GAME,
                        "games": [{"id": g, "label": PROVIDERS[g].label}
                                  for g in GAMES],
                        "assistant": assistant is not None and assistant.available()})

    def api_chat(self):
        if assistant is None or not assistant.available():
            self.send_json({"error": "Assistant not installed \u2014 pip install "
                                     "cactus-needle, then restart"}, 501)
            return
        try:
            body = json.loads(self.read_body())
        except Exception:
            body = {}
        text = (body.get("text") or "").strip()
        if not text:
            self.send_json({"error": "empty message"}, 400)
            return
        self.send_json(assistant.ask(text))

    def api_qr(self):
        if segno is None:
            self.send_json({"error": "segno not installed"}, 501)
            return
        url = phone_url()
        buf = io.BytesIO()
        segno.make(url, error="m").save(
            buf, kind="svg", scale=6, border=2,
            dark="#e8e9ec", light=None, xmldecl=False)
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_events(self):
        q = queue.Queue(maxsize=64)
        with _subscribers_lock:
            _subscribers.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keepalive
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _subscribers_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    # -- api implementations
    def api_collection(self, game=None):
        game = (game or "").strip().lower()
        with db_lock, db() as conn:
            if game in GAMES:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM cards WHERE game=? ORDER BY name COLLATE NOCASE",
                    (game,))]
            else:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM cards ORDER BY name COLLATE NOCASE")]
        total = 0.0
        count = 0
        for r in rows:
            r["condition"] = r.get("condition") or "NM"
            mult = cond_mult(r["condition"])
            r["cond_mult"] = mult
            price = r["price_usd_foil"] if r["foil"] else r["price_usd"]
            r["unit_price"] = round((price or 0) * mult, 2)
            r["line_value"] = round((price or 0) * mult * r["quantity"], 2)
            total += r["line_value"]
            count += r["quantity"]
        self.send_json({"cards": rows, "total_value": round(total, 2),
                        "total_cards": count})

    def api_search(self, q, game=None):
        game = (game or "").strip().lower() or DEFAULT_GAME
        prov = provider_for(game)
        q = q.strip()
        if not q:
            self.send_json({"cards": []})
            return
        m = NUM_Q_RE.match(q)
        if m and m.group(2):
            head, num = m.group(1).strip(), m.group(2)
            cards = _search_numbered_local(head, num, game)
            if not cards and prov.has_api:
                try:
                    cards = prov.search_numbered(head, num)
                except Exception:
                    cards = []
            if cards:
                self.send_json({"cards": cards})
                return
        # local-first search — the offline index needs no API at all
        cards = local_search(q, game)
        if not cards and prov.has_api:
            try:
                cards = prov.search(q)
            except Exception:
                cards = None
        self.send_json({"cards": cards or []})

    def api_scan(self):
        game = self.game_param()
        data = self.read_body(MAX_UPLOAD)
        if not data:
            self.send_json({"error": "empty upload"}, 400)
            return
        suffix = ".jpg"
        ctype = self.headers.get("Content-Type", "")
        if "heic" in ctype or "heif" in ctype:
            suffix = ".heic"
        elif "png" in ctype:
            suffix = ".png"
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            result = identify_card(tmp, game)
        finally:
            os.unlink(tmp)
        if result.get("match"):
            sid = result["match"]["scryfall_id"]
            now = time.time()
            with _last_scan_lock:
                if _last_scan[0] != sid or now - _last_scan[1] > 8:
                    broadcast({"type": "scan", "game": game, "card": result["match"],
                               "method": result.get("method"),
                               "source": result.get("source")})
                _last_scan[0], _last_scan[1] = sid, now
        self.send_json(result)

    def api_add(self):
        game = self.game_param()
        body = json.loads(self.read_body())
        foil = 1 if body.get("foil") else 0
        qty = max(1, int(body.get("quantity", 1)))
        cond = body.get("condition") or "NM"
        if cond not in COND_MULT:
            cond = "NM"
        # The frontend usually already has the full card (from search or a
        # scan match) — use it directly so adding works instantly and even
        # when the card API is down. Only fall back to a lookup otherwise
        # (e.g. auto-add from live scan).
        card = body.get("card")
        if isinstance(card, dict) and card.get("scryfall_id") and card.get("name"):
            s = {k: card.get(k) for k in SUMMARY_KEYS}
            sid = s["scryfall_id"]
        else:
            sid = body["scryfall_id"]
            card = fetch_card(sid, game)
            if card is None:
                self.send_json({"error": "card not found"}, 404)
                return
            s = provider_for(game).summary(card)
        ts = now_iso()
        wl_match = None
        with db_lock, db() as conn:
            existing = conn.execute(
                "SELECT id, quantity FROM cards WHERE scryfall_id=? AND foil=? AND game=?",
                (sid, foil, game)).fetchone()
            if existing:
                conn.execute("UPDATE cards SET quantity=?, price_usd=?, "
                             "price_usd_foil=?, price_updated_at=?, back_image_uri=? "
                             "WHERE id=?",
                             (existing["quantity"] + qty, s["price_usd"],
                              s["price_usd_foil"], ts, s.get("back_image_uri"),
                              existing["id"]))
            else:
                conn.execute(
                    """INSERT INTO cards (scryfall_id, name, set_code, set_name,
                       collector_number, rarity, mana_cost, type_line, colors,
                       image_uri, scryfall_uri, back_image_uri, foil, quantity,
                       price_usd, price_usd_foil, price_updated_at, added_at,
                       condition, game)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sid, s["name"], s["set_code"], s["set_name"],
                     s["collector_number"], s["rarity"], s["mana_cost"],
                     s["type_line"], s["colors"], s["image_uri"],
                     s["scryfall_uri"], s.get("back_image_uri"), foil, qty,
                     s["price_usd"], s["price_usd_foil"], ts, ts, cond, game))
            conn.execute(
                "INSERT INTO price_history (scryfall_id, recorded_at, usd, usd_foil) "
                "VALUES (?,?,?,?)", (sid, ts, s["price_usd"], s["price_usd_foil"]))
            wl = conn.execute(
                "SELECT id, name, quantity FROM wishlist WHERE scryfall_id=? AND game=?",
                (sid, game)).fetchone()
            if wl:
                wl_match = {"id": wl["id"], "name": wl["name"],
                            "qty": wl["quantity"]}
        broadcast({"type": "add", "game": game, "name": s["name"], "quantity": qty,
                   "foil": bool(foil), "image_uri": s["image_uri"],
                   "unit_price": s["price_usd_foil"] if foil else s["price_usd"]})
        resp = {"ok": True, "name": s["name"]}
        if wl_match:
            resp["wishlist_match"] = wl_match
        self.send_json(resp)

    def api_update(self):
        """Quantity, foil flag, printing replacement, or delete."""
        game = self.game_param()
        body = json.loads(self.read_body())
        card_id = int(body["id"])
        if body.get("delete"):
            with db_lock, db() as conn:
                conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
            broadcast({"type": "library-changed"})
            self.send_json({"ok": True})
            return

        s = None
        new_sid = (body.get("scryfall_id") or "").strip()
        card = body.get("card")
        if isinstance(card, dict) and card.get("scryfall_id") and card.get("name"):
            # printing chosen in the UI — summary already in hand
            s = {k: card.get(k) for k in SUMMARY_KEYS}
            new_sid = s["scryfall_id"]
        elif new_sid:
            card = fetch_card(new_sid, game)
            if card is None:
                self.send_json({"error": "printing not found"}, 404)
                return
            s = provider_for(game).summary(card)
            new_sid = s["scryfall_id"]

        with db_lock, db() as conn:
            row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
            if not row:
                self.send_json({"error": "card not found"}, 404)
                return
            target_sid = new_sid or row["scryfall_id"]
            target_foil = 1 if body.get("foil") else row["foil"]
            merged = False
            if target_sid != row["scryfall_id"] or target_foil != row["foil"]:
                clash = conn.execute(
                    "SELECT id FROM cards WHERE scryfall_id=? AND foil=? AND game=? AND id<>?",
                    (target_sid, target_foil, row["game"], card_id)).fetchone()
                if clash:
                    conn.execute("UPDATE cards SET quantity=quantity+? WHERE id=?",
                                 (row["quantity"], clash["id"]))
                    conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
                    merged = True
                elif s:
                    conn.execute(
                        """UPDATE cards SET scryfall_id=?, name=?, set_code=?,
                           set_name=?, collector_number=?, rarity=?, mana_cost=?,
                           type_line=?, colors=?, image_uri=?, scryfall_uri=?,
                           back_image_uri=?, price_usd=?, price_usd_foil=?,
                           price_updated_at=? WHERE id=?""",
                        (s["scryfall_id"], s["name"], s["set_code"], s["set_name"],
                         s["collector_number"], s["rarity"], s["mana_cost"],
                         s["type_line"], s["colors"], s["image_uri"],
                         s["scryfall_uri"], s.get("back_image_uri"),
                         s["price_usd"], s["price_usd_foil"],
                         now_iso(), card_id))
                else:
                    conn.execute("UPDATE cards SET foil=? WHERE id=?",
                                 (target_foil, card_id))
            if not merged and "quantity" in body:
                qty = int(body["quantity"])
                if qty <= 0:
                    conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
                else:
                    conn.execute("UPDATE cards SET quantity=? WHERE id=?",
                                 (qty, card_id))
            if not merged and body.get("condition"):
                cond = body["condition"]
                if cond in COND_MULT:
                    conn.execute("UPDATE cards SET condition=? WHERE id=?",
                                 (cond, card_id))
            if not merged and "purchase_price" in body:
                pp = body.get("purchase_price")
                try:
                    pp = float(pp) if pp not in (None, "") else None
                except (TypeError, ValueError):
                    pp = None
                conn.execute("UPDATE cards SET purchase_price=? WHERE id=?",
                             (pp, card_id))
            if not merged and "for_trade" in body:
                conn.execute("UPDATE cards SET for_trade=? WHERE id=?",
                             (1 if body.get("for_trade") else 0, card_id))
            if not merged and "for_sale" in body:
                conn.execute("UPDATE cards SET for_sale=? WHERE id=?",
                             (1 if body.get("for_sale") else 0, card_id))
        broadcast({"type": "library-changed"})
        self.send_json({"ok": True})

    def api_refresh_prices(self):
        """Start a background refresh; progress streams over SSE."""
        res = run_price_refresh(self.game_param())
        if "error" in res:
            self.send_json(res, 409)
            return
        self.send_json({"ok": True, "started": res["started"],
                        "total": res.get("total", 0)})

    def api_history(self, sid, game=None):
        with db_lock, db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT recorded_at, usd, usd_foil FROM price_history "
                "WHERE scryfall_id=? ORDER BY recorded_at", (sid,))]
        self.send_json({"history": rows})

    # -- card details (oracle text + rulings)
    _rulings_cache = {}

    def api_card(self, sid, game=None):
        game = (game or "").strip().lower() or DEFAULT_GAME
        prov = provider_for(game)
        text = None
        with db_lock, db() as conn:
            row = conn.execute(
                "SELECT oracle_text FROM cards WHERE scryfall_id=? LIMIT 1",
                (sid,)).fetchone()
            if row:
                text = row["oracle_text"]
        if not text and prov.has_api:
            try:
                raw = prov.get_card(sid)
            except Exception:
                raw = None
            if raw:
                text = _oracle_text(raw)
                if text:
                    with db_lock, db() as conn:
                        conn.execute(
                            "UPDATE cards SET oracle_text=? WHERE scryfall_id=?",
                            (text, sid))
        rulings = self.fetch_rulings(sid) if game == "mtg" else []
        self.send_json({"oracle_text": text or "", "rulings": rulings})

    def fetch_rulings(self, sid):
        now = time.time()
        hit = self._rulings_cache.get(sid)
        if hit and now - hit[0] < 3600:
            return hit[1]
        try:
            P._throttle()
            req = urllib.request.Request(
                "https://api.scryfall.com/cards/%s/rulings" % sid,
                headers={"User-Agent": "LocalCardTracker/1.0 "
                                        "(personal collection tool)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                d = json.loads(resp.read())
            rulings = [{"date": r.get("published_at", ""),
                        "text": r.get("comment", "")}
                       for r in (d or {}).get("data", [])]
        except Exception:
            rulings = []
        self._rulings_cache[sid] = (time.time(), rulings)
        return rulings

    # -- batch edit (delete / set quantity)
    def api_batch(self):
        body = json.loads(self.read_body())
        ids = []
        for x in body.get("ids", []):
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                pass
        if not ids:
            self.send_json({"error": "no cards selected"}, 400)
            return
        marks = ",".join("?" * len(ids))
        with db_lock, db() as conn:
            if body.get("delete"):
                conn.execute("DELETE FROM cards WHERE id IN (%s)" % marks, ids)
            elif "quantity" in body:
                q = int(body["quantity"])
                if q <= 0:
                    conn.execute("DELETE FROM cards WHERE id IN (%s)" % marks, ids)
                else:
                    conn.execute(
                        "UPDATE cards SET quantity=? WHERE id IN (%s)" % marks,
                        [q] + ids)
            else:
                self.send_json({"error": "unknown action"}, 400)
                return
        broadcast({"type": "library-changed"})
        self.send_json({"ok": True, "count": len(ids)})

    # -- wishlist
    def api_wishlist(self, game=None):
        game = (game or "").strip().lower()
        with db_lock, db() as conn:
            if game in GAMES:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM wishlist WHERE game=? ORDER BY added_at DESC",
                    (game,))]
            else:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM wishlist ORDER BY added_at DESC")]
        alerts = 0
        for r in rows:
            price = r["price_usd_foil"] if r.get("foil") else r["price_usd"]
            r["price"] = price
            if r.get("target_price") is not None and price is not None \
                    and price <= r["target_price"]:
                alerts += 1
        self.send_json({"items": rows, "alert_count": alerts})

    def api_wishlist_add(self):
        game = self.game_param()
        body = json.loads(self.read_body())
        card = body.get("card")
        if not (isinstance(card, dict) and card.get("scryfall_id") and card.get("name")):
            self.send_json({"error": "card required"}, 400)
            return
        try:
            target = float(body.get("target_price")) if body.get("target_price") not in (None, "") else None
        except (TypeError, ValueError):
            target = None
        try:
            qty = max(1, int(body.get("quantity", 1)))
        except (TypeError, ValueError):
            qty = 1
        with db_lock, db() as conn:
            existing = conn.execute(
                "SELECT id FROM wishlist WHERE scryfall_id=? AND game=?",
                (card["scryfall_id"], game)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE wishlist SET quantity=?, target_price=?, price_usd=?, "
                    "price_usd_foil=?, image_uri=? WHERE id=?",
                    (qty, target, card.get("price_usd"), card.get("price_usd_foil"),
                     card.get("image_uri"), existing["id"]))
            else:
                conn.execute(
                    "INSERT INTO wishlist (scryfall_id, name, set_code, set_name, "
                    "collector_number, rarity, image_uri, price_usd, price_usd_foil, "
                    "target_price, quantity, added_at, game) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (card["scryfall_id"], card["name"], card.get("set_code"),
                     card.get("set_name"), card.get("collector_number"),
                     card.get("rarity"), card.get("image_uri"),
                     card.get("price_usd"), card.get("price_usd_foil"),
                     target, qty, now_iso(), game))
        broadcast({"type": "wishlist-changed"})
        self.send_json({"ok": True})

    def api_wishlist_update(self):
        body = json.loads(self.read_body())
        wl_id = int(body["id"])
        with db_lock, db() as conn:
            row = conn.execute("SELECT id FROM wishlist WHERE id=?", (wl_id,)).fetchone()
            if row is None:
                self.send_json({"error": "not found"}, 404)
                return
            if "target_price" in body:
                try:
                    target = float(body["target_price"]) if body["target_price"] not in (None, "") else None
                except (TypeError, ValueError):
                    target = None
                conn.execute("UPDATE wishlist SET target_price=? WHERE id=?",
                             (target, wl_id))
            if "quantity" in body:
                try:
                    qty = max(1, int(body["quantity"]))
                except (TypeError, ValueError):
                    qty = 1
                conn.execute("UPDATE wishlist SET quantity=? WHERE id=?",
                             (qty, wl_id))
        broadcast({"type": "wishlist-changed"})
        self.send_json({"ok": True})

    def api_wishlist_remove(self):
        body = json.loads(self.read_body())
        with db_lock, db() as conn:
            conn.execute("DELETE FROM wishlist WHERE id=?", (int(body["id"]),))
        broadcast({"type": "wishlist-changed"})
        self.send_json({"ok": True})

    def api_wishlist_bought(self):
        """Move wishlist item(s) into the collection (mark as bought)."""
        body = json.loads(self.read_body())
        wl_id = int(body["id"])
        to_collection = bool(body.get("to_collection", True))
        with db_lock, db() as conn:
            row = conn.execute(
                "SELECT * FROM wishlist WHERE id=?", (wl_id,)).fetchone()
            if row is None:
                self.send_json({"error": "not found"}, 404)
                return
            game = row["game"]
            if "qty" in body:
                try:
                    qty = max(1, int(body.get("qty") or 0))
                except (TypeError, ValueError):
                    qty = row["quantity"]
            else:
                qty = row["quantity"]
            qty = min(qty, row["quantity"])
            if to_collection:
                card = fetch_card(row["scryfall_id"], game)
                s = None
                if isinstance(card, dict):
                    try:
                        s = provider_for(game).summary(card)
                    except Exception:
                        s = None
                if not s or not s.get("name"):
                    s = {"scryfall_id": row["scryfall_id"],
                         "name": row["name"], "set_code": row["set_code"],
                         "set_name": row["set_name"],
                         "collector_number": row["collector_number"],
                         "rarity": row["rarity"], "mana_cost": "",
                         "type_line": "", "colors": "",
                         "image_uri": row["image_uri"], "scryfall_uri": "",
                         "price_usd": row["price_usd"],
                         "price_usd_foil": row["price_usd_foil"],
                         "back_image_uri": None}
                ts = now_iso()
                existing = conn.execute(
                    "SELECT id, quantity FROM cards WHERE scryfall_id=? AND foil=? AND game=?",
                    (row["scryfall_id"], 0, game)).fetchone()
                if existing:
                    conn.execute("UPDATE cards SET quantity=?, price_usd=?, "
                                 "price_usd_foil=?, price_updated_at=?, back_image_uri=? "
                                 "WHERE id=?",
                                 (existing["quantity"] + qty, s["price_usd"],
                                  s["price_usd_foil"], ts, s.get("back_image_uri"),
                                  existing["id"]))
                else:
                    conn.execute(
                        """INSERT INTO cards (scryfall_id, name, set_code, set_name,
                           collector_number, rarity, mana_cost, type_line, colors,
                           image_uri, scryfall_uri, back_image_uri, foil, quantity,
                           price_usd, price_usd_foil, price_updated_at, added_at, game)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (row["scryfall_id"], s["name"], s["set_code"],
                         s["set_name"], s["collector_number"], s["rarity"],
                         s["mana_cost"], s["type_line"], s["colors"],
                         s["image_uri"], s["scryfall_uri"],
                         s.get("back_image_uri"), 0, qty, s["price_usd"],
                         s["price_usd_foil"], ts, ts, game))
                conn.execute(
                    "INSERT INTO price_history (scryfall_id, recorded_at, usd, usd_foil) "
                    "VALUES (?,?,?,?)", (row["scryfall_id"], ts, s["price_usd"],
                                          s["price_usd_foil"]))
            new_qty = row["quantity"] - qty
            if new_qty <= 0:
                conn.execute("DELETE FROM wishlist WHERE id=?", (wl_id,))
                removed = True
            else:
                conn.execute("UPDATE wishlist SET quantity=? WHERE id=?",
                             (new_qty, wl_id))
                removed = False
        broadcast({"type": "wishlist-changed"})
        if to_collection:
            broadcast({"type": "library-changed"})
        self.send_json({"ok": True, "name": row["name"], "qty": qty,
                        "removed": bool(removed)})

    def api_wishlist_refresh(self):
        """Background refresh of wishlist prices; alerts broadcast over SSE."""
        res = run_wishlist_refresh(self.game_param())
        if "error" in res:
            self.send_json(res, 409)
            return
        self.send_json({"ok": True, "started": res["started"]})

    def api_wishlist_alerts(self, game=None):
        game = (game or "").strip().lower()
        with db_lock, db() as conn:
            if game in GAMES:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM wishlist WHERE game=?", (game,))]
            else:
                rows = [dict(r) for r in conn.execute("SELECT * FROM wishlist")]
        count = 0
        for r in rows:
            price = r["price_usd_foil"] if r.get("foil") else r["price_usd"]
            if r.get("target_price") is not None and price is not None \
                    and price <= r["target_price"]:
                count += 1
        self.send_json({"count": count})

    # collection insights
    def api_insights(self, game=None):
        from collections import defaultdict
        game = (game or "").strip().lower()

        def card_value(c):
            p = c.get("price_usd_foil") if c["foil"] else c.get("price_usd")
            return (p or 0) * c["quantity"] * cond_mult(c.get("condition"))

        with db_lock, db() as conn:
            if game in GAMES:
                cards = [dict(r) for r in conn.execute(
                    "SELECT * FROM cards WHERE game=?", (game,))]
            else:
                cards = [dict(r) for r in conn.execute("SELECT * FROM cards")]
            hist = [dict(r) for r in conn.execute(
                "SELECT recorded_at, scryfall_id, usd, usd_foil FROM price_history")]
            try:
                if game in GAMES:
                    wl_count = conn.execute(
                        "SELECT COUNT(*) FROM wishlist WHERE game=?", (game,)).fetchone()[0]
                else:
                    wl_count = conn.execute("SELECT COUNT(*) FROM wishlist").fetchone()[0]
            except Exception:
                wl_count = 0
        # value over time: forward-fill each card's price so every date
        # shows the whole collection valued at its latest known price on-or-
        # before that date (a partial refresh can't drag the curve down).
        by_sid = defaultdict(list)
        for r in hist:
            by_sid[r["scryfall_id"]].append(r)
        for v in by_sid.values():
            v.sort(key=lambda r: r["recorded_at"])
        dates = sorted({r["recorded_at"][:10] for r in hist})
        value_history = []
        for d in dates:
            total = 0.0
            for c in cards:
                use = "usd_foil" if c["foil"] else "usd"
                price = None
                for r in by_sid.get(c["scryfall_id"], []):
                    if r["recorded_at"][:10] > d:
                        break
                    v = r.get(use)
                    if v is not None:
                        price = v
                if price is not None:
                    total += price * c["quantity"] * cond_mult(c.get("condition"))
            value_history.append({"date": d, "value": round(total, 2)})

        total_value = round(sum(card_value(c) for c in cards), 2)
        total_cards = sum(c["quantity"] for c in cards)

        rarity = defaultdict(lambda: {"count": 0, "value": 0.0})
        colors = defaultdict(int)
        sets = defaultdict(lambda: {"count": 0, "value": 0.0})
        for c in cards:
            v = card_value(c)
            k = c.get("rarity") or "?"
            rarity[k]["count"] += c["quantity"]
            rarity[k]["value"] += v
            cols = c.get("colors") or ""
            if len(cols) > 1:
                colors["multicolor"] += c["quantity"]
            elif cols:
                colors[cols] += c["quantity"]
            else:
                colors["colorless"] += c["quantity"]
            sk = c.get("set_name") or c.get("set_code") or "?"
            sets[sk]["count"] += c["quantity"]
            sets[sk]["value"] += v

        top_cards = sorted(cards, key=card_value, reverse=True)[:10]
        for c in top_cards:
            c["line_value"] = round(card_value(c), 2)
        recent = sorted(cards, key=lambda c: c.get("added_at") or "", reverse=True)[:10]

        # set completion vs offline index (when available)
        set_progress = []
        load_local(game)
        local_cards = _state_for(game).get("cards")
        if local_cards:
            set_totals = defaultdict(set)
            for c in local_cards:
                set_totals[(c.get("set_code") or "").lower()].add(
                    str(c.get("collector_number") or ""))
            owned = defaultdict(set)
            for c in cards:
                owned[(c.get("set_code") or "").lower()].add(
                    str(c.get("collector_number") or ""))
            by_set = defaultdict(lambda: {"count": 0, "value": 0.0})
            name_of_code = {}
            for c in cards:
                code = (c.get("set_code") or "").lower()
                v = card_value(c)
                by_set[code]["count"] += c["quantity"]
                by_set[code]["value"] += v
                name_of_code[code] = c.get("set_name") or code
            for code, info in sorted(by_set.items(), key=lambda x: -x[1]["value"])[:14]:
                total = len(set_totals.get(code, set()))
                own = len(owned.get(code, set()))
                if total:
                    set_progress.append({"name": name_of_code.get(code, code),
                                         "owned": own, "total": total,
                                         "pct": round(own / total * 100, 1)})

        # most-played commanders (deck extension present)
        commanders = []
        if deckbuilder is not None:
            try:
                with db_lock, db() as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT name, SUM(quantity) AS q FROM deck_cards "
                        "WHERE role='commander' GROUP BY name ORDER BY q DESC LIMIT 8")]
                commanders = [{"name": r["name"], "qty": r["q"]} for r in rows]
            except Exception:
                commanders = []

        # deck win/loss records + head-to-head matchups (deck extension present)
        matchups = {"records": [], "matchups_by": []}
        if deckbuilder is not None:
            try:
                with db_lock, db() as conn:
                    decks = [dict(r) for r in conn.execute(
                        "SELECT id, name FROM decks")]
                    matches = [dict(r) for r in conn.execute(
                        "SELECT deck_id, result, opponent FROM deck_matches")]
                rec = {d["id"]: {"name": d["name"], "wins": 0, "losses": 0}
                       for d in decks}
                opp = {}
                for m in matches:
                    r = rec.get(m["deck_id"])
                    if r is not None:
                        if m["result"] == "win":
                            r["wins"] += 1
                        else:
                            r["losses"] += 1
                    if m.get("opponent"):
                        k = m["opponent"].strip().lower()
                        a = opp.get(k)
                        if a is None:
                            a = {"commander": m["opponent"].strip(),
                                 "wins": 0, "losses": 0}
                            opp[k] = a
                        if m["result"] == "win":
                            a["wins"] += 1
                        else:
                            a["losses"] += 1
                records = []
                for r in rec.values():
                    total = r["wins"] + r["losses"]
                    r["winrate"] = round(r["wins"] / total * 100) if total else None
                    records.append(r)
                records.sort(key=lambda r: -(r["wins"] + r["losses"]))
                matchup_list = sorted(opp.values(),
                                      key=lambda a: -(a["wins"] + a["losses"]))
                for a in matchup_list:
                    total_a = a["wins"] + a["losses"]
                    a["winrate"] = round(a["wins"] / total_a * 100) if total_a else None
                matchups = {
                    "records": records,
                    "matchups_by": matchup_list[:10],
                }
            except Exception:
                matchups = {"records": [], "matchups_by": []}

        # cost basis, trade/sale value, and price movers
        total_paid = 0.0
        trade_value = sale_value = 0.0
        for c in cards:
            v = card_value(c)
            if c.get("purchase_price") is not None:
                total_paid += c["purchase_price"] * c["quantity"]
            if c.get("for_trade"):
                trade_value += v
            if c.get("for_sale"):
                sale_value += v
        movers = []
        for c in cards:
            rows = sorted(by_sid.get(c["scryfall_id"], []),
                          key=lambda r: r["recorded_at"])
            if len(rows) < 2:
                continue
            last = rows[-1]
            prev = None
            for r in reversed(rows[:-1]):
                if r["recorded_at"] != last["recorded_at"]:
                    prev = r
                    break
            if prev is None:
                continue
            old, new = prev.get("usd"), last.get("usd")
            if not old or not new:
                continue
            pct = (new - old) / old * 100
            if abs(pct) < 0.5:
                continue
            movers.append({"scryfall_id": c["scryfall_id"], "name": c["name"],
                           "set_name": c.get("set_name"),
                           "set_code": c.get("set_code"),
                           "collector_number": c.get("collector_number"),
                           "rarity": c.get("rarity"),
                           "image_uri": c.get("image_uri"),
                           "scryfall_uri": c.get("scryfall_uri"),
                           "price_usd": c.get("price_usd"),
                           "price_usd_foil": c.get("price_usd_foil"),
                           "pct": round(pct, 1), "old": round(old, 2),
                           "new": round(new, 2)})
        movers.sort(key=lambda m: m["pct"], reverse=True)
        seen, picked = set(), []
        for m in movers[:6] + movers[-6:]:
            if m["scryfall_id"] not in seen:
                seen.add(m["scryfall_id"])
                picked.append(m)
        movers = picked

        self.send_json({
            "total_value": total_value, "total_cards": total_cards,
            "wishlist_count": wl_count, "deck_count": len(commanders),
            "total_paid": round(total_paid, 2),
            "gain": round(total_value - total_paid, 2) if total_paid else None,
            "trade_value": round(trade_value, 2),
            "sale_value": round(sale_value, 2),
            "movers": movers,
            "value_history": value_history,
            "rarity": [{"rarity": k, "count": v["count"], "value": round(v["value"], 2)}
                        for k, v in sorted(rarity.items())],
            "colors": sorted(colors.items()),
            "top_sets": [{"name": k, "count": v["count"], "value": round(v["value"], 2)}
                          for k, v in sorted(sets.items(), key=lambda x: -x[1]["value"])[:10]],
            "top_cards": top_cards, "recent": recent,
            "set_progress": set_progress, "commanders": commanders,
            "matchups": matchups,
        })

    # -- export / import
    def api_export(self, game=None):
        game = (game or "").strip().lower()
        with db_lock, db() as conn:
            if game in GAMES:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM cards WHERE game=? ORDER BY name COLLATE NOCASE",
                    (game,))]
            else:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM cards ORDER BY name COLLATE NOCASE")]
        fields = ["scryfall_id", "name", "set_code", "set_name",
                  "collector_number", "rarity", "foil", "quantity",
                  "condition", "purchase_price", "for_trade", "for_sale",
                  "price_usd", "price_usd_foil", "added_at", "game"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})
        body = buf.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="collection-%s.csv"'
                         % time.strftime("%Y%m%d"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_import(self):
        """CSV import (header: scryfall_id, name, set_code, collector_number,
        foil, quantity; a plain list of card names also works).
        """
        game = self.game_param()
        prov = provider_for(game)
        data = self.read_body(MAX_UPLOAD)
        text = data.decode("utf-8-sig", "replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        if rows and not any(any(k in r for k in ("name", "scryfall_id", "set_code"))
                            for r in rows):
            rows = [{"name": line.strip()} for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")]
        if not rows:
            self.send_json({"error": "empty CSV"}, 400)
            return

        summary_map = {}
        if prov.can_batch:
            sids = [r.get("scryfall_id", "").strip() for r in rows
                    if r.get("scryfall_id", "").strip()]
            for i in range(0, len(sids), 75):
                chunk = sids[i:i + 75]
                try:
                    res = prov.post_json("/cards/collection",
                                         {"identifiers": [{"id": s} for s in chunk]})
                    for c in (res or {}).get("data", []):
                        summary_map[c["id"]] = prov.summary(c)
                except Exception:
                    pass

        resolved = []
        errors = []
        for r in rows:
            if (r.get("name") or "").strip().startswith("#"):
                continue  # # comment lines
            sid = (r.get("scryfall_id") or "").strip()
            row_game = (r.get("game") or "").strip().lower() or game
            foil = 1 if str(r.get("foil", "0")).strip().lower() in (
                "1", "true", "yes", "y", "foil", "✦") else 0
            try:
                qty = max(1, int(r.get("quantity") or 1))
            except (TypeError, ValueError):
                qty = 1
            cond = (r.get("condition") or "").strip().upper()
            if cond not in COND_MULT:
                cond = "NM"
            pp = r.get("purchase_price")
            try:
                pp = float(pp) if pp not in (None, "") else None
            except (TypeError, ValueError):
                pp = None
            trade = 1 if str(r.get("for_trade", "")).strip().lower() in (
                "1", "true", "yes", "y") else 0
            sale = 1 if str(r.get("for_sale", "")).strip().lower() in (
                "1", "true", "yes", "y") else 0
            s = summary_map.get(sid)
            if s is None and sid:
                card = fetch_card(sid, row_game)
                if card:
                    s = provider_for(row_game).summary(card)
            if s is None and row_game == "riftbound":
                s = provider_for("riftbound").resolve_row(r)
            if s is None:
                set_code = (r.get("set_code") or "").strip()
                num = (r.get("collector_number") or "").strip()
                name = (r.get("name") or "").strip()
                if set_code and num:
                    s = local_exact_match(set_code, num, row_game)
                    if s is None and provider_for(row_game).has_api:
                        try:
                            s = provider_for(row_game).exact_lookup(set_code, num)
                        except Exception:
                            s = None
                if s is None and name:
                    s = local_name_match(name, row_game)
                    if s is None and provider_for(row_game).has_api:
                        try:
                            s = provider_for(row_game).name_lookup(name)
                        except Exception:
                            s = None
            if s is None:
                errors.append(name or sid or "?")
                continue
            resolved.append((s, foil, qty, cond, pp, trade, sale, row_game))

        added = updated = 0
        ts = now_iso()
        with db_lock, db() as conn:
            for s, foil, qty, cond, pp, trade, sale, row_game in resolved:
                existing = conn.execute(
                    "SELECT id FROM cards WHERE scryfall_id=? AND foil=? AND game=?",
                    (s["scryfall_id"], foil, row_game)).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE cards SET quantity=quantity+?, price_usd=?, "
                        "price_usd_foil=?, price_updated_at=?, back_image_uri=?, "
                        "purchase_price=?, for_trade=?, for_sale=? WHERE id=?",
                        (qty, s["price_usd"], s["price_usd_foil"], ts,
                         s.get("back_image_uri"), pp, trade, sale,
                         existing["id"]))
                    updated += 1
                else:
                    conn.execute(
                        """INSERT INTO cards (scryfall_id, name, set_code,
                           set_name, collector_number, rarity, mana_cost,
                           type_line, colors, image_uri, scryfall_uri,
                           back_image_uri, foil, quantity, price_usd,
                           price_usd_foil, price_updated_at, added_at, condition,
                           purchase_price, for_trade, for_sale, game)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (s["scryfall_id"], s["name"], s["set_code"],
                         s["set_name"], s["collector_number"], s["rarity"],
                         s["mana_cost"], s["type_line"], s["colors"],
                         s["image_uri"], s["scryfall_uri"],
                         s.get("back_image_uri"), foil, qty, s["price_usd"],
                         s["price_usd_foil"], ts, ts, cond, pp, trade, sale,
                         row_game))
                    added += 1
        backup_db()
        broadcast({"type": "library-changed"})
        self.send_json({"ok": True, "added": added, "updated": updated,
                        "skipped": len(errors), "errors": errors[:5]})

    # -- offline database
    def api_localdb(self, game=None):
        game = (game or "").strip().lower() or DEFAULT_GAME
        meta = local_meta(game)
        cards_file, _ = _local_files(game)
        dstate = _download_state_for(game)
        with _local_lock:
            available = "cards" in _state_for(game) or os.path.exists(cards_file)
            downloading = dstate["active"]
            phase = dstate["phase"]
            done_mb = dstate["done_mb"]
            total_mb = dstate["total_mb"]
            cards = dstate["cards"]
        self.send_json({
            "available": available,
            "card_count": (meta or {}).get("card_count", 0),
            "downloaded_at": (meta or {}).get("downloaded_at"),
            "downloading": downloading, "phase": phase,
            "done_mb": done_mb, "total_mb": total_mb, "cards": cards})

    def api_localdb_download(self):
        game = self.game_param()
        with _local_lock:
            if _download_state_for(game)["active"]:
                self.send_json({"error": "download already running"}, 409)
                return
        download_localdb(game)
        self.send_json({"ok": True, "started": True})


TLS_AVAILABLE = True  # set for real in __main__


def phone_url():
    """URL the phone should open — lands straight on the library/scanner page."""
    if TLS_AVAILABLE:
        return f"https://{lan_ip()}:{TLS_PORT}/library.html"
    return f"http://{lan_ip()}:{PORT}/library.html"


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_cert(ip):
    """Self-signed cert for the HTTPS listener (regenerated if the LAN IP
    changed). Browsers show a one-time trust warning; after 'visit anyway'
    the live camera works. Returns False when openssl isn't available —
    the server then runs HTTP-only (photo-upload scanning still works)."""
    marker = os.path.join(ROOT, ".cert_ip")
    if (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)
            and os.path.exists(marker)
            and open(marker).read().strip() == ip):
        return True
    cnf = os.path.join(ROOT, ".openssl.cnf")
    with open(cnf, "w") as f:
        f.write(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            "[dn]\nCN=Card Tracker\n"
            "[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1,IP:%s\n" % ip)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "3650", "-keyout", KEY_FILE, "-out", CERT_FILE,
             "-config", cnf],
            check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as e:
        sys.stderr.write("could not generate TLS cert (%s) — HTTPS disabled, "
                         "live camera scanning unavailable\n" % e)
        return False
    finally:
        if os.path.exists(cnf):
            os.unlink(cnf)
    with open(marker, "w") as f:
        f.write(ip)
    return True


class TrackerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True  # don't let open SSE streams block shutdown


if __name__ == "__main__":
    init_db()
    if not backup_today_exists():
        try:
            backup_db()
        except Exception as e:
            sys.stderr.write("backup failed: %s\n" % e)
    ip = lan_ip()
    backend = detect_ocr_backend()

    tls_ok = ensure_cert(ip)
    TLS_AVAILABLE = tls_ok
    if tls_ok:
        try:
            https_server = TrackerHTTPServer(("0.0.0.0", TLS_PORT), Handler)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(CERT_FILE, KEY_FILE)
            https_server.socket = ctx.wrap_socket(https_server.socket, server_side=True)
            threading.Thread(target=https_server.serve_forever, daemon=True).start()
        except OSError as e:
            tls_ok = TLS_AVAILABLE = False
            sys.stderr.write("HTTPS listener failed (%s) — live camera "
                             "scanning unavailable\n" % e)

    server = TrackerHTTPServer(("0.0.0.0", PORT), Handler)
    print("Card tracker running — MTG · Pokémon TCG · Yu-Gi-Oh! (OCR: %s):"
          % backend)
    print(f"  This computer: http://localhost:{PORT}/")
    if tls_ok:
        print(f"  Phone:         {phone_url()}  (same Wi-Fi; accept "
              "the cert warning once — needed for live camera)")
    else:
        print(f"  Phone:         {phone_url()}  (photo scanning only — "
              "install openssl for live camera)")
    if backend == "none":
        print("  WARNING: no OCR backend found — scanning disabled. Install "
              "Tesseract (brew/apt/choco install tesseract).")
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    server.serve_forever()
