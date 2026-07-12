"""
Tests for help.py (doesn't need Discord) and TournamentCommands._get_weeks_per_tournament
(week-resolution logic; mocks Google Sheets and the scheduler DB).
"""
import json
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