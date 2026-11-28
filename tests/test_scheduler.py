"""
Unit tests for SchedulerCog._dispatch — specifically the blocking-I/O
regression from the 2026-07-17 production incident (see test_commands.py
for the sibling test on TournamentCommands._get_most_recent_week), and the
2026-08-08 fix for season-end detection (see _season_over in scheduler.py).

_dispatch is exercised directly via SchedulerCog._dispatch(fake_self, ...)
rather than through a real SchedulerCog instance, since the constructor
starts a discord.ext.tasks background loop that isn't needed here and
isn't safe to spin up against a plain MagicMock bot.
"""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from scheduler import SchedulerCog


def make_fake_self(tournaments, default_week=2):
    cog_ref = MagicMock()
    cog_ref.tournaments = tournaments
    cog_ref.default_week = default_week
    admin = MagicMock()
    admin.send = AsyncMock()
    bot = MagicMock()
    bot.config = {"admin_user_ids": [99]}
    bot.fetch_user = AsyncMock(return_value=admin)
    fake = SimpleNamespace(cog_ref=cog_ref, bot=bot, admin=admin)
    for name in ("_notify_admins", "_report", "_end_season", "_post_season_leftovers"):
        setattr(fake, name, getattr(SchedulerCog, name).__get__(fake))
    return fake


def assert_season_over_message(fake_self, destination):
    """The channel only hears that the season is over; the task details
    (ID, how to remove it) go to the admins by DM."""
    public = destination.send.call_args[0][0]
    assert "the season is over" in public.lower()
    assert "ID" not in public and "schedule remove" not in public
    fake_self.admin.send.assert_awaited_once()
    assert "!schedule remove 1" in fake_self.admin.send.call_args[0][0]


def sheets_with_week(week: int) -> dict:
    """A minimal division sheet with one match row under `Week {week}`."""
    df = pd.DataFrame({
        'A': ['SCHEDULE', f'Week {week}', None],
        'B': [None, 'Round 1', None],
        'C': [None, 'PlayerA - HeroA', None],
        'D': [None, 'PlayerB - HeroB', None],
        'E': [None, None, None],
        'F': [None, None, None],
        'G': [None, '', None],
    })
    return {'Diamond': df}


@pytest.mark.asyncio
async def test_dispatch_post_divisions_sheets_refetch_does_not_block_event_loop(assert_does_not_block_event_loop):
    """
    Regression test for the 2026-07-17 production incident
    ("Shard ID None heartbeat blocked for more than 10 seconds"). Before
    posting divisions with auto-advance on, _dispatch checks season-end via
    _season_over, which calls get_tournament_sheets directly — this is the
    exact code path that froze the bot in production. It must run via
    asyncio.to_thread so a slow/hanging Google Sheets fetch can't stall
    the Discord heartbeat or any other concurrent task.
    """
    tournaments = [{"name": "Meridian Ascension", "alias": "MA", "url": "fake_url"}]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    def slow_fetch(url, force_refresh=False):
        time.sleep(0.25)
        return sheets_with_week(3)

    with patch("scheduler.run_post_divisions", new=AsyncMock(return_value=(1, [], []))), \
         patch("scheduler.advance_auto_week", new=AsyncMock()), \
         patch("match_utils.get_tournament_sheets", side_effect=slow_fetch):
        await assert_does_not_block_event_loop(
            SchedulerCog._dispatch(
                fake_self, "post_divisions", destination,
                {"tournament": "MA", "week": "3"}, task_id=1, auto_week=3, end_week=None,
            )
        )


@pytest.mark.asyncio
async def test_dispatch_notify_all_sheets_refetch_does_not_block_event_loop(assert_does_not_block_event_loop):
    """Same regression, other branch: notify_all's auto-advance season-over check."""
    tournaments = [{"name": "Echoes of Valor", "alias": "EoV", "url": "fake_url"}]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    def slow_fetch(url, force_refresh=False):
        time.sleep(0.25)
        return sheets_with_week(3)

    with patch("scheduler.run_notify_all", new=AsyncMock(return_value=(0, 0))), \
         patch("scheduler.advance_auto_week", new=AsyncMock()), \
         patch("match_utils.get_tournament_sheets", side_effect=slow_fetch):
        await assert_does_not_block_event_loop(
            SchedulerCog._dispatch(
                fake_self, "notify_all", destination,
                {"week": "3"}, task_id=2, auto_week=3, end_week=None,
            )
        )


