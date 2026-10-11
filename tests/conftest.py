import asyncio
import time

import pandas as pd
import pytest

from match_utils import (
    is_division_sheet, get_division_matches, get_player_matches,
    normalize_name, player_matches, get_latest_week
)


@pytest.fixture
def assert_does_not_block_event_loop():
    """
    Shared regression-test helper for the 2026-07-17 production incident
    (a blocking Google Sheets fetch froze the event loop and blocked the
    Discord heartbeat). Runs `coro` concurrently with a lightweight
    heartbeat coroutine and asserts they complete in roughly the time of
    the longer one (concurrent) rather than their sum (sequential — i.e.
    the event loop was blocked).
    """
    async def _assert(coro, *, threshold: float = 0.4,
                       heartbeat_ticks: int = 5, heartbeat_interval: float = 0.05):
        async def heartbeat():
            for _ in range(heartbeat_ticks):
                await asyncio.sleep(heartbeat_interval)

        start = time.monotonic()
        await asyncio.gather(coro, heartbeat())
        elapsed = time.monotonic() - start
        assert elapsed < threshold, f"elapsed {elapsed:.3f}s >= {threshold}s threshold — event loop was likely blocked"

    return _assert

def test_is_division_sheet():
    # A valid division sheet must have SCHEDULE and then match rows.
    valid = pd.DataFrame({
        'A': ['SCHEDULE', 'Week 1', None],
        'C': [None, 'Player - Hero1', 'Player - Hero2'],
        'D': [None, 'Other - Hero3', 'Other - Hero4'],
    })
    assert is_division_sheet(valid)

    # Missing SCHEDULE
    invalid = pd.DataFrame({
        'A': ['Tier 0', 'Week 1'],
        'C': ['', 'Player - Hero1'],
        'D': ['', 'Other - Hero3'],
    })
    assert not is_division_sheet(invalid)

    # SCHEDULE but no match rows
    invalid2 = pd.DataFrame({
        'A': ['SCHEDULE', 'Week 1'],
        'C': ['', 'Not a match'],
        'D': ['', 'Not either'],
    })
    assert not is_division_sheet(invalid2)


def test_get_division_matches(sample_sheets):
    current, pending = get_division_matches(sample_sheets, 'Diamond', week=1)
    # Week 1 has one match with OK, so it should be in current
    assert len(current) == 1
    assert current[0]['check'] == 'OK'
    assert current[0]['player1'] == 'DblDubz - Lil Nikki'
    assert current[0]['player2'] == 'DblDubz - Grok'
    # Pending: Week 2 match where check is not OK and week < target_week (target is 2?)
    current2, pending2 = get_division_matches(sample_sheets, 'Diamond', week=2)
    # Week 1 is OK, Week 2 match check is NO → pending? Wait: week<target_week means week1 < 2, but week1 is OK so not pending. week2 == target_week, check is 'NO' but it's current week, not pending. So pending should be empty.
    assert not pending2
    # If target_week is 3, then week2 match (NO) should be pending.
    curr3, pend3 = get_division_matches(sample_sheets, 'Diamond', week=3)
    assert not curr3  # no matches for week 3
    assert len(pend3) == 1
    assert pend3[0]['week'] == 2
    assert pend3[0]['check'] == 'NO'
    assert not pend3[0].get('misreported')  # score is 1+1=2, correct

def test_get_player_matches(sample_sheets):
    current, pending = get_player_matches(sample_sheets, 'DblDubz', week=1)
    # Week 1 has DblDubz vs DblDubz, so it should find one match
    assert len(current) == 1
    assert current[0]['player1'] == 'DblDubz - Lil Nikki'
    assert current[0]['player2'] == 'DblDubz - Grok'

    # Looking for week 3, week 2 match should be pending
    curr, pend = get_player_matches(sample_sheets, 'Eindeloos', week=3)
    assert not curr
    assert len(pend) == 1
    assert pend[0]['week'] == 2

def test_normalize_name():
    assert normalize_name("DblDubz - Lil Nikki") == "dbldubz - lil nikki"
    assert normalize_name("  Hello – World  ") == "hello - world"

def test_player_matches():
    assert player_matches("DblDubz", "DblDubz - Grok")
    assert not player_matches("lil", "DblDubz - Lil Nikki")
    assert player_matches("Lil", "DblDubz - Lil Nikki")

def test_get_latest_week(sample_sheets):
    # Our sample has Week 1 and Week 2 rows with match data → max week = 2
    # Simulate tournament list
    tournaments = [{'name': 'Test', 'url': 'fake_url'}]
    # Need to mock get_tournament_sheets to return sample_sheets; we'll patch
    from unittest.mock import patch
    with patch('match_utils.get_tournament_sheets', return_value=sample_sheets):
        # get_latest_week now takes guild_id, but we can pass 0
        week = get_latest_week(tournaments, guild_id=0)
        assert week == 2