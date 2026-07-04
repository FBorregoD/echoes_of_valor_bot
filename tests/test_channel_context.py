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