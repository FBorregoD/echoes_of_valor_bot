"""One log line per command a user runs, and one when it finishes.

Replaces the old print() of every message the bot could read: only
messages that actually invoke a command are logged (other chatter in the
channels isn't stored), and it goes through `logging`, so it shows up in
`docker logs` immediately. Filter with:  docker logs echoes-of-valor | grep audit

Every finished or failed command is also stored in the command_usage table
(tasks.db) — who, which command, where, how long it took and how many
Google Sheets downloads it needed — for the admin-only !usage_report.
"""
import logging
import time
from datetime import datetime, timezone

from match_utils import sheet_fetch_counter

audit_logger = logging.getLogger("audit")

MAX_CONTENT_LEN = 200


def describe_location(ctx) -> str:
    """'DM' for direct messages, otherwise 'Guild/#channel' (threads included)."""
    if ctx.guild is None:
        return "DM"
    return f"{ctx.guild.name}/#{getattr(ctx.channel, 'name', ctx.channel)}"


def _content(ctx) -> str:
    text = ctx.message.content.replace("\n", " ")
    if len(text) > MAX_CONTENT_LEN:
        text = text[:MAX_CONTENT_LEN] + "…"
    return text


def log_command_start(ctx) -> None:
    ctx.audit_started = time.monotonic()
    ctx.audit_started_at = datetime.now(timezone.utc)
    audit_logger.info(f"{ctx.author} ({ctx.author.id}) in {describe_location(ctx)}: {_content(ctx)!r}")


async def start_sheet_fetch_tracking(ctx) -> None:
    """bot.before_invoke hook: count this command's Google Sheets downloads.

    Runs in the same task as the command itself (unlike the on_command
    event, which discord.py dispatches as a separate task), so the context
    variable set here is the one the command's fetches see.
    """
    ctx.sheet_fetches = [0]
    sheet_fetch_counter.set(ctx.sheet_fetches)


def _elapsed(ctx) -> str:
    started = getattr(ctx, "audit_started", None)
    return f" in {time.monotonic() - started:.1f}s" if started is not None else ""


def _record(ctx, success: bool, error: BaseException | None = None) -> None:
    """Store the command in command_usage; never lets a DB problem break the bot."""
    from scheduler import record_command_usage
    started = getattr(ctx, "audit_started", None)
    fetches = getattr(ctx, "sheet_fetches", None)
    try:
        record_command_usage(
            ts=getattr(ctx, "audit_started_at", None) or datetime.now(timezone.utc),
            user_id=ctx.author.id,
            user_name=str(ctx.author),
            command=ctx.command.qualified_name if ctx.command else "?",
            invoked_as=getattr(ctx, "invoked_with", None),
            location="DM" if ctx.guild is None else ctx.guild.name,
            success=success,
            error=type(error).__name__ if error else None,
            duration_ms=round((time.monotonic() - started) * 1000) if started is not None else None,
            sheet_fetches=fetches[0] if fetches is not None else None,
        )
    except Exception as e:
        audit_logger.warning(f"could not store command usage: {e}")


def log_command_finish(ctx) -> None:
    audit_logger.info(f"{ctx.author} !{ctx.command.qualified_name} done{_elapsed(ctx)}")
    _record(ctx, success=True)


def log_command_failure(ctx, error) -> None:
    """Log a command that errored, or a prefixed message that isn't a command."""
    name = f"!{ctx.command.qualified_name}" if ctx.command else repr(_content(ctx))
    audit_logger.info(
        f"{ctx.author} {name} failed{_elapsed(ctx)} "
        f"({type(error).__name__}) in {describe_location(ctx)}"
    )
    _record(ctx, success=False, error=error)
