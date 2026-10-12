import logging
from types import SimpleNamespace

from discord.ext import commands

import command_audit
from command_audit import describe_location, log_command_failure, log_command_finish, log_command_start


def _ctx(content="!m scorium", guild=None, channel_name="general", command="matches"):
    return SimpleNamespace(
        author=_User(),
        guild=SimpleNamespace(name=guild) if guild else None,
        channel=SimpleNamespace(name=channel_name),
        message=SimpleNamespace(content=content),
        command=SimpleNamespace(qualified_name=command) if command else None,
    )


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
