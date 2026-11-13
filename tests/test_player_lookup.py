"""Player lookups in !m / !sum / !sub: "maybe you meant" suggestions for a
mistyped name, and reusing the user's last successful search when no name
is given. (conftest gives every test its own empty tasks.db.)"""
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

import scheduler
from commands import TournamentCommands, _did_you_mean
from match_utils import collect_player_names, suggest_players

NAMES = {"Eindeloos", "TheJeb", "Jebbins", "Scorium", "Samwise", "DanieLegend",
         "DrussLegend", "WardenSlayer", "wrip", "Wrip"}


# ── suggestions ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("search, expected", [
    (".eindeloos", ["Eindeloos"]),            # punctuation ignored
    ("EINDELOOS!", ["Eindeloos"]),
    ("jeb", ["Jebbins", "TheJeb"]),           # starts-with ranks before contains
    ("scorim", ["Scorium"]),                  # typo
    ("wardenslayr", ["WardenSlayer"]),
    ("daniel legend", ["DanieLegend", "DrussLegend"]),
    ("wrip", ["Wrip"]),                       # "Wrip"/"wrip" collapse to one (stable pick)
    ("zzzz", []),
    ("...", []),
])
def test_suggest_players(search, expected):
    assert suggest_players(search, NAMES) == expected


def test_did_you_mean_line():
    assert _did_you_mean("jeb", NAMES, "m") == "\n❓ Maybe you meant **Jebbins** or **TheJeb**? Try `!m Jebbins`."
    assert _did_you_mean("zzzz", NAMES, "m") == ""


def test_collect_player_names_from_division_sheets():
    sheets = {
        "GOLD": pd.DataFrame([
            ["SCHEDULE", None, None, None, None, None, None],
            ["Week 1", "Round 1", "Scorium - Mildagas", "TheJeb - Thornenach", 0, 2, ""],
            ["Week 1", "Round 1", "Eindeloos - Loos", "Scorium - Bruno", 0, 0, ""],
        ], columns=list("ABCDEFG")),
        "Hero builds": pd.DataFrame([["Tier", "x"], ["Gold", "Nobody - Ignored"]]),
    }
    assert collect_player_names(sheets) == {"Scorium", "TheJeb", "Eindeloos"}


# ── last player memory ──────────────────────────────────────────────────────

def test_last_player_is_stored_per_user_and_overwritten():
    assert scheduler.get_last_player(1) is None
    scheduler.set_last_player(1, "Scorium")
    scheduler.set_last_player(2, "Tuff")
    scheduler.set_last_player(1, "  Eindeloos ")
    assert scheduler.get_last_player(1) == "Eindeloos"
    assert scheduler.get_last_player(2) == "Tuff"


# ── commands ────────────────────────────────────────────────────────────────

@pytest.fixture
def cog():
    c = TournamentCommands(MagicMock(), [{"name": "Echoes of Valor", "alias": "EoV", "url": "u"}],
                           mapping_sheet_url="x", default_week=2)
    c._ctx = MagicMock(return_value={"allowed": True, "tournament": None, "division": None})
    c._ensure_dm_or_admin = AsyncMock()
    return c


def _dm_ctx(user_id=42):
    ctx = MagicMock()
    ctx.guild = None
    ctx.author.id = user_id
    ctx.send = AsyncMock()
    return ctx


def _sent(ctx):
    return [c.args[0] for c in ctx.send.call_args_list]


@pytest.mark.asyncio
async def test_m_without_name_and_no_memory_asks_for_one(cog):
    ctx = _dm_ctx()
    cog._build_and_send_player_matches = AsyncMock(return_value="Scorium")
    await TournamentCommands.matches_command.callback(cog, ctx, None)
    cog._build_and_send_player_matches.assert_not_called()
    assert "Please specify a player name" in _sent(ctx)[0]


