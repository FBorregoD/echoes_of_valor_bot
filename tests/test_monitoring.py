"""Tests for the admin-only monitoring commands (!status, !usage_report) and
the usage_report scheduled action — monitoring.py plus the command_usage
table in scheduler.py and the per-command sheet-fetch counter in match_utils."""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import match_utils
import monitoring
import scheduler
from monitoring import build_status, build_usage_report, format_span, parse_days, send_usage_report
from remote_config import CONFIG_SOURCE_FALLBACK, CONFIG_SOURCE_REMOTE

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(scheduler, "DB_PATH", path)
    scheduler.init_db()
    yield
    os.remove(path)


def _row(user_id, name, command, *, invoked_as=None, location="DM", success=True,
         error=None, duration_ms=1000, sheet_fetches=0, ts=NOW):
    return {"ts": ts.isoformat(), "user_id": user_id, "user_name": name, "command": command,
            "invoked_as": invoked_as or command, "location": location, "success": int(success),
            "error": error, "duration_ms": duration_ms, "sheet_fetches": sheet_fetches}


# ── helpers ─────────────────────────────────────────────────────────────────

def test_parse_days():
    assert parse_days(None) == 7
    assert parse_days("30") == 30
    assert parse_days("ALL") is None
    for bad in ("0", "-3", "week"):
        with pytest.raises(ValueError):
            parse_days(bad)


@pytest.mark.parametrize("seconds, text", [(40, "40s"), (300, "5m"), (3 * 3600 + 720, "3h 12m"),
                                           (2 * 86400 + 4 * 3600, "2d 4h")])
def test_format_span(seconds, text):
    assert format_span(seconds) == text


# ── usage report ────────────────────────────────────────────────────────────

def test_usage_report_counts_users_commands_and_unused():
    rows = [
        _row(1, "supertalls", "matches", invoked_as="m", duration_ms=12000, sheet_fetches=1),
        _row(1, "supertalls", "matches", invoked_as="m", duration_ms=2000, sheet_fetches=0),
        _row(1, "supertalls", "summary", invoked_as="sum", duration_ms=3000, sheet_fetches=0),
        _row(2, "scorium", "matches", duration_ms=1000, sheet_fetches=0),
        _row(2, "scorium", "schedule add", location="Realms Rising", duration_ms=100, sheet_fetches=None),
        _row(3, "zeric", "?", invoked_as="mm", success=False, error="CommandNotFound",
             duration_ms=None, sheet_fetches=None),
    ]
    report = build_usage_report(rows, "last 7 days",
                                ["matches", "summary", "schedule", "standings", "division"])

    assert "**6** commands by **3** users" in report
    # matches: 3 uses, avg (12+2+1)/3 = 5.0s, 1 of 3 had to download -> 33%
    assert "`!matches` 3 · 5.0s · 33%" in report
    assert "`!summary` 1 · 3.0s · 0%" in report
    # subcommands count towards their group; no fetch data -> —
    assert "`!schedule` 1 · 0.1s · —" in report
    assert "**Never used:** `!division`, `!standings`" in report
    assert report.index("supertalls 3") < report.index("scorium 2") < report.index("zeric 1")
    assert "**Where:** DM 5 · Realms Rising 1" in report
    assert "**Failures:** 1 — CommandNotFound 1" in report
    assert "Unknown commands typed: `mm` 1" in report
    # Speed: cached rows 2s,3s,1s -> 2.0s (3); fetched 12s (1)
    assert "from cache 2.0s (3) · downloading the sheet 12.0s (1)" in report


def test_usage_report_empty_period():
    report = build_usage_report([], "last 7 days", ["matches"])
    assert "No commands used in this period." in report


