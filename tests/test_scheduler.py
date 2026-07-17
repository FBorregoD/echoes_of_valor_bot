"""
Unit tests for SchedulerCog._dispatch — specifically the blocking-I/O
regression from the 2026-07-17 production incident (see test_commands.py
for the sibling test on TournamentCommands._get_most_recent_week).

_dispatch is exercised directly via SchedulerCog._dispatch(fake_self, ...)
rather than through a real SchedulerCog instance, since the constructor
starts a discord.ext.tasks background loop that isn't needed here and
isn't safe to spin up against a plain MagicMock bot.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scheduler import SchedulerCog


def make_fake_self(tournaments, default_week=2):
    cog_ref = MagicMock()
    cog_ref.tournaments = tournaments
    cog_ref.default_week = default_week
    return SimpleNamespace(cog_ref=cog_ref, bot=MagicMock())


@pytest.mark.asyncio
async def test_dispatch_post_divisions_sheets_refetch_does_not_block_event_loop():
    """
    Regression test for the 2026-07-17 production incident
    ("Shard ID None heartbeat blocked for more than 10 seconds"). After
    posting divisions with auto-advance on, _dispatch rebuilds
    sheets_by_tourney by calling get_tournament_sheets directly — this is
    the exact code path that froze the bot in production. It must run via
    asyncio.to_thread so a slow/hanging Google Sheets fetch can't stall
    the Discord heartbeat or any other concurrent task.
    """
    tournaments = [{"name": "Meridian Ascension", "alias": "MA", "url": "fake_url"}]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    def slow_fetch(url, force_refresh=False):
        time.sleep(0.25)
        return {}

    async def heartbeat():
        for _ in range(5):
            await asyncio.sleep(0.05)

    with patch("scheduler.run_post_divisions", new=AsyncMock(return_value=(1, [], []))), \
         patch("scheduler.advance_auto_week", new=AsyncMock()), \
         patch("match_utils.get_tournament_sheets", side_effect=slow_fetch):
        start = time.monotonic()
        await asyncio.gather(
            SchedulerCog._dispatch(
                fake_self, "post_divisions", destination,
                {"tournament": "MA", "week": "3"}, task_id=1, auto_week=3, end_week=None,
            ),
            heartbeat(),
        )
        elapsed = time.monotonic() - start

    # Concurrent (fixed): ~0.25s, dominated by the longer of the two.
    # Sequential (blocked event loop): ~0.25 + 0.25 = 0.5s.
    assert elapsed < 0.4


@pytest.mark.asyncio
async def test_dispatch_notify_all_sheets_refetch_does_not_block_event_loop():
    """Same regression, other branch: notify_all's auto-advance sheets refetch."""
    tournaments = [{"name": "Echoes of Valor", "alias": "EoV", "url": "fake_url"}]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    def slow_fetch(url, force_refresh=False):
        time.sleep(0.25)
        return {}

    async def heartbeat():
        for _ in range(5):
            await asyncio.sleep(0.05)

    with patch("scheduler.run_notify_all", new=AsyncMock(return_value=(0, 0))), \
         patch("scheduler.advance_auto_week", new=AsyncMock()), \
         patch("match_utils.get_tournament_sheets", side_effect=slow_fetch):
        start = time.monotonic()
        await asyncio.gather(
            SchedulerCog._dispatch(
                fake_self, "notify_all", destination,
                {"week": "3"}, task_id=2, auto_week=3, end_week=None,
            ),
            heartbeat(),
        )
        elapsed = time.monotonic() - start

    assert elapsed < 0.4


@pytest.mark.asyncio
async def test_dispatch_post_divisions_still_advances_week_correctly():
    """Functional check alongside the timing test: values still flow through correctly."""
    tournaments = [{"name": "Meridian Ascension", "alias": "MA", "url": "fake_url"}]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    with patch("scheduler.run_post_divisions", new=AsyncMock(return_value=(3, [], []))), \
         patch("scheduler.advance_auto_week", new=AsyncMock()) as mock_advance, \
         patch("match_utils.get_tournament_sheets", return_value={}):
        await SchedulerCog._dispatch(
            fake_self, "post_divisions", destination,
            {"tournament": "MA", "week": "3"}, task_id=1, auto_week=3, end_week=8,
        )

    destination.send.assert_awaited_once()
    assert "Posted to 3 divisions" in destination.send.call_args[0][0]
    mock_advance.assert_awaited_once()
    _, kwargs = mock_advance.call_args
    assert kwargs["task_id"] == 1
    assert kwargs["current_week"] == 3
    assert kwargs["end_week"] == 8
    assert "Meridian Ascension" in kwargs["sheets_by_tourney"]