@pytest.mark.asyncio
async def test_dispatch_post_divisions_still_advances_week_correctly():
    """Functional check alongside the timing test: values still flow through correctly."""
    tournaments = [{"name": "Meridian Ascension", "alias": "MA", "url": "fake_url"}]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    with patch("scheduler.run_post_divisions", new=AsyncMock(return_value=(3, [], []))), \
         patch("scheduler.advance_auto_week", new=AsyncMock()) as mock_advance, \
         patch("match_utils.get_tournament_sheets", return_value=sheets_with_week(3)):
        await SchedulerCog._dispatch(
            fake_self, "post_divisions", destination,
            {"tournament": "MA", "week": "3"}, task_id=1, auto_week=3, end_week=8,
        )

    # The run summary is for the admins by DM, not for the public channel.
    destination.send.assert_not_awaited()
    fake_self.admin.send.assert_awaited_once()
    assert "Posted to 3 divisions" in fake_self.admin.send.call_args[0][0]
    mock_advance.assert_awaited_once()
    _, kwargs = mock_advance.call_args
    assert kwargs["task_id"] == 1
    assert kwargs["current_week"] == 3
    assert kwargs["end_week"] == 8


@pytest.mark.asyncio
async def test_dispatch_post_divisions_marks_season_over_on_first_empty_week():
    """
    2026-08-08 fix: season-end must be detected when the fire for the (now
    nonexistent) next week finds no matches — not by peeking ahead right
    after posting the last real week. Here auto_week=4 has no matches, so
    this fire should mark the task exhausted and skip posting entirely
    (no "Posted to 0 divisions" spam, no advance_auto_week call).
    """
    tournaments = [{"name": "Meridian Ascension", "alias": "MA", "url": "fake_url"}]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    with patch("scheduler.run_post_divisions", new=AsyncMock(return_value=(2, [], []))) as mock_post, \
         patch("scheduler.advance_auto_week", new=AsyncMock()) as mock_advance, \
         patch("scheduler.update_current_week") as mock_update, \
         patch("match_utils.get_tournament_sheets", return_value=sheets_with_week(3)):
        await SchedulerCog._dispatch(
            fake_self, "post_divisions", destination,
            {"tournament": "MA", "week": "4"}, task_id=1, auto_week=4, end_week=None,
        )

    # Only the leftovers are posted (week 4 = after the last week, so every
    # division lists just its unfinished matches), under the season-over label.
    mock_post.assert_awaited_once()
    assert mock_post.call_args.kwargs["week_raw"] == 4
    assert mock_post.call_args.kwargs["week_label"] == "Season over · Matches left"
    mock_advance.assert_not_awaited()
    mock_update.assert_called_once_with(1, -1)
    destination.send.assert_awaited_once()
    assert_season_over_message(fake_self, destination)
    assert "still unfinished" in destination.send.call_args[0][0]


@pytest.mark.asyncio
async def test_dispatch_post_divisions_keeps_posting_last_real_week():
    """
    The week that still has matches (even if it's the tournament's last one)
    must still be posted normally and advance current_week — the season is
    only declared over on the *following* fire, once that next week is
    checked and found empty (see the test above).
    """
    tournaments = [{"name": "Meridian Ascension", "alias": "MA", "url": "fake_url"}]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    with patch("scheduler.run_post_divisions", new=AsyncMock(return_value=(1, [], []))) as mock_post, \
         patch("scheduler.advance_auto_week", new=AsyncMock()) as mock_advance, \
         patch("scheduler.update_current_week") as mock_update, \
         patch("match_utils.get_tournament_sheets", return_value=sheets_with_week(3)):
        await SchedulerCog._dispatch(
            fake_self, "post_divisions", destination,
            {"tournament": "MA", "week": "3"}, task_id=1, auto_week=3, end_week=None,
        )

    mock_post.assert_awaited_once()
    mock_advance.assert_awaited_once()
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_post_divisions_season_over_scoped_to_own_tournament():
    """
    2026-08-08 follow-up fix: a task's season-over check must only look at
    its OWN tournament's sheet, not every configured tournament. Before this
    fix, _season_over checked week_has_matches across ALL of cog.tournaments,
    so a shorter tournament's task would never be marked exhausted as long
    as some OTHER, longer-running tournament still had matches for that week
    number — riding the wrong tournament's schedule.
    """
    tournaments = [
        {"name": "Meridian Ascension", "alias": "MA", "url": "fake_url_ma"},
        {"name": "Echoes of Valor", "alias": "EoV", "url": "fake_url_eov"},
    ]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    def fake_fetch(url, force_refresh=False):
        # MA's season ended at week 3 (no week-4 matches); EoV's is longer
        # and still has week-4 matches.
        if url == "fake_url_ma":
            return sheets_with_week(3)
        return sheets_with_week(4)

    with patch("scheduler.run_post_divisions", new=AsyncMock(return_value=(2, [], []))) as mock_post, \
         patch("scheduler.advance_auto_week", new=AsyncMock()) as mock_advance, \
         patch("scheduler.update_current_week") as mock_update, \
         patch("match_utils.get_tournament_sheets", side_effect=fake_fetch):
        await SchedulerCog._dispatch(
            fake_self, "post_divisions", destination,
            {"tournament": "MA", "week": "4"}, task_id=1, auto_week=4, end_week=None,
        )

    # MA's own task must be marked exhausted, ignoring EoV's week-4 data.
    # Only the leftovers are posted (week 4 = after the last week, so every
    # division lists just its unfinished matches), under the season-over label.
    mock_post.assert_awaited_once()
    assert mock_post.call_args.kwargs["week_raw"] == 4
    assert mock_post.call_args.kwargs["week_label"] == "Season over · Matches left"
    mock_advance.assert_not_awaited()
    mock_update.assert_called_once_with(1, -1)
    destination.send.assert_awaited_once()
    assert_season_over_message(fake_self, destination)
    assert "still unfinished" in destination.send.call_args[0][0]


