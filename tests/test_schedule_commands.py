"""
Unit tests for ScheduleCommands.schedule_remove — the id / all / tournament=
variants. Mocks Discord (ctx) and the scheduler DB functions.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from schedule_commands import ScheduleCommands


@pytest.fixture
def cog():
    with patch("schedule_commands.init_db"):
        return ScheduleCommands(bot=MagicMock())


def make_ctx(guild_id=100):
    ctx = MagicMock()
    ctx.guild.id = guild_id
    ctx.send = AsyncMock()
    return ctx


def make_task(task_id, guild_id, tournament=None, action="post_divisions"):
    params = {"tournament": tournament} if tournament else {}
    return {"id": task_id, "guild_id": guild_id, "action": action, "params": json.dumps(params)}


async def _remove(cog, ctx, arg):
    await ScheduleCommands.schedule_remove.callback(cog, ctx, arg=arg)


@pytest.mark.asyncio
async def test_remove_all_deletes_every_task_for_guild(cog):
    ctx = make_ctx(guild_id=100)
    rows = [make_task(1, 100), make_task(2, 100)]
    with patch("schedule_commands.list_tasks", return_value=rows) as mock_list, \
         patch("schedule_commands.remove_task") as mock_remove:
        await _remove(cog, ctx, "all")
    mock_list.assert_called_once_with(guild_id=100)
    assert mock_remove.call_count == 2
    mock_remove.assert_any_call(1)
    mock_remove.assert_any_call(2)
    assert "2" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_all_no_tasks(cog):
    ctx = make_ctx(guild_id=100)
    with patch("schedule_commands.list_tasks", return_value=[]), \
         patch("schedule_commands.remove_task") as mock_remove:
        await _remove(cog, ctx, "all")
    mock_remove.assert_not_called()
    assert "No scheduled tasks" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_by_tournament_filters_case_insensitively(cog):
    ctx = make_ctx(guild_id=100)
    rows = [
        make_task(1, 100, tournament="EoV"),
        make_task(2, 100, tournament="MA"),
        make_task(3, 100, tournament="eov"),
    ]
    with patch("schedule_commands.list_tasks", return_value=rows), \
         patch("schedule_commands.remove_task") as mock_remove:
        await _remove(cog, ctx, "tournament=EoV")
    assert mock_remove.call_count == 2
    mock_remove.assert_any_call(1)
    mock_remove.assert_any_call(3)


@pytest.mark.asyncio
async def test_remove_by_tournament_no_match(cog):
    ctx = make_ctx(guild_id=100)
    rows = [make_task(1, 100, tournament="MA")]
    with patch("schedule_commands.list_tasks", return_value=rows), \
         patch("schedule_commands.remove_task") as mock_remove:
        await _remove(cog, ctx, "tournament=EoV")
    mock_remove.assert_not_called()
    assert "No scheduled tasks found" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_by_tournament_missing_value(cog):
    ctx = make_ctx(guild_id=100)
    with patch("schedule_commands.remove_task") as mock_remove:
        await _remove(cog, ctx, "tournament=")
    mock_remove.assert_not_called()
    assert "Specify a tournament" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_by_numeric_id_still_works(cog):
    ctx = make_ctx(guild_id=100)
    row = make_task(5, 100)
    with patch("schedule_commands.get_task", return_value=row), \
         patch("schedule_commands.remove_task") as mock_remove:
        await _remove(cog, ctx, "5")
    mock_remove.assert_called_once_with(5)
    assert "removed successfully" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_by_id_unknown_task(cog):
    ctx = make_ctx(guild_id=100)
    with patch("schedule_commands.get_task", return_value=None), \
         patch("schedule_commands.remove_task") as mock_remove:
        await _remove(cog, ctx, "999")
    mock_remove.assert_not_called()
    assert "No task found" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_by_id_wrong_guild(cog):
    ctx = make_ctx(guild_id=100)
    row = make_task(5, guild_id=999)
    with patch("schedule_commands.get_task", return_value=row), \
         patch("schedule_commands.remove_task") as mock_remove:
        await _remove(cog, ctx, "5")
    mock_remove.assert_not_called()
    assert "doesn't belong" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_invalid_argument(cog):
    ctx = make_ctx(guild_id=100)
    with patch("schedule_commands.remove_task") as mock_remove:
        await _remove(cog, ctx, "banana")
    mock_remove.assert_not_called()
    assert "Invalid argument" in ctx.send.call_args[0][0]
