import io
from image_render import render_standings, render_matchups, render_player_matches

def test_render_standings():
    rows = [
        [1, 'DblDubz - Lil Nikki', 3, 4, 'Half-Demon Wizard'],
        [2, 'Eindeloos - Kaelthas', 2, 2, 'Elf Wizard'],
    ]
    data = render_standings("Test Standings", rows)
    assert isinstance(data, bytes)
    # check PNG magic
    assert data[:8] == b'\x89PNG\r\n\x1a\n'

def test_render_matchups():
    current = [('Player1', 'Human Fighter', 'Player2', 'Elf Wizard', True)]
    pending = [(2, 'Player1', 'Human Fighter', 'Player3', 'Orc Druid')]
    misreported = [(1, 'Player4', 'Smallfolk Thief', 'Player5', 'Human Bard')]
    data = render_matchups("Test Matchups", "Week 1", current, pending, misreported)
    assert isinstance(data, bytes)
    assert data[:8] == b'\x89PNG\r\n\x1a\n'

def test_render_player_matches():
    results = [
        {
            'tourney_name': 'Test',
            'week': 1,
            'season_complete': False,
            'current': [('Division', 'Player1', 'Build1', 'Player2', 'Build2', True)],
            'pending': [(2, 'Division', 'Player1', 'Build1', 'Player3', 'Build3')],
            'misreported': [],
        }
    ]
    data = render_player_matches("Player1", results)
    assert isinstance(data, bytes)
    assert data[:8] == b'\x89PNG\r\n\x1a\n'