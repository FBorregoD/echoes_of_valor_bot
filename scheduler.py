"""
scheduler.py — Persistent task scheduler for the tournament bot.

Storage: SQLite (tasks.db)
Each task stores: id, action, parameters (JSON), guild_id, channel_id,
thread_id (optional), weekday (0=Mon … 6=Sun), hour, minute, timezone,
last_run (ISO timestamp), created_by.

The scheduler loop runs every minute and fires tasks whose
(weekday, hour, minute) matches the current local time and that have
not already run in the current calendar minute.
"""

import sqlite3
import json
import logging
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

DB_PATH = "tasks.db"

# ------------------------------------------------------------------
# Registry of available scheduled actions.
# Each entry: action_key -> {"description": str, "params": [str, ...]}
# Add new actions here without touching the scheduler logic.
# ------------------------------------------------------------------
REGISTERED_ACTIONS = {
    "post_divisions": {
        "description": "Post division matchups to threads",
        "params": ["week", "tournament"],   # week: int or 'default', tournament: alias
    },
    "notify_all": {
        "description": "Send DMs to all players with pending matches",
        "params": ["week"],                 # week: int or 'default'
    },
}


# ------------------------------------------------------------------
# DB helpers
# ------------------------------------------------------------------
def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                action      TEXT NOT NULL,
                params      TEXT NOT NULL DEFAULT '{}',
                guild_id    INTEGER NOT NULL,
                channel_id  INTEGER NOT NULL,
                thread_id   INTEGER,
                weekday     INTEGER NOT NULL,  -- 0=Mon, 6=Sun
                hour        INTEGER NOT NULL,
                minute      INTEGER NOT NULL,
                tz          TEXT NOT NULL DEFAULT 'UTC',
                last_run    TEXT,              -- ISO timestamp of last execution
                created_by  INTEGER NOT NULL
            )
        """)
        conn.commit()
    logger.info("Scheduler DB initialised.")


def add_task(action: str, params: dict, guild_id: int, channel_id: int,
             thread_id: int | None, weekday: int, hour: int, minute: int,
             tz: str, created_by: int) -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO scheduled_tasks
               (action, params, guild_id, channel_id, thread_id,
                weekday, hour, minute, tz, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action, json.dumps(params), guild_id, channel_id, thread_id,
             weekday, hour, minute, tz, created_by)
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
                "SELECT * FROM scheduled_tasks WHERE guild_id = ? ORDER BY id",
                (guild_id,)
            ).fetchall()
        return conn.execute(
            "SELECT * FROM scheduled_tasks ORDER BY guild_id, id"
        ).fetchall()


def get_task(task_id: int) -> sqlite3.Row | None:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()


def update_last_run(task_id: int, ts: str):
    with _get_conn() as conn:
        conn.execute(
            "UPDATE scheduled_tasks SET last_run = ? WHERE id = ?", (ts, task_id)
        )
        conn.commit()


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def format_task(row: sqlite3.Row) -> str:
    params = json.loads(row["params"])
    action_info = REGISTERED_ACTIONS.get(row["action"], {})
    params_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "—"
    thread_str = f" / thread `{row['thread_id']}`" if row["thread_id"] else ""
    last_run = row["last_run"] or "never"
    return (
        f"**ID {row['id']}** — `{row['action']}` ({action_info.get('description', '')})\n"
        f"  📅 {WEEKDAY_NAMES[row['weekday']]} at {row['hour']:02d}:{row['minute']:02d} ({row['tz']})\n"
        f"  📌 Guild `{row['guild_id']}` · Channel `{row['channel_id']}`{thread_str}\n"
        f"  ⚙️ Params: {params_str}\n"
        f"  🕐 Last run: {last_run}"
    )


# ------------------------------------------------------------------
# Scheduler Cog
# ------------------------------------------------------------------
class SchedulerCog(commands.Cog):
    """Runs a background loop that fires scheduled tasks."""

    def __init__(self, bot: commands.Bot, cog_ref):
        """
        bot       — the Discord bot instance
        cog_ref   — reference to TournamentCommands cog (to call its methods)
        """
        self.bot = bot
        self.cog_ref = cog_ref
        self._check_loop.start()

    def cog_unload(self):
        self._check_loop.cancel()

    @tasks.loop(minutes=1)
    async def _check_loop(self):
        now_utc = datetime.now(timezone.utc)
        rows = list_tasks()
        for row in rows:
            try:
                tz = ZoneInfo(row["tz"])
            except ZoneInfoNotFoundError:
                logger.warning(f"Task {row['id']}: unknown timezone '{row['tz']}', skipping.")
                continue

            now_local = now_utc.astimezone(tz)
            if (now_local.weekday() != row["weekday"]
                    or now_local.hour != row["hour"]
                    or now_local.minute != row["minute"]):
                continue

            # Avoid double-firing within the same minute
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
        params = json.loads(row["params"])
        guild = self.bot.get_guild(row["guild_id"])
        if not guild:
            logger.warning(f"Task {row['id']}: guild {row['guild_id']} not found.")
            return

        # Resolve destination: thread > channel
        destination = None
        if row["thread_id"]:
            destination = guild.get_thread(row["thread_id"])
        if destination is None:
            destination = guild.get_channel(row["channel_id"])
        if destination is None:
            logger.warning(f"Task {row['id']}: channel/thread not found.")
            return

        try:
            action = row["action"]
            if action == "post_divisions":
                await self._run_post_divisions(destination, params)
            elif action == "notify_all":
                await self._run_notify_all(destination, params)
            else:
                logger.warning(f"Task {row['id']}: unknown action '{action}'.")
        except Exception as e:
            logger.error(f"Task {row['id']} failed: {e}", exc_info=True)
            try:
                await destination.send(f"❌ Scheduled task `{row['action']}` (ID {row['id']}) failed: {e}")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Action implementations
    # ------------------------------------------------------------------
    async def _run_post_divisions(self, destination, params: dict):
        """Mirrors TournamentCommands.post_divisions but posts to a specific destination."""
        from match_utils import (
            get_tournament_sheets, get_division_matches,
            load_hero_builds_from_sheets, format_table, split_message, normalize_name
        )
        tournaments = self.cog_ref.tournaments
        default_week = self.cog_ref.default_week

        week_raw = params.get("week", "default")
        week = default_week if week_raw == "default" else int(week_raw)
        tournament_alias = params.get("tournament", "MA")

        tourney = self.cog_ref.find_tournament(tournament_alias)
        if not tourney:
            await destination.send(f"❌ Scheduled task: tournament '{tournament_alias}' not found.")
            return

        await destination.send(f"🤖 Scheduled: posting **{tourney['name']}** week {week} matchups...")

        sheets = get_tournament_sheets(tourney['url'], force_refresh=True)
        builds = load_hero_builds_from_sheets(
            sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
        )

        excluded_keywords = ['formulierreacties', 'hero builds', 'leagues overview',
                             'format', 'scoresheet', 'arma heroum']
        division_sheets = [
            name for name in sheets.keys()
            if not any(kw in name.lower() for kw in excluded_keywords)
        ]

        guild = destination.guild
        thread_dict = {}
        for channel in guild.text_channels:
            for thread in channel.threads:
                thread_dict[thread.name.lower()] = thread

        success_count, not_found, error_list = 0, [], []
        for div_name in division_sheets:
            thread = thread_dict.get(div_name.strip().lower())
            if not thread:
                not_found.append(div_name)
                continue
            try:
                current, pending = get_division_matches(sheets, div_name, week)
                if not current and not pending:
                    continue
                msg = f"**🏆 {tourney['name']} - Division {div_name}**\n📅 **Pairings for week {week}**\n\n"
                if current:
                    rows = []
                    for m in current:
                        p1, p2 = m['player1'], m['player2']
                        p1_disp = f"{p1} ({builds.get(normalize_name(p1), '?')})"
                        p2_disp = f"{p2} ({builds.get(normalize_name(p2), '?')})"
                        rows.append([p1_disp, p2_disp])
                    msg += f"```\n{format_table(rows, ['Player 1', 'Player 2'], f'Week {week}')}\n```"
                else:
                    msg += "📅 No matches for this week.\n"
                if pending:
                    rows = []
                    for m in pending:
                        p1, p2 = m['player1'], m['player2']
                        p1_disp = f"{p1} ({builds.get(normalize_name(p1), '?')})"
                        p2_disp = f"{p2} ({builds.get(normalize_name(p2), '?')})"
                        rows.append([m['week'], p1_disp, p2_disp])
                    msg += f"\n**⏳ Pending:**\n```\n{format_table(rows, ['Week', 'Player 1', 'Player 2'], 'Pending')}\n```"
                for chunk in split_message(msg):
                    await thread.send(chunk)
                success_count += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                error_list.append(f"{div_name}: {e}")

        result = f"✅ Posted to {success_count} divisions."
        if not_found:
            result += f"\n⚠️ Threads not found: {', '.join(not_found)}"
        if error_list:
            result += f"\n❌ Errors: {', '.join(error_list)}"
        await destination.send(result)

    async def _run_notify_all(self, destination, params: dict):
        """Mirrors TournamentCommands.notify_all but reports to a specific destination."""
        from match_utils import (
            get_tournament_sheets, get_player_matches,
            load_hero_builds_from_sheets, load_player_mapping,
            send_dm_to_player, format_table, split_message, normalize_name
        )
        default_week = self.cog_ref.default_week
        mapping_url = self.cog_ref.mapping_sheet_url
        tournaments = self.cog_ref.tournaments

        week_raw = params.get("week", "default")
        week = default_week if week_raw == "default" else int(week_raw)

        await destination.send(f"🤖 Scheduled: gathering players with pending matches (week {week})...")
        mapping = load_player_mapping(mapping_url)
        if not mapping:
            await destination.send("❌ Scheduled task: no player mapping loaded.")
            return

        tourney_data = {}
        for tourney in tournaments:
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=True)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                tourney_data[tourney['name']] = {'tourney': tourney, 'sheets': sheets, 'builds': builds}
            except Exception as e:
                await destination.send(f"⚠️ Error loading {tourney['name']}: {e}")

        players_with_pending = set()
        for player in mapping:
            for td in tourney_data.values():
                try:
                    _, pending = get_player_matches(td['sheets'], player, week)
                    if pending:
                        players_with_pending.add(player)
                        break
                except Exception:
                    pass

        if not players_with_pending:
            await destination.send("✅ Scheduled task: no players have pending matches.")
            return

        await destination.send(f"📬 Sending DMs to {len(players_with_pending)} players...")
        success_count = 0
        for player in players_with_pending:
            discord_id = mapping[player]
            pending_details = []
            for td in tourney_data.values():
                try:
                    _, pending = get_player_matches(td['sheets'], player, week)
                    if not pending:
                        continue
                    builds = td['builds']
                    rows = []
                    for m in pending:
                        if player in m['player1']:
                            ph, opp = m['player1'], m['player2']
                        else:
                            ph, opp = m['player2'], m['player1']
                        rows.append([
                            m['week'], m['division'],
                            f"{ph} ({builds.get(normalize_name(ph), '?')})",
                            f"{opp} ({builds.get(normalize_name(opp), '?')})"
                        ])
                    pending_details.append(
                        f"**{td['tourney']['name']}**\n```\n"
                        f"{format_table(rows, ['Week', 'Division', 'Your Hero', 'Opponent'], 'Pending')}\n```"
                    )
                except Exception as e:
                    pending_details.append(f"⚠️ Error in {td['tourney']['name']}: {e}")
            if pending_details:
                msg = f"⏳ **{player}**, you have pending matches:\n\n" + "\n".join(pending_details)
                for chunk in split_message(msg):
                    ok = await send_dm_to_player(self.bot, discord_id, chunk)
                    if not ok:
                        await destination.send(f"⚠️ Could not DM {player} (DMs disabled).")
                        break
                else:
                    success_count += 1
            await asyncio.sleep(1)

        await destination.send(f"✅ DMs sent to {success_count}/{len(players_with_pending)} players.")
