"""!drop / !afk: a dropped player's matches all count as 0-2 and they leave the
standings; an AFK player's unplayed matches leave the pending lists. Both can
be undone. (conftest gives every test an empty tasks.db, so no flags.)"""
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

import scheduler
from commands import TournamentCommands
from match_utils import (
    apply_drops, find_player_division_rank, get_division_matches, get_division_standings,
    get_player_division_record, get_player_matches, is_afk, is_dropped, load_hero_builds_from_sheets,
    tag_tournament,
)

# Schedule in A-G, the sheet's own standings table in H-K (header on row 0).
GOLD = pd.DataFrame([
    ["SCHEDULE", None, None, None, None, None, None, "Rank", "Player & Hero Name", "Played", "Points"],
    ["Week 1", "R1", "Scorium - Mildagas", "TheJeb - Thornenach", 2, 0, "", 1, "Scorium - Mildagas", 2, 3],
    ["Week 1", "R2", "Lunapig - Felice", "Scorium - Mildagas", 1, 1, "", 2, "Lunapig - Felice", 1, 1],
    ["Week 2", "R1", "TheJeb - Thornenach", "Lunapig - Felice", 0, 0, "", 3, "TheJeb - Thornenach", 1, 0],
    ["Week 3", "R1", "Scorium - Mildagas", "Lunapig - Felice", 0, 0, "", None, None, None, None],  # future
    ["Week 3", "R2", "TheJeb - Thornenach", "Tuff - Calcium", 0, 0, "", None, None, None, None],
], columns=list("ABCDEFGHIJK"))
SHEETS = {"GOLD": GOLD}


def _scores(sheets, row):
    return [int(sheets["GOLD"].iat[row, c]) for c in (4, 5)]


# ── drop ────────────────────────────────────────────────────────────────────

def test_drop_scores_every_match_of_that_character_0_2():
    scheduler.add_player_flag("Scorium", "Mildagas", "drop", set_by=1)
    sheets = apply_drops(SHEETS)
    assert _scores(sheets, 1) == [0, 2]      # was a 2-0 win: now a loss
    assert _scores(sheets, 2) == [2, 0]      # dropped player is player 2
    assert _scores(sheets, 4) == [0, 2]      # future match too
    assert _scores(sheets, 3) == [0, 0]      # other players untouched
    assert int(GOLD.iat[1, 4]) == 2          # the original sheet isn't changed


def test_drop_whole_player_covers_every_character_and_is_case_insensitive():
    scheduler.add_player_flag("scorium", None, "drop", set_by=1)
    assert is_dropped("Scorium - Mildagas") and is_dropped("SCORIUM - Other Hero")
    assert not is_dropped("TheJeb - Thornenach")


def test_drop_in_one_tournament_only_applies_to_that_tournaments_sheets():
    scheduler.add_player_flag("Scorium", None, "drop", set_by=1,
                              tournament="eov-url", tournament_name="Echoes of Valor")
    eov = apply_drops(tag_tournament({"GOLD": GOLD.copy()}, "eov-url"))
    other = apply_drops(tag_tournament({"GOLD": GOLD.copy()}, "ma-url"))
    assert _scores(eov, 1) == [0, 2]
    assert _scores(other, 1) == [2, 0]          # another tournament: the sheet's result
    assert not is_dropped("Scorium - Mildagas")  # untagged sheet: only every-tournament flags


def test_no_flags_returns_the_same_sheets():
    assert apply_drops(SHEETS) is SHEETS


def test_dropped_character_leaves_standings_and_rivals_are_recounted():
    rows, _ = get_division_standings(SHEETS, "GOLD")
    assert [r[1] for r in rows] == ["Scorium - Mildagas", "Lunapig - Felice", "TheJeb - Thornenach"]

    scheduler.add_player_flag("Scorium", "Mildagas", "drop", set_by=1)
    sheets = apply_drops(SHEETS)
    rows, _ = get_division_standings(sheets, "GOLD")
    # Lunapig: 2-0 (W1, was 1-1) + 2-0 (W3, future) = 2 played, 4 pts; TheJeb: 2-0 (W1) = 1 played, 2 pts
    assert rows == [["1", "Lunapig - Felice", "2", "4"], ["2", "TheJeb - Thornenach", "1", "2"]]
    assert get_player_division_record(sheets, "GOLD", "scorium-mildagas") == (3, 0)
    assert find_player_division_rank(sheets, "scorium-mildagas") == ("GOLD", None, None)


