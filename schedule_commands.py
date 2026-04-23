"""
schedule_commands.py — Admin commands to manage scheduled tasks.

Commands:
  !schedule add <action> <weekday> <HH:MM> [tz=UTC] [week=default] [tournament=MA] [thread=<id>]
  !schedule list
  !schedule remove <id>
  !schedule info <id>
  !schedule actions        — list available actions and their params

Weekday names accepted: monday/mon, tuesday/tue, … sunday/sun  (or 0–6)

Examples:
  !schedule add post_divisions monday 09:00 tz=Europe/Madrid tournament=MA week=4
  !schedule add notify_all friday 18:30 tz=UTC week=default
  !schedule remove 3
  !schedule list
"""

import re
import discord
from discord.ext import commands
import logging

from commands import is_bot_admin
from scheduler import (
    add_task, remove_task, list_tasks, get_task,
    format_task, REGISTERED_ACTIONS, WEEKDAY_NAMES, init_db
)

logger = logging.getLogger(__name__)

WEEKDAY_MAP = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
    **{str(i): i for i in range(7)},
}


def _parse_interval(value: str) -> int | None:
    """Parse '30m', '2h', '1d', '1h30m' etc. into total minutes. Returns None on failure."""
    value = value.strip().lower()
    total = 0
    pattern = re.findall(r'(\d+)([dhm])', value)
    if not pattern:
        return None
    for amount, unit in pattern:
        n = int(amount)
        if unit == 'd':
            total += n * 1440
        elif unit == 'h':
            total += n * 60
        elif unit == 'm':
            total += n
    return total if total > 0 else None


def _parse_kwargs(tokens: list[str]) -> dict[str, str]:
    """Parse key=value tokens into a dict."""
    result = {}
    for token in tokens:
        if "=" in token:
            k, _, v = token.partition("=")
            result[k.strip().lower()] = v.strip()
    return result


def _when_display(row) -> str:
    """Format the schedule timing for display."""
    if row["interval_minutes"]:
        from scheduler import _format_interval
        return _format_interval(row["interval_minutes"])
    if row["weekday"] is not None:
        return f"{WEEKDAY_NAMES[row['weekday']]} {row['hour']:02d}:{row['minute']:02d} ({row['tz']})"
    return f"daily at {row['hour']:02d}:{row['minute']:02d} ({row['tz']})"


class ScheduleCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()

    @is_bot_admin()
    @commands.group(name='schedule', invoke_without_command=True)
    async def schedule_group(self, ctx):
        """Base command — shows usage if no subcommand given."""
        embed = discord.Embed(
            title="🗓️ Schedule Commands",
            description="Manage recurring bot tasks.",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="Add a task",
            value=(
                "`!schedule add <action> <weekday> <HH:MM> [options]`\n"
                "Options: `tz=UTC` `week=default` `tournament=MA` `channel=<id>` `thread=<id>`\n"
                "Example: `!schedule add post_divisions monday 09:00 tz=Europe/Madrid tournament=MA`"
            ),
            inline=False
        )
        embed.add_field(
            name="List tasks",
            value="`!schedule list`",
            inline=False
        )
        embed.add_field(
            name="Remove a task",
            value="`!schedule remove <id>`",
            inline=False
        )
        embed.add_field(
            name="Task details",
            value="`!schedule info <id>`",
            inline=False
        )
        embed.add_field(
            name="Available actions",
            value="`!schedule actions`",
            inline=False
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # !schedule add
    # ------------------------------------------------------------------
    @is_bot_admin()
    @schedule_group.command(name='add')
    async def schedule_add(self, ctx, action: str, when_str: str, *args):
        """
        Two modes:
          !schedule add <action> every=<interval> [options]   — interval (e.g. every=30m, every=2h, every=1d)
          !schedule add <action> <weekday> <HH:MM> [options]  — weekly at a fixed day/time
        Options: tz=UTC  week=default  tournament=MA  channel=<id>  thread=<id>
        """
        # Validate action
        if action not in REGISTERED_ACTIONS:
            known = ", ".join(f"`{k}`" for k in REGISTERED_ACTIONS)
            await ctx.send(f"❌ Unknown action `{action}`. Available: {known}")
            return

        # Detect mode: interval vs weekly
        # when_str is either "every=2h" / "30m" (interval) or a weekday name
        interval_minutes = None
        weekday = None
        h, m = 0, 0
        remaining_args = list(args)

        # Check if it's an interval: when_str starts with "every=" or looks like "30m"/"2h"
        interval_raw = None
        if when_str.lower().startswith("every="):
            interval_raw = when_str[6:]
        elif re.match(r'^\d+[dhm]', when_str.lower()):
            interval_raw = when_str

        if interval_raw:
            interval_minutes = _parse_interval(interval_raw)
            if not interval_minutes:
                await ctx.send(
                    f"❌ Invalid interval `{interval_raw}`. "
                    f"Use formats like `30m`, `2h`, `1d`, `1h30m`."
                )
                return
        else:
            # Weekly mode: when_str = weekday, first remaining arg = HH:MM
            weekday = WEEKDAY_MAP.get(when_str.lower())
            if weekday is None:
                await ctx.send(
                    f"❌ Invalid weekday or interval `{when_str}`. "
                    f"Use a weekday (e.g. `monday`) or an interval (e.g. `every=2h`, `30m`)."
                )
                return
            if not remaining_args:
                await ctx.send("❌ Weekly mode requires a time: `!schedule add <action> <weekday> <HH:MM>`")
                return
            time_str = remaining_args.pop(0)
            try:
                h, m = map(int, time_str.split(":"))
                assert 0 <= h <= 23 and 0 <= m <= 59
            except (ValueError, AssertionError):
                await ctx.send("❌ Invalid time format. Use `HH:MM` (e.g. `09:30`).")
                return

        # Parse optional key=value args
        opts = _parse_kwargs(remaining_args)

        tz = opts.get("tz", "UTC")
        # Quick timezone validation
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            await ctx.send(
                f"❌ Unknown timezone `{tz}`. "
                f"Use a valid IANA timezone, e.g. `Europe/Madrid`, `UTC`, `America/New_York`."
            )
            return

        # Channel: defaults to current channel
        channel_id_raw = opts.get("channel")
        if channel_id_raw:
            try:
                channel_id = int(channel_id_raw)
            except ValueError:
                await ctx.send("❌ `channel` must be a numeric Discord channel ID.")
                return
        else:
            channel_id = ctx.channel.id

        # Thread (optional)
        thread_id = None
        thread_id_raw = opts.get("thread")
        if thread_id_raw:
            try:
                thread_id = int(thread_id_raw)
            except ValueError:
                await ctx.send("❌ `thread` must be a numeric Discord thread ID.")
                return

        # Build action params (everything except scheduler-level opts)
        reserved = {"tz", "channel", "thread"}
        action_params = {k: v for k, v in opts.items() if k not in reserved}

        # Defaults per action
        if action == "post_divisions":
            action_params.setdefault("week", "default")
            action_params.setdefault("tournament", "MA")
        elif action == "notify_all":
            action_params.setdefault("week", "default")
        elif action == "standings":
            action_params.setdefault("tournament", "MA")

        # ── Auto-advancing week ──────────────────────────────────────────
        # If week param is a plain integer (not "default"), store it as
        # current_week so the scheduler can increment it after each run.
        # week="default" keeps current_week=None (fixed, no auto-advance).
        current_week = None
        week_val = action_params.get("week", "default")
        if week_val not in ("default", None):
            try:
                current_week = int(week_val)
            except ValueError:
                pass

        # end_week: optional upper bound for auto-advancing
        end_week = None
        end_week_raw = opts.get("end_week")
        if end_week_raw:
            try:
                end_week = int(end_week_raw)
            except ValueError:
                await ctx.send(f"❌ `end_week` must be a number (e.g. `end_week=8`).")
                return
            if current_week is not None and end_week < current_week:
                await ctx.send(f"❌ `end_week` ({end_week}) must be ≥ `week` ({current_week}).")
                return

        task_id = add_task(
            action=action,
            params=action_params,
            guild_id=ctx.guild.id,
            channel_id=channel_id,
            thread_id=thread_id,
            weekday=weekday,
            hour=h,
            minute=m,
            tz=tz,
            created_by=ctx.author.id,
            current_week=current_week,
            end_week=end_week,
            interval_minutes=interval_minutes,
        )

        thread_note = f" in thread `{thread_id}`" if thread_id else ""
        if current_week is None:
            auto_note = "Fixed week (no auto-advance)."
        elif end_week is not None:
            auto_note = f"Starting at **week {current_week}**, advancing each run, stopping after **week {end_week}**."
        else:
            auto_note = f"Starting at **week {current_week}**, advancing automatically each run until season ends."
        embed = discord.Embed(
            title="✅ Scheduled task created",
            color=discord.Color.green()
        )
        embed.add_field(name="ID", value=str(task_id), inline=True)
        embed.add_field(name="Action", value=f"`{action}`", inline=True)
        if interval_minutes:
            from scheduler import _format_interval
            when_display = f"Every {_format_interval(interval_minutes).replace('every ', '')}"
        else:
            when_display = f"{WEEKDAY_NAMES[weekday]} at {h:02d}:{m:02d} ({tz})"
        embed.add_field(
            name="When",
            value=when_display,
            inline=False
        )
        embed.add_field(
            name="Destination",
            value=f"Channel `{channel_id}`{thread_note}",
            inline=False
        )
        embed.add_field(
            name="Params",
            value=", ".join(f"`{k}={v}`" for k, v in action_params.items()) or "—",
            inline=False
        )
        embed.add_field(name="Week mode", value=auto_note, inline=False)
        embed.set_footer(text=f"Use !schedule remove {task_id} to cancel it.")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # !schedule list
    # ------------------------------------------------------------------
    @is_bot_admin()
    @schedule_group.command(name='list')
    async def schedule_list(self, ctx):
        rows = list_tasks(guild_id=ctx.guild.id)
        if not rows:
            await ctx.send("📭 No scheduled tasks for this server.")
            return

        embed = discord.Embed(
            title=f"🗓️ Scheduled Tasks — {ctx.guild.name}",
            color=discord.Color.blurple()
        )
        for row in rows:
            params_str = ", ".join(
                f"{k}={v}" for k, v in __import__('json').loads(row["params"]).items()
            ) or "—"
            thread_str = f" · thread `{row['thread_id']}`" if row["thread_id"] else ""
            cw = row["current_week"]
            ew = row["end_week"] if "end_week" in row.keys() else None
            if cw is None:
                week_mode_str = "fixed"
            elif cw == -1:
                week_mode_str = "🏁 season complete"
            else:
                limit = f" → max wk {ew}" if ew else ""
                week_mode_str = f"auto (next: wk {cw}{limit})"
            value = (
                f"**Action:** `{row['action']}`\n"
                f"**When:** {_when_display(row)}\n"
                f"**Channel:** `{row['channel_id']}`{thread_str}\n"
                f"**Params:** {params_str}\n"
                f"**Week mode:** {week_mode_str}\n"
                f"**Last run:** {row['last_run'] or 'never'}"
            )
            embed.add_field(name=f"ID {row['id']}", value=value, inline=False)

        embed.set_footer(text="Use !schedule remove <id> to delete a task.")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # !schedule remove
    # ------------------------------------------------------------------
    @is_bot_admin()
    @schedule_group.command(name='remove')
    async def schedule_remove(self, ctx, task_id: int):
        row = get_task(task_id)
        if not row:
            await ctx.send(f"❌ No task found with ID `{task_id}`.")
            return
        if row["guild_id"] != ctx.guild.id:
            await ctx.send("❌ That task doesn't belong to this server.")
            return
        remove_task(task_id)
        await ctx.send(f"🗑️ Task `{task_id}` (`{row['action']}`) removed successfully.")

    # ------------------------------------------------------------------
    # !schedule info
    # ------------------------------------------------------------------
    @is_bot_admin()
    @schedule_group.command(name='info')
    async def schedule_info(self, ctx, task_id: int):
        row = get_task(task_id)
        if not row:
            await ctx.send(f"❌ No task found with ID `{task_id}`.")
            return
        if row["guild_id"] != ctx.guild.id:
            await ctx.send("❌ That task doesn't belong to this server.")
            return
        embed = discord.Embed(
            title=f"🗓️ Task {task_id} — `{row['action']}`",
            description=REGISTERED_ACTIONS.get(row["action"], {}).get("description", ""),
            color=discord.Color.blurple()
        )
        if row["interval_minutes"]:
            from scheduler import _format_interval
            embed.add_field(name="Schedule", value=_format_interval(row["interval_minutes"]), inline=True)
        else:
            embed.add_field(name="Weekday", value=WEEKDAY_NAMES[row["weekday"]] if row["weekday"] is not None else "—", inline=True)
            embed.add_field(name="Time", value=f"{row['hour']:02d}:{row['minute']:02d}", inline=True)
            embed.add_field(name="Timezone", value=row["tz"], inline=True)
        embed.add_field(name="Guild ID", value=str(row["guild_id"]), inline=True)
        embed.add_field(name="Channel ID", value=str(row["channel_id"]), inline=True)
        embed.add_field(name="Thread ID", value=str(row["thread_id"]) if row["thread_id"] else "—", inline=True)
        import json
        params = json.loads(row["params"])
        embed.add_field(
            name="Params",
            value="\n".join(f"`{k}` = `{v}`" for k, v in params.items()) or "—",
            inline=False
        )
        cw = row["current_week"]
        ew = row["end_week"] if "end_week" in row.keys() else None
        if cw is None:
            cw_display = "Fixed week (no auto-advance)"
        elif cw == -1:
            cw_display = "🏁 Season complete — task will no longer run"
        else:
            limit = f", stops after **week {ew}**" if ew else " until season ends"
            cw_display = f"Auto-advancing — next: **week {cw}**{limit}"
        embed.add_field(name="Week mode", value=cw_display, inline=False)
        embed.add_field(name="Created by", value=f"<@{row['created_by']}>", inline=True)
        embed.add_field(name="Last run", value=row["last_run"] or "never", inline=True)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # !schedule actions
    # ------------------------------------------------------------------
    @is_bot_admin()
    @schedule_group.command(name='actions')
    async def schedule_actions(self, ctx):
        embed = discord.Embed(
            title="⚙️ Available Scheduled Actions",
            color=discord.Color.gold()
        )
        for key, info in REGISTERED_ACTIONS.items():
            params_doc = ", ".join(f"`{p}`" for p in info.get("params", [])) or "—"
            embed.add_field(
                name=f"`{key}`",
                value=f"{info['description']}\nParams: {params_doc}",
                inline=False
            )
        embed.set_footer(text="Use !schedule add <action> ... to create a task.")
        await ctx.send(embed=embed)


async def setup_schedule(bot: commands.Bot):
    await bot.add_cog(ScheduleCommands(bot))
