"""
channel_context.py — Canal/hilo → torneo/división resolution.

Rules
-----
- DM (no guild)         → neutral: all tournaments, no default division
- Guild channel listed
    tournament = <alias> → bound to that tournament
    tournament = null    → neutral channel: all tournaments, no default division
- Guild channel NOT listed → ignored (bot stays silent)

Context object returned by resolve_context():
    {
        "allowed":     bool,   # False = bot must stay silent
        "neutral":     bool,   # True = no tournament bound (DM or explicit null)
        "tournament":  dict|None,  # tournament config dict or None
        "division":    str|None,   # thread name if inside a division thread
        "channel":     discord.abc.Messageable,  # resolved channel/thread
    }
"""

from __future__ import annotations
import logging
import discord

logger = logging.getLogger(__name__)


# ── Build fast lookup tables from config ──────────────────────────────────────

def build_channel_index(guild_channels_config: list[dict]) -> dict:
    """
    Returns a nested dict:
        index[guild_id][channel_id] = {"tournament": alias_or_None, "channel_name": str}

    Also builds a fallback by name:
        index[guild_id]["_by_name"][channel_name_lower] = same dict

    Duplicate channel_ids within a guild are logged and skipped (first entry wins).
    """
    import logging
    _log = logging.getLogger(__name__)

    index: dict[int, dict] = {}
    for guild_entry in guild_channels_config:
        gid = guild_entry.get("guild_id")
        if not gid:
            continue
        index[gid] = {"_by_name": {}}
        for ch in guild_entry.get("channels", []):
            cid  = ch.get("channel_id")
            name = ch.get("channel_name", "").lower()
            rec  = {
                "tournament": ch.get("tournament"),
                "channel_name": ch.get("channel_name", ""),
            }
            if cid:
                if cid in index[gid]:
                    _log.warning(
                        f"config: guild {gid} has duplicate channel_id {cid} "
                        f"('{ch.get('channel_name')}' vs '{index[gid][cid]['channel_name']}'). "
                        f"First entry kept — fix the config."
                    )
                else:
                    index[gid][cid] = rec
            if name:
                index[gid]["_by_name"][name] = rec
    return index


# ── Core resolver ─────────────────────────────────────────────────────────────

def resolve_context(
    ctx_channel,         # discord.TextChannel | discord.Thread | discord.DMChannel
    guild,               # discord.Guild | None
    channel_index: dict, # built by build_channel_index()
    tournaments: list[dict],
    find_tournament_fn,  # tournament_actions.find_tournament
) -> dict:
    """
    Resolve the channel/thread context into a structured dict.
    Never raises — always returns a context dict.
    """
    # ── DM ────────────────────────────────────────────────────────────────────
    if guild is None:
        return {
            "allowed": True,
            "neutral": True,
            "tournament": None,
            "division": None,
            "channel": ctx_channel,
        }

    gid = guild.id

    # ── Guild not configured at all → silent ──────────────────────────────────
    if gid not in channel_index:
        logger.debug(f"resolve_context: guild {gid} not in channel_index → silent")
        return _silent()

    guild_map = channel_index[gid]

    # ── Resolve thread vs text channel ────────────────────────────────────────
    is_thread = isinstance(ctx_channel, discord.Thread)
    if is_thread:
        parent_channel = ctx_channel.parent
        thread_name    = ctx_channel.name
    else:
        parent_channel = ctx_channel
        thread_name    = None

    # Look up the parent channel (by id first, then by name)
    parent_id   = parent_channel.id   if parent_channel else None
    parent_name = (parent_channel.name or "").lower() if parent_channel else ""

    rec = guild_map.get(parent_id) or guild_map["_by_name"].get(parent_name)

    if rec is None:
        logger.debug(
            f"resolve_context: channel {parent_id!r}/{parent_name!r} "
            f"not listed for guild {gid} → silent"
        )
        return _silent()

    # ── Resolve tournament ────────────────────────────────────────────────────
    tourney_alias = rec["tournament"]
    if tourney_alias is None:
        # Neutral channel
        return {
            "allowed": True,
            "neutral": True,
            "tournament": None,
            "division": thread_name,
            "channel": ctx_channel,
        }

    tourney = find_tournament_fn(tournaments, tourney_alias)
    if tourney is None:
        logger.warning(
            f"resolve_context: alias {tourney_alias!r} in config not found in tournaments list"
        )
        return _silent()

    return {
        "allowed": True,
        "neutral": False,
        "tournament": tourney,
        "division": thread_name,   # None if not in a thread
        "channel": ctx_channel,
    }


def _silent() -> dict:
    return {
        "allowed": False,
        "neutral": False,
        "tournament": None,
        "division": None,
        "channel": None,
    }


# ── Argument parsing helper ───────────────────────────────────────────────────

def parse_division_args(args: tuple, ctx_division: str | None) -> tuple[str | None, int | None]:
    """
    Parse the raw argument tuple for !division / !d into (division_name, week).

    Cases:
      In a division thread:
        !d 4         → (ctx_division, 4)
        !d cadmium   → (cadmium, None)   ← ambiguous: could be name or week
        !d cadmium 2 → (cadmium, 2)

      Outside thread (ctx_division=None):
        !d cadmium   → (cadmium, None)
        !d cadmium 2 → (cadmium, 2)
        !d 4         → (None, 4)         ← no division known, week given

    Returns (division_name_or_None, week_or_None).
    """
    if not args:
        return ctx_division, None

    first = args[0]
    rest  = args[1:]

    # If first arg is a pure integer → it's a week number
    try:
        week = int(first)
        return ctx_division, week
    except ValueError:
        pass

    # First arg is a division name
    division = first
    week = None
    if rest:
        try:
            week = int(rest[0])
        except ValueError:
            pass
    return division, week
