"""
tournament_actions.py — Shared business logic for tournament bot actions.

Both direct commands (commands.py) and scheduled tasks (scheduler.py)
call these functions. No Discord command/cog logic lives here.
"""

import asyncio
import logging

import discord

import io
import discord
from image_render import render_matchups
from match_utils import (
    get_tournament_sheets,
    get_division_matches,
    get_player_matches,
    load_hero_builds_from_sheets,
    load_player_mapping,
    send_dm_to_player,
    split_message,
    normalize_name,
    week_has_matches,
    is_division_sheet,
)

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

def find_tournament(tournaments: list[dict], name_or_alias: str) -> dict | None:
    """Find a tournament by name or alias (case-insensitive)."""
    key = name_or_alias.lower()
    for t in tournaments:
        if t['name'].lower() == key or t.get('alias', '').lower() == key:
            return t
    return None


def get_division_sheets(sheets: dict) -> list[str]:
    """
    Return sheet names that are real division sheets.
    Uses is_division_sheet() from match_utils: a sheet is a division if it
    contains a SCHEDULE marker followed by match rows in 'Player - Hero' format.
    This reliably excludes meta-sheets (Legions, Leagues overview, Format, etc.)
    regardless of their name.
    """
    return [name for name, df in sheets.items() if is_division_sheet(df)]


def get_threads_for_channel(channel: discord.TextChannel) -> dict[str, discord.Thread]:
    """Return {thread_name_lower: thread} for all threads in a single channel."""
    return {thread.name.lower(): thread for thread in channel.threads}


def resolve_week(week_raw: str | int, default_week: int) -> int:
    """Resolve 'default' or a numeric string/int to an integer week number."""
    if week_raw in ("default", None):
        return default_week
    return int(week_raw)


def build_division_image(tourney_name: str, div_name: str, week: int,
                          current: list, pending: list, builds: dict) -> bytes | None:
    """
    Render the division matchups as a PNG image (bytes).
    Returns None if there are no matches at all.
    """
    def get_build(name): return builds.get(normalize_name(name), "?")

    cur_rows = [
        (m["player1"], get_build(m["player1"]), m["player2"], get_build(m["player2"]))
        for m in current
    ]
    pend_rows = [
        (m["week"], m["player1"], get_build(m["player1"]), m["player2"], get_build(m["player2"]))
        for m in pending
    ]
    if not cur_rows and not pend_rows:
        return None
    return render_matchups(
        title=f"{tourney_name} · {div_name}",
        week_label=f"Week {week}",
        current_rows=cur_rows,
        pending_rows=pend_rows,
    )


async def send_division_image(destination, tourney_name: str, div_name: str, week: int,
                               current: list, pending: list, builds: dict):
    """Send the matchup image to a Discord channel/thread. Falls back to text on error."""
    try:
        img_bytes = build_division_image(tourney_name, div_name, week, current, pending, builds)
        if img_bytes is None:
            return
        filename = f"{div_name.lower()}_week{week}.png"
        await destination.send(file=discord.File(io.BytesIO(img_bytes), filename=filename))
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Image render failed for {div_name}: {e}", exc_info=True)
        # Fallback: plain text
        lines = [f"**🏆 {tourney_name} · {div_name}** — Week {week}"]
        for m in current:
            lines.append(f"{m['player1']} vs {m['player2']}")
        for m in pending:
            lines.append(f"Wk {m['week']}: {m['player1']} vs {m['player2']} (pending)")
        await destination.send("\n".join(lines))


def build_pending_dm(player: str, tourney_name: str, pending: list, builds: dict) -> list[str]:
    """Return a list of DM chunks for a player's pending matches in one tournament."""
    rows = []
    for m in pending:
        ph, opp = (m['player1'], m['player2']) if player in m['player1'] else (m['player2'], m['player1'])
        rows.append([
            m['week'], m['division'],
            f"{ph} ({builds.get(normalize_name(ph), '?')})",
            f"{opp} ({builds.get(normalize_name(opp), '?')})",
        ])
    chunks = format_table_messages(rows, ['Week', 'Division', 'Your Hero', 'Opponent'], f'Pending — {tourney_name}')
    return chunks


# ── Core actions ───────────────────────────────────────────────────────────────