def test_dropped_character_is_marked_in_its_build():
    builds_sheet = pd.DataFrame([["Player & Hero Name", "Ancestry", "Class"],
                                 ["Scorium - Mildagas", "Elf", "Thief"],
                                 ["TheJeb - Thornenach", "Half-Demon", "Fighter"]])
    scheduler.add_player_flag("Scorium", "Mildagas", "drop", set_by=1)
    builds = load_hero_builds_from_sheets({"Hero Builds": builds_sheet}, None,
                                          {"player_col": 0, "ancestry_col": 1, "class_col": 2})
    assert builds["scorium-mildagas"] == "Elf Thief · dropped"
    assert builds["thejeb-thornenach"] == "Half-Demon Fighter"


def test_undrop_restores_the_sheet_results():
    scheduler.add_player_flag("Scorium", "Mildagas", "drop", set_by=1)
    assert scheduler.remove_player_flag("scorium", "mildagas", "drop") is True
    assert apply_drops(SHEETS) is SHEETS
    assert scheduler.remove_player_flag("Scorium", "Mildagas", "drop") is False


# ── afk ─────────────────────────────────────────────────────────────────────

def test_afk_opponent_leaves_rivals_pending_but_not_their_own():
    _, pending = get_player_matches(SHEETS, "Lunapig", 3, exact=True)
    assert len(pending) == 1                                   # W2 vs TheJeb, unplayed

    scheduler.add_player_flag("TheJeb", None, "afk", set_by=1)
    assert is_afk("TheJeb - Thornenach")
    _, pending = get_player_matches(SHEETS, "Lunapig", 3, exact=True)
    assert pending == []                                       # rival's list: hidden
    _, pending = get_player_matches(SHEETS, "TheJeb", 3, exact=True)
    assert len(pending) == 1                                   # the AFK player's own: kept
    current, pending = get_division_matches(SHEETS, "GOLD", 3)
    assert pending == []                                       # pairings: pending hidden
    assert any(m["player1"] == "TheJeb - Thornenach" for m in current)   # this week's still shown


def test_afk_keeps_misreported_matches_visible():
    sheet = GOLD.copy()
    sheet.iat[3, 4], sheet.iat[3, 5] = 2, 2                     # W2 TheJeb vs Lunapig: misreported
    scheduler.add_player_flag("TheJeb", None, "afk", set_by=1)
    _, pending = get_division_matches({"GOLD": sheet}, "GOLD", 3)
    assert len(pending) == 1 and pending[0]["misreported"]


def test_drop_and_afk_are_independent_flags():
    scheduler.add_player_flag("TheJeb", None, "afk", set_by=1)
    scheduler.add_player_flag("TheJeb", None, "drop", set_by=1)
    scheduler.remove_player_flag("TheJeb", None, "drop")
    assert is_afk("TheJeb - Thornenach") and not is_dropped("TheJeb - Thornenach")
    assert [f["label"] for f in scheduler.list_player_flags("afk")] == ["TheJeb"]


# ── commands ────────────────────────────────────────────────────────────────

@pytest.fixture
def cog():
    tournaments = [{"name": "Echoes of Valor", "alias": "EoV", "url": "eov"}]
    bot = MagicMock()
    bot.get_user.return_value = None
    return TournamentCommands(bot, tournaments, mapping_sheet_url="x", default_week=2)


