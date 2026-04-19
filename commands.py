import discord
from discord.ext import commands
import logging

from match_utils import (
    get_tournament_sheets,
    refresh_tournament_cache,
    get_division_matches,
    load_hero_builds_from_sheets,
    load_player_mapping,
    send_dm_to_player,
    build_matches_message,
    split_message,
    normalize_name,
    format_table,
    get_division_standings,
    format_table_messages,
)
from tournament_actions import (
    find_tournament,
    run_post_divisions,
    run_notify_all,
    get_threads_for_channel,
)
from channel_context import resolve_context, parse_division_args

logger = logging.getLogger(__name__)


# ── Admin check ────────────────────────────────────────────────────────────────

def _is_bot_admin(ctx) -> bool:
    return ctx.author.id in getattr(ctx.bot, 'admin_user_ids', [])


def is_bot_admin():
    async def predicate(ctx):
        if _is_bot_admin(ctx):
            return True
        raise commands.CheckFailure(
            "❌ You don't have permission to use this command. "
            "Only authorised bot admins can do that."
        )
    return commands.check(predicate)


# ── Cog ────────────────────────────────────────────────────────────────────────

class TournamentCommands(commands.Cog):
    def __init__(self, bot, tournaments, mapping_sheet_url, default_week):
        self.bot = bot
        self.tournaments = tournaments
        self.mapping_sheet_url = mapping_sheet_url
        self.default_week = default_week

    # ── Context helper ─────────────────────────────────────────────────────────

    def _ctx(self, ctx) -> dict:
        """Resolve channel context for a command invocation."""
        return resolve_context(
            ctx_channel=ctx.channel,
            guild=ctx.guild,
            channel_index=getattr(self.bot, 'channel_index', {}),
            tournaments=self.tournaments,
            find_tournament_fn=find_tournament,
        )

    def _tourneys_for_ctx(self, context: dict) -> list[dict]:
        """Return the list of tournaments to operate on given a context."""
        if context['tournament']:
            return [context['tournament']]
        return self.tournaments  # neutral / DM → all

    # ── Public commands ────────────────────────────────────────────────────────

    @commands.command(name='ping')
    async def ping(self, ctx):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        await ctx.send("Pong!")

    @commands.command(name='tournaments')
    async def list_tournaments(self, ctx):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        embed = discord.Embed(title="🏆 Available Tournaments", color=discord.Color.gold())
        tourneys = self._tourneys_for_ctx(context)
        for t in tourneys:
            embed.add_field(name=t['name'], value=f"Alias: `{t.get('alias', 'No alias')}`", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name='matches', aliases=['m'])
    async def matches_command(self, ctx, player: str, week: int = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        week = week or self.default_week
        tourneys = self._tourneys_for_ctx(context)
        await ctx.send(f"🔍 Searching for **{player}** in week **{week}**...")
        for tourney in tourneys:
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                messages, err = build_matches_message(tourney, player, week, force_refresh=False, builds=builds)
                if err:
                    await ctx.send(f"⚠️ Error in {tourney['name']}: {err}")
                elif messages:
                    for msg in messages:
                        for chunk in split_message(msg):
                            await ctx.send(chunk)
            except Exception as e:
                logger.error(f"matches_command ({tourney['name']}): {e}", exc_info=True)
                await ctx.send(f"❌ Unexpected error in {tourney['name']}: {e}")

    @commands.command(name='division', aliases=['d'])
    async def division_command(self, ctx, *args):
        context = self._ctx(ctx)
        if not context['allowed']:
            return

        # Parse args: resolve division name and week from args + thread context
        division_name, week = parse_division_args(args, context['division'])
        week = week or self.default_week

        if not division_name:
            await ctx.send(
                "❌ No division specified. "
                "Run this command inside a division thread, or use `!d <division> [week]`."
            )
            return

        await ctx.send(f"🔍 Looking for division **{division_name}** in week **{week}**...")
        tourneys = self._tourneys_for_ctx(context)
        found_any = False

        for tourney in tourneys:
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                current, pending = get_division_matches(sheets, division_name, week)
                if not current and not pending:
                    continue
                found_any = True
                msg = f"**🏆 {tourney['name']} - Division {division_name}**\n"
                if current:
                    rows = [[
                        f"{m['player1']} ({builds.get(normalize_name(m['player1']), '?')})",
                        f"{m['player2']} ({builds.get(normalize_name(m['player2']), '?')})",
                    ] for m in current]
                    msg += f"```\n{format_table(rows, ['Player 1', 'Player 2'], f'Week {week}')}\n```"
                else:
                    msg += "📅 No matches for this week.\n"
                if pending:
                    rows = [[
                        m['week'],
                        f"{m['player1']} ({builds.get(normalize_name(m['player1']), '?')})",
                        f"{m['player2']} ({builds.get(normalize_name(m['player2']), '?')})",
                    ] for m in pending]
                    msg += f"\n**⏳ Pending matches from previous weeks:**\n```\n{format_table(rows, ['Week', 'Player 1', 'Player 2'], 'Pending')}\n```"
                for chunk in split_message(msg):
                    await ctx.send(chunk)
            except Exception as e:
                logger.error(f"division_command ({tourney['name']}): {e}", exc_info=True)
                await ctx.send(f"❌ Error in {tourney['name']}: {e}")

        if not found_any:
            await ctx.send(f"⚠️ No matches found for division **{division_name}** in week **{week}**.")

    @commands.command(name='standings', aliases=['standing', 'c'])
    async def standings_command(self, ctx, *args):
        context = self._ctx(ctx)
        if not context['allowed']:
            return

        # Resolve tournament alias and division from args
        # Possible invocations:
        #   !standings              → use ctx tournament + ctx division (thread)
        #   !standings cadmium      → use ctx tournament + cadmium
        #   !standings eov          → tournament alias only (in neutral/thread)
        #   !standings eov cadmium  → explicit both
        tourney  = context['tournament']
        division = context['division']

        if args:
            # Try first arg as tournament alias
            maybe_tourney = find_tournament(self.tournaments, args[0])
            if maybe_tourney:
                tourney = maybe_tourney
                division = args[1] if len(args) > 1 else division
            else:
                # First arg is a division name
                division = args[0]

        if tourney is None:
            await ctx.send(
                "❌ No tournament resolved. "
                "Specify one: `!standings <tournament> [division]` (e.g. `!standings eov cadmium`)"
            )
            return

        if division is None:
            await ctx.send(
                "❌ No division specified. "
                "Run inside a division thread or use `!standings [tournament] <division>`."
            )
            return

        msg_loading = await ctx.send(
            f"🔍 Loading standings for **{division}** in **{tourney['name']}**..."
        )

        try:
            sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
            standings_data, headers = get_division_standings(sheets, division)

            if standings_data is None:
                await msg_loading.edit(
                    content=f"❌ Division `{division}` not found in **{tourney['name']}**."
                )
                return
            if not standings_data:
                await msg_loading.edit(
                    content=f"⚠️ Division `{division}` found but standings table is empty."
                )
                return

            messages = format_table_messages(
                standings_data, headers,
                f'🏆 Standings — {division} ({tourney["name"]})'
            )

            # ── Route to the correct thread ────────────────────────────────
            target = None

            # Already in the right thread?
            if context['division'] and context['division'].lower() == division.lower():
                target = ctx.channel
            else:
                # Search threads of the parent channel
                search_ch = getattr(ctx.channel, 'parent', ctx.channel)
                if isinstance(search_ch, discord.TextChannel):
                    threads = get_threads_for_channel(search_ch)
                    target = threads.get(division.lower())

                # Fallback: search guild-wide (only within allowed channels)
                if not target and ctx.guild:
                    ch_index = getattr(self.bot, 'channel_index', {})
                    gid = ctx.guild.id
                    allowed_ids = set()
                    if gid in ch_index:
                        for k, v in ch_index[gid].items():
                            if k != '_by_name' and isinstance(k, int):
                                allowed_ids.add(k)
                    for ch in ctx.guild.text_channels:
                        if allowed_ids and ch.id not in allowed_ids:
                            continue
                        threads = get_threads_for_channel(ch)
                        if division.lower() in threads:
                            target = threads[division.lower()]
                            break

            if target:
                for msg in messages:
                    await target.send(msg)
                if ctx.channel.id != target.id:
                    await msg_loading.edit(
                        content=f"✅ Standings published in {target.mention}"
                    )
                else:
                    await msg_loading.delete()
            else:
                # No thread found — post inline
                await msg_loading.delete()
                for msg in messages:
                    await ctx.send(msg)

        except Exception as e:
            logger.error(f"standings_command ({tourney['name']}): {e}", exc_info=True)
            await msg_loading.edit(content=f"❌ Unexpected error: {e}")

    # ── Admin commands ─────────────────────────────────────────────────────────

    @is_bot_admin()
    @commands.command(name='sendto')
    async def sendto_command(self, ctx, player: str, week: int = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        week = week or self.default_week
        await ctx.send(f"📬 Fetching matches for **{player}** (week {week})...")
        mapping = load_player_mapping(self.mapping_sheet_url)
        if player not in mapping:
            await ctx.send(f"❌ No Discord ID found for player **{player}**.")
            return
        discord_id = mapping[player]
        tourneys = self._tourneys_for_ctx(context)
        success_count = 0
        for tourney in tourneys:
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                messages, err = build_matches_message(tourney, player, week, force_refresh=False, builds=builds)
                if err:
                    await ctx.send(f"⚠️ Error in {tourney['name']}: {err}")
                elif messages:
                    for msg in messages:
                        for chunk in split_message(msg):
                            if not await send_dm_to_player(self.bot, discord_id, chunk):
                                await ctx.send(f"⚠️ Could not DM {player} for {tourney['name']}.")
                                break
                        else:
                            success_count += 1
            except Exception as e:
                logger.error(f"sendto_command ({tourney['name']}): {e}", exc_info=True)
                await ctx.send(f"❌ Unexpected error in {tourney['name']}: {e}")

        if success_count > 0:
            await ctx.send(f"✅ DM(s) sent to **{player}** ({success_count} tournament(s)).")
        else:
            try:
                user = await self.bot.fetch_user(discord_id)
                mention = user.mention
            except Exception:
                mention = player
            await ctx.send(
                f"❌ {mention}, I couldn't send you any DM.\n"
                "👉 Please enable DMs from server members or check your privacy settings."
            )

    @is_bot_admin()
    @commands.command(name='notify_all')
    async def notify_all_command(self, ctx, week: int = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        await ctx.send("🚀 Gathering players with pending matches...")
        success, total = await run_notify_all(
            bot=self.bot,
            destination=ctx,
            tournaments=self._tourneys_for_ctx(context),
            mapping_url=self.mapping_sheet_url,
            default_week=self.default_week,
            week_raw=week or "default",
            force_refresh=False,
        )
        if total > 0:
            await ctx.send(f"✅ DMs sent to {success} out of {total} players.")

    @is_bot_admin()
    @commands.command(name='post_divisions')
    async def post_divisions_command(self, ctx, week: int = None, tournament_alias: str = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        # Default alias: from context if bound, else "MA"
        if tournament_alias is None:
            tournament_alias = context['tournament']['alias'] if context['tournament'] else "MA"
        tourney = find_tournament(self.tournaments, tournament_alias)
        if not tourney:
            await ctx.send(f"❌ Tournament `{tournament_alias}` not found.")
            return
        await ctx.send(f"🚀 Posting **{tourney['name']}** week {week or self.default_week} matchups to threads...")
        success, not_found, errors = await run_post_divisions(
            destination=ctx.channel,
            tournaments=self.tournaments,
            default_week=self.default_week,
            tournament_alias=tournament_alias,
            week_raw=week or "default",
            force_refresh=False,
        )
        result = f"✅ Posted to {success} divisions.\n"
        if not_found:
            result += f"⚠️ Threads not found: {', '.join(not_found)}\n"
        if errors:
            result += f"❌ Errors: {', '.join(errors)}\n"
        await ctx.send(result)

    @is_bot_admin()
    @commands.command(name='refresh')
    async def refresh_cache(self, ctx):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        await ctx.send("🔄 Refreshing cache... This may take a moment.")
        for tourney in self.tournaments:
            try:
                refresh_tournament_cache(tourney['url'])
                await ctx.send(f"✅ Refreshed {tourney['name']}")
            except Exception as e:
                await ctx.send(f"❌ Error refreshing {tourney['name']}: {e}")
        try:
            load_player_mapping(self.mapping_sheet_url, force_refresh=True)
            await ctx.send("✅ Refreshed player mapping sheet")
        except Exception as e:
            await ctx.send(f"❌ Error refreshing mapping: {e}")
        await ctx.send("🎉 Cache refresh complete!")

    # ── Debug commands ─────────────────────────────────────────────────────────

    @is_bot_admin()
    @commands.command(name='test_map')
    async def test_map(self, ctx):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        mapping = load_player_mapping(self.mapping_sheet_url)
        if not mapping:
            await ctx.send("❌ Mapping is empty.")
        else:
            for chunk in split_message(f"📋 Mapping loaded: {mapping}"):
                await ctx.send(chunk)

    @is_bot_admin()
    @commands.command(name='test_id')
    async def test_id(self, ctx, player: str):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        mapping = load_player_mapping(self.mapping_sheet_url)
        if player in mapping:
            await ctx.send(f"🆔 ID for {player}: {mapping[player]}")
        else:
            await ctx.send(f"❌ Player `{player}` not found in mapping.")

    @is_bot_admin()
    @commands.command(name='dmtest')
    async def dmtest(self, ctx, user_id: int, *, message: str):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(message)
            await ctx.send("✅ DM sent.")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

    @is_bot_admin()
    @commands.command(name='context_debug')
    async def context_debug(self, ctx):
        """Show resolved context for the current channel (admin debug)."""
        context = self._ctx(ctx)
        lines = [
            f"**allowed:** {context['allowed']}",
            f"**neutral:** {context['neutral']}",
            f"**tournament:** {context['tournament']['name'] if context['tournament'] else 'None'}",
            f"**division (thread):** {context['division'] or 'None'}",
            f"**channel:** {ctx.channel.name} (id: {ctx.channel.id})",
        ]
        if ctx.guild:
            lines.append(f"**guild:** {ctx.guild.name} (id: {ctx.guild.id})")
        await ctx.send("\n".join(lines))

    # ── Help ───────────────────────────────────────────────────────────────────

    @commands.command(name='help')
    async def help_command(self, ctx, command_name: str = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        bot_mention = f"@{ctx.bot.user.name}"
        if command_name is None:
            embed = discord.Embed(
                title="📖 Bot Commands",
                description=(
                    f"Mention the bot before every command: `{bot_mention} !matches …`\n"
                    f"Use `{bot_mention} !help <command>` for details."
                ),
                color=discord.Color.blue()
            )
            embed.add_field(
                name="🌐 Public",
                value=(
                    "`!matches` / `!m` — Show matches for a player in a given week.\n"
                    "`!division` / `!d` — Show matchups for a division (or current thread).\n"
                    "`!standings` / `!c` — Show division standings.\n"
                    "`!tournaments` — List tournaments and their aliases."
                ),
                inline=False
            )
            embed.add_field(
                name="🔐 Admin — Tournament",
                value=(
                    "`!sendto` — Send a player their matches by DM.\n"
                    "`!notify_all` — DM all players with pending matches.\n"
                    "`!post_divisions` — Post matchups to division threads.\n"
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
                    f"{bot_mention} !schedule add post_divisions monday 09:00 tz=Europe/Madrid tournament=MA week=4\n"
                    f"{bot_mention} !schedule add post_divisions every=2h tournament=MA week=4\n"
                    f"{bot_mention} !schedule remove 3\n"
                    "```"
                ),
                inline=False
            )
            embed.add_field(
                name="🛠️ Admin — Debug",
                value="`!test_map` · `!test_id` · `!dmtest` · `!context_debug`",
                inline=False
            )
            embed.set_footer(text=f"Example: {bot_mention} !matches Scorium 4")
            await ctx.send(embed=embed)
        else:
            cmd = command_name.lstrip('!').lower()
            help_texts = {
                'standings':      ("!standings / !c",     "Show current division standings.",
                    f"`{bot_mention} !standings [tournament] [division]`",
                    f"`{bot_mention} !standings eov cadmium`",
                    "In a division thread, tournament and division are inferred automatically."),
                'matches':        ("!matches / !m",       "Show matches for a player in a given week.",
                    f"`{bot_mention} !matches <player> [week]`",
                    f"`{bot_mention} !matches Scorium 4`",
                    "Omitting week uses the default."),
                'division':       ("!division / !d",      "Show all matchups for a division.",
                    f"`{bot_mention} !division [division] [week]`",
                    f"`{bot_mention} !d 4`  (inside a division thread)\n`{bot_mention} !d cadmium 2`",
                    "Inside a division thread, the division name is inferred from the thread."),
                'tournaments':    ("!tournaments",        "List all tournaments with their aliases.",
                    f"`{bot_mention} !tournaments`", "", ""),
                'sendto':         ("!sendto",             "Send a player their matches by DM.",
                    f"`{bot_mention} !sendto <player> [week]`",
                    f"`{bot_mention} !sendto Scorium 4`",
                    "Requires admin. Player must be in mapping sheet."),
                'notify_all':     ("!notify_all",         "DM all players with pending matches.",
                    f"`{bot_mention} !notify_all [week]`",
                    f"`{bot_mention} !notify_all 4`",
                    "Requires admin."),
                'post_divisions': ("!post_divisions",     "Post division matchups to threads.",
                    f"`{bot_mention} !post_divisions [week] [alias]`",
                    f"`{bot_mention} !post_divisions 4 MA`",
                    "Requires admin. Tournament defaults to the channel's bound tournament."),
                'refresh':        ("!refresh",            "Reload all cached Google Sheets data.",
                    f"`{bot_mention} !refresh`", "", "Requires admin."),
                'context_debug':  ("!context_debug",      "Show resolved channel/tournament context.",
                    f"`{bot_mention} !context_debug`", "", "Requires admin."),
            }
            if cmd in help_texts:
                title, desc, usage, example, note = help_texts[cmd]
                embed = discord.Embed(title=title, description=desc, color=discord.Color.green())
                embed.add_field(name="Usage", value=usage, inline=False)
                if example:
                    embed.add_field(name="Example", value=example, inline=False)
                if note:
                    embed.add_field(name="Note", value=note, inline=False)
                await ctx.send(embed=embed)
            else:
                await ctx.send(embed=discord.Embed(
                    title="Unknown Command",
                    description=f"`{command_name}` is not recognised. Use `{bot_mention} !help` to see all commands.",
                    color=discord.Color.red()
                ))


async def setup(bot, tournaments, mapping_sheet_url, default_week):
    cog = TournamentCommands(bot, tournaments, mapping_sheet_url, default_week)
    await bot.add_cog(cog)
    return cog
