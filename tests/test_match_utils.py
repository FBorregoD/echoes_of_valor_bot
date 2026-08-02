"""
Unit tests for match_utils.get_all_misreported_matches, plus a regression
test for the 2026-07-17 cache-race finding in get_tournament_sheets, and
for the 2026-07-17 normalize_name non-Latin-name collision bug.
Mocks get_division_matches / is_division_sheet / get_latest_week_from_sheets
so the get_all_misreported_matches tests only exercise the looping +
deduplication logic.
"""
import threading
import time
from unittest.mock import patch

import match_utils
from match_utils import normalize_name
from match_utils import get_all_misreported_matches


def _match(week, player1, player2, division="Diamond", misreported=True):
    return {
        "week": week, "player1": player1, "player2": player2,
        "division": division, "check": "NO", "score1": 1, "score2": 1,
        "total": 2, "misreported": misreported,
    }


def test_no_matches_when_no_data():
    with patch.object(match_utils, "is_division_sheet", return_value=True), \
         patch.object(match_utils, "get_latest_week_from_sheets", return_value=-1):
        result = get_all_misreported_matches({"Diamond": object()})
    assert result == []


def test_same_match_reported_twice_is_deduplicated():
    """
    get_division_matches(sheets, div, week) is called once per week 1..latest.
    A match from week 1 shows up as "current" when target_week=1, and again
    as "pending" for every later target_week — that's the duplication the
    dedup step in get_all_misreported_matches must collapse.
    """
    stale_match = _match(week=1, player1="Player One", player2="Player Two")

    def fake_get_division_matches(sheets_dict, div, week):
        if week == 1:
            return [stale_match], []
        return [], [stale_match]

    with patch.object(match_utils, "is_division_sheet", return_value=True), \
         patch.object(match_utils, "get_latest_week_from_sheets", return_value=3), \
         patch.object(match_utils, "get_division_matches", side_effect=fake_get_division_matches):
        result = get_all_misreported_matches({"Diamond": object()})

    assert len(result) == 1
    assert result[0]["player1"] == "Player One"


def test_distinct_misreported_matches_are_kept():
    match_a = _match(week=1, player1="Player One", player2="Player Two")
    match_b = _match(week=2, player1="Player Three", player2="Player Four")

    def fake_get_division_matches(sheets_dict, div, week):
        if week == 1:
            return [match_a], []
        if week == 2:
            return [match_b], [match_a]
        return [], [match_a, match_b]

    with patch.object(match_utils, "is_division_sheet", return_value=True), \
         patch.object(match_utils, "get_latest_week_from_sheets", return_value=3), \
         patch.object(match_utils, "get_division_matches", side_effect=fake_get_division_matches):
        result = get_all_misreported_matches({"Diamond": object()})

    assert len(result) == 2
    players = {(m["player1"], m["player2"]) for m in result}
    assert players == {("Player One", "Player Two"), ("Player Three", "Player Four")}


def test_non_misreported_matches_are_excluded():
    ok_match = _match(week=1, player1="Player One", player2="Player Two", misreported=False)

    def fake_get_division_matches(sheets_dict, div, week):
        return [ok_match], []

    with patch.object(match_utils, "is_division_sheet", return_value=True), \
         patch.object(match_utils, "get_latest_week_from_sheets", return_value=1), \
         patch.object(match_utils, "get_division_matches", side_effect=fake_get_division_matches):
        result = get_all_misreported_matches({"Diamond": object()})

    assert result == []


def test_non_division_sheets_are_skipped():
    def fake_get_division_matches(sheets_dict, div, week):
        raise AssertionError("should not be called for non-division sheets")

    with patch.object(match_utils, "is_division_sheet", return_value=False), \
         patch.object(match_utils, "get_latest_week_from_sheets", return_value=2), \
         patch.object(match_utils, "get_division_matches", side_effect=fake_get_division_matches):
        result = get_all_misreported_matches({"Hero builds": object()})

    assert result == []


# ── Cache race regression (2026-07-17) ──────────────────────────────────────

def test_get_tournament_sheets_concurrent_calls_dont_stampede():
    """
    Regression test for the 2026-07-17 finding: asyncio.to_thread makes
    get_tournament_sheets callable from multiple worker threads concurrently.
    Without the per-URL lock, several threads racing a cold/expired cache
    entry would all miss it and all hit the network. The lock should make
    every thread but the first wait, then reuse the first thread's result.
    """
    match_utils._memory_cache.clear()
    match_utils._fetch_locks.clear()

    call_count = 0
    call_count_lock = threading.Lock()

    def fake_fetch(url):
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        time.sleep(0.1)  # long enough that concurrent threads would overlap without the lock
        return {"Diamond": "fake_data"}

    with patch.object(match_utils.ggx, "data_fromAllSheets", side_effect=fake_fetch):
        threads = [
            threading.Thread(target=match_utils.get_tournament_sheets, args=("http://fake-url", False))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert call_count == 1


# ── normalize_name (2026-07-17 non-Latin collision + dash-stripping bugs) ───

def test_normalize_name_preserves_distinct_non_latin_names():
    """
    Regression test: normalize_name used to run .encode('ascii', 'ignore'),
    which silently deletes any non-Latin-script character. Two different
    players with e.g. Japanese or Cyrillic names both collapsed to the same
    empty string, colliding wherever normalize_name is used as a dedup/lookup
    key (hero-build lookups, misreported-match dedup, !subscribe's player_key).
    """
    assert normalize_name("田中") != ""
    assert normalize_name("Иванов") != ""
    assert normalize_name("田中") != normalize_name("Иванов")
    # Case-folding still applies to scripts that have case (Cyrillic does, CJK doesn't).
    assert normalize_name("Иванов") == normalize_name("иванов")


def test_normalize_name_folds_latin_accents():
    """Accented Latin names should still fold to their base ASCII letters."""
    assert normalize_name("José") == "jose"
    assert normalize_name("François") == "francois"


def test_normalize_name_converts_typographic_dashes_to_hyphen():
    """
    Regression test: en-dash/em-dash/minus were being deleted (not converted)
    because the ASCII-strip ran before the dash-normalization regex, so a
    player registered with a "smart" dash (common from copy-pasting out of
    Word/Docs) wouldn't match the same name typed with a plain hyphen.
    """
    assert normalize_name("Scor–ium Prime") == normalize_name("Scor-ium Prime")
    assert normalize_name("Scor—ium Prime") == normalize_name("Scor-ium Prime")
    assert "-" in normalize_name("Scor–ium Prime")


def test_normalize_name_collapses_whitespace_and_lowercases():
    assert normalize_name("  DblDubz   -   Lil Nikki  ") == "dbldubz-lil nikki"


def test_normalize_name_empty_input():
    assert normalize_name("") == ""
    assert normalize_name(None) == ""