@pytest.mark.asyncio
async def test_m_remembers_last_successful_search_and_reuses_it(cog):
    ctx = _dm_ctx()
    cog._build_and_send_player_matches = AsyncMock(return_value="Scorium")
    await TournamentCommands.matches_command.callback(cog, ctx, "Scorium")
    assert scheduler.get_last_player(42) == "Scorium"

    ctx = _dm_ctx()
    await TournamentCommands.matches_command.callback(cog, ctx, None)      # bare "!m"
    assert _sent(ctx)[0] == "🔍 Searching for **Scorium** (your last search)..."
    assert cog._build_and_send_player_matches.call_args.args[1] == "Scorium"


@pytest.mark.asyncio
async def test_m_text_alone_uses_memory_in_text_mode(cog):
    scheduler.set_last_player(42, "Scorium")
    ctx = _dm_ctx()
    cog._build_and_send_player_matches = AsyncMock(return_value="Scorium")
    await TournamentCommands.matches_command.callback(cog, ctx, "text")    # "!m text"
    args = cog._build_and_send_player_matches.call_args.args
    assert args[1] == "Scorium" and args[2] is True     # force_text


@pytest.mark.asyncio
async def test_m_failed_search_is_not_remembered(cog):
    scheduler.set_last_player(42, "Scorium")
    cog._build_and_send_player_matches = AsyncMock(return_value=None)
    await TournamentCommands.matches_command.callback(cog, _dm_ctx(), ".eindeloos")
    assert scheduler.get_last_player(42) == "Scorium"


@pytest.mark.asyncio
async def test_m_not_found_suggests_close_names():
    cog = TournamentCommands(MagicMock(), [{"name": "Echoes of Valor", "alias": "EoV", "url": "u"}],
                             mapping_sheet_url="x", default_week=2)
    sheets = {"GOLD": pd.DataFrame([
        ["SCHEDULE", None, None, None, None, None, None],
        ["Week 2", "Round 1", "Eindeloos - Loos", "TheJeb - Eluna", 0, 0, ""],
    ], columns=list("ABCDEFG"))}
    dest = MagicMock()
    dest.send = AsyncMock()
    with patch.object(TournamentCommands, "_get_weeks_per_tournament",
                      AsyncMock(return_value=({"EoV": 2}, {"EoV": sheets}))), \
         patch("commands.load_hero_builds_from_sheets", return_value={}):
        found = await cog._build_and_send_player_matches(dest, ".eindeloos", False, None, {"tournament": None})
    assert found is None
    assert dest.send.call_args.args[0] == (
        "❌ No player named **.eindeloos** in any tournament."
        "\n❓ Maybe you meant **Eindeloos**? Try `!m Eindeloos`.")


@pytest.mark.asyncio
async def test_sum_reuses_memory_and_remembers(cog):
    scheduler.set_last_player(42, "Scorium")
    ctx = _dm_ctx()
    cog._build_and_send_player_summary = AsyncMock(return_value="Scorium")
    await TournamentCommands.summary_command.callback(cog, ctx, None)
    assert _sent(ctx)[0] == "🔍 Looking up **Scorium**'s characters (your last search)..."
    assert cog._build_and_send_player_summary.call_args.args[1] == "Scorium"

    cog._build_and_send_player_summary = AsyncMock(return_value="Tuff")
    await TournamentCommands.summary_command.callback(cog, _dm_ctx(), "tuff")
    assert scheduler.get_last_player(42) == "Tuff"          # real spelling stored


@pytest.mark.asyncio
async def test_sub_not_found_suggests_close_names(cog):
    sheets = {"GOLD": pd.DataFrame([
        ["SCHEDULE", None, None, None, None, None, None],
        ["Week 1", "Round 1", "TheJeb - Eluna", "Scorium - Bruno", 0, 0, ""],
    ], columns=list("ABCDEFG"))}
    ctx = _dm_ctx()
    with patch("commands.get_tournament_sheets_async", AsyncMock(return_value=sheets)), \
         patch("commands.load_hero_builds_from_sheets", return_value={"thejeb-eluna": "Elf Thief"}):
        await TournamentCommands.subscribe_command.callback(cog, ctx, "jeb")
    last = _sent(ctx)[-1]
    assert "couldn't find a player named **jeb**" in last
    assert "Maybe you meant **TheJeb**? Try `!sub TheJeb`." in last
    assert scheduler.list_subscriptions(42) == []


# ── remembered output format (text / image) ─────────────────────────────────

