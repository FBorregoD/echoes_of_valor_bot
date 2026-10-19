import discord
from channel_context import build_channel_index, resolve_context

def test_build_channel_index():
    config = [
        {
            "guild_id": 123,
            "prefix": "2!",
            "channels": [
                {"channel_id": 10, "channel_name": "eov", "tournament": "EoV"},
                {"channel_id": 11, "channel_name": "general", "tournament": None},
            ]
        },
        {
            "guild_id": 456,
            "channels": [
                {"channel_id": 20, "channel_name": "ma", "tournament": "MA"},
            ]
        }
    ]
    index = build_channel_index(config)
    assert index[123][10]['tournament'] == "EoV"
    assert index[123][11]['tournament'] is None
    assert index[123]["_prefix"] == "2!"
    assert index[456].get("_prefix") is None  # no prefix → default

# Test resolve_context with dummy Discord objects would require mocks,
# but the core logic is straightforward; we skip for brevity.

# ── prefix resolution / PREFIX_OVERRIDE (local dev bot) ─────────────────────

from channel_context import normalize_prefix_override, resolve_base_prefix


def test_normalize_prefix_override():
    assert normalize_prefix_override("dev") == "dev "
    assert normalize_prefix_override("dev ") == "dev "
    assert normalize_prefix_override("  dev  ") == "dev "
    assert normalize_prefix_override("?") == "?"        # symbol prefixes stay glued: "?ping"
    assert normalize_prefix_override("") is None
    assert normalize_prefix_override("   ") is None
    assert normalize_prefix_override(None) is None


def test_resolve_base_prefix():
    index = build_channel_index([{"guild_id": 1, "prefix": "lol ", "channels": []},
                                 {"guild_id": 2, "channels": []}])
    # no override: per-server prefix, else default; DMs use the default
    assert resolve_base_prefix(1, index, "!") == "lol "
    assert resolve_base_prefix(2, index, "!") == "!"
    assert resolve_base_prefix(None, index, "!") == "!"
    # override: every server uses it, DMs keep the default
    assert resolve_base_prefix(1, index, "!", "dev ") == "dev "
    assert resolve_base_prefix(2, index, "!", "dev ") == "dev "
    assert resolve_base_prefix(99, index, "!", "dev ") == "dev "
    assert resolve_base_prefix(None, index, "!", "dev ") == "!"
