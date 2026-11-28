"""!left [tournament]: every match not finished yet, up to this week, by
division. Any user by DM; AFK players' matches only show to an admin by DM."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import scheduler
from commands import TournamentCommands
from match_utils import apply_drops, get_unfinished_matches
from tests.test_player_flags import GOLD

SHEETS = {"GOLD": GOLD}


def _pairs(matches):
    return [(m["week"], m["player1"], m["player2"]) for m in matches]


def test_unfinished_matches_up_to_the_week():
    assert _pairs(get_unfinished_matches(SHEETS, 2)) == [(2, "TheJeb - Thornenach", "Lunapig - Felice")]
    assert _pairs(get_unfinished_matches(SHEETS, 3)) == [
        (2, "TheJeb - Thornenach", "Lunapig - Felice"),
        (3, "Scorium - Mildagas", "Lunapig - Felice"),
        (3, "TheJeb - Thornenach", "Tuff - Calcium"),
    ]


def test_misreported_counts_as_unfinished():
    sheet = GOLD.copy()
    sheet.iat[1, 4], sheet.iat[1, 5] = 2, 2
    first = get_unfinished_matches({"GOLD": sheet}, 1)
    assert len(first) == 1 and first[0]["misreported"]


def test_dropped_players_matches_are_finished():
    scheduler.add_player_flag("Scorium", None, "drop", set_by=1)
    assert (3, "Scorium - Mildagas", "Lunapig - Felice") not in _pairs(
        get_unfinished_matches(apply_drops(SHEETS), 3))


# ── command ─────────────────────────────────────────────────────────────────

@pytest.fixture
def cog():
    tournaments = [{"name": "Echoes of Valor", "alias": "EoV", "url": "eov"}]
    bot = MagicMock()
    bot.admin_user_ids = [1]
    return TournamentCommands(bot, tournaments, mapping_sheet_url="x", default_week=2)


async def _run(cog, *args, user_id=42, week=3):
    ctx = MagicMock()
    ctx.guild = None
    ctx.author.id = user_id
    ctx.send = AsyncMock()
    with patch.object(TournamentCommands, "_get_weeks_per_tournament",
                      AsyncMock(return_value=({"EoV": week}, {"EoV": SHEETS}))):
        await TournamentCommands.left_command.callback(cog, ctx, *args)
    return "\n".join(c.args[0] for c in ctx.send.call_args_list)


@pytest.mark.asyncio
async def test_left_lists_by_division(cog):
    text = await _run(cog)
    assert "**🏆 Echoes of Valor — 3 matches left (weeks 1–3)**" in text
    assert "**Gold** (3)\nW2 TheJeb - Thornenach vs Lunapig - Felice\nW3 Scorium - Mildagas vs Lunapig - Felice" in text


@pytest.mark.asyncio
async def test_left_hides_afk_for_players_and_marks_it_for_admins(cog):
    scheduler.add_player_flag("Tuff", None, "afk", set_by=1)
    player_view = await _run(cog, "EoV")
    assert "Tuff" not in player_view and "2 matches left" in player_view
    admin_view = await _run(cog, "EoV", user_id=1)
    assert "💤 W3 TheJeb - Thornenach vs Tuff - Calcium" in admin_view


@pytest.mark.asyncio
async def test_left_nothing_left_and_unknown_tournament(cog):
    assert "✅ every match up to week 1 is finished" in await _run(cog, week=1)
    text = await _run(cog, "Nope")
    assert "❌ No tournament called **Nope**. Tournaments: Echoes of Valor (`EoV`)." in text


@pytest.mark.asyncio
async def test_left_week_argument_overrides_this_week(cog):
    text = await _run(cog, "2", week=1)
    assert "2 matches left (weeks 1–2)" not in text and "1 match left (weeks 1–2)" in text
    text = await _run(cog, "EoV", "w3", week=1)
    assert "3 matches left (weeks 1–3)" in text
    assert "❌ The week must be 1 or more." in await _run(cog, "0")


def test_match_between_two_dropped_players_is_closed():
    scheduler.add_player_flag("Scorium", None, "drop", set_by=1)
    scheduler.add_player_flag("Lunapig", None, "drop", set_by=1)
    assert _pairs(get_unfinished_matches(apply_drops(SHEETS), 3)) == [
        (3, "TheJeb - Thornenach", "Tuff - Calcium")]


def test_week_after_the_last_lists_only_the_leftovers():
    """Season-end post_divisions asks for the week after the last one: no
    current matches, every unfinished one as pending, under its own label."""
    from match_utils import get_division_matches
    from tournament_actions import build_division_image
    current, pending = get_division_matches(SHEETS, "GOLD", 4)
    assert current == []
    # (Scorium v Lunapig also played in week 1 here; pairings list a pair
    # once per tournament, and real schedules never repeat a pair.)
    assert {(m["week"], m["player1"], m["player2"]) for m in pending} == {
        (2, "TheJeb - Thornenach", "Lunapig - Felice"),
        (3, "TheJeb - Thornenach", "Tuff - Calcium"),
    }
    image = build_division_image("Echoes of Valor", "GOLD", 4, current, pending, {},
                                 week_label="Season over · Matches left")
    assert image and image[:4] == b"\x89PNG"