async def _run(cog, command, *args):
    ctx = MagicMock()
    ctx.guild = None
    ctx.author.id = 7
    ctx.send = AsyncMock()
    chars = [{"hero_key": "scorium-mildagas", "display_name": "Scorium - Mildagas",
              "hero_name": "Mildagas", "ancestry": "Elf", "class": "Thief"}]
    with patch("commands.get_tournament_sheets_async", AsyncMock(return_value=SHEETS)), \
         patch("commands.get_player_characters", return_value=chars):
        await getattr(TournamentCommands, command).callback(cog, ctx, *args)
    return "\n".join(c.args[0] for c in ctx.send.call_args_list)


@pytest.mark.asyncio
async def test_drop_command_uses_the_real_spelling_and_can_be_undone(cog):
    text = await _run(cog, "drop_command", "scorium", "MILDAGAS")
    assert "✅ **Scorium - Mildagas** is now dropped in **every tournament**" in text
    assert "`!undrop Scorium Mildagas`" in text
    assert is_dropped("Scorium - Mildagas") and not is_dropped("Scorium - Other")

    assert "was already dropped" in await _run(cog, "drop_command", "Scorium", "Mildagas")
    listing = await _run(cog, "drop_command")
    assert "**Dropped** (1)" in listing and "• **Scorium - Mildagas** — every tournament — since 20" in listing

    assert "is no longer dropped" in await _run(cog, "undrop_command", "Scorium", "Mildagas")
    assert not is_dropped("Scorium - Mildagas")
    assert "Nobody is dropped." in await _run(cog, "drop_command")


@pytest.mark.asyncio
async def test_drop_whole_player(cog):
    text = await _run(cog, "drop_command", "Scorium")
    assert "**Scorium** (all their characters) is now dropped" in text


@pytest.mark.asyncio
async def test_drop_in_one_tournament_and_undo_needs_the_same_tournament(cog):
    text = await _run(cog, "drop_command", "Scorium", "Mildagas", "eov")
    assert "is now dropped in **Echoes of Valor**" in text
    assert "`!undrop Scorium Mildagas EoV`" in text
    flag = scheduler.list_player_flags("drop")[0]
    assert (flag["tournament"], flag["tournament_name"]) == ("eov", "Echoes of Valor")

    text = await _run(cog, "undrop_command", "Scorium", "Mildagas")      # no tournament: a different flag
    assert "isn't dropped in every tournament" in text and "Echoes of Valor" in text
    text = await _run(cog, "undrop_command", "Scorium", "Mildagas", "Echoes", "of", "Valor")
    assert "is no longer dropped in **Echoes of Valor**" in text
    assert scheduler.list_player_flags() == []


@pytest.mark.asyncio
async def test_inactive_lists_dropped_and_afk_with_what_and_where(cog):
    scheduler.add_player_flag("Scorium", "Mildagas", "drop", set_by=1,
                              tournament="eov", tournament_name="Echoes of Valor")
    scheduler.add_player_flag("TheJeb", None, "afk", set_by=1)
    ctx = MagicMock()
    ctx.send = AsyncMock()
    await TournamentCommands.inactive_command.callback(cog, ctx)
    text = "\n".join(c.args[0] for c in ctx.send.call_args_list)
    assert "🚫 **Dropped** (1)\n• **Scorium - Mildagas** — Echoes of Valor — since " in text
    assert "💤 **AFK** (1)\n• **TheJeb (all characters)** — every tournament — since " in text


@pytest.mark.asyncio
async def test_drop_unknown_player_or_character_sets_nothing(cog):
    text = await _run(cog, "drop_command", "scorum")
    assert "No player named **scorum**" in text and "Maybe you meant **Scorium**?" in text
    text = await _run(cog, "afk_command", "Scorium", "Nobody")
    assert "**Scorium** has no character named **Nobody**. Their characters: Mildagas." in text
    assert scheduler.list_player_flags() == []


@pytest.mark.asyncio
async def test_unafk_when_not_afk(cog):
    await _run(cog, "afk_command", "TheJeb")
    text = await _run(cog, "unafk_command", "Lunapig")
    assert "**Lunapig** isn't AFK in every tournament." in text
