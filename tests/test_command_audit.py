import asyncio
import logging
import os
import tempfile
from types import SimpleNamespace

import pytest
from discord.ext import commands

import command_audit
import scheduler
from match_utils import sheet_fetch_counter, _note_sheet_fetch
from command_audit import describe_location, log_command_failure, log_command_finish, log_command_start


def _ctx(content="!m scorium", guild=None, channel_name="general", command="matches"):
    return SimpleNamespace(
        author=_User(),
        guild=SimpleNamespace(name=guild) if guild else None,
        channel=SimpleNamespace(name=channel_name),
        message=SimpleNamespace(content=content),
        command=SimpleNamespace(qualified_name=command) if command else None,
    )


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    """Every finished/failed command is stored in command_usage — keep that
    out of the real tasks.db."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(scheduler, "DB_PATH", path)
    scheduler.init_db()
    yield
    os.remove(path)


class _User:
    id = 42

    def __str__(self):
        return "fulano"


def test_describe_location_dm_and_guild():
    assert describe_location(_ctx()) == "DM"
    assert describe_location(_ctx(guild="Realms Rising", channel_name="eov")) == "Realms Rising/#eov"


def test_start_and_finish_lines(caplog):
    ctx = _ctx(guild="Realms Rising", channel_name="eov")
    with caplog.at_level(logging.INFO, logger="audit"):
        log_command_start(ctx)
        log_command_finish(ctx)
    start, finish = [r.getMessage() for r in caplog.records]
    assert start == "fulano (42) in Realms Rising/#eov: '!m scorium'"
    assert finish.startswith("fulano !matches done in ") and finish.endswith("s")


def test_failure_of_unknown_command_logs_message_text(caplog):
    ctx = _ctx(content="!mm scorium", command=None)
    with caplog.at_level(logging.INFO, logger="audit"):
        log_command_failure(ctx, commands.CommandNotFound("x"))
    assert caplog.records[0].getMessage() == "fulano '!mm scorium' failed (CommandNotFound) in DM"


def test_failure_of_real_command_includes_name_and_duration(caplog):
    ctx = _ctx(command="summary")
    with caplog.at_level(logging.INFO, logger="audit"):
        log_command_start(ctx)
        log_command_failure(ctx, commands.CheckFailure("nope"))
    message = caplog.records[-1].getMessage()
    assert message.startswith("fulano !summary failed in ")
    assert message.endswith("(CheckFailure) in DM")


def test_long_and_multiline_content_is_trimmed(caplog):
    ctx = _ctx(content="!m " + "x" * 500 + "\nsecond line")
    with caplog.at_level(logging.INFO, logger="audit"):
        log_command_start(ctx)
    message = caplog.records[0].getMessage()
    assert "\n" not in message
    assert len(message) < command_audit.MAX_CONTENT_LEN + 40
    assert message.endswith("…'")


# ── command_usage storage ───────────────────────────────────────────────────

def test_finished_command_is_stored_with_duration_and_fetches():
    ctx = _ctx(guild="Realms Rising", channel_name="eov")
    ctx.invoked_with = "m"
    log_command_start(ctx)

    async def command_task():
        # In the bot the before_invoke hook and the command run in one task.
        await command_audit.start_sheet_fetch_tracking(ctx)
        _note_sheet_fetch()                          # cache miss in the event loop
        await asyncio.to_thread(_note_sheet_fetch)   # cache miss in a worker thread (real path)

    asyncio.run(command_task())
    log_command_finish(ctx)

    (row,) = scheduler.list_command_usage()
    assert row["user_id"] == 42 and row["user_name"] == "fulano"
    assert row["command"] == "matches" and row["invoked_as"] == "m"
    assert row["location"] == "Realms Rising"
    assert row["success"] == 1 and row["error"] is None
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0
    assert row["sheet_fetches"] == 2


def test_unknown_command_is_stored_as_failure_with_typed_word():
    ctx = _ctx(content="!mm scorium", command=None)
    ctx.invoked_with = "mm"
    log_command_failure(ctx, commands.CommandNotFound("x"))
    (row,) = scheduler.list_command_usage()
    assert row["command"] == "?" and row["invoked_as"] == "mm"
    assert row["success"] == 0 and row["error"] == "CommandNotFound"
    assert row["location"] == "DM"
    assert row["sheet_fetches"] is None   # never reached the command


def test_fetch_counter_is_inert_outside_commands():
    token = sheet_fetch_counter.set(None)
    _note_sheet_fetch()                   # e.g. a scheduled task: must not raise
    sheet_fetch_counter.reset(token)


def test_storage_error_does_not_break_logging(monkeypatch, caplog):
    def boom(**_):
        raise RuntimeError("disk full")
    monkeypatch.setattr(scheduler, "record_command_usage", boom)
    with caplog.at_level(logging.INFO, logger="audit"):
        log_command_finish(_ctx())
    assert any("could not store command usage" in r.getMessage() for r in caplog.records)
