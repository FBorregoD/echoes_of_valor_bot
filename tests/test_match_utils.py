"""
Unit tests for match_utils.get_all_misreported_matches.
Mocks get_division_matches / is_division_sheet / get_latest_week_from_sheets
so the test only exercises the looping + deduplication logic.
"""
from unittest.mock import patch

import match_utils
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
