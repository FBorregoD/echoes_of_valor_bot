"""
Unit tests for the !subscribe / !unsubscribe feature:
- scheduler.py's subscriptions table (add_subscription, remove_all_subscriptions,
  list_subscriptions) against a real temp SQLite file.
- scheduler.py's describe_schedule_gmt (weekly times converted to GMT/UTC).
- commands.py's subscribe_command / unsubscribe_command (DM-only check + happy path).
- scheduler.py's SchedulerCog._dispatch "notify_subscribed_players" branch.
"""
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord.ext import commands

import scheduler
from scheduler import SchedulerCog
from commands import TournamentCommands


# ── subscriptions table (real temp DB) ──────────────────────────────────────

@pytest.fixture
def temp_db(monkeypatch):
    """Point scheduler.DB_PATH at a fresh temp file so tests don't touch tasks.db."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(scheduler, "DB_PATH", path)
    scheduler.init_db()
    yield
    os.remove(path)


def test_add_subscription_creates_a_row(temp_db):
    scheduler.add_subscription(111, "Scorium", False)
    rows = scheduler.list_subscriptions(111)
    assert len(rows) == 1
    assert rows[0]["player"] == "Scorium"
    assert rows[0]["text_flag"] == 0


def test_add_subscription_rejects_empty_player(temp_db):
    with pytest.raises(ValueError):
        scheduler.add_subscription(111, "", False)
    with pytest.raises(ValueError):
        scheduler.add_subscription(111, "   ", False)
    assert scheduler.list_subscriptions(111) == []


def test_add_subscription_rejects_second_subscription_for_same_user(temp_db):
    """Only one active subscription per user — re-subscribing (same or different player) is rejected."""
    scheduler.add_subscription(111, "Scorium", False)
    with pytest.raises(ValueError):
        scheduler.add_subscription(111, "Scorium", True)  # same player again
    with pytest.raises(ValueError):
        scheduler.add_subscription(111, "OtherPlayer", False)  # different player
    rows = scheduler.list_subscriptions(111)
    assert len(rows) == 1
    assert rows[0]["player"] == "Scorium"
    assert rows[0]["text_flag"] == 0  # unchanged by the rejected attempts


def test_add_subscription_allows_different_users_independently(temp_db):
    scheduler.add_subscription(111, "Scorium", False)
    scheduler.add_subscription(222, "OtherPlayer", True)
    assert len(scheduler.list_subscriptions(111)) == 1
    assert len(scheduler.list_subscriptions(222)) == 1


def test_add_subscription_allows_resubscribing_after_unsubscribe(temp_db):
    """After !unsub clears the row, the user is free to subscribe to a new player."""
    scheduler.add_subscription(111, "Scorium", False)
    scheduler.remove_all_subscriptions(111)
    scheduler.add_subscription(111, "OtherPlayer", True)
    rows = scheduler.list_subscriptions(111)
    assert len(rows) == 1
    assert rows[0]["player"] == "OtherPlayer"


def test_add_subscription_non_latin_names_work_for_different_users(temp_db):
    """
    Regression test (2026-07-17): normalize_name used to strip all non-Latin
    characters, so two different non-Latin player names both produced the
    same empty player_key. With the one-subscription-per-user table now keyed
    on user_id alone, this could still matter for any future lookup by
    player_key — confirm two different users' non-Latin names are stored
    distinctly and correctly.
    """
    scheduler.add_subscription(111, "田中", False)
    scheduler.add_subscription(222, "Иванов", True)
    assert scheduler.list_subscriptions(111)[0]["player"] == "田中"
    assert scheduler.list_subscriptions(222)[0]["player"] == "Иванов"


def test_remove_all_subscriptions_clears_only_that_user(temp_db):
    scheduler.add_subscription(111, "Scorium", False)
    scheduler.add_subscription(222, "OtherPlayer", False)

    removed = scheduler.remove_all_subscriptions(111)

    assert removed == 1
    assert scheduler.list_subscriptions(111) == []
    assert len(scheduler.list_subscriptions(222)) == 1


def test_remove_all_subscriptions_with_none_returns_zero(temp_db):
    assert scheduler.remove_all_subscriptions(999) == 0


def test_list_subscriptions_without_user_id_returns_everyone(temp_db):
    scheduler.add_subscription(111, "Scorium", False)
    scheduler.add_subscription(222, "OtherPlayer", True)
    assert len(scheduler.list_subscriptions()) == 2


# ── describe_schedule_gmt ────────────────────────────────────────────────────

def test_describe_schedule_gmt_interval_task():
    row = {"interval_minutes": 120, "weekday": None, "hour": 0, "minute": 0, "tz": "UTC"}
    assert scheduler.describe_schedule_gmt(row) == "every 2h"


def test_describe_schedule_gmt_weekly_utc_is_unchanged():
    row = {"interval_minutes": None, "weekday": 0, "hour": 9, "minute": 0, "tz": "UTC"}
    assert scheduler.describe_schedule_gmt(row) == "Monday at 09:00 GMT"


def test_describe_schedule_gmt_weekly_converts_across_day_boundary():
    # Buenos Aires is a fixed UTC-3 offset (no DST) -> 22:00 Monday local = 01:00 Tuesday GMT.
    row = {"interval_minutes": None, "weekday": 0, "hour": 22, "minute": 0,
           "tz": "America/Argentina/Buenos_Aires"}
    assert scheduler.describe_schedule_gmt(row) == "Tuesday at 01:00 GMT"


# ── subscribe_command / unsubscribe_command ─────────────────────────────────

@pytest.fixture
def cog():
    tournaments = [{"name": "Echoes of Valor", "alias": "EoV", "url": "fake_url"}]
    return TournamentCommands(MagicMock(), tournaments, mapping_sheet_url="fake", default_week=2)


def make_guild_ctx():
    ctx = MagicMock()
    ctx.guild.id = 100
    ctx.send = AsyncMock()
    return ctx


def make_dm_ctx(user_id=555):
    ctx = MagicMock()
    ctx.guild = None
    ctx.author.id = user_id
    ctx.send = AsyncMock()
    return ctx


async def _run_checks(command, ctx):
    for check in command.checks:
        await check(ctx)


@pytest.mark.asyncio
async def test_subscribe_requires_dm(cog):
    ctx = make_guild_ctx()
    with pytest.raises(commands.CheckFailure, match="only works in DMs"):
        await _run_checks(TournamentCommands.subscribe_command, ctx)


@pytest.mark.asyncio
async def test_unsubscribe_requires_dm(cog):
    ctx = make_guild_ctx()
    with pytest.raises(commands.CheckFailure, match="only works in DMs"):
        await _run_checks(TournamentCommands.unsubscribe_command, ctx)


# Simulates a hero-builds roster with several *distinct* registered players —
# not just Scorium. Regression coverage for the 2026-08-02 bug: !sub validated
# against the !sendto Discord-ID mapping sheet (only 3 people had linked an
# ID) instead of the actual tournament roster, so real players like "samwise"
# and "tuff" (73 real entries in the live builds sheet) were wrongly rejected.
# Keys are normalize_name("Player - Hero"), matching what
# load_hero_builds_from_sheets really returns.
FAKE_BUILDS = {
    "scorium-saltamontes": "Human Wizard",
    "samwise-mordek": "Elf Fighter",
    "tuff-hugemistake": "Orc Barbarian",
    "supertalls-samwise ard": "Dwarf Cleric",  # hero name contains another player's name — must not false-match
}


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["Scorium", "samwise", "Samwise", "TUFF"])
async def test_subscribe_command_accepts_any_registered_player(cog, typed):
    ctx = make_dm_ctx(user_id=555)
    with patch("scheduler.list_subscriptions", return_value=[]), \
         patch("commands.get_tournament_sheets_async", return_value={}), \
         patch("commands.load_hero_builds_from_sheets", return_value=FAKE_BUILDS), \
         patch("scheduler.list_tasks_by_action", return_value=[]), \
         patch("scheduler.add_subscription") as mock_add:
        await TournamentCommands.subscribe_command.callback(cog, ctx, typed)
    mock_add.assert_called_once_with(555, typed, False)
    assert f"Subscribed to **{typed}**" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_subscribe_command_with_text_flag(cog):
    ctx = make_dm_ctx(user_id=555)
    with patch("scheduler.list_subscriptions", return_value=[]), \
         patch("commands.get_tournament_sheets_async", return_value={}), \
         patch("commands.load_hero_builds_from_sheets", return_value=FAKE_BUILDS), \
         patch("scheduler.list_tasks_by_action", return_value=[]), \
         patch("scheduler.add_subscription") as mock_add:
        await TournamentCommands.subscribe_command.callback(cog, ctx, "tuff", "text")
    mock_add.assert_called_once_with(555, "tuff", True)
    assert "as plain text" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_subscribe_command_never_matches_a_hero_name_substring(cog):
    """'ard' is a substring of the hero 'Samwise Ard' but must not match on that basis."""
    ctx = make_dm_ctx(user_id=555)
    with patch("scheduler.list_subscriptions", return_value=[]), \
         patch("commands.get_tournament_sheets_async", return_value={}), \
         patch("commands.load_hero_builds_from_sheets", return_value=FAKE_BUILDS), \
         patch("scheduler.add_subscription") as mock_add:
        await TournamentCommands.subscribe_command.callback(cog, ctx, "ard")
    mock_add.assert_not_called()
    assert "couldn't find a player named **ard**" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_subscribe_command_rejects_when_already_subscribed(cog):
    """One active subscription per user — a second !sub is rejected without even
    checking the tournament roster, and tells the user to !unsub first."""
    ctx = make_dm_ctx(user_id=555)
    existing = [{"player": "OtherGuy", "text_flag": 0}]
    with patch("scheduler.list_subscriptions", return_value=existing), \
         patch("commands.get_tournament_sheets_async") as mock_sheets, \
         patch("scheduler.add_subscription") as mock_add:
        await TournamentCommands.subscribe_command.callback(cog, ctx, "Scorium")
    mock_add.assert_not_called()
    mock_sheets.assert_not_called()
    msg = ctx.send.call_args[0][0]
    assert "already subscribed to **OtherGuy**" in msg
    assert "!unsub" in msg


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["NotAPlayer", "samwize", "zzz-nonexistent"])
async def test_subscribe_command_unknown_player_is_rejected(cog, typed):
    ctx = make_dm_ctx(user_id=555)
    with patch("scheduler.list_subscriptions", return_value=[]), \
         patch("commands.get_tournament_sheets_async", return_value={}), \
         patch("commands.load_hero_builds_from_sheets", return_value=FAKE_BUILDS), \
         patch("scheduler.add_subscription") as mock_add:
        await TournamentCommands.subscribe_command.callback(cog, ctx, typed)
    mock_add.assert_not_called()
    msg = ctx.send.call_args[0][0]
    assert f"couldn't find a player named **{typed}**" in msg
    assert "registered in the tournaments" in msg


@pytest.mark.asyncio
async def test_subscribe_command_handles_roster_load_failure_gracefully(cog):
    """If every tournament's roster fails to load, tell the user to retry
    rather than the misleading 'player not found' (which implies a typo)."""
    ctx = make_dm_ctx(user_id=555)
    with patch("scheduler.list_subscriptions", return_value=[]), \
         patch("commands.get_tournament_sheets_async", side_effect=RuntimeError("sheet unreachable")), \
         patch("scheduler.add_subscription") as mock_add:
        await TournamentCommands.subscribe_command.callback(cog, ctx, "Scorium")
    mock_add.assert_not_called()
    assert "Couldn't load the tournament rosters" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_subscribe_command_keeps_checking_other_tournaments_if_one_fails(cog):
    """One tournament's sheet failing to load shouldn't block finding the
    player in a different tournament."""
    ctx = make_dm_ctx(user_id=555)
    cog.tournaments = [
        {"name": "Echoes of Valor", "alias": "EoV", "url": "eov_url"},
        {"name": "Meridian Ascension", "alias": "MA", "url": "ma_url"},
    ]
    with patch("scheduler.list_subscriptions", return_value=[]), \
         patch("commands.get_tournament_sheets_async", side_effect=[RuntimeError("down"), {}]), \
         patch("commands.load_hero_builds_from_sheets", return_value=FAKE_BUILDS), \
         patch("scheduler.list_tasks_by_action", return_value=[]), \
         patch("scheduler.add_subscription") as mock_add:
        await TournamentCommands.subscribe_command.callback(cog, ctx, "tuff")
    mock_add.assert_called_once_with(555, "tuff", False)


@pytest.mark.asyncio
async def test_subscribe_command_reports_configured_schedule_in_gmt(cog):
    ctx = make_dm_ctx(user_id=555)
    fake_task = {"id": 1}  # describe_schedule_gmt is mocked, so its shape doesn't matter here
    with patch("scheduler.list_subscriptions", return_value=[]), \
         patch("commands.get_tournament_sheets_async", return_value={}), \
         patch("commands.load_hero_builds_from_sheets", return_value=FAKE_BUILDS), \
         patch("scheduler.list_tasks_by_action", return_value=[fake_task]), \
         patch("scheduler.describe_schedule_gmt", return_value="Monday at 08:00 GMT"), \
         patch("scheduler.add_subscription"):
        await TournamentCommands.subscribe_command.callback(cog, ctx, "Scorium")
    assert "Monday at 08:00 GMT" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_subscribe_command_reports_no_schedule_configured(cog):
    ctx = make_dm_ctx(user_id=555)
    with patch("scheduler.list_subscriptions", return_value=[]), \
         patch("commands.get_tournament_sheets_async", return_value={}), \
         patch("commands.load_hero_builds_from_sheets", return_value=FAKE_BUILDS), \
         patch("scheduler.list_tasks_by_action", return_value=[]), \
         patch("scheduler.add_subscription"):
        await TournamentCommands.subscribe_command.callback(cog, ctx, "Scorium")
    assert "no notification schedule is set up yet" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_subscribe_command_requires_player_name(cog):
    ctx = make_dm_ctx()
    with patch("scheduler.add_subscription") as mock_add:
        await TournamentCommands.subscribe_command.callback(cog, ctx, None)
    mock_add.assert_not_called()
    assert "Please specify a player name" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_unsubscribe_command_reports_count(cog):
    ctx = make_dm_ctx(user_id=555)
    with patch("scheduler.remove_all_subscriptions", return_value=3) as mock_remove:
        await TournamentCommands.unsubscribe_command.callback(cog, ctx)
    mock_remove.assert_called_once_with(555)
    assert "Removed 3 subscription(s)" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_unsubscribe_command_with_none_reports_empty(cog):
    ctx = make_dm_ctx(user_id=555)
    with patch("scheduler.remove_all_subscriptions", return_value=0):
        await TournamentCommands.unsubscribe_command.callback(cog, ctx)
    assert "no active subscriptions" in ctx.send.call_args[0][0]


# ── show_subscriptions_command (!show_sub) ──────────────────────────────────

class FakeDiscordUser:
    """Minimal stand-in for a fetched discord.User — just needs str()."""
    def __init__(self, uid):
        self.id = uid

    def __str__(self):
        return f"user{self.id}"


@pytest.mark.asyncio
async def test_show_sub_requires_dm(cog):
    ctx = make_guild_ctx()
    with pytest.raises(commands.CheckFailure, match="only works in DMs"):
        await _run_checks(TournamentCommands.show_subscriptions_command, ctx)


@pytest.mark.asyncio
async def test_show_sub_requires_admin(cog):
    ctx = make_dm_ctx(user_id=555)
    ctx.bot.admin_user_ids = []  # explicitly not an admin
    with pytest.raises(commands.CheckFailure, match="permission"):
        await _run_checks(TournamentCommands.show_subscriptions_command, ctx)


@pytest.mark.asyncio
async def test_show_sub_with_no_subscriptions(cog):
    ctx = make_dm_ctx(user_id=555)
    with patch("scheduler.list_subscriptions", return_value=[]):
        await TournamentCommands.show_subscriptions_command.callback(cog, ctx)
    assert "No active subscriptions" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_show_sub_lists_users_and_their_players_comma_separated(cog):
    ctx = make_dm_ctx(user_id=555)
    subs = [
        {"user_id": 111, "player": "Scorium"},
        {"user_id": 222, "player": "samwise"},
    ]
    cog.bot.fetch_user = AsyncMock(side_effect=lambda uid: FakeDiscordUser(uid))
    with patch("scheduler.list_subscriptions", return_value=subs):
        await TournamentCommands.show_subscriptions_command.callback(cog, ctx)
    msg = ctx.send.call_args[0][0]
    assert "2 active subscription(s)" in msg
    assert "user111 → Scorium" in msg
    assert "user222 → samwise" in msg
    assert "," in msg


@pytest.mark.asyncio
async def test_show_sub_falls_back_to_raw_id_if_fetch_user_fails(cog):
    ctx = make_dm_ctx(user_id=555)
    subs = [{"user_id": 999, "player": "Scorium"}]
    cog.bot.fetch_user = AsyncMock(side_effect=RuntimeError("unknown user"))
    with patch("scheduler.list_subscriptions", return_value=subs):
        await TournamentCommands.show_subscriptions_command.callback(cog, ctx)
    assert "999 → Scorium" in ctx.send.call_args[0][0]


# ── SchedulerCog._dispatch("notify_subscribed_players") ────────────────────

def _make_fake_self():
    cog_ref = MagicMock()
    cog_ref._build_and_send_player_matches = AsyncMock()
    cog_ref._build_and_send_player_summary = AsyncMock()
    fake_self = SimpleNamespace(cog_ref=cog_ref, bot=MagicMock())
    return fake_self, cog_ref


@pytest.mark.asyncio
async def test_dispatch_notify_subscribed_players_sends_to_each_subscriber():
    fake_self, cog_ref = _make_fake_self()
    fake_self.bot.fetch_user = AsyncMock(side_effect=lambda uid: MagicMock(id=uid))
    destination = MagicMock()
    destination.send = AsyncMock()

    subs = [
        {"user_id": 111, "player": "Scorium", "text_flag": 0},
        {"user_id": 222, "player": "OtherPlayer", "text_flag": 1},
    ]

    with patch("scheduler.list_subscriptions", return_value=subs), \
         patch("asyncio.sleep", new=AsyncMock()):
        await SchedulerCog._dispatch(
            fake_self, "notify_subscribed_players", destination, {}, task_id=1, auto_week=None,
        )

    assert cog_ref._build_and_send_player_matches.await_count == 2
    first_call_args = cog_ref._build_and_send_player_matches.await_args_list[0].args
    assert first_call_args[1] == "Scorium"
    assert first_call_args[2] is False  # text_flag=0 -> False
    second_call_args = cog_ref._build_and_send_player_matches.await_args_list[1].args
    assert second_call_args[2] is True  # text_flag=1 -> True
    destination.send.assert_awaited_once()
    assert "Notified 2 subscriber(s)" in destination.send.call_args[0][0]

    # Each subscriber also gets a !summary appended after their matches.
    assert cog_ref._build_and_send_player_summary.await_count == 2
    summary_call_args = [c.args for c in cog_ref._build_and_send_player_summary.await_args_list]
    assert summary_call_args[0][1] == "Scorium"
    assert summary_call_args[1][1] == "OtherPlayer"


@pytest.mark.asyncio
async def test_dispatch_notify_subscribed_players_continues_after_one_failure():
    fake_self, cog_ref = _make_fake_self()

    async def fetch_user(uid):
        if uid == 111:
            raise RuntimeError("DMs disabled")
        return MagicMock(id=uid)
    fake_self.bot.fetch_user = fetch_user

    destination = MagicMock()
    destination.send = AsyncMock()

    subs = [
        {"user_id": 111, "player": "Scorium", "text_flag": 0},
        {"user_id": 222, "player": "OtherPlayer", "text_flag": 0},
    ]

    with patch("scheduler.list_subscriptions", return_value=subs), \
         patch("asyncio.sleep", new=AsyncMock()):
        await SchedulerCog._dispatch(
            fake_self, "notify_subscribed_players", destination, {}, task_id=1, auto_week=None,
        )

    assert cog_ref._build_and_send_player_matches.await_count == 1
    assert cog_ref._build_and_send_player_summary.await_count == 1
    msg = destination.send.call_args[0][0]
    assert "Notified 1 subscriber(s)" in msg
    assert "1 failed" in msg


@pytest.mark.asyncio
async def test_dispatch_notify_subscribed_players_with_no_subscribers():
    fake_self, cog_ref = _make_fake_self()
    destination = MagicMock()
    destination.send = AsyncMock()

    with patch("scheduler.list_subscriptions", return_value=[]):
        await SchedulerCog._dispatch(
            fake_self, "notify_subscribed_players", destination, {}, task_id=1, auto_week=None,
        )

    cog_ref._build_and_send_player_matches.assert_not_awaited()
    cog_ref._build_and_send_player_summary.assert_not_awaited()
    assert "Notified 0 subscriber(s)" in destination.send.call_args[0][0]


@pytest.mark.asyncio
async def test_dispatch_notify_subscribed_players_sends_summary_after_matches():
    """The !summary appended to a subscriber's DM must come after their
    matches, not before or instead of them."""
    fake_self, cog_ref = _make_fake_self()
    fake_self.bot.fetch_user = AsyncMock(side_effect=lambda uid: MagicMock(id=uid))
    destination = MagicMock()
    destination.send = AsyncMock()

    order = []
    cog_ref._build_and_send_player_matches.side_effect = lambda *a, **k: order.append("matches")
    cog_ref._build_and_send_player_summary.side_effect = lambda *a, **k: order.append("summary")

    subs = [{"user_id": 111, "player": "Scorium", "text_flag": 0}]

    with patch("scheduler.list_subscriptions", return_value=subs), \
         patch("asyncio.sleep", new=AsyncMock()):
        await SchedulerCog._dispatch(
            fake_self, "notify_subscribed_players", destination, {}, task_id=1, auto_week=None,
        )

    assert order == ["matches", "summary"]
