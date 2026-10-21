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


# ── server message filter: admins only, configured channels only ───────────

from types import SimpleNamespace
from unittest.mock import MagicMock

from channel_context import accepts_server_message, is_channel_allowed

ADMINS = [254177975417700352]
RR_GUILD = 870492122023866448


def _index():
    return build_channel_index([{
        "guild_id": RR_GUILD, "prefix": "!",
        "channels": [
            {"channel_id": 1104950545376088075, "channel_name": "bot-testing", "tournament": None},
            {"channel_id": 1180965888732708884, "channel_name": "echoes-of-valor-hero-league", "tournament": "EoV"},
        ],
    }])


def _text_channel(channel_id, name):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id, ch.name = channel_id, name
    return ch


def _message(author_id, channel, guild_id=RR_GUILD):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), guild=SimpleNamespace(id=guild_id),
                           channel=channel)


def test_admin_in_configured_channel_is_accepted():
    msg = _message(ADMINS[0], _text_channel(1104950545376088075, "bot-testing"))
    assert accepts_server_message(msg, ADMINS, _index())


def test_regular_user_is_ignored_even_in_configured_channel():
    msg = _message(1204452353001005058, _text_channel(1104950545376088075, "bot-testing"))
    assert not accepts_server_message(msg, ADMINS, _index())


def test_admin_in_unconfigured_channel_is_ignored():
    # e.g. "!roll" meant for another bot in a general channel
    msg = _message(ADMINS[0], _text_channel(999, "general-chat"))
    assert not accepts_server_message(msg, ADMINS, _index())


def test_admin_in_thread_of_configured_channel_is_accepted():
    parent = _text_channel(1180965888732708884, "echoes-of-valor-hero-league")
    thread = MagicMock(spec=discord.Thread)
    thread.parent = parent
    assert accepts_server_message(_message(ADMINS[0], thread), ADMINS, _index())


def test_unconfigured_server_is_ignored():
    msg = _message(ADMINS[0], _text_channel(1104950545376088075, "bot-testing"), guild_id=12345)
    assert not accepts_server_message(msg, ADMINS, _index())


def test_channel_matched_by_name_when_id_differs():
    msg = _message(ADMINS[0], _text_channel(42, "Bot-Testing"))
    assert is_channel_allowed(msg.guild, msg.channel, _index())