def test_text_mode_setting_defaults_to_images():
    assert scheduler.get_text_mode(42) is False
    scheduler.set_text_mode(42, True)
    assert scheduler.get_text_mode(42) is True
    scheduler.set_text_mode(42, False)
    assert scheduler.get_text_mode(42) is False


@pytest.mark.asyncio
async def test_m_remembers_text_then_image(cog):
    cog._build_and_send_player_matches = AsyncMock(return_value="Scorium")

    ctx = _dm_ctx()
    await TournamentCommands.matches_command.callback(cog, ctx, "Scorium", "text")
    assert cog._build_and_send_player_matches.call_args.args[2] is True
    assert "From now on I'll answer in text" in _sent(ctx)[0]

    ctx = _dm_ctx()
    await TournamentCommands.matches_command.callback(cog, ctx, "Scorium")      # no flag: remembered
    assert cog._build_and_send_player_matches.call_args.args[2] is True
    assert not any("From now on" in m for m in _sent(ctx))                    # no repeat notice

    ctx = _dm_ctx()
    await TournamentCommands.matches_command.callback(cog, ctx, "image")        # "!m image": last player, images
    args = cog._build_and_send_player_matches.call_args.args
    assert args[1] == "Scorium" and args[2] is False
    assert "From now on I'll answer with images" in _sent(ctx)[0]
    assert scheduler.get_text_mode(42) is False


@pytest.mark.asyncio
async def test_text_preference_is_ignored_in_servers(cog):
    scheduler.set_text_mode(42, True)
    cog._build_and_send_player_matches = AsyncMock(return_value="Scorium")
    ctx = _dm_ctx()
    ctx.guild = MagicMock()
    ctx.guild.id = 1
    await TournamentCommands.matches_command.callback(cog, ctx, "Scorium")
    assert cog._build_and_send_player_matches.call_args.args[2] is False


@pytest.mark.asyncio
async def test_division_uses_remembered_text_mode(cog):
    scheduler.set_text_mode(42, True)
    ctx = _dm_ctx()
    cog._get_weeks_per_tournament = AsyncMock(return_value=({"EoV": 2}, {}))
    sheets = {"GOLD": pd.DataFrame([
        ["SCHEDULE", None, None, None, None, None, None],
        ["Week 2", "Round 1", "Scorium - Bruno", "TheJeb - Eluna", 2, 0, ""],
    ], columns=list("ABCDEFG"))}
    with patch("commands.get_tournament_sheets_async", AsyncMock(return_value=sheets)), \
         patch("commands.load_hero_builds_from_sheets", return_value={}), \
         patch("commands.send_division_image", AsyncMock()) as image:
        await TournamentCommands.division_command.callback(cog, ctx, "GOLD")
    image.assert_not_called()                                   # answered in text
    assert any("Scorium - Bruno vs TheJeb - Eluna" in m for m in _sent(ctx))



@pytest.mark.asyncio
async def test_division_without_week_uses_the_same_week_as_m(cog):
    """The sheet pre-fills every week, so "the last week with matches" is the
    final one (7); without a week !d must show the published week, like !m."""
    sheets = {"GOLD": pd.DataFrame([
        ["SCHEDULE", None, None, None, None, None, None],
        ["Week 2", "Round 1", "Scorium - Bruno", "TheJeb - Eluna", 2, 0, ""],
        ["Week 7", "Round 1", "Scorium - Bruno", "Tuff - Calcium", 0, 0, ""],
    ], columns=list("ABCDEFG"))}
    scheduler.set_text_mode(42, True)
    cog._get_weeks_per_tournament = AsyncMock(return_value=({"EoV": 2}, {}))
    ctx = _dm_ctx()
    with patch("commands.get_tournament_sheets_async", AsyncMock(return_value=sheets)), \
         patch("commands.load_hero_builds_from_sheets", return_value={}):
        await TournamentCommands.division_command.callback(cog, ctx, "GOLD")
    text = "\n".join(_sent(ctx))
    assert "— Week 2" in text and "TheJeb - Eluna" in text
    assert "Tuff - Calcium" not in text                       # week 7 not shown
