"""
Unit tests for ScheduleCommands: schedule_remove (id / all / tournament=),
schedule_listall, and the require_guild() check on the server-only
subcommands (add / list / remove / info). Mocks Discord (ctx) and the
scheduler DB functions.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord.ext import commands

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


def make_task(task_id, guild_id, tournament=None, action="post_divisions", current_week=None,
              weekday=0, hour=9, minute=0, tz="UTC", interval_minutes=None):
    params = {"tournament": tournament} if tournament else {}
    return {
        "id": task_id, "guild_id": guild_id, "action": action, "params": json.dumps(params),
        "current_week": current_week, "weekday": weekday, "hour": hour, "minute": minute,
        "tz": tz, "interval_minutes": interval_minutes,
    }


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


# ── !schedule listall ───────────────────────────────────────────────────────

async def _listall(cog, ctx):
    await ScheduleCommands.schedule_listall.callback(cog, ctx)


def make_dm_ctx():
    """A ctx with no guild at all, like a DM invocation."""
    ctx = MagicMock()
    ctx.guild = None
    # Deterministically pass the is_bot_admin() check so these tests isolate
    # the guild-required check instead of relying on MagicMock's default
    # (accidentally truthy) `in` behavior.
    ctx.author.id = 999
    ctx.bot.admin_user_ids = [999]
    ctx.send = AsyncMock()
    return ctx


async def _run_checks(command, ctx):
    """Run every check attached to `command` against ctx — bypassed when a
    test calls `.callback` directly, which is why the require_guild()/
    is_bot_admin() checks need to be exercised explicitly here."""
    for check in command.checks:
        await check(ctx)


@pytest.mark.asyncio
async def test_listall_no_tasks(cog):
    ctx = make_dm_ctx()
    with patch("schedule_commands.list_tasks", return_value=[]):
        await _listall(cog, ctx)
    assert "No scheduled tasks stored" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_listall_works_from_a_dm_without_touching_ctx_guild(cog):
    """Regression test: schedule_list crashes in DMs because it reads ctx.guild.id;
    listall must never touch ctx.guild at all."""
    ctx = make_dm_ctx()
    rows = [make_task(1, guild_id=100, tournament="EoV")]
    with patch("schedule_commands.list_tasks", return_value=rows):
        await _listall(cog, ctx)
    ctx.send.assert_awaited_once()
    embed = ctx.send.call_args.kwargs["embed"]
    assert embed.footer.text == "Total: 1 task(s) across 1 server(s)."


@pytest.mark.asyncio
async def test_listall_groups_tasks_by_guild(cog):
    ctx = make_dm_ctx()
    rows = [
        make_task(1, guild_id=100, tournament="EoV"),
        make_task(2, guild_id=100, tournament="MA"),
        make_task(3, guild_id=200, tournament="EoV"),
    ]
    known_guild = MagicMock()
    known_guild.name = "Test Server"
    cog.bot.get_guild = MagicMock(side_effect=lambda gid: known_guild if gid == 100 else None)

    with patch("schedule_commands.list_tasks", return_value=rows):
        await _listall(cog, ctx)

    embed = ctx.send.call_args.kwargs["embed"]
    field_names = [f.name for f in embed.fields]
    assert "Test Server (`100`)" in field_names
    assert "Unknown server (`200`)" in field_names
    # The guild with 2 tasks should list both IDs in its field value
    guild_100_field = next(f for f in embed.fields if f.name == "Test Server (`100`)")
    assert "ID 1" in guild_100_field.value
    assert "ID 2" in guild_100_field.value


# ── require_guild() check on server-only subcommands ────────────────────────
# These checks run in discord.py's Command.checks, not the callback body, so
# calling `.callback(...)` directly (as the tests above do) bypasses them —
# the checks have to be invoked explicitly to be exercised here.

@pytest.mark.asyncio
async def test_add_requires_guild(cog):
    ctx = make_dm_ctx()
    with pytest.raises(commands.CheckFailure, match="only works inside a server"):
        await _run_checks(ScheduleCommands.schedule_add, ctx)


@pytest.mark.asyncio
async def test_list_requires_guild_and_hints_listall(cog):
    ctx = make_dm_ctx()
    with pytest.raises(commands.CheckFailure) as exc_info:
        await _run_checks(ScheduleCommands.schedule_list, ctx)
    msg = str(exc_info.value)
    assert "only works inside a server" in msg
    assert "listall" in msg


@pytest.mark.asyncio
async def test_remove_requires_guild(cog):
    ctx = make_dm_ctx()
    with pytest.raises(commands.CheckFailure, match="only works inside a server"):
        await _run_checks(ScheduleCommands.schedule_remove, ctx)


@pytest.mark.asyncio
async def test_info_requires_guild(cog):
    ctx = make_dm_ctx()
    with pytest.raises(commands.CheckFailure, match="only works inside a server"):
        await _run_checks(ScheduleCommands.schedule_info, ctx)
