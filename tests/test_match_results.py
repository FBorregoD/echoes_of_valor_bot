"""!m shows the result of a finished current-week match (from the player's
side, their points first) instead of "vs" + ✓."""
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from commands import TournamentCommands
from image_render import render_player_matches, result_text

SHEETS = {"GOLD": pd.DataFrame([
    ["SCHEDULE", None, None, None, None, None, None],
    ["Week 2", "Round 1", "Scorium - Mildagas", "Samwise - Endless", 0, 2, "OK"],   # Scorium is player1
    ["Week 2", "Round 1", "TheJeb - Lilleth", "Scorium - Bruno", 0, 2, "NO"],      # Scorium is player2
    ["Week 2", "Round 1", "Scorium - Kardonel", "Zembu - Gorgeous", 2, 2, ""],     # misreported
    ["Week 2", "Round 1", "Scorium - Carabella", "Zeric - Glorfindel", 0, 0, ""],  # not played yet
], columns=list("ABCDEFG"))}


async def _run_text_matches():
    cog = TournamentCommands(MagicMock(), [{"name": "Echoes of Valor", "alias": "EoV", "url": "u"}],
                             mapping_sheet_url="x", default_week=2)
    dest = MagicMock()
    dest.send = AsyncMock()
    with patch.object(TournamentCommands, "_get_weeks_per_tournament",
                      AsyncMock(return_value=({"EoV": 2}, {"EoV": SHEETS}))), \
         patch("commands.load_hero_builds_from_sheets", return_value={}):
        await cog._build_and_send_player_matches(dest, "Scorium", True, None, {"tournament": None})
    return "\n".join(c.args[0] for c in dest.send.call_args_list)


@pytest.mark.asyncio
async def test_text_shows_result_from_the_players_side():
    text = await _run_text_matches()
    assert "Scorium - Mildagas (?) **0 - 2** Samwise - Endless (?)" in text
    # Scorium was player2 with 2 points: shown first, as 2 - 0
    assert "Scorium - Bruno (?) **2 - 0** TheJeb - Lilleth (?)" in text
    assert "✓" not in text
    # misreported keeps "vs" + warning; unplayed keeps plain "vs"
    assert "Scorium - Kardonel (?) vs Zembu - Gorgeous (?) ⚠️" in text
    assert "Scorium - Carabella (?) vs Zeric - Glorfindel (?)" in text


def test_result_text_and_image_with_scores():
    assert result_text((2, 0)) == "2 - 0"
    rows = [("GOLD", "A - X", "b", "B - Y", "b", True, False, (1, 1)),
            ("GOLD", "A - X", "b", "C - Z", "b", False, True, None),
            ("GOLD", "A - X", "b", "D - W", "b", True)]          # old tuple without score: ✓
    png = render_player_matches("A", [{"tourney_name": "T", "week": 2, "current": rows,
                                       "pending": [], "misreported": []}])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