@pytest.mark.asyncio
async def test_dispatch_notify_all_season_over_checks_all_tournaments():
    """
    Unlike post_divisions, notify_all has no single 'tournament' param — it
    DMs pending matches across every configured tournament (run_notify_all
    iterates all of cog.tournaments), so its season-over check is
    intentionally scoped to all tournaments too, not just one. This is a
    documented asymmetry (see the comment in _dispatch), not a bug — this
    test guards against it being "fixed" into the same per-task scoping as
    post_divisions, which would be wrong for this action.
    """
    tournaments = [
        {"name": "Meridian Ascension", "alias": "MA", "url": "fake_url_ma"},
        {"name": "Echoes of Valor", "alias": "EoV", "url": "fake_url_eov"},
    ]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    def fake_fetch(url, force_refresh=False):
        if url == "fake_url_ma":
            return sheets_with_week(3)   # no week-4 matches
        return sheets_with_week(4)       # EoV still has week-4 matches

    with patch("scheduler.run_notify_all", new=AsyncMock(return_value=(0, 0))) as mock_notify, \
         patch("scheduler.advance_auto_week", new=AsyncMock()) as mock_advance, \
         patch("scheduler.update_current_week") as mock_update, \
         patch("match_utils.get_tournament_sheets", side_effect=fake_fetch):
        await SchedulerCog._dispatch(
            fake_self, "notify_all", destination,
            {"week": "4"}, task_id=2, auto_week=4, end_week=None,
        )

    # Because EoV still has week-4 matches, notify_all must NOT be marked
    # exhausted — it keeps running.
    mock_notify.assert_awaited_once()
    mock_advance.assert_awaited_once()
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_season_over_with_every_match_finished_thanks_players():
    tournaments = [{"name": "Echoes of Valor", "alias": "EoV", "url": "fake_url"}]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    with patch("scheduler.run_post_divisions", new=AsyncMock(return_value=(0, [], []))),          patch("scheduler.update_current_week"),          patch("match_utils.get_tournament_sheets", return_value=sheets_with_week(3)):
        await SchedulerCog._dispatch(
            fake_self, "post_divisions", destination,
            {"tournament": "EoV", "week": "4"}, task_id=1, auto_week=4, end_week=None,
        )

    public = destination.send.call_args[0][0]
    assert "Echoes of Valor — the season is over!" in public
    assert "Every match is finished" in public
    assert_season_over_message(fake_self, destination)


@pytest.mark.asyncio
async def test_notify_all_season_over_keeps_task_details_out_of_the_channel():
    tournaments = [{"name": "Echoes of Valor", "alias": "EoV", "url": "fake_url"}]
    fake_self = make_fake_self(tournaments)
    destination = MagicMock()
    destination.send = AsyncMock()

    with patch("scheduler.run_notify_all", new=AsyncMock()) as mock_notify,          patch("scheduler.update_current_week") as mock_update,          patch("match_utils.get_tournament_sheets", return_value=sheets_with_week(3)):
        await SchedulerCog._dispatch(
            fake_self, "notify_all", destination,
            {"week": "4"}, task_id=1, auto_week=4, end_week=None,
        )

    mock_notify.assert_not_awaited()
    mock_update.assert_called_once_with(1, -1)
    assert_season_over_message(fake_self, destination)


@pytest.mark.asyncio
async def test_failed_task_is_reported_to_admins_not_the_channel():
    fake_self = make_fake_self([{"name": "Echoes of Valor", "alias": "EoV", "url": "u"}])
    destination = MagicMock()
    destination.send = AsyncMock()
    guild = MagicMock()
    guild.get_thread.return_value = None
    guild.get_channel.return_value = destination
    fake_self.bot.get_guild.return_value = guild
    fake_self.bot.channel_index = {}
    fake_self._dispatch = AsyncMock(side_effect=RuntimeError("sheet down"))
    row = {"guild_id": 1, "thread_id": None, "channel_id": 2, "params": "{}", "id": 7,
           "current_week": None, "end_week": None, "action": "standings"}

    with patch("scheduler._is_channel_allowed", return_value=True):
        await SchedulerCog._fire(fake_self, row)

    destination.send.assert_not_awaited()
    assert "(ID 7) failed: sheet down" in fake_self.admin.send.call_args[0][0]
