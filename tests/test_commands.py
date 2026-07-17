"""
Tests for help.py (doesn't need Discord) and TournamentCommands._get_weeks_per_tournament
(week-resolution logic; mocks Google Sheets and the scheduler DB).
"""
import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from help import get_help_embed
from commands import TournamentCommands

def test_help_embed_main():
    embed = get_help_embed("@TestBot")
    assert embed.title == "📖 Bot Commands"
    assert any("!matches" in field.value for field in embed.fields)

def test_help_embed_specific():
    embed = get_help_embed("@TestBot", "matches")
    assert "!matches / !m" in embed.title


# ── _get_weeks_per_tournament ───────────────────────────────────────────────

@pytest.fixture
def cog():
    tournaments = [{"name": "Echoes of Valor", "alias": "EoV", "url": "fake_url"}]
    return TournamentCommands(MagicMock(), tournaments, mapping_sheet_url="fake", default_week=2)

CONTEXT = {"tournament": None}

def _task(tournament, current_week, week_param="default", action="post_divisions"):
    return {
        "action": action,
        "params": json.dumps({"tournament": tournament, "week": week_param}),
        "current_week": current_week,
    }

@pytest.mark.asyncio
async def test_weeks_no_task_uses_latest_plus_one(cog):
    with patch("commands.get_tournament_sheets", return_value={}), \
         patch("commands.get_latest_week_from_sheets", return_value=3), \
         patch("scheduler.list_tasks", return_value=[]):
        weeks = await cog._get_weeks_per_tournament(CONTEXT, guild_id=123)
    assert weeks["EoV"] == 4

@pytest.mark.asyncio
async def test_weeks_no_matches_falls_back_to_default(cog):
    with patch("commands.get_tournament_sheets", return_value={}), \
         patch("commands.get_latest_week_from_sheets", return_value=-1), \
         patch("scheduler.list_tasks", return_value=[]):
        weeks = await cog._get_weeks_per_tournament(CONTEXT, guild_id=123)
    assert weeks["EoV"] == 2  # default_week

@pytest.mark.asyncio
async def test_weeks_auto_advance_shows_last_published_week(cog):
    """Regression test for the 2026-07-10 bug: showed current_week instead of current_week - 1."""
    task = _task("EoV", current_week=5)
    with patch("commands.get_tournament_sheets", return_value={}), \
         patch("commands.get_latest_week_from_sheets", return_value=3), \
         patch("scheduler.list_tasks", return_value=[task]):
        weeks = await cog._get_weeks_per_tournament(CONTEXT, guild_id=123)
    assert weeks["EoV"] == 4  # max(1, 5 - 1)

@pytest.mark.asyncio
async def test_weeks_auto_advance_never_goes_below_one(cog):
    task = _task("EoV", current_week=1)
    with patch("commands.get_tournament_sheets", return_value={}), \
         patch("commands.get_latest_week_from_sheets", return_value=3), \
         patch("scheduler.list_tasks", return_value=[task]):
        weeks = await cog._get_weeks_per_tournament(CONTEXT, guild_id=123)
    assert weeks["EoV"] == 1  # max(1, 1 - 1)

@pytest.mark.asyncio
async def test_weeks_season_finished_uses_latest_plus_one(cog):
    task = _task("EoV", current_week=-1)
    with patch("commands.get_tournament_sheets", return_value={}), \
         patch("commands.get_latest_week_from_sheets", return_value=6), \
         patch("scheduler.list_tasks", return_value=[task]):
        weeks = await cog._get_weeks_per_tournament(CONTEXT, guild_id=123)
    assert weeks["EoV"] == 7

@pytest.mark.asyncio
async def test_weeks_fixed_task_uses_week_param(cog):
    task = _task("EoV", current_week=None, week_param="9")
    with patch("commands.get_tournament_sheets", return_value={}), \
         patch("commands.get_latest_week_from_sheets", return_value=3), \
         patch("scheduler.list_tasks", return_value=[task]):
        weeks = await cog._get_weeks_per_tournament(CONTEXT, guild_id=123)
    assert weeks["EoV"] == 9

@pytest.mark.asyncio
async def test_weeks_task_matched_by_full_tournament_name(cog):
    task = _task("Echoes of Valor", current_week=5)
    with patch("commands.get_tournament_sheets", return_value={}), \
         patch("commands.get_latest_week_from_sheets", return_value=3), \
         patch("scheduler.list_tasks", return_value=[task]):
        weeks = await cog._get_weeks_per_tournament(CONTEXT, guild_id=123)
    assert weeks["EoV"] == 4

@pytest.mark.asyncio
async def test_weeks_dm_context_lists_tasks_without_guild_filter(cog):
    with patch("commands.get_tournament_sheets", return_value={}), \
         patch("commands.get_latest_week_from_sheets", return_value=3), \
         patch("scheduler.list_tasks", return_value=[]) as mock_list:
        await cog._get_weeks_per_tournament(CONTEXT, guild_id=None)
    mock_list.assert_called_once_with()

@pytest.mark.asyncio
async def test_weeks_force_week_overrides_everything(cog):
    task = _task("EoV", current_week=5)
    with patch("commands.get_tournament_sheets", return_value={}), \
         patch("commands.get_latest_week_from_sheets", return_value=3), \
         patch("scheduler.list_tasks", return_value=[task]):
        weeks = await cog._get_weeks_per_tournament(CONTEXT, guild_id=123, force_week=42)
    assert weeks["EoV"] == 42


# ── Blocking-I/O regression (2026-07-17 production incident) ───────────────

@pytest.mark.asyncio
async def test_get_most_recent_week_does_not_block_event_loop(cog):
    """
    Regression test for the 2026-07-17 production incident: get_tournament_sheets
    does a synchronous, un-timeout-ed HTTP call under the hood. Called directly
    from async code it froze the whole event loop for 20+ seconds, blocking the
    Discord heartbeat ("Shard ID None heartbeat blocked") and risking a forced
    gateway disconnect. It must run via asyncio.to_thread so a slow/hanging
    fetch can't stall anything else the bot is doing concurrently.
    """
    def slow_fetch(url, force_refresh=False):
        time.sleep(0.25)
        return {}

    async def heartbeat():
        for _ in range(5):
            await asyncio.sleep(0.05)

    with patch("commands.get_tournament_sheets", side_effect=slow_fetch), \
         patch("commands.get_latest_week_from_sheets", return_value=3):
        start = time.monotonic()
        await asyncio.gather(cog._get_most_recent_week(CONTEXT), heartbeat())
        elapsed = time.monotonic() - start

    # Concurrent (fixed): ~0.25s, dominated by the longer of the two.
    # Sequential (blocked event loop): ~0.25 + 0.25 = 0.5s.
    assert elapsed < 0.4