"""
Unit tests for tournament_actions.py.
Mocks all external dependencies (Discord, Google Sheets).
"""

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import discord

from tournament_actions import (
    find_tournament,
    build_division_image,
    _split_pending,
    build_pending_dm,
    run_post_divisions,
    run_notify_all,
    run_post_standings,
)

# ── Sample data ──────────────────────────────────────────────────────────────

@pytest.fixture
def sample_tournaments():
    return [
        {"name": "Echoes of Valor", "alias": "EoV", "url": "fake_url",
         "builds_sheet": "hero builds Season 9",
         "builds_mapping": {"player_col": 3, "ancestry_col": 4, "class_col": 5}},
    ]

@pytest.fixture
def sample_builds():
    return {
        "dbl dubz - lil nikki": "Half-Demon Wizard",
        "dbl dubz - grok": "Ogre Fighter",
        "eindeloos - kaelthas": "Elf Wizard",
        "emil - noon eonce": "Smallfolk Wizard",
    }

@pytest.fixture
def sample_sheets():
    import pandas as pd
    diamond = pd.DataFrame({
        'A': [None, None, 'SCHEDULE', 'Week 1', None, 'Week 2', None],
        'B': [None, None, None, 'Round 1', None, 'Round 2', None],
        'C': [None, None, None, 'DblDubz - Lil Nikki', 'Eindeloos - Kaelthas', 'DblDubz - Lil Nikki',
              'DblDubz - Grok'],
        'D': [None, None, None, 'DblDubz - Grok', 'Emil - NoOneOnce', 'Eindeloos - Kaelthas',
              'Emil - NoOneOnce'],
        'E': [None, None, None, 2, 1, 1, 0],    # score1
        'F': [None, None, None, 0, 1, 1, 2],    # score2
        'G': [None, None, None, 'OK', 'NO', 'NO', 'NO'],  # check
    })
    diamond.columns = ['A','B','C','D','E','F','G']
    return {
        'Diamond': diamond,
        'Hero builds Season 9': pd.DataFrame({
            'League Tier #': ['Diamond', 'Diamond'],
            'Player + Hero': ['DblDubz - Lil Nikki', 'DblDubz - Grok'],
            'Ancestry': ['Half-Demon', 'Ogre'],
            'Class': ['Wizard', 'Fighter'],
        }),
    }

# ── Tests ────────────────────────────────────────────────────────────────────

def test_find_tournament(sample_tournaments):
    t = find_tournament(sample_tournaments, 'EoV')
    assert t['name'] == 'Echoes of Valor'
    t2 = find_tournament(sample_tournaments, 'Unknown')
    assert t2 is None

def test_split_pending():
    pending = [
        {"week": 1, "player1": "A", "player2": "B", "check": "NO", "misreported": False},
        {"week": 2, "player1": "C", "player2": "D", "check": "NO", "misreported": True},
        {"week": 3, "player1": "E", "player2": "F", "check": "NO", "misreported": True},
    ]
    normal, misr = _split_pending(pending)
    assert len(normal) == 1
    assert len(misr) == 2
    assert normal[0]["week"] == 1
    assert misr[0]["week"] == 2

@patch('tournament_actions.render_matchups')
def test_build_division_image(mock_render, sample_builds):
    mock_render.return_value = b'fake_png_bytes'
    current = [
        {"player1": "P1 - H1", "player2": "P2 - H2", "check": "OK"},
    ]
    pending = [
        {"week": 1, "player1": "P3 - H3", "player2": "P4 - H4", "check": "NO", "misreported": False},
        {"week": 2, "player1": "P5 - H5", "player2": "P6 - H6", "check": "NO", "misreported": True},
    ]
    img = build_division_image("Test", "Div", 1, current, pending, sample_builds)
    assert img == b'fake_png_bytes'
    _, kw = mock_render.call_args
    assert len(kw['current_rows']) == 1
    assert len(kw['pending_rows']) == 1
    assert len(kw['misreported_rows']) == 1
    assert kw['misreported_rows'][0][0] == 2

def test_build_pending_dm(sample_builds):
    pending = [
        {"week": 1, "division": "Div", "player1": "Player - Hero1", "player2": "Opp - Hero2", "misreported": False},
        {"week": 2, "division": "Div2", "player1": "Player - Hero1", "player2": "Opp3 - Hero3", "misreported": True},
    ]
    chunks = build_pending_dm("Player", "Tourney", pending, sample_builds)
    # The pending match (week 1) should be present, misreported (week 2) excluded
    assert len(chunks) == 1
    # Check for the division name and the "Your Hero" column text in the generated table
    assert "Div" in chunks[0]            # Division name appears
    assert "Hero1" in chunks[0]          # Player's hero name appears
    # The misreported match should NOT appear
    assert "Div2" not in chunks[0]
    assert "Hero3" not in chunks[0]


# ── Async tests ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_sheets_ctx(sample_tournaments, sample_sheets):
    """Patch get_tournament_sheets and get_division_matches."""
    with patch('tournament_actions.get_tournament_sheets') as mock_get:
        mock_get.return_value = sample_sheets
        with patch('tournament_actions.get_division_matches') as mock_matches:
            mock_matches.return_value = ([], [])   # no matches
            yield


class AsyncMockChannel:
    """Minimal async send for Discord, faking a TextChannel."""
    def __init__(self):
        self.sent = []
        self.guild = MagicMock()
        self.guild.text_channels = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


@pytest.mark.asyncio
async def test_run_post_divisions(sample_tournaments, mock_sheets_ctx):
    channel = AsyncMockChannel()
    with patch('tournament_actions.get_division_sheets', return_value=['Diamond']):
        with patch('tournament_actions.get_threads_for_channel', return_value={}):
            success, not_found, errors = await run_post_divisions(
                destination=channel,
                tournaments=sample_tournaments,
                default_week=1,
                tournament_alias="EoV",
                week_raw=1,
                force_refresh=False,
            )
    assert success == 0
    assert 'Diamond' in not_found
    assert len(channel.sent) == 0   # Function does not send summary


@pytest.mark.asyncio
async def test_run_notify_all_empty_mapping(sample_tournaments, mock_sheets_ctx):
    channel = AsyncMockChannel()
    with patch('tournament_actions.load_player_mapping', return_value={}):
        success, total = await run_notify_all(
            bot=MagicMock(),
            destination=channel,
            tournaments=sample_tournaments,
            mapping_url="fake_url",
            default_week=1,
            week_raw=1,
            force_refresh=False,
        )
    assert success == 0
    assert total == 0
    assert any("mapping" in str((args, kwargs)) for args, kwargs in channel.sent)