async def run_post_divisions(
    *,
    destination: discord.abc.Messageable,
    tournaments: list[dict],
    default_week: int,
    tournament_alias: str = "MA",
    week_raw: str | int = "default",
    force_refresh: bool = False,
) -> tuple[int, list[str], list[str]]:
    """
    Post division matchups to threads inside `destination`'s parent channel.

    Returns (success_count, not_found_list, error_list).
    The caller is responsible for sending the summary message.
    """
    week = resolve_week(week_raw, default_week)
    tourney = find_tournament(tournaments, tournament_alias)
    if not tourney:
        await destination.send(f"❌ Tournament `{tournament_alias}` not found.")
        return 0, [], []

    sheets = get_tournament_sheets(tourney['url'], force_refresh=force_refresh)
    builds = load_hero_builds_from_sheets(
        sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
    )

    division_names = get_division_sheets(sheets)
    if not division_names:
        await destination.send("⚠️ No division sheets found.")
        return 0, [], []

    # Resolve the channel to search threads in
    if isinstance(destination, discord.TextChannel):
        target_channel = destination
    else:
        target_channel = getattr(destination, 'parent', None)

    if target_channel and isinstance(target_channel, discord.TextChannel):
        thread_dict = get_threads_for_channel(target_channel)
    else:
        # Fallback: search the whole guild
        thread_dict = {}
        for ch in destination.guild.text_channels:
            thread_dict.update(get_threads_for_channel(ch))

    success_count, not_found, error_list = 0, [], []
    for div_name in division_names:
        thread = thread_dict.get(div_name.strip().lower())
        if not thread:
            not_found.append(div_name)
            continue
        try:
            current, pending = get_division_matches(sheets, div_name, week)
            if not current and not pending:
                continue
            await send_division_image(thread, tourney['name'], div_name, week, current, pending, builds)
            success_count += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error posting to thread '{div_name}': {e}", exc_info=True)
            error_list.append(f"{div_name}: {e}")

    return success_count, not_found, error_list


async def run_notify_all(
    *,
    bot,
    destination: discord.abc.Messageable,
    tournaments: list[dict],
    mapping_url: str,
    default_week: int,
    week_raw: str | int = "default",
    force_refresh: bool = False,
) -> tuple[int, int]:
    """
    Send DMs to all players with pending matches.

    Returns (success_count, total_players_with_pending).
    """
    week = resolve_week(week_raw, default_week)
    mapping = load_player_mapping(mapping_url)
    if not mapping:
        await destination.send("❌ No player mapping loaded. Cannot proceed.")
        return 0, 0

    # Load all tournament data once
    tourney_data = {}
    for tourney in tournaments:
        try:
            sheets = get_tournament_sheets(tourney['url'], force_refresh=force_refresh)
            builds = load_hero_builds_from_sheets(
                sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
            )
            tourney_data[tourney['name']] = {'tourney': tourney, 'sheets': sheets, 'builds': builds}
        except Exception as e:
            await destination.send(f"⚠️ Error loading {tourney['name']}: {e}")

    # Find players with pending matches
    players_with_pending = {
        player
        for player in mapping
        for td in tourney_data.values()
        if _has_pending(td['sheets'], player, week)
    }

    if not players_with_pending:
        await destination.send("✅ No players have pending matches.")
        return 0, 0

    await destination.send(f"📬 Sending DMs to {len(players_with_pending)} players...")
    success_count = 0
    for player in players_with_pending:
        discord_id = mapping[player]
        details = []
        for td in tourney_data.values():
            try:
                _, pending = get_player_matches(td['sheets'], player, week)
                if pending:
                    details.extend(build_pending_dm(player, td['tourney']['name'], pending, td['builds']))
            except Exception as e:
                details.append(f"⚠️ Error in {td['tourney']['name']}: {e}")

        if details:
            # First message: intro line
            intro = f"⏳ **{player}**, you have pending matches from previous weeks:"
            all_chunks = [intro] + details
            failed = False
            for chunk in all_chunks:
                ok = await send_dm_to_player(bot, discord_id, chunk)
                if not ok:
                    await destination.send(f"⚠️ Could not DM **{player}** (DMs disabled).")
                    failed = True
                    break
            if not failed:
                success_count += 1
        await asyncio.sleep(1)

    return success_count, len(players_with_pending)


async def advance_auto_week(
    *,
    task_id: int,
    current_week: int,
    sheets_by_tourney: dict,
    destination: discord.abc.Messageable,
    action_name: str,
    end_week: int | None = None,
):
    """
    After a successful scheduled run, advance current_week by 1.
    Stops (marks exhausted) if:
      - end_week is set and next_week > end_week, OR
      - no matches exist for the next week in any tournament.
    """
    from scheduler import update_current_week

    next_week = current_week + 1

    # Respect explicit end_week ceiling
    if end_week is not None and next_week > end_week:
        update_current_week(task_id, -1)
        await destination.send(
            f"🏁 **Reached end of schedule** (week {end_week}). "
            f"Scheduled task `{action_name}` (ID {task_id}) will no longer run. "
            f"Use `!schedule remove {task_id}` to clean it up."
        )
        logger.info(f"Task {task_id}: end_week {end_week} reached — marked as exhausted.")
        return

    has_next = any(
        week_has_matches(sheets, next_week)
        for sheets in sheets_by_tourney.values()
    )

    if has_next:
        update_current_week(task_id, next_week)
        logger.info(f"Task {task_id}: advanced to week {next_week}.")
    else:
        update_current_week(task_id, -1)
        await destination.send(
            f"🏁 **Season complete!** No matches found for week {next_week}. "
            f"Scheduled task `{action_name}` (ID {task_id}) will no longer run automatically. "
            f"Use `!schedule remove {task_id}` to clean it up if needed."
        )
        logger.info(f"Task {task_id}: no week {next_week} — marked as exhausted.")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _has_pending(sheets: dict, player: str, week: int) -> bool:
    try:
        _, pending = get_player_matches(sheets, player, week)
        return bool(pending)
    except Exception:
        return False
