"""
scheduler.py — Persistent task scheduler for the tournament bot.

Storage: SQLite (tasks.db).
The loop fires tasks when their schedule matches:
  - Weekly mode:   weekday + hour + minute in the task's timezone
  - Interval mode: every N minutes (interval_minutes column)

Auto-advancing week: if current_week is set (not NULL), it overrides
params["week"] and increments by 1 after each successful run.
current_week = -1 means the season is over and the task is skipped.
"""

import sqlite3
import json
import logging
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks

from tournament_actions import run_post_divisions, run_notify_all, run_post_standings, advance_auto_week

logger = logging.getLogger(__name__)

DB_PATH = "tasks.db"

REGISTERED_ACTIONS = {
    "post_divisions": {
        "description": "Post division matchups to threads",
        "params": ["week", "tournament"],
    },
    "notify_all": {
        "description": "Send DMs to all players with pending matches",
        "params": ["week"],
    },
    "standings": {
        "description": "Post current standings to each division thread",
        "params": ["tournament"],
    },
}

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                action           TEXT    NOT NULL,
                params           TEXT    NOT NULL DEFAULT '{}',
                guild_id         INTEGER NOT NULL,
                channel_id       INTEGER NOT NULL,
                thread_id        INTEGER,
                weekday          INTEGER,           -- 0=Mon … 6=Sun; NULL for interval tasks
                hour             INTEGER NOT NULL DEFAULT 0,
                minute           INTEGER NOT NULL DEFAULT 0,
                tz               TEXT    NOT NULL DEFAULT 'UTC',
                last_run         TEXT,              -- ISO timestamp of last execution
                created_by       INTEGER NOT NULL,
                current_week     INTEGER,           -- auto-incrementing week; NULL = fixed; -1 = exhausted
                end_week         INTEGER,           -- stop auto-advancing after this week (NULL = run until no matches)
                interval_minutes INTEGER            -- if set: ignore weekday/hour/minute
            )
        """)
        # Migrations for existing databases
        existing = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)")}
        for col, definition in [
            ("current_week",     "INTEGER"),
            ("end_week",         "INTEGER"),
            ("interval_minutes", "INTEGER"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {col} {definition}")
        conn.commit()
    logger.info("Scheduler DB initialised.")


def add_task(*, action: str, params: dict, guild_id: int, channel_id: int,
             thread_id: int | None, weekday: int | None, hour: int, minute: int,
             tz: str, created_by: int, current_week: int | None = None,
             end_week: int | None = None, interval_minutes: int | None = None) -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO scheduled_tasks
               (action, params, guild_id, channel_id, thread_id,
                weekday, hour, minute, tz, created_by, current_week, end_week, interval_minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action, json.dumps(params), guild_id, channel_id, thread_id,
             weekday, hour, minute, tz, created_by, current_week, end_week, interval_minutes)
        )
        conn.commit()
        return cur.lastrowid


def remove_task(task_id: int) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cur.rowcount > 0


def list_tasks(guild_id: int | None = None) -> list[sqlite3.Row]:
    with _get_conn() as conn:
        if guild_id is not None:
            return conn.execute(
                "SELECT * FROM scheduled_tasks WHERE guild_id = ? ORDER BY id", (guild_id,)
            ).fetchall()
        return conn.execute("SELECT * FROM scheduled_tasks ORDER BY guild_id, id").fetchall()


def get_task(task_id: int) -> sqlite3.Row | None:
    with _get_conn() as conn:
        return conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()


def update_last_run(task_id: int, ts: str):
    with _get_conn() as conn:
        conn.execute("UPDATE scheduled_tasks SET last_run = ? WHERE id = ?", (ts, task_id))
        conn.commit()


def update_current_week(task_id: int, week: int | None):
    with _get_conn() as conn:
        conn.execute("UPDATE scheduled_tasks SET current_week = ? WHERE id = ?", (week, task_id))
        conn.commit()


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _format_interval(minutes: int) -> str:
    if minutes < 60:
        return f"every {minutes}m"
    if minutes < 1440:
        h, m = divmod(minutes, 60)
        return f"every {h}h" + (f" {m}m" if m else "")
    d, rem = divmod(minutes, 1440)
    h, m = divmod(rem, 60)
    parts = [f"{d}d"] + ([f"{h}h"] if h else []) + ([f"{m}m"] if m else [])
    return "every " + " ".join(parts)


def format_task(row: sqlite3.Row) -> str:
    params = json.loads(row["params"])
    action_info = REGISTERED_ACTIONS.get(row["action"], {})
    params_str = ", ".join(f"{k}={v}" for k, v in params.items()) or "—"
    thread_str = f" / thread `{row['thread_id']}`" if row["thread_id"] else ""
    interval = row["interval_minutes"]
    if interval:
        when_str = _format_interval(interval)
    elif row["weekday"] is not None:
        when_str = f"{WEEKDAY_NAMES[row['weekday']]} at {row['hour']:02d}:{row['minute']:02d} ({row['tz']})"
    else:
        when_str = f"daily at {row['hour']:02d}:{row['minute']:02d} ({row['tz']})"
    return (
        f"**ID {row['id']}** — `{row['action']}` ({action_info.get('description', '')})\n"
        f"  📅 {when_str}\n"
        f"  📌 Guild `{row['guild_id']}` · Channel `{row['channel_id']}`{thread_str}\n"
        f"  ⚙️ Params: {params_str}\n"
        f"  🕐 Last run: {row['last_run'] or 'never'}"
    )


# ── Scheduler Cog ──────────────────────────────────────────────────────────────

class SchedulerCog(commands.Cog):
    """Background loop that fires scheduled tasks every minute."""

    def __init__(self, bot: commands.Bot, cog_ref):
        self.bot = bot
        self.cog_ref = cog_ref   # reference to TournamentCommands
        self._check_loop.start()

    def cog_unload(self):
        self._check_loop.cancel()

    @tasks.loop(minutes=1)
    async def _check_loop(self):
        now_utc = datetime.now(timezone.utc)
        for row in list_tasks():
            try:
                tz = ZoneInfo(row["tz"])
            except ZoneInfoNotFoundError:
                logger.warning(f"Task {row['id']}: unknown timezone '{row['tz']}', skipping.")
                continue

            now_local = now_utc.astimezone(tz)
            interval = row["interval_minutes"]

            if interval:
                if row["last_run"]:
                    elapsed = (now_utc - datetime.fromisoformat(row["last_run"])).total_seconds() / 60
                    if elapsed < interval:
                        continue
            else:
                if (now_local.weekday() != row["weekday"]
                        or now_local.hour   != row["hour"]
                        or now_local.minute != row["minute"]):
                    continue

            # Deduplicate within the same calendar minute
            run_key = now_local.strftime("%Y-%m-%dT%H:%M")
            if row["last_run"] and row["last_run"].startswith(run_key):
                continue

            logger.info(f"Firing task {row['id']} ({row['action']}) for guild {row['guild_id']}")
            update_last_run(row["id"], now_utc.isoformat())
            asyncio.create_task(self._fire(row))

    @_check_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()

    async def _fire(self, row: sqlite3.Row):
        guild = self.bot.get_guild(row["guild_id"])
        if not guild:
            logger.warning(f"Task {row['id']}: guild {row['guild_id']} not found.")
            return

        # Resolve destination: explicit thread > channel
        destination = (
            guild.get_thread(row["thread_id"]) if row["thread_id"] else None
        ) or guild.get_channel(row["channel_id"])
        if not destination:
            logger.warning(f"Task {row['id']}: channel/thread not found.")
            return

        params = json.loads(row["params"])
        task_id = row["id"]
        auto_week = row["current_week"]   # None → fixed, -1 → exhausted

        if auto_week == -1:
            logger.info(f"Task {task_id}: season finished, skipping.")
            return
        end_week = row["end_week"]
        if auto_week is not None:
            if end_week is not None and auto_week > end_week:
                logger.info(f"Task {task_id}: reached end_week {end_week}, stopping.")
                update_current_week(task_id, -1)
                return
            params = {**params, "week": str(auto_week)}

        try:
            await self._dispatch(row["action"], destination, params, task_id, auto_week, end_week=row["end_week"])
        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            try:
                await destination.send(f"❌ Scheduled task `{row['action']}` (ID {task_id}) failed: {e}")
            except Exception:
                pass

    async def _dispatch(self, action: str, destination, params: dict,
                        task_id: int, auto_week: int | None, end_week: int | None = None):
        """Route an action to the shared tournament_actions functions."""
        cog = self.cog_ref

        if action == "post_divisions":
            success, not_found, errors = await run_post_divisions(
                destination=destination,
                tournaments=cog.tournaments,
                default_week=cog.default_week,
                tournament_alias=params.get("tournament", "MA"),
                week_raw=params.get("week", "default"),
                force_refresh=True,
            )
            result = f"✅ Posted to {success} divisions."
            if not_found:
                result += f"\n⚠️ Threads not found: {', '.join(not_found)}"
            if errors:
                result += f"\n❌ Errors: {', '.join(errors)}"
            await destination.send(result)

            if auto_week is not None:
                from match_utils import get_tournament_sheets
                sheets_by_tourney = {}
                for t in cog.tournaments:
                    try:
                        sheets_by_tourney[t['name']] = get_tournament_sheets(t['url'], force_refresh=False)
                    except Exception:
                        pass
                await advance_auto_week(
                    task_id=task_id, current_week=auto_week,
                    sheets_by_tourney=sheets_by_tourney,
                    destination=destination, action_name=action,
                    end_week=end_week,
                )

        elif action == "notify_all":
            success, total = await run_notify_all(
                bot=self.bot,
                destination=destination,
                tournaments=cog.tournaments,
                mapping_url=cog.mapping_sheet_url,
                default_week=cog.default_week,
                week_raw=params.get("week", "default"),
                force_refresh=True,
            )
            await destination.send(f"✅ DMs sent to {success}/{total} players.")

            if auto_week is not None:
                from match_utils import get_tournament_sheets
                sheets_by_tourney = {}
                for t in cog.tournaments:
                    try:
                        sheets_by_tourney[t['name']] = get_tournament_sheets(t['url'], force_refresh=False)
                    except Exception:
                        pass
                await advance_auto_week(
                    task_id=task_id, current_week=auto_week,
                    sheets_by_tourney=sheets_by_tourney,
                    destination=destination, action_name=action,
                    end_week=end_week,
                )

        elif action == "standings":
            success, not_found, errors = await run_post_standings(
                destination=destination,
                tournaments=cog.tournaments,
                tournament_alias=params.get("tournament", "MA"),
                force_refresh=True,
            )
            result = f"✅ Standings posted to {success} divisions."
            if not_found:
                result += f"\n⚠️ Threads not found: {', '.join(not_found)}"
            if errors:
                result += f"\n❌ Errors: {', '.join(errors)}"
            await destination.send(result)

        else:
            logger.warning(f"Task {task_id}: unknown action '{action}'.")