def test_usage_report_reads_only_the_requested_period(temp_db):
    old = NOW - timedelta(days=20)
    for ts, command in ((old, "standings"), (NOW, "matches")):
        scheduler.record_command_usage(ts=ts, user_id=1, user_name="u", command=command,
                                       invoked_as=command, location="DM", success=True,
                                       error=None, duration_ms=10, sheet_fetches=0)
    assert [r["command"] for r in scheduler.list_command_usage()] == ["standings", "matches"]
    assert [r["command"] for r in scheduler.list_command_usage(since=NOW - timedelta(days=7))] == ["matches"]


# ── status ──────────────────────────────────────────────────────────────────

def _fake_bot(**overrides):
    bot = SimpleNamespace(
        user="Echoes_bot#4252",
        latency=0.12,
        started_at=NOW - timedelta(days=2, hours=4),
        last_outage=None,
        config_source=CONFIG_SOURCE_REMOTE,
        config={"tournaments": [{"alias": "EoV", "season": 11}], "guild_channels": [{}, {}, {}],
                "admin_user_ids": [10, 20]},
        commands=[SimpleNamespace(name="matches"), SimpleNamespace(name="summary")],
    )
    for k, v in overrides.items():
        setattr(bot, k, v)
    return bot


def test_status_reports_uptime_config_tasks_and_usage(temp_db):
    scheduler.add_task(action="post_divisions", params={"tournament": "EoV", "week": "3"},
                       guild_id=1, channel_id=2, thread_id=None, weekday=4, hour=8, minute=0,
                       tz="Europe/Madrid", created_by=10, current_week=3)
    bot = _fake_bot()
    for success in (True, False):
        scheduler.record_command_usage(ts=NOW - timedelta(hours=1), user_id=1, user_name="u",
                                       command="matches", invoked_as="m", location="DM",
                                       success=success, error=None, duration_ms=5, sheet_fetches=0)
    status = build_status(bot, now=NOW)
    assert "online 2d 4h" in status
    assert "Latency: 120 ms · no disconnects since start" in status
    assert "Config: Google Drive ✅ · tournaments: EoV S11 · 3 servers configured" in status
    assert "**Scheduled tasks:** 1" in status and "post_divisions" in status and "next week 3" in status
    assert "**Subscriptions:** 0" in status
    assert "**Commands since start:** 2 (1 failed)" in status


def test_status_flags_config_fallback_and_last_outage(temp_db):
    down = NOW - timedelta(hours=5)
    bot = _fake_bot(config_source=CONFIG_SOURCE_FALLBACK, last_outage=(down, down + timedelta(minutes=4)))
    status = build_status(bot, now=NOW)
    assert "⚠️ local file (Drive download failed)" in status
    assert "(4m)" in status


# ── scheduled usage_report ──────────────────────────────────────────────────

def _bot_with_users(config):
    users = {}

    async def fetch_user(uid):
        users[uid] = MagicMock()
        users[uid].send = AsyncMock()
        return users[uid]

    bot = _fake_bot(config=config)
    bot.fetch_user = fetch_user
    return bot, users


def test_scheduled_report_goes_to_alert_list(temp_db):
    bot, users = _bot_with_users({"alert_user_ids": [99], "admin_user_ids": [10, 20]})
    sent, failed = asyncio.run(send_usage_report(bot, 7))
    assert (sent, failed) == (1, 0)
    assert list(users) == [99]
    assert "Usage report — last 7 days" in users[99].send.call_args.args[0]


def test_scheduled_report_falls_back_to_admins(temp_db):
    bot, users = _bot_with_users({"admin_user_ids": [10, 20]})
    sent, failed = asyncio.run(send_usage_report(bot, None))
    assert (sent, failed) == (2, 0)
    assert "all time" in users[10].send.call_args.args[0]


def test_usage_report_is_a_registered_action():
    assert scheduler.REGISTERED_ACTIONS["usage_report"]["params"] == ["days", "others"]


# ── per-command sheet fetch counter ─────────────────────────────────────────

