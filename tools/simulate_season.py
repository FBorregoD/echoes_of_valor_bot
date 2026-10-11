"""Simulate a whole season of the auto-advancing post_divisions task, offline.

Runs the REAL scheduler code (SchedulerCog._fire → _season_over →
run_post_divisions → advance_auto_week) and the real !m week resolution
against a REAL tournament sheet, with Discord replaced by fakes and a
throwaway tasks.db. Nothing is sent to Discord; nothing touches tasks.db.

Time is simulated by hiding results that "haven't been played yet": at
step N (the fire that posts week N), results of weeks >= N are shown as
0-0. A few matches per week are deliberately reported one week late, so
pending matches show up. Use a sheet whose season is complete (default:
EoV season 10) to see every week, the season end, and what comes after.

Usage (from the repo root):
    python tools/simulate_season.py
    python tools/simulate_season.py --player Tuff --save-images sim_images
    python tools/simulate_season.py --url <sheet url> --builds-sheet "hero builds Season 11"
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands as commands_mod  # noqa: E402
import match_utils  # noqa: E402
import scheduler  # noqa: E402
import tournament_actions  # noqa: E402
from channel_context import build_channel_index  # noqa: E402
from commands import TournamentCommands  # noqa: E402
from match_utils import get_player_matches, is_division_sheet, parse_week_number  # noqa: E402

DEFAULT_URL = "https://docs.google.com/spreadsheets/d/19C1UHkmA7LroihS0C9IkjojU8UC6gSAyx2i_yncY0wk"
DEFAULT_BUILDS_SHEET = "hero builds Season 10"
GUILD_ID, CHANNEL_ID = 1, 100
LATE_EVERY = 6  # every Nth match row is reported one week late

sim_now = {"step": 1}


def mask_sheets(original: dict, step: int) -> dict:
    """Copy of the sheets as they'd look when week `step` is being posted."""
    masked = {}
    for name, df in original.items():
        if not is_division_sheet(df):
            masked[name] = df
            continue
        df = df.copy()
        week = None
        for idx in range(len(df)):
            cell = df.iat[idx, 0]
            if pd.notna(cell) and str(cell).strip().startswith("Week"):
                week = parse_week_number(str(cell)) or week
            if week is None or df.shape[1] < 6 or " - " not in str(df.iat[idx, 2]):
                continue
            available_at = week + (2 if idx % LATE_EVERY == 0 else 1)
            if step < available_at:
                df.iat[idx, 4] = 0
                df.iat[idx, 5] = 0
        masked[name] = df
    return masked


def install_fake_sheets(url: str) -> dict:
    print(f"Downloading {url} ...")
    original = match_utils.get_tournament_sheets(url, force_refresh=True)

    async def fake_async(_url, force_refresh=False, **_):
        return mask_sheets(original, sim_now["step"])

    for module in (match_utils, tournament_actions, commands_mod):
        if hasattr(module, "get_tournament_sheets_async"):
            module.get_tournament_sheets_async = fake_async
    return original


def make_discord_fakes(division_names: list[str], image_dir: str | None):
    log = []

    def sender(where):
        async def send(content=None, file=None, **_):
            if file is not None:
                log.append((where, f"[image {file.filename}]"))
                if image_dir:
                    os.makedirs(image_dir, exist_ok=True)
                    with open(os.path.join(image_dir, f"step{sim_now['step']:02d}_{file.filename}"), "wb") as f:
                        f.write(file.fp.read())
            else:
                log.append((where, content))
        return send

    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.name = CHANNEL_ID, "eov-general"
    channel.send = AsyncMock(side_effect=sender("#eov-general"))
    threads = []
    for div in division_names:
        thread = MagicMock(spec=discord.Thread)
        thread.name, thread.parent = div, channel
        thread.send = AsyncMock(side_effect=sender(f"thread {div}"))
        threads.append(thread)
    channel.threads = threads

    guild = MagicMock()
    guild.id = GUILD_ID
    guild.get_channel = lambda cid: channel if cid == CHANNEL_ID else None
    guild.get_thread = lambda tid: None
    channel.guild = guild

    bot = SimpleNamespace(
        get_guild=lambda gid: guild if gid == GUILD_ID else None,
        channel_index=build_channel_index([{
            "guild_id": GUILD_ID,
            "channels": [{"channel_id": CHANNEL_ID, "channel_name": "eov-general", "tournament": "EoV"}],
        }]),
    )
    return bot, log


