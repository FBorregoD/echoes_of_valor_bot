"""
Minimal tests for the help embed (doesn't need Discord).
"""
from help import get_help_embed

def test_help_embed_main():
    embed = get_help_embed("@TestBot")
    assert embed.title == "📖 Bot Commands"
    assert any("!matches" in field.value for field in embed.fields)

def test_help_embed_specific():
    embed = get_help_embed("@TestBot", "matches")
    assert "!matches / !m" in embed.title