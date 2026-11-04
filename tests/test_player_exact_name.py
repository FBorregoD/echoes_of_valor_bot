"""!m only accepts a player's exact name (case/accents don't matter): a
prefix like "sco" is reported as not found with suggestions, never answered
with Scorium's matches; when found, the real spelling is used everywhere."""
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from commands import TournamentCommands
from match_utils import find_player_name, same_player

SHEETS = {"GOLD": pd.DataFrame([
    ["SCHEDULE", None, None, None, None, None, None],
    ["Week 2", "Round 1", "Scorium - Mildagas", "Samwise - Endless", 0, 2, ""],
    ["Week 2", "Round 1", "Scoria - Other", "Tuff - Calcium", 0, 0, ""],
], columns=list("ABCDEFG"))}


async def _matches(player, force_text=True):
    cog = TournamentCommands(MagicMock(), [{"name": "Echoes of Valor", "alias": "EoV", "url": "u"}],
                             mapping_sheet_url="x", default_week=2)
    dest = MagicMock()
    dest.send = AsyncMock()
    with patch.object(TournamentCommands, "_get_weeks_per_tournament",
                      AsyncMock(return_value=({"EoV": 2}, {"EoV": SHEETS}))), \
         patch("commands.load_hero_builds_from_sheets", return_value={}):
        found = await cog._build_and_send_player_matches(dest, player, force_text, None, {"tournament": None})
    # text messages only (the image is sent as file=..., without text)
    return found, [c.args[0] for c in dest.send.call_args_list if c.args]


@pytest.mark.asyncio
async def test_prefix_is_not_a_match_it_suggests_instead():
    found, sent = await _matches("sco")
    assert found is None
    assert sent == ["❌ No player named **sco** in any tournament.\n"
                    "❓ Maybe you meant **Scoria** or **Scorium**? Try `!m Scoria`."]


@pytest.mark.asyncio
async def test_exact_name_in_any_case_uses_real_spelling():
    found, sent = await _matches("scorium")
    assert found == "Scorium"
    text = "\n".join(sent)
    assert "Scorium - Mildagas" in text
    assert "Scoria" not in text            # only that exact player, no prefix siblings


@pytest.mark.asyncio
async def test_image_title_uses_real_spelling():
    with patch("commands.render_player_matches", return_value=b"png") as render:
        found, _ = await _matches("SCORIUM", force_text=False)
    assert found == "Scorium"
    assert render.call_args.args[0] == "Scorium"


def test_find_player_name_and_same_player():
    names = {"Scorium", "Scoria", "Éowyn"}
    assert find_player_name("scorium", names) == "Scorium"
    assert find_player_name("eowyn", names) == "Éowyn"      # accents ignored
    assert find_player_name("sco", names) is None
    assert same_player("scorium", "Scorium - Bruno")
    assert not same_player("sco", "Scorium - Bruno")