def test_sheet_fetch_counted_on_cache_miss_only(monkeypatch):
    monkeypatch.setattr(match_utils, "_memory_cache", {})
    monkeypatch.setattr(match_utils.ggx, "data_fromAllSheets", lambda url: {"Sheet": "data"})
    counter = [0]
    token = match_utils.sheet_fetch_counter.set(counter)
    try:
        match_utils.get_tournament_sheets("url-a")   # miss -> download
        match_utils.get_tournament_sheets("url-a")   # served from cache
        match_utils.get_tournament_sheets("url-b")   # miss -> download
    finally:
        match_utils.sheet_fetch_counter.reset(token)
    assert counter == [2]


def test_usage_report_alias_and_help():
    from help import get_help_embed
    assert "us" in monitoring.MonitoringCog.usage_report_command.aliases
    assert get_help_embed("@B", "us").title == "!usage_report / !us"


def test_parse_report_args():
    from monitoring import parse_report_args
    assert parse_report_args(()) == (7, False)
    assert parse_report_args(("30",)) == (30, False)
    assert parse_report_args(("others",)) == (7, True)
    assert parse_report_args(("all", "OTHERS")) == (None, True)
    assert parse_report_args(("others", "14")) == (14, True)
    for bad in (("week",), ("30", "7"), ("others", "others"), ("0",)):
        with pytest.raises(ValueError):
            parse_report_args(bad)


def test_report_excluding_the_callers_own_commands(temp_db):
    from monitoring import usage_report_text
    for user_id, name, command in ((10, "scorium", "status"), (10, "scorium", "matches"),
                                   (1, "supertalls", "matches")):
        scheduler.record_command_usage(ts=datetime.now(timezone.utc), user_id=user_id, user_name=name,
                                       command=command, invoked_as=command, location="DM",
                                       success=True, error=None, duration_ms=10, sheet_fetches=0)
    bot = _fake_bot(commands=[SimpleNamespace(name="matches"), SimpleNamespace(name="status")])
    full = usage_report_text(bot, 7)
    assert "**3** commands by **2** users" in full and "scorium" in full

    others = usage_report_text(bot, 7, exclude_user_id=10)
    assert "excluding your own commands" in others
    assert "**1** commands by **1** users" in others
    assert "scorium" not in others
    assert "**Never used:** `!status`" in others   # only the excluded user ran it


def test_scheduled_report_others_excludes_each_recipients_own_commands(temp_db):
    for user_id, name in ((10, "scorium"), (20, "otheradmin"), (1, "supertalls")):
        scheduler.record_command_usage(ts=datetime.now(timezone.utc), user_id=user_id, user_name=name,
                                       command="matches", invoked_as="m", location="DM",
                                       success=True, error=None, duration_ms=10, sheet_fetches=0)
    bot, users = _bot_with_users({"admin_user_ids": [10, 20]})
    assert asyncio.run(send_usage_report(bot, 7, exclude_recipient=True)) == (2, 0)
    to_10 = users[10].send.call_args.args[0]
    to_20 = users[20].send.call_args.args[0]
    assert "scorium" not in to_10 and "otheradmin" in to_10
    assert "otheradmin" not in to_20 and "scorium" in to_20
    assert "excluding your own commands" in to_10


def test_scheduler_dispatch_passes_others_flag(temp_db):
    cog = scheduler.SchedulerCog.__new__(scheduler.SchedulerCog)
    cog.bot, cog.cog_ref = _fake_bot(), MagicMock()
    with patch("monitoring.send_usage_report", AsyncMock(return_value=(1, 0))) as send:
        asyncio.run(cog._dispatch("usage_report", MagicMock(), {"days": "30", "others": "yes"}, 1, None))
        send.assert_awaited_once_with(cog.bot, 30, exclude_recipient=True)
        asyncio.run(cog._dispatch("usage_report", MagicMock(), {}, 1, None))
        send.assert_awaited_with(cog.bot, 7, exclude_recipient=False)
