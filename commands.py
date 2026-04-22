import io
import discord
from discord.ext import commands
import logging

from match_utils import (
    get_tournament_sheets,
    refresh_tournament_cache,
    get_division_matches,
    get_player_matches,
    player_matches,
    load_hero_builds_from_sheets,
    load_player_mapping,
    send_dm_to_player,
    build_matches_message,
    split_message,
    normalize_name,
    get_division_standings,
    format_table_messages,
)
from tournament_actions import (
    find_tournament,
    run_post_divisions,
    run_notify_all,
    get_threads_for_channel,
    send_division_image,
)
from channel_context import resolve_context, parse_division_args
from image_render import render_standings, render_player_matches

logger = logging.getLogger(__name__)


# ── Admin check ────────────────────────────────────────────────────────────────

def is_bot_admin():
    async def predicate(ctx):
        if ctx.author.id in getattr(ctx.bot, 'admin_user_ids', []):
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

    # ── Context helpers ────────────────────────────────────────────────────────

    def _ctx(self, ctx) -> dict:
        return resolve_context(
            ctx_channel=ctx.channel,
            guild=ctx.guild,
            channel_index=getattr(self.bot, 'channel_index', {}),
            tournaments=self.tournaments,
            find_tournament_fn=find_tournament,
        )

    def _tourneys_for_ctx(self, context: dict) -> list[dict]:
        if context['tournament']:
            return [context['tournament']]
        return self.tournaments

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
        for t in self._tourneys_for_ctx(context):
            embed.add_field(name=t['name'], value=f"Alias: `{t.get('alias', 'No alias')}`", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name='matches', aliases=['m'])
    async def matches_command(self, ctx, player: str, week: int = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        week = week or self.default_week
        is_dm = ctx.guild is None
        tourneys = self._tourneys_for_ctx(context)

        status = await ctx.send(f"🔍 Searching for **{player}** in week **{week}**...")

        # Collect results across all relevant tournaments
        tourney_results = []
        errors = []
        for tourney in tourneys:
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                current, pending = get_player_matches(sheets, player, week)
                if not current and not pending:
                    continue

                def fmt(name, b=builds):
                    return (name, b.get(normalize_name(name), '?'))

                def pick(m):
                    """Return (player_hero, opponent) in correct order."""
                    if player_matches(player, m['player1']):
                        return m['player1'], m['player2']
                    return m['player2'], m['player1']

                cur_rows = []
                for m in current:
                    ph, opp = pick(m)
                    cur_rows.append((m['division'], *fmt(ph), *fmt(opp)))

                pend_rows = []
                for m in pending:
                    ph, opp = pick(m)
                    pend_rows.append((m['week'], m['division'], *fmt(ph), *fmt(opp)))

                tourney_results.append({
                    'tourney_name': tourney['name'],
                    'current': cur_rows,
                    'pending': pend_rows,
                })
            except Exception as e:
                logger.error(f"matches_command ({tourney['name']}): {e}", exc_info=True)
                errors.append(f"❌ Error in {tourney['name']}: {e}")

        await status.delete()

        for err in errors:
            await ctx.send(err)

        if not tourney_results:
            if not errors:
                await ctx.send(f"📅 No matches found for **{player}** in week **{week}**.")
            return

        if is_dm:
            # DM: plain text, built directly from collected results
            for t in tourney_results:
                lines = [f"**🏆 {t['tourney_name']}**"]
                if t['current']:
                    lines.append(f"**Week {week} matches:**")
                    for r in t['current']:
                        lines.append(f"**{r[0]}** · {r[1]} ({r[2]}) vs {r[3]} ({r[4]})")
                if t['pending']:
                    lines.append("\n**⏳ Pending matches:**")
                    for r in t['pending']:
                        lines.append(f"Wk {r[0]} · **{r[1]}** · {r[2]} ({r[3]}) vs {r[4]} ({r[5]})")
                for chunk in split_message("\n".join(lines)):
                    await ctx.send(chunk)
        else:
            # Channel/thread: image
            try:
                img_bytes = render_player_matches(player, week, tourney_results)
                filename = f"matches_{player.lower().replace(' ', '_')}_w{week}.png"
                await ctx.send(file=discord.File(io.BytesIO(img_bytes), filename=filename))
            except Exception as e:
                logger.error(f"matches_command image render failed: {e}", exc_info=True)
                # Fallback to plain text
                for t in tourney_results:
                    lines = [f"**🏆 {t['tourney_name']}**"]
                    for r in t['current']:
                        lines.append(f"**{r[0]}** · {r[1]} ({r[2]}) vs {r[3]} ({r[4]})")
                    for r in t['pending']:
                        lines.append(f"Wk {r[0]} · **{r[1]}** · {r[2]} ({r[3]}) vs {r[4]} ({r[5]}) *(pending)*")
                    for chunk in split_message("\n".join(lines)):
                        await ctx.send(chunk)

    @commands.command(name='division', aliases=['d'])
    async def division_command(self, ctx, *args):
        context = self._ctx(ctx)
        if not context['allowed']:
            return

        division_name, week = parse_division_args(args, context['division'])
        week = week or self.default_week

        if not division_name:
            await ctx.send(
                "❌ No division specified. "
                "Run this command inside a division thread, or use `!d <division> [week]`."
            )
            return

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
                await send_division_image(
                    ctx.channel, tourney['name'], division_name, week, current, pending, builds
                )
            except Exception as e:
                logger.error(f"division_command ({tourney['name']}): {e}", exc_info=True)
                await ctx.send(f"❌ Error in {tourney['name']}: {e}")

        if not found_any:
            await ctx.send(
                f"⚠️ No matches found for division **{division_name}** in week **{week}**."
            )

    @commands.command(name='standings', aliases=['c'])
    async def standings_command(self, ctx, *args):
        context = self._ctx(ctx)
        if not context['allowed']:
            return

        # Resolve tournament and division from args + context
        # Forms: !standings | !standings cadmium | !standings eov | !standings eov cadmium
        tourney  = context['tournament']
        division = context['division']

        if args:
            maybe_tourney = find_tournament(self.tournaments, args[0])
            if maybe_tourney:
                tourney = maybe_tourney
                division = args[1] if len(args) > 1 else division
            else:
                division = args[0]

        if tourney is None:
            await ctx.send(
                "❌ No tournament resolved. "
                "Specify one: `!standings <tournament> [division]` — e.g. `!standings eov cadmium`"
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
            standings_data, _ = get_division_standings(sheets, division)

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

            # Enrich with build from the builds sheet (same source as !m and !d)
            builds = load_hero_builds_from_sheets(
                sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
            )
            rows_with_build = [
                row + [builds.get(normalize_name(row[1]), '')]
                for row in standings_data
            ]

            img_bytes = render_standings(
                title=f"{tourney['name']} · {division}",
                rows=rows_with_build,
            )
            img_file = discord.File(
                io.BytesIO(img_bytes),
                filename=f"standings_{division.lower()}.png"
            )

            # Route to correct thread if possible
            target = None
            if context['division'] and context['division'].lower() == division.lower():
                target = ctx.channel
            else:
                search_ch = getattr(ctx.channel, 'parent', ctx.channel)
                if isinstance(search_ch, discord.TextChannel):
                    target = get_threads_for_channel(search_ch).get(division.lower())
                if not target and ctx.guild:
                    ch_index = getattr(self.bot, 'channel_index', {})
                    gid = ctx.guild.id
                    allowed_ids = {k for k in ch_index.get(gid, {}) if isinstance(k, int)}
                    for ch in ctx.guild.text_channels:
                        if allowed_ids and ch.id not in allowed_ids:
                            continue
                        found = get_threads_for_channel(ch).get(division.lower())
                        if found:
                            target = found
                            break

            if target:
                await target.send(
                    file=discord.File(io.BytesIO(img_bytes), filename=f"standings_{division.lower()}.png")
                )
                if ctx.channel.id != target.id:
                    await msg_loading.edit(content=f"✅ Standings published in {target.mention}")
                else:
                    await msg_loading.delete()
            else:
                await msg_loading.delete()
                await ctx.send(file=img_file)

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
        success_count = 0
        for tourney in self._tourneys_for_ctx(context):
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                messages, err = build_matches_message(
                    tourney, player, week, force_refresh=False, builds=builds
                )
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
                mention = (await self.bot.fetch_user(discord_id)).mention
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
        if tournament_alias is None:
            tournament_alias = context['tournament']['alias'] if context['tournament'] else "MA"
        tourney = find_tournament(self.tournaments, tournament_alias)
        if not tourney:
            await ctx.send(f"❌ Tournament `{tournament_alias}` not found.")
            return
        await ctx.send(
            f"🚀 Posting **{tourney['name']}** week {week or self.default_week} matchups to threads..."
        )
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

    # ── Debug / admin commands ─────────────────────────────────────────────────

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
                    f"Use `{bot_mention} !<command>` or just `!<command>` if the bot has prefix access.\n"
                    f"Use `{bot_mention} !help <command>` for details on any command."
                ),
                color=discord.Color.blue()
            )
            embed.add_field(
                name="🌐 Public",
                value=(
                    "`!matches` / `!m <player> [week]` — Player's matches as image (text in DMs).\n"
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
            await ctx.send(embed=embed)
        else:
            cmd = command_name.lstrip('!').lower()
            # Alias normalisation
            cmd = {'m': 'matches', 'c': 'standings', 'd': 'division'}.get(cmd, cmd)
            help_texts = {
                'matches': (
                    "!matches / !m",
                    "Show a player's matches for a given week.",
                    f"`{bot_mention} !m <player> [week]`",
                    f"`{bot_mention} !m Scorium 4`",
                    "Posts an image in channels/threads. Sends plain text in DMs. Omitting week uses the server default."
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
                await ctx.send(embed=embed)
            else:
                await ctx.send(embed=discord.Embed(
                    title="Unknown Command",
                    description=(
                        f"`{command_name}` is not recognised. "
                        f"Use `{bot_mention} !help` to see all commands."
                    ),
                    color=discord.Color.red()
                ))


async def setup(bot, tournaments, mapping_sheet_url, default_week):
    cog = TournamentCommands(bot, tournaments, mapping_sheet_url, default_week)
    await bot.add_cog(cog)
    return cog
