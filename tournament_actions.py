"""
tournament_actions.py — Shared business logic for tournament bot actions.

Both direct commands (commands.py) and scheduled tasks (scheduler.py)
call these functions. No Discord command/cog logic lives here.
"""

import asyncio
import logging

import discord

from match_utils import (
    get_tournament_sheets,
    get_division_matches,
    get_player_matches,
    load_hero_builds_from_sheets,
    load_player_mapping,
    send_dm_to_player,
    format_table,
    split_message,
    normalize_name,
    week_has_matches,
)

logger = logging.getLogger(__name__)

EXCLUDED_SHEET_KEYWORDS = [
    'formulierreacties', 'hero builds', 'leagues overview',
    'format', 'scoresheet', 'arma heroum',
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_tournament(tournaments: list[dict], name_or_alias: str) -> dict | None:
    """Find a tournament by name or alias (case-insensitive)."""
    key = name_or_alias.lower()
    for t in tournaments:
        if t['name'].lower() == key or t.get('alias', '').lower() == key:
            return t
    return None


def get_division_sheets(sheets: dict) -> list[str]:
    """Return sheet names that represent actual divisions (filtering meta-sheets)."""
    return [
        name for name in sheets.keys()
        if not any(kw in name.lower() for kw in EXCLUDED_SHEET_KEYWORDS)
    ]


def get_threads_for_channel(channel: discord.TextChannel) -> dict[str, discord.Thread]:
    """Return {thread_name_lower: thread} for all threads in a single channel."""
    return {thread.name.lower(): thread for thread in channel.threads}


def resolve_week(week_raw: str | int, default_week: int) -> int:
    """Resolve 'default' or a numeric string/int to an integer week number."""
    if week_raw in ("default", None):
        return default_week
    return int(week_raw)


def build_division_message(tourney_name: str, div_name: str, week: int,
                            current: list, pending: list, builds: dict) -> str:
    """Build the formatted message for a single division's matchups."""
    msg = f"**🏆 {tourney_name} - Division {div_name}**\n📅 **Pairings for week {week}**\n\n"

    if current:
        rows = []
        for m in current:
            p1, p2 = m['player1'], m['player2']
            rows.append([
                f"{p1} ({builds.get(normalize_name(p1), '?')})",
                f"{p2} ({builds.get(normalize_name(p2), '?')})",
            ])
        msg += f"```\n{format_table(rows, ['Player 1', 'Player 2'], f'Week {week}')}\n```"
    else:
        msg += "📅 No matches for this week.\n"

    if pending:
        rows = []
        for m in pending:
            p1, p2 = m['player1'], m['player2']
            rows.append([
                m['week'],
                f"{p1} ({builds.get(normalize_name(p1), '?')})",
                f"{p2} ({builds.get(normalize_name(p2), '?')})",
            ])
        msg += (
            f"\n**⏳ Pending matches from previous weeks:**\n"
            f"```\n{format_table(rows, ['Week', 'Player 1', 'Player 2'], 'Pending')}\n```"
        )

    return msg


def build_pending_dm(player: str, tourney_name: str, pending: list, builds: dict) -> str:
    """Build the DM content for a player's pending matches in one tournament."""
    rows = []
    for m in pending:
        if player in m['player1']:
            ph, opp = m['player1'], m['player2']
        else:
            ph, opp = m['player2'], m['player1']
        rows.append([
            m['week'], m['division'],
            f"{ph} ({builds.get(normalize_name(ph), '?')})",
            f"{opp} ({builds.get(normalize_name(opp), '?')})",
        ])
    return (
        f"**{tourney_name}**\n```\n"
        f"{format_table(rows, ['Week', 'Division', 'Your Hero', 'Opponent'], 'Pending matches')}\n```"
    )


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
            msg = build_division_message(tourney['name'], div_name, week, current, pending, builds)
            for chunk in split_message(msg):
                await thread.send(chunk)
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
                    details.append(build_pending_dm(player, td['tourney']['name'], pending, td['builds']))
            except Exception as e:
                details.append(f"⚠️ Error in {td['tourney']['name']}: {e}")

        if details:
            msg = f"⏳ **{player}**, you have pending matches from previous weeks:\n\n" + "\n".join(details)
            for chunk in split_message(msg):
                ok = await send_dm_to_player(bot, discord_id, chunk)
                if not ok:
                    await destination.send(f"⚠️ Could not DM **{player}** (DMs disabled).")
                    break
            else:
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
):
    """
    After a successful scheduled run, advance current_week by 1.
    If the next week has no matches, mark the task as exhausted (current_week = -1).

    Imports scheduler helpers lazily to avoid circular imports.
    """
    from scheduler import update_current_week

    next_week = current_week + 1
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
