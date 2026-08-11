# Local MTG Scanner — local, subscription-free collection tracker

Scan **Magic: The Gathering** cards with your phone's camera, keep the
library on your own computer. No accounts, no cloud, no premium tier. Card
data and prices come from the free [Scryfall](https://scryfall.com) API.
(Experimental support for Pokémon TCG and CSV-defined games is included via
`CARD_TRACKER_GAME`, but MTG is the focus.)

<p align="center"><img src="docs/demo.gif" width="420" alt="Library view on a phone — stats, filters, and card grid"></p>

- **Live scanning** — point your phone at cards; each one is OCR'd, matched
  to its *exact printing* (set code + collector number), and streamed to
  your computer's library view in real time. Auto-add mode + a
  [3D-printable scanning rig](https://makerworld.com/en/models/1152661)
  make batch-scanning a shoebox genuinely fast.
- **Insights page** — collection value over time, value by rarity, cards by
  color, top sets, set-completion progress (vs. the offline index), most
  valuable cards, most-played commanders, and recent additions.
- **Wishlist with price alerts** — track cards you want to buy with a target
  price; when a refresh finds one at or under target you get an alert
  badge and a toast.
- **Local-first** — the library is a single SQLite file. An optional offline
  card database (~60 MB) makes scanning and search work with no internet.
- **Prices & history** — current prices per printing (foil and non-foil),
  snapshotted over time with per-card charts. Automatic backups.
- **Library tools** — grid/list views, grouping with expand/collapse-all,
  filters, sorting, batch select/edit/delete, oracle text &amp; rulings in
  the card editor, CSV export/import, draggable 3D card flip.
- **Deck Builder** — build decks out of your own cards: every deck slot is
  compared against the collection so you see exactly what you own vs. what
  you need to order, with a priced buy list, Archidekt/precon/decklist
  import, legality checks, mana curve, and playtest draws.

## Requirements

| | macOS | Linux | Windows |
|---|---|---|---|
| Python 3.9+ | ✅ built-in/brew | ✅ | ✅ [python.org](https://python.org) |
| OCR | **Apple Vision** (auto-built, best quality) or Tesseract | **Tesseract**: `sudo apt install tesseract-ocr` | **Tesseract**: `choco install tesseract` (or the [UB-Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki)) |
| Live camera (optional) | `openssl` (built-in) | `openssl` (built-in) | openssl (bundled with Git for Windows) |
| QR pairing (optional) | `pip install segno` | same | same |

On macOS the server compiles a tiny Swift helper (`ocr.swift`) on first run to
use Apple's Vision framework — noticeably better than Tesseract on card
photos, and it reads iPhone HEIC natively. Everywhere else (or if the build
fails) it falls back to Tesseract automatically. The startup banner tells you
which OCR backend and URLs are active.

## Run it

```bash
python3 server.py
```

Then open `http://localhost:8484` on your computer: the **home page** shows
your portfolio value, recent additions and quick access to the **Library**
(scanner + collection), **Insights** (analytics) and **Decks** (deck
builder). Click **📱 Pair** on the Library page and point your phone's
camera at the QR code. Your browser will warn about the self-signed
certificate once — proceed (that's what enables the live camera on the
phone). Both devices must be on the same Wi-Fi.

> **Note:** the server is unauthenticated and LAN-visible by design — run it
> on a trusted home network, not a public one.

Configuration is via environment variables (all optional):

```bash
CARD_TRACKER_GAME=mtg|pokemon|riftbound   # default mtg
CARD_TRACKER_PORT=8484                    # HTTP
CARD_TRACKER_TLS_PORT=8485                # HTTPS (live camera)
CARD_TRACKER_TESS_LANGS=eng               # Tesseract language packs
```

Auto-start: macOS users can adapt `com.mtgtracker.server.plist` (edit the
two absolute paths, copy to `~/Library/LaunchAgents/`, `launchctl load` it);
Linux users can wrap `python3 server.py` in a systemd user unit; Windows
users can drop `start.bat` into shell:startup.

## How scanning works

Each camera frame is OCR'd locally. The card **title** and the **bottom
collector line** (e.g. `M 0026` + `TLE • EN`) are parsed; set code + number
gives the exact printing (badge: *exact print ✓*), with fuzzy name matching
as fallback. Matching order: offline database first (instant), then the
game's API. The search box understands `Plains 189` / `tle 26` style
queries for pinpointing exact printings manually.

Live camera streaming requires HTTPS (browser security), which is why the
phone URL is `https://<your-ip>:8485` with a self-signed cert. If openssl
isn't available the server runs HTTP-only and the phone can still scan via
single photos.

## Offline database (recommended)

Click **Download** in the offline-database box once. Scanning and search
then match locally — instant, and fully offline. Card images are cached in
`img_cache/` as you browse. Click **Update** to pull new sets.

## Deck Builder

The tracker includes a **Deck Builder** on its own page
(`/decks.html`, linked from the 🂠 button in the top bar): import a deck,
and see — for every slot — how many copies you already own in the
collection vs. how many you need to order.

- **Archidekt import** — paste any Archidekt deck URL (or just its ID) and
  the deck is pulled in: commanders and sideboards are mapped to the right
  slots, every card is indexed against your scanned collection, and the
  buy list for the missing cards is one click away.
- **Archidekt search** *(best-effort)* — a search box for popular decks is
  wired in, but Archidekt removed their public search API; if their search
  is down the UI tells you so and importing by URL still works.
- **Preconstructed deck search** — search the official MTGJSON decklists
  (deck name, set, or commander) and import any precon with exact
  printings. The first search builds a local index automatically
  (one-time ~250 MB download, cached for fast updates); afterwards
  searching and importing are instant/offline. Covers commander, duel,
  planechase, archenemy and vanguard precons — new sets are one "sync"
  away.
- **Resource planner** — a cross-deck view that aggregates demand: for
  every card, which decks want it, how many you own, and where the
  shortages are (e.g. *two decks each want 4 Lightning Bolt, you own 2*).
- **Legality engine** — one click checks the deck against its format:
  ban lists, card counts, Commander singleton + color identity, 4-of
  limits, Pauper rarity. Legalities are fetched from Scryfall once and
  cached on the deck, so repeat checks are instant.
- **Mana curve / colors / avg CMC** — deck stats drawer with a mana curve
  chart, color-pip counts, average CMC, lands and creature counts.
- **Deck viewer** — an Archidekt-style deck view (🃏 View in the editor)
  with list and visual modes: cards grouped by type with per-card prices,
  line totals, owned/need, commander marking, and a quick summary strip.
- **Playtest mode** — draw simulated opening hands (with mulligans) and
  compute hypergeometric odds (*chance to see a card by turn N*).
- **Deck value over time** — deck value snapshotted automatically (about
  once an hour) plus a manual “record now” button, charted per deck.
- **Owned vs. need** — the exact printing you own counts first; if you own
  the same card in a *different* printing, those copies count too (a deck
  slot doesn't care about the art).
- **Cheapest-printing buy list** — toggle the buy list to substitute the
  cheapest printing of each missing card (from the offline index).
- **Paste decklist** — paste a decklist from the web (`4 Lightning Bolt`,
  `SB: 3 Duress`, or a `Sideboard:` section). Names resolve to the newest
  printing automatically; switch printings in the editor afterwards. You
  can also add the list *straight to your collection* (no deck created).
- **Add deck to collection** — merge every card in a deck into your
  scanned collection with one click — the fast way to index preconstructed
  commander decks (import the precon list, then “Add to collection”).
- **Live** — when a phone scan adds cards, open decks refresh their owned
  counts in real time.

Decks live in `collection.db` (tables `decks`, `deck_cards`) and survive
restarts; all deck data is stored locally like the rest of the library.
The module is fully optional at runtime — delete `deckbuilder.py` and
`static/decks.*` and the tracker runs identically with the deck UI hidden.

Deck data sources: [Archidekt](https://archidekt.com) (deck import) and
[MTGJSON](https://mtgjson.com) (preconstructed decklists) — both free;
please be considerate with request volume.

## Data & files

Everything lives next to `server.py`:

- `collection.db` — your library (SQLite). Back up this one file.
- `backups/` — automatic snapshots (at startup, before refreshes/imports).
- `local_cards.json`, `img_cache/` — optional offline database + images.
- CSV export/import from the Library header — works with plain name lists
  and CSVs from other tools too.

For custom/unsupported games (`CARD_TRACKER_GAME=riftbound`), provide a
`riftbound_cards.csv` with columns
`name,set_code,set_name,collector_number,rarity,type_line,image_uri,price_usd,price_usd_foil`
and use the offline-database Download button to index it.

## Credits & legal

Card data and images come from [Scryfall](https://scryfall.com) (MTG) and
[pokemontcg.io](https://pokemontcg.io) (Pokémon) — please respect their API
guidelines (this server rate-limits and sends a proper User-Agent). QR codes
by [segno](https://github.com/heuer/segno).

Local MTG Scanner is unofficial Fan Content permitted under the
[Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy).
Not approved/endorsed by Wizards. Portions of the materials used (including
card images and the Magic card back) are property of Wizards of the Coast.
© Wizards of the Coast LLC.

Code is MIT licensed — see [LICENSE](LICENSE). Built by McDandle with the
use of Claude and DeepSeek.
