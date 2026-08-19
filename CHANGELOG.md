# Changelog

All notable changes to **Local TCG Scanner** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.0] — 2026-08-18

### Added
- **Multi-game support** — MTG, Pokémon TCG and Yu-Gi-Oh! now run
  simultaneously from a single server. A game switcher in the header
  selects the active library/scanner — no more `CARD_TRACKER_GAME` restart
  to change games.
- **Per-game everything** — a `game` column tags every card, and search,
  scanning, the library, insights, wishlist, price history and CSV
  export/import are all scoped to the selected game.
- **Yu-Gi-Oh! support** — new provider built on
  [YGOPRODeck](https://ygoprodeck.com): bulk offline index (every card,
  printing and price in one request), exact set+number lookup, and
  per-printing TCGplayer prices.
- **Pokémon scanning & library** — full scanning, library and price
  tracking. The offline index comes from the
  [pokemon-tcg-data](https://github.com/PokemonTCG/pokemon-tcg-data) GitHub
  repo as a single tarball download — **no API rate limit** for
  searching/indexing.
- **Accurate Pokémon pricing via TCGplayer** — prices come from
  [tcgcsv.com](https://tcgcsv.com), TCGplayer's own bulk price feed (no API
  key, no rate limit). Cards map to TCGplayer products by the set's
  `ptcgoCode` + collector number, so the library shows exact TCGplayer
  market prices. pokemontcg.io remains an automatic fallback.
- **OCR scanning for all three games** — `identify_card` is now
  game-aware, including Yu-Gi-Oh! set-code parsing (e.g. `FOTB-EN043`).
- **Per-game card backs** — the 3D flip uses the correct card back for
  Pokémon and Yu-Gi-Oh! (double-faced MTG cards still show their real back).
- **Per-game external links** — "view on" links resolve to Scryfall (MTG),
  TCGplayer (Pokémon) and YGOPRODeck (Yu-Gi-Oh!).
- **Self-check** — `test_games.py` covers the multi-game parsing/summary
  logic (`python3 test_games.py`).

### Changed
- Renamed the project from **Local MTG Scanner** to **Local TCG Scanner**;
  app branding and titles updated throughout.
- Pokémon offline index now includes TCGplayer prices and set `ptcgoCode`
  so search results show prices immediately.
- Price refresh for Pokémon uses the cached TCGplayer bulk map — instant,
  no per-card API calls and no rate limits.

### Fixed
- Pokémon `get_card` unwrapped the response envelope so per-card price
  refresh actually returned data.
- Pokémon "view on TCGplayer" link no longer points at `#`; it opens a
  real TCGplayer page/search.

### Removed
- The `CARD_TRACKER_GAME` env var is no longer needed to switch games (all
  games run at once); it is retained only as a fallback default.

## [1.2.1] — 2026-08-12
- Fuzzy card matching + routing fixes.
- Optional needle2 assistant bubble.

## [1.2.0] — 2026-08-12
- Purchase price &amp; gain/loss, trade/sell flags, daily auto-refresh.

## [1.1.1] — 2026-08-12
- Card condition (NM/LP/MP/HP/D) with condition-adjusted pricing.

## [1.1.0] — 2026-08-12
- Full visual redesign per the design handoff (collector's case +
  parchment themes).

## [1.0.0] — 2026-08-11
- Initial release: local, subscription-free Magic: The Gathering scanner,
  library, insights, wishlist and deck builder.

[1.3.0]: https://github.com/McDandle/local-mtg-scanner/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/McDandle/local-mtg-scanner/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/McDandle/local-mtg-scanner/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/McDandle/local-mtg-scanner/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/McDandle/local-mtg-scanner/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/McDandle/local-mtg-scanner/releases/tag/v1.0.0
