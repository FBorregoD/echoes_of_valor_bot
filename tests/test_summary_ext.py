"""!sum_ext [player] [character] [tournament]: every finished match of a
player's characters, week by week, with position and win rate. Only valid
results are listed (unplayed / misreported matches are left out)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

import scheduler
from commands import TournamentCommands
from match_utils import get_character_results

GOLD = pd.DataFrame([
    ["SCHEDULE", None, None, None, None, None, None],
    ["Week 1", "Round 1", "Scorium - Mildagas", "TheJeb - Thornenach", 0, 2, ""],
    ["Week 1", "Round 2", "WardenSlayer - Aaravos", "Scorium - Mildagas", 0, 2, ""],   # player 2: flipped
    ["Week 2", "Round 1", "Scorium - Mildagas", "Samwise - Endless", 0, 0, ""],        # unplayed: hidden
    ["Week 2", "Round 2", "Scorium - Mildagas", "Lunapig - Felice", 1, 1, ""],         # still G2
    ["Week 3", "Round 1", "Scorium - Mildagas", "Machetout - Elanna", 2, 2, "OK"],     # misreported: hidden
    ["Week 3", "Round 1", "Tuff - Calcium", "Zeric - Bard", 2, 0, ""],                 # someone else
], columns=list("ABCDEFG"))


def test_character_results_only_valid_results_with_stable_game_numbers():
    results = get_character_results({"GOLD": GOLD}, "gold", "scorium-mildagas")
    assert results == [
        {"week": 1, "game": 1, "opponent": "TheJeb - Thornenach", "own_score": 0, "opp_score": 2},
        {"week": 1, "game": 2, "opponent": "WardenSlayer - Aaravos", "own_score": 2, "opp_score": 0},
        {"week": 2, "game": 2, "opponent": "Lunapig - Felice", "own_score": 1, "opp_score": 1},
    ]


def test_character_results_unknown_division():
    assert get_character_results({"GOLD": GOLD}, "COPPER", "scorium-mildagas") == []


# ── command ─────────────────────────────────────────────────────────────────

TOURNAMENTS = [{"name": "Echoes of Valor", "alias": "EoV", "url": "eov"},
               {"name": "Meridian Ascension", "alias": "MA", "url": "ma"}]
CHARS = [
    {"hero_key": "scorium-mildagas", "display_name": "Scorium - Mildagas", "hero_name": "Mildagas",
     "ancestry": "Elf", "class": "Thief"},
    {"hero_key": "scorium-lollipop guild", "display_name": "Scorium - Lollipop Guild",
     "hero_name": "Lollipop Guild", "ancestry": "Smallfolk", "class": "Bard"},
]


@pytest.fixture
def cog():
    return TournamentCommands(MagicMock(), TOURNAMENTS, mapping_sheet_url="x", default_week=2)


def _dm_ctx():
    ctx = MagicMock()
    ctx.guild = None
    ctx.author.id = 42
    ctx.send = AsyncMock()
    return ctx


async def _run(cog, *args, chars=CHARS):
    ctx = _dm_ctx()
    with patch("commands.get_tournament_sheets_async", AsyncMock(return_value={"GOLD": GOLD})), \
         patch("commands.get_player_characters", return_value=chars), \
         patch("commands.load_hero_builds_from_sheets",
               return_value={"thejeb-thornenach": "Half-Demon Fighter"}), \
         patch("commands.find_player_division_rank",
               side_effect=lambda s, key: ("GOLD", 5, 9) if key == "scorium-mildagas" else (None, None, None)):
        await TournamentCommands.summary_ext_command.callback(cog, ctx, *args)
    return "\n".join(c.args[0] for c in ctx.send.call_args_list)


@pytest.mark.asyncio
async def test_sum_ext_lists_results_week_by_week_with_position(cog):
    text = await _run(cog, "Scorium", "mildagas", "EoV")
    assert "**🏆 Echoes of Valor — Gold — Mildagas (Elf Thief)**" in text
    assert "W1 G1 vs TheJeb - Thornenach (Half-Demon Fighter) 🔴 **0 - 2**" in text
    assert "W1 G2 vs WardenSlayer - Aaravos (?) 🟢 **2 - 0**" in text
    assert "W2 G2 vs Lunapig - Felice (?) 🟡 **1 - 1**" in text
    assert text.count("Mildagas") == 1                           # named once, in the header
    assert "Samwise" not in text and "Machetout" not in text      # unplayed / misreported
    # 3 played, 0+2+1 = 3 points -> 50%
    assert "Position: 5th out of 9 · Win rate: 50% (3 played, 3 points)" in text
    assert "Meridian Ascension" not in text                       # tournament filter
    assert "Lollipop" not in text                                 # character filter
    assert scheduler.get_last_player(42) == "Scorium"


@pytest.mark.asyncio
async def test_sum_ext_character_with_spaces_and_multiword_tournament(cog):
    text = await _run(cog, "Scorium", "Lollipop", "Guild", "Meridian", "Ascension")
    assert "**🏆 Meridian Ascension — Unranked — Lollipop Guild (Smallfolk Bard)**" in text
    assert "No results yet." in text
    assert "Echoes of Valor" not in text


@pytest.mark.asyncio
async def test_sum_ext_unknown_character_lists_the_players_characters(cog):
    text = await _run(cog, "Scorium", "Nobody", "EoV")
    assert "❌ **Scorium** has no character named **Nobody**. Their characters: Lollipop Guild, Mildagas." in text


@pytest.mark.asyncio
async def test_sum_ext_unknown_player_suggests(cog):
    text = await _run(cog, "sco", chars=[])
    assert "No characters found for **sco** in any tournament." in text
    assert "Maybe you meant **Scorium**? Try `!sum_ext Scorium`." in text


@pytest.mark.asyncio
async def test_sum_ext_without_args_uses_last_player(cog):
    scheduler.set_last_player(42, "Scorium")
    text = await _run(cog)
    assert "🔍 Looking up **Scorium**'s matches (your last search)..." in text
    assert "W1 G1 vs TheJeb - Thornenach" in text
