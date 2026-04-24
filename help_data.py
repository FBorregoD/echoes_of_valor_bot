"""
help_data.py — Help embed builders for the tournament bot.
"""

import discord


def build_help_embed(command_name: str = None, bot_mention: str = "@Bot") -> discord.Embed:
    """
    Return an embed for !help or !help <command>.
    """
    if command_name is None:
        embed = discord.Embed(
            title="📖 Bot Commands",
            description=(
                f"Use `{bot_mention} !<command>` or just `!<command>` if the bot has prefix access.\n"
                f"Use `{bot_mention} !help <command>` for details on any command."
            ),
            color=discord.Color.blue()
        )
        embed.add_field(
            name="🌐 Public",
            value=(
                "`!matches` / `!m <player> [week]` — Player's matches as image (with text fallback).\n"
                "`!division` / `!d [division] [week]` — Division matchups as image.\n"
                "`!standings` / `!c [tournament] [division]` — Division standings as image.\n"
                "`!tournaments` — List available tournaments and their aliases."
            ),
            inline=False
        )
        embed.add_field(
            name="🔐 Admin — Tournament",
            value=(
                "`!sendto <player> [week]` — Send a player their matches by DM.\n"
                "`!notify_all [week]` — DM all players with pending matches.\n"
                "`!post_divisions [week] [tournament]` — Post matchups to all division threads.\n"
                "`!post_standings [tournament]` — Post standings to all division threads.\n"
                "`!refresh` — Reload all cached Google Sheets data."
            ),
            inline=False
        )
        embed.add_field(
            name="🗓️ Admin — Scheduler",
            value=(
                "`!schedule add <action> <when> [options]` — Create a recurring task.\n"
                "`!schedule list` — List all scheduled tasks.\n"
                "`!schedule remove <id>` — Delete a task.\n"
                "`!schedule info <id>` — Show task details.\n"
                "`!schedule actions` — List available actions.\n\n"
                "**When:** `monday 09:00` (weekly) or `every=2h` / `30m` / `1d` (interval)\n"
                "**Options:** `tz=UTC` · `week=4` · `tournament=MA` · `channel=<id>`\n\n"
                "**Examples:**\n"
                "```\n"
                f"!schedule add post_divisions monday 09:00 tz=Europe/Madrid tournament=MA week=4\n"
                f"!schedule add standings monday 09:00 tz=Europe/Madrid tournament=MA\n"
                f"!schedule add notify_all every=2h week=4\n"
                f"!schedule remove 3\n"
                "```"
            ),
            inline=False
        )
        embed.add_field(
            name="🛠️ Admin — Debug",
            value=(
                "`!context_debug` — Show channel/tournament context for this channel.\n"
                "`!test_map` — Show full player → Discord ID mapping.\n"
                "`!test_id <player>` — Look up Discord ID for one player.\n"
                "`!dmtest <user_id> <message>` — Send a test DM."
            ),
            inline=False
        )
        embed.set_footer(text=f"Example: {bot_mention} !m Scorium 4  |  !d 4  (inside a division thread)")
        return embed

    # Detailed command help
    cmd = command_name.lstrip('!').lower()
    cmd = {'m': 'matches', 'c': 'standings', 'd': 'division'}.get(cmd, cmd)

    help_texts = {
        'matches': (
            "!matches / !m",
            "Show a player's matches for a given week.",
            f"`{bot_mention} !m <player> [week]`",
            f"`{bot_mention} !m Scorium 4`",
            "Posts an image in all contexts (including DMs). Falls back to text if image rendering fails."
        ),
        'division': (
            "!division / !d",
            "Show all matchups for a division in a given week.",
            f"`{bot_mention} !d [division] [week]`",
            (
                f"`{bot_mention} !d 4`  — week 4 of the current thread's division\n"
                f"`{bot_mention} !d cadmium 2`  — week 2 of CADMIUM"
            ),
            "Inside a division thread the division name is inferred automatically."
        ),
        'standings': (
            "!standings / !c",
            "Show the current standings table for a division.",
            f"`{bot_mention} !c [tournament] [division]`",
            (
                f"`{bot_mention} !c`  — standings for the current thread\n"
                f"`{bot_mention} !c eov cadmium`  — explicit tournament + division"
            ),
            "Tournament and division are inferred from the channel/thread when possible."
        ),
        'tournaments': (
            "!tournaments",
            "List all configured tournaments and their short aliases.",
            f"`{bot_mention} !tournaments`",
            "", ""
        ),
        'sendto': (
            "!sendto",
            "Send a player their match schedule by DM.",
            f"`{bot_mention} !sendto <player> [week]`",
            f"`{bot_mention} !sendto Scorium 4`",
            "Requires admin. Player must be in the mapping sheet."
        ),
        'notify_all': (
            "!notify_all",
            "DM every player who has pending (unplayed) matches.",
            f"`{bot_mention} !notify_all [week]`",
            f"`{bot_mention} !notify_all 4`",
            "Requires admin. May be slow for large rosters."
        ),
        'post_divisions': (
            "!post_divisions",
            "Post this week's matchups to every division thread.",
            f"`{bot_mention} !post_divisions [week] [tournament]`",
            f"`{bot_mention} !post_divisions 4 MA`",
            "Requires admin. Tournament defaults to the channel's bound tournament."
        ),
        'post_standings': (
            "!post_standings",
            "Post current standings to every division thread of a tournament.",
            f"`{bot_mention} !post_standings [tournament]`",
            f"`{bot_mention} !post_standings EoV`",
            "Requires admin. Tournament defaults to the channel's bound tournament."
        ),
        'refresh': (
            "!refresh",
            "Clear and reload all cached Google Sheets data.",
            f"`{bot_mention} !refresh`",
            "", "Requires admin."
        ),
        'context_debug': (
            "!context_debug",
            "Show how the bot resolves the current channel (tournament, thread, neutral…).",
            f"`{bot_mention} !context_debug`",
            "", "Requires admin."
        ),
        'test_map': (
            "!test_map",
            "Print the full player → Discord ID mapping (for debugging).",
            f"`{bot_mention} !test_map`",
            "", "Requires admin."
        ),
        'test_id': (
            "!test_id",
            "Look up the Discord ID registered for a specific player name.",
            f"`{bot_mention} !test_id <player>`",
            f"`{bot_mention} !test_id Scorium`",
            "Requires admin."
        ),
        'dmtest': (
            "!dmtest",
            "Send a test DM to any Discord user by numeric ID.",
            f"`{bot_mention} !dmtest <user_id> <message>`",
            f"`{bot_mention} !dmtest 254177975417700352 Hello there`",
            "Requires admin."
        ),
        'schedule': (
            "!schedule",
            "Manage recurring scheduled tasks (post_divisions, notify_all, standings).",
            (
                f"`{bot_mention} !schedule add <action> <when> [options]`\n"
                f"`{bot_mention} !schedule list`\n"
                f"`{bot_mention} !schedule remove <id>`\n"
                f"`{bot_mention} !schedule info <id>`\n"
                f"`{bot_mention} !schedule actions`"
            ),
            (
                f"`{bot_mention} !schedule add post_divisions monday 09:00 tz=Europe/Madrid tournament=MA week=4`\n"
                f"`{bot_mention} !schedule add notify_all every=2h week=4`\n"
                f"`{bot_mention} !schedule remove 3`"
            ),
            (
                "Requires admin.\n"
                "**`<when>` formats:** `monday 09:00` (+ `tz=`) · `every=30m` · `every=2h` · `every=1d`\n"
                "**Options:** `tz` · `week` · `tournament` · `channel` · `end_week`\n"
                "**Actions:** `post_divisions`, `notify_all`, `standings`\n"
                "`standings` does not use `week` — it always posts current standings."
            )
        ),
    }

    if cmd in help_texts:
        title, desc, usage, example, note = help_texts[cmd]
        embed = discord.Embed(title=title, description=desc, color=discord.Color.green())
        embed.add_field(name="Usage", value=usage, inline=False)
        if example:
            embed.add_field(name="Example", value=example, inline=False)
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        return embed
    else:
        return discord.Embed(
            title="Unknown Command",
            description=(
                f"`{command_name}` is not recognised. "
                f"Use `{bot_mention} !help` to see all commands."
            ),
            color=discord.Color.red()
        )