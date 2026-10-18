"""Admin-only monitoring commands: !status and !usage_report.

Both only work in DMs and only for bot admins. Usage data comes from the
command_usage table (tasks.db), filled by command_audit for every command a
user runs; scheduled tasks don't count as usage.
"""
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from discord.ext import commands

from commands import is_bot_admin, require_dm
from match_utils import split_message
from remote_config import CONFIG_SOURCE_REMOTE, CONFIG_SOURCE_FALLBACK
from scheduler import list_command_usage, list_tasks, list_subscriptions, describe_schedule_gmt

logger = logging.getLogger(__name__)

DEFAULT_REPORT_DAYS = 7
TOP_USERS = 10
TOP_UNKNOWN = 5


# ── Formatting helpers ─────────────────────────────────────────────────────────

def parse_days(arg: str | None) -> int | None:
    """'7' -> 7, None -> DEFAULT_REPORT_DAYS, 'all' -> None (whole history).
    Raises ValueError for anything else."""
    if arg is None:
        return DEFAULT_REPORT_DAYS
    if arg.lower() == "all":
        return None
    days = int(arg)
    if days <= 0:
        raise ValueError("days must be positive")
    return days


def format_span(seconds: float) -> str:
    """Compact duration: '2d 4h', '3h 12m', '5m', '40s'."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _avg_seconds(durations: list[int]) -> str:
    return f"{sum(durations) / len(durations) / 1000:.1f}s" if durations else "—"


def _root(command: str) -> str:
    """'schedule add' -> 'schedule', so subcommands count towards their group."""
    return command.split(" ")[0]


# ── Report builders (pure, testable) ───────────────────────────────────────────

def build_usage_report(rows, period_label: str, command_names: list[str]) -> str:
    """Text report of command usage. `rows` are command_usage rows (dict-like),
    `command_names` every top-level command the bot has, to list the unused ones."""
    header = f"📊 **Usage report — {period_label}**"
    if not rows:
        return f"{header}\nNo commands used in this period."

    users = {}
    user_counts = Counter()
    for r in rows:
        users[r["user_id"]] = r["user_name"]
        user_counts[r["user_id"]] += 1

    lines = [f"{header}\n**{len(rows)}** commands by **{len(users)}** users"]

    known = [r for r in rows if r["command"] != "?"]
    per_command = defaultdict(list)
    for r in known:
        per_command[_root(r["command"])].append(r)

    lines.append("\n**Commands** (uses · avg time · % that had to download the sheet)")
    for name, cmd_rows in sorted(per_command.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        durations = [r["duration_ms"] for r in cmd_rows if r["duration_ms"] is not None]
        tracked = [r for r in cmd_rows if r["sheet_fetches"] is not None]
        fetched = sum(1 for r in tracked if r["sheet_fetches"] > 0)
        fetch_pct = f"{round(fetched / len(tracked) * 100)}%" if tracked else "—"
        lines.append(f"`!{name}` {len(cmd_rows)} · {_avg_seconds(durations)} · {fetch_pct}")

    unused = sorted(set(command_names) - set(per_command))
    if unused:
        lines.append("\n**Never used:** " + ", ".join(f"`!{n}`" for n in unused))

    lines.append("\n**Top users**")
    lines.append(" · ".join(f"{users[uid]} {n}" for uid, n in user_counts.most_common(TOP_USERS)))

    dm = sum(1 for r in rows if r["location"] == "DM")
    servers = Counter(r["location"] for r in rows if r["location"] != "DM")
    where = [f"DM {dm}"] + [f"{name} {n}" for name, n in servers.most_common()]
    lines.append("\n**Where:** " + " · ".join(where))

    cached = [r["duration_ms"] for r in known
              if r["sheet_fetches"] == 0 and r["duration_ms"] is not None]
    fetched = [r["duration_ms"] for r in known
               if (r["sheet_fetches"] or 0) > 0 and r["duration_ms"] is not None]
    if cached or fetched:
        lines.append(f"**Speed:** from cache {_avg_seconds(cached)} ({len(cached)}) · "
                     f"downloading the sheet {_avg_seconds(fetched)} ({len(fetched)})")

    failures = [r for r in rows if not r["success"]]
    if failures:
        by_error = Counter(r["error"] or "?" for r in failures)
        lines.append(f"\n**Failures:** {len(failures)} — "
                     + ", ".join(f"{err} {n}" for err, n in by_error.most_common()))
        unknown = Counter(r["invoked_as"] for r in rows if r["command"] == "?" and r["invoked_as"])
        if unknown:
            lines.append("Unknown commands typed: "
                         + ", ".join(f"`{w}` {n}" for w, n in unknown.most_common(TOP_UNKNOWN)))

    return "\n".join(lines)


def build_status(bot, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    started = getattr(bot, "started_at", None)
    lines = []

    if started:
        lines.append(f"🟢 **{bot.user}** — online {format_span((now - started).total_seconds())} "
                     f"(since {started:%b %d %H:%M} UTC)")
    else:
        lines.append(f"🟢 **{bot.user}**")

    latency = getattr(bot, "latency", None)
    disconnect = getattr(bot, "last_outage", None)
    conn = f"Latency: {round(latency * 1000)} ms" if latency is not None else "Latency: —"
    if disconnect:
        down, up = disconnect
        conn += (f" · last disconnect: {down:%b %d %H:%M} → {up:%H:%M} UTC "
                 f"({format_span((up - down).total_seconds())})")
    else:
        conn += " · no disconnects since start"
    lines.append(conn)

    source = getattr(bot, "config_source", None)
    source_label = {CONFIG_SOURCE_REMOTE: "Google Drive ✅",
                    CONFIG_SOURCE_FALLBACK: "⚠️ local file (Drive download failed)"}.get(source, "local file")
    config = getattr(bot, "config", {}) or {}
    tournaments = ", ".join(f"{t['alias']} S{t.get('season', '?')}" for t in config.get("tournaments", []))
    lines.append(f"Config: {source_label} · tournaments: {tournaments or '—'} · "
                 f"{len(config.get('guild_channels', []))} servers configured")

    tasks = list_tasks()
    lines.append(f"\n**Scheduled tasks:** {len(tasks)}")
    for row in tasks:
        cw = row["current_week"]
        week = "" if cw is None else (" · season over" if cw == -1 else f" · next week {cw}")
        tournament = json.loads(row["params"] or "{}").get("tournament")
        action = f"{row['action']} **{tournament}**" if tournament else row["action"]
        lines.append(f"`{row['id']}` {action} — {describe_schedule_gmt(row)}{week}")

    lines.append(f"\n**Subscriptions:** {len(list_subscriptions())}")

    if started:
        used = list_command_usage(since=started)
        failed = sum(1 for r in used if not r["success"])
        lines.append(f"**Commands since start:** {len(used)} ({failed} failed)")

    return "\n".join(lines)


async def send_usage_report(bot, days: int | None = DEFAULT_REPORT_DAYS,
                            exclude_recipient: bool = False) -> tuple[int, int]:
    """DM the usage report to the alert list (config alert_user_ids, falling
    back to admin_user_ids). Used by the usage_report scheduled action. With
    exclude_recipient, each admin gets a report without their own commands
    (like `!us others`). Returns (sent, failed)."""
    config = getattr(bot, "config", {}) or {}
    recipients = config.get("alert_user_ids") or config.get("admin_user_ids") or []
    sent = failed = 0
    for user_id in recipients:
        text = usage_report_text(bot, days, exclude_user_id=user_id if exclude_recipient else None)
        try:
            user = await bot.fetch_user(user_id)
            for chunk in split_message(text):
                await user.send(chunk)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"usage_report: could not DM {user_id}: {e}")
    return sent, failed


def usage_report_text(bot, days: int | None, exclude_user_id: int | None = None) -> str:
    """Usage report for the last `days` days (None = all time); with
    `exclude_user_id`, that user's own commands are left out (e.g. an admin's
    own testing, so the report only shows what everyone else does)."""
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    period = f"last {days} days" if days else "all time"
    rows = list_command_usage(since=since)
    if exclude_user_id is not None:
        rows = [r for r in rows if r["user_id"] != exclude_user_id]
        period += " · excluding your own commands"
    names = sorted(c.name for c in bot.commands)
    return build_usage_report(rows, period, names)


def parse_report_args(args: tuple[str, ...]) -> tuple[int | None, bool]:
    """'!us [days|all] [others]' in any order -> (days, exclude_self).
    Raises ValueError on anything unrecognised or repeated."""
    days_arg, others = None, False
    for arg in args:
        if arg.lower() == "others" and not others:
            others = True
        elif days_arg is None and (arg.isdigit() or arg.lower() == "all"):
            days_arg = arg
        else:
            raise ValueError(arg)
    return parse_days(days_arg), others


# ── Cog ────────────────────────────────────────────────────────────────────────

class MonitoringCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @is_bot_admin()
    @require_dm()
    @commands.command(name="status")
    async def status_command(self, ctx):
        """Admin-only, DM-only. Bot health: uptime, config source, tasks, usage since start."""
        for chunk in split_message(build_status(self.bot)):
            await ctx.send(chunk)

    @is_bot_admin()
    @require_dm()
    @commands.command(name="usage_report", aliases=["us"])
    async def usage_report_command(self, ctx, *args):
        """Admin-only, DM-only. Which commands are used, by whom, how fast.
        `others` leaves out the caller's own commands."""
        try:
            days, others = parse_report_args(args)
        except ValueError:
            await ctx.send("❌ Usage: `!us [days|all] [others]` — e.g. `!us 30`, `!us all others`.")
            return
        exclude = ctx.author.id if others else None
        for chunk in split_message(usage_report_text(self.bot, days, exclude_user_id=exclude)):
            await ctx.send(chunk)