def describe_week(week: int | None) -> str:
    return {None: "fixed", -1: "-1 (season over)"}.get(week, str(week))


async def run(args):
    original = install_fake_sheets(args.url)
    division_names = [n for n, df in original.items() if is_division_sheet(df)]
    last_week = match_utils.get_latest_week_from_sheets(original)
    print(f"{len(division_names)} divisions, last week with matches: {last_week}\n")

    player = args.player
    if not player:
        first_div = original[division_names[0]]
        first_row = first_div[first_div.iloc[:, 2].astype(str).str.contains(" - ", regex=False)].iloc[0]
        player = str(first_row.iloc[2]).split(" - ")[0].strip()

    tmp_db = os.path.join(tempfile.mkdtemp(), "sim_tasks.db")
    scheduler.DB_PATH = tmp_db
    scheduler.init_db()

    tournament = {"name": "Echoes of Valor", "alias": "EoV", "url": args.url,
                  "builds_sheet": args.builds_sheet,
                  "builds_mapping": {"player_col": 3, "ancestry_col": 4, "class_col": 5},
                  "season": 0}
    tournament_cmds = TournamentCommands(MagicMock(), [tournament], mapping_sheet_url="unused", default_week=2)
    bot, log = make_discord_fakes(division_names, args.save_images)

    sched = scheduler.SchedulerCog.__new__(scheduler.SchedulerCog)  # skip __init__: no background loop
    sched.bot, sched.cog_ref = bot, tournament_cmds

    task_id = scheduler.add_task(
        action="post_divisions", params={"tournament": "EoV", "week": "default"},
        guild_id=GUILD_ID, channel_id=CHANNEL_ID, thread_id=None,
        weekday=0, hour=9, minute=0, tz="Europe/Madrid", created_by=0,
        current_week=args.start_week,
    )
    print(f"Task {task_id}: post_divisions EoV, auto-advance from week {args.start_week}. "
          f"Following player: {player}\n")

    for step in range(args.start_week, last_week + 3):
        sim_now["step"] = step
        before = scheduler.get_task(task_id)["current_week"]
        log.clear()
        await sched._fire(scheduler.get_task(task_id))
        after = scheduler.get_task(task_id)["current_week"]

        print(f"=== Fire #{step - args.start_week + 1} (results known up to week {step - 1}) "
              f"— current_week {describe_week(before)} → {describe_week(after)}")
        images = sum(1 for where, msg in log if where.startswith("thread"))
        if images:
            print(f"   posted {images} division image(s) to threads")
        for where, msg in log:
            if not where.startswith("thread"):
                print(f"   {where}: {msg}")
        if not log:
            print("   (nothing sent — task skipped)")

        weeks, _ = await tournament_cmds._get_weeks_per_tournament({"tournament": tournament}, guild_id=GUILD_ID)
        week = weeks["EoV"]
        current, pending = get_player_matches(mask_sheets(original, step), player, week)
        cur_txt = ", ".join(f"{m['player1']} vs {m['player2']}{' ✓' if m['finished'] else ''}" for m in current) or "—"
        pend_txt = ", ".join(f"wk{m['week']}{' (misreported)' if m['misreported'] else ''}" for m in pending) or "—"
        print(f"   !m {player} → shows week {week}: current [{cur_txt}] | pending [{pend_txt}]\n")

    print(f"Done. Throwaway DB: {tmp_db}")
    if args.save_images:
        print(f"Images saved to {os.path.abspath(args.save_images)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL, help="tournament sheet URL (default: EoV season 10)")
    parser.add_argument("--builds-sheet", default=DEFAULT_BUILDS_SHEET)
    parser.add_argument("--player", help="player to follow with !m (default: first one found)")
    parser.add_argument("--start-week", type=int, default=1)
    parser.add_argument("--save-images", metavar="DIR", help="save every posted division image here")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252 (no ✓/→/emoji)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    tournament_actions.asyncio.sleep = AsyncMock()  # skip the 0.5s pause between thread posts
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
