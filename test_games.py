#!/usr/bin/env python3
"""Self-check for the multi-game card parsing/summary logic (no network).

Run: python3 test_games.py
Covers: Yu-Gi-Oh! set-code splitting, per-game OCR collector-line parsing,
and the YGO / Pokémon summary mapping (the parts most likely to regress).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def test_ygo_set_parts():
    cases = {
        "FOTB-EN043": ("FOTB", "043"),
        "SDK-007": ("SDK", "007"),
        "LON-E088": ("LON", "088"),
        "ORCS-ENSE1": ("ORCS", "SE1"),
        "DB49": ("DB", "49"),
        "5DS2-EN012": ("5DS2", "012"),
        "25YC-ENP05": ("25YC", "P05"),
    }
    for code, want in cases.items():
        got = server._ygo_set_parts(code)
        assert got == want, "%s: %r != %r" % (code, got, want)


def test_parse_print_info():
    def lines(*pairs):
        return [{"text": t, "y": y, "confidence": 0.9,
                 "x": 0, "w": 0.1, "h": 0.05} for t, y in pairs]

    sc, num = server.parse_print_info(
        lines(("Sol Ring", 0.95), ("TLE \u2022 EN", 0.05), ("0026", 0.03)), "mtg")
    assert (sc, num) == ("tle", "26"), (sc, num)

    sc, num = server.parse_print_info(
        lines(("Pikachu", 0.95), ("189/165", 0.05)), "pokemon")
    assert num == "189", (sc, num)

    sc, num = server.parse_print_info(
        lines(("Dark Magician", 0.95), ("FOTB-EN043", 0.04)), "yugioh")
    assert (sc, num) == ("FOTB", "43"), (sc, num)


def test_ygo_summary():
    raw = {
        "id": 46986414, "name": "Dark Magician", "type": "Normal Monster",
        "card_sets": [{"set_name": "2016 Mega-Tins", "set_code": "CT13-EN003",
                       "set_rarity": "Ultra Rare", "set_price": "6.97"}],
        "card_prices": [{"tcgplayer_price": "0.30"}],
        "card_images": [{"image_url":
                         "https://images.ygoprodeck.com/images/cards/46986414.jpg"}],
        "ygoprodeck_url": "https://ygoprodeck.com/card/dark-magician-4003",
    }
    s = server.provider_for("yugioh").summary(raw)
    assert s["name"] == "Dark Magician"
    assert (s["set_code"], s["collector_number"]) == ("CT13", "003")
    assert s["price_usd"] == 6.97
    assert s["scryfall_id"] == "46986414-ct13en003-ultrarare"
    assert server.image_ok_any(s["image_uri"])


def test_pokemon_summary():
    raw = {
        "id": "base1-1", "name": "Alakazam", "supertype": "Pok\u00e9mon",
        "subtypes": ["Stage 2"], "number": "1", "rarity": "Rare Holo",
        "set": {"id": "base1", "name": "Base", "releaseDate": "1999/01/09"},
        "images": {"large": "https://images.pokemontcg.io/base1/1_hires.png"},
        "tcgplayer": {"url": "x",
                      "prices": {"holofoil": {"market": 66.33, "mid": 64.29}}},
        "types": ["Psychic"],
    }
    s = server.provider_for("pokemon").summary(raw)
    assert s["name"] == "Alakazam" and s["set_code"] == "base1"
    assert s["price_usd_foil"] == 66.33
    assert server.image_ok_any(s["image_uri"])


if __name__ == "__main__":
    test_ygo_set_parts()
    test_parse_print_info()
    test_ygo_summary()
    test_pokemon_summary()
    print("OK \u2014 multi-game parsing/summary checks passed")
