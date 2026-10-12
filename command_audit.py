"""One log line per command a user runs, and one when it finishes.

Replaces the old print() of every message the bot could read: only
messages that actually invoke a command are logged (other chatter in the
channels isn't stored), and it goes through `logging`, so it shows up in
`docker logs` immediately. Filter with:  docker logs echoes-of-valor | grep audit
"""
import logging
import time

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
    audit_logger.info(f"{ctx.author} ({ctx.author.id}) in {describe_location(ctx)}: {_content(ctx)!r}")


def _elapsed(ctx) -> str:
    started = getattr(ctx, "audit_started", None)
    return f" in {time.monotonic() - started:.1f}s" if started is not None else ""


def log_command_finish(ctx) -> None:
    audit_logger.info(f"{ctx.author} !{ctx.command.qualified_name} done{_elapsed(ctx)}")


def log_command_failure(ctx, error) -> None:
    """Log a command that errored, or a prefixed message that isn't a command."""
    name = f"!{ctx.command.qualified_name}" if ctx.command else repr(_content(ctx))
    audit_logger.info(
        f"{ctx.author} {name} failed{_elapsed(ctx)} "
        f"({type(error).__name__}) in {describe_location(ctx)}"
    )
