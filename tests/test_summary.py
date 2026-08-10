"""
Unit tests for the !summary / !sum command (2026-08-09): commands.py's
summary_command / _build_and_send_player_summary, plus its DM-only check.

Patches commands.get_player_characters / find_player_division_rank /
get_player_division_record directly rather than building real sheet
DataFrames — those functions' own behavior (roster prefix-matching, rank
lookup, played/won counting) is already covered in test_match_utils.py.
These tests focus on the assembly/formatting logic in commands.py instead.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord.ext import commands

from commands import TournamentCommands


@pytest.fixture
def cog():
    tournaments = [{"name": "Echoes of Valor", "alias": "EoV", "url": "fake_url"}]
    return TournamentCommands(MagicMock(), tournaments, mapping_sheet_url="fake", default_week=2)


def make_dm_ctx():
    ctx = MagicMock()
    ctx.guild = None
    ctx.send = AsyncMock()
    return ctx


def make_guild_ctx():
    ctx = MagicMock()
    ctx.guild.id = 100
    ctx.send = AsyncMock()
    return ctx


async def _run_checks(command, ctx):
    for check in command.checks:
        await check(ctx)


# ── summary_command ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summary_requires_dm(cog):
    ctx = make_guild_ctx()
    with pytest.raises(commands.CheckFailure, match="only works in DMs"):
        await _run_checks(TournamentCommands.summary_command, ctx)


@pytest.mark.asyncio
async def test_summary_command_requires_player_name(cog):
    ctx = make_dm_ctx()
    await TournamentCommands.summary_command.callback(cog, ctx, None)
    assert "specify a player name" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_summary_command_warns_on_unknown_option(cog):
    ctx = make_dm_ctx()
    with patch("commands.get_tournament_sheets_async", return_value={}), \
         patch("commands.get_player_characters", return_value=[]):
        await TournamentCommands.summary_command.callback(cog, ctx, "Scorium", "image")
    assert any("Ignoring unknown option" in c.args[0] for c in ctx.send.call_args_list)


@pytest.mark.asyncio
async def test_summary_no_characters_found_anywhere(cog):
    ctx = make_dm_ctx()
    with patch("commands.get_tournament_sheets_async", return_value={}), \
         patch("commands.get_player_characters", return_value=[]):
        await TournamentCommands.summary_command.callback(cog, ctx, "Nobody")
    assert "No characters found for **Nobody**" in ctx.send.call_args[0][0]


# ── _build_and_send_player_summary ──────────────────────────────────────────

def _make_cog(tournaments):
    return TournamentCommands(MagicMock(), tournaments, mapping_sheet_url="fake", default_week=2)


@pytest.mark.asyncio
async def test_summary_builds_character_and_total_lines():
    cog = _make_cog([{"name": "Echoes of Valor", "alias": "EoV", "url": "fake_eov",
                       "builds_sheet": "hero builds", "builds_mapping": {}}])
    destination = MagicMock()
    destination.send = AsyncMock()

    characters = [
        {"hero_key": "samwise-mordek", "display_name": "Samwise - Mordek", "hero_name": "Mordek",
         "ancestry": "Human", "class": "Warlock"},
        {"hero_key": "samwise-fizzle", "display_name": "Samwise - Fizzle", "hero_name": "Fizzle",
         "ancestry": "Gnome", "class": "Tinkerer"},
    ]

    def fake_rank(sheets, hero_key):
        return ("Diamond", 1, 2) if hero_key == "samwise-mordek" else (None, None, None)

    def fake_record(sheets, division, hero_key):
        # 3 played, 5 points (e.g. two 2-0 wins + one 1-1 draw) -> not a
        # binary win count, exercises the points/(played*2) formula.
        return (3, 5) if hero_key == "samwise-mordek" else (0, 0)

    with patch("commands.get_tournament_sheets_async", return_value={}), \
         patch("commands.get_player_characters", return_value=characters), \
         patch("commands.find_player_division_rank", side_effect=fake_rank), \
         patch("commands.get_player_division_record", side_effect=fake_record):
        await cog._build_and_send_player_summary(destination, "Samwise", context={'tournament': None})

    sent = "\n".join(c.args[0] for c in destination.send.call_args_list)
    assert "Echoes of Valor" in sent
    # Division comes before the character name; no player name shown.
    assert "**Diamond** — Mordek (Human Warlock) — 1st out of 2" in sent
    assert "Samwise -" not in sent  # the "Player - Hero" display_name form must not leak in
    assert "3 played, 5 points (83%)" in sent
    assert "**Unranked** — Fizzle (Gnome Tinkerer) —" in sent
    assert "0 played, 0 points (—)" in sent
    # Total across both characters: 3 played, 5 points combined.
    assert "**Total:** 3 played, 5 points (83%)" in sent


@pytest.mark.asyncio
async def test_summary_skips_tournaments_with_no_characters():
    cog = _make_cog([
        {"name": "Echoes of Valor", "alias": "EoV", "url": "fake_eov", "builds_sheet": None, "builds_mapping": None},
        {"name": "Meridian Ascension", "alias": "MA", "url": "fake_ma", "builds_sheet": None, "builds_mapping": None},
    ])
    destination = MagicMock()
    destination.send = AsyncMock()

    calls = {"n": 0}

    def side_effect(sheets, player, sheet_name, mapping):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"hero_key": "scorium-x", "display_name": "Scorium - X", "hero_name": "X",
                      "ancestry": "A", "class": "B"}]
        return []

    with patch("commands.get_tournament_sheets_async", return_value={}), \
         patch("commands.get_player_characters", side_effect=side_effect), \
         patch("commands.find_player_division_rank", return_value=(None, None, None)), \
         patch("commands.get_player_division_record", return_value=(0, 0)):
        await cog._build_and_send_player_summary(destination, "Scorium", context={'tournament': None})

    sent = "\n".join(c.args[0] for c in destination.send.call_args_list)
    assert "Echoes of Valor" in sent
    assert "Meridian Ascension" not in sent


@pytest.mark.asyncio
async def test_summary_sheet_fetch_error_is_skipped():
    cog = _make_cog([
        {"name": "Echoes of Valor", "alias": "EoV", "url": "fake_eov", "builds_sheet": None, "builds_mapping": None},
    ])
    destination = MagicMock()
    destination.send = AsyncMock()

    with patch("commands.get_tournament_sheets_async", side_effect=RuntimeError("boom")):
        await cog._build_and_send_player_summary(destination, "Scorium", context={'tournament': None})

    assert "No characters found for **Scorium**" in destination.send.call_args[0][0]
