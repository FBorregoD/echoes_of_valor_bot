import discord
from discord.ext import commands
import asyncio
import logging
from match_utils import (
    get_tournament_sheets,
    get_division_matches,
    refresh_tournament_cache,
    get_player_matches,
    format_table,
    build_matches_message,
    load_player_mapping,
    send_dm_to_player,
    split_message,
    load_hero_builds_from_sheets,
    normalize_name
)

logger = logging.getLogger(__name__)


class TournamentCommands(commands.Cog):
    def __init__(self, bot, tournaments, mapping_sheet_url, default_week):
        self.bot = bot
        self.tournaments = tournaments
        self.mapping_sheet_url = mapping_sheet_url
        self.default_week = default_week

    def find_tournament(self, name_or_alias: str):
        name_or_alias = name_or_alias.lower()
        for t in self.tournaments:
            if t['name'].lower() == name_or_alias:
                return t
            if t.get('alias', '').lower() == name_or_alias:
                return t
        return None

    async def _send_chunks(self, ctx, content: str):
        for chunk in split_message(content):
            await ctx.send(chunk)

    # ------------------------------------------------------------------
    # Public commands
    # ------------------------------------------------------------------

    @commands.command(name='ping')
    async def ping(self, ctx):
        await ctx.send("Pong!")

    @commands.command(name='tournaments')
    async def list_tournaments(self, ctx):
        embed = discord.Embed(title="🏆 Available Tournaments", color=discord.Color.gold())
        for t in self.tournaments:
            alias = t.get('alias', 'No alias')
            embed.add_field(name=t['name'], value=f"Alias: `{alias}`", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name='matches', aliases=['m'])
    async def matches_command(self, ctx, player: str, week: int = None):
        if week is None:
            week = self.default_week
        await ctx.send(f"🔍 Searching for **{player}** in week **{week}**...")
        for tourney in self.tournaments:
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets,
                    tourney.get('builds_sheet'),
                    tourney.get('builds_mapping')
                )
                messages, err = build_matches_message(tourney, player, week, force_refresh=False, builds=builds)
                if err:
                    await ctx.send(f"⚠️ Error in {tourney['name']}: {err}")
                elif messages:
                    for msg in messages:
                        for chunk in split_message(msg):
                            await ctx.send(chunk)
            except Exception as e:
                logger.error(f"Unexpected error in matches_command ({tourney['name']}): {e}", exc_info=True)
                await ctx.send(f"❌ Unexpected error in {tourney['name']}: {str(e)}")

    @commands.command(name='division', aliases=['d'])
    async def division_command(self, ctx, division_name: str, week: int = None):
        if week is None:
            week = self.default_week
        await ctx.send(f"🔍 Looking for division **{division_name}** in week **{week}**...")
        for tourney in self.tournaments:
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets,
                    tourney.get('builds_sheet'),
                    tourney.get('builds_mapping')
                )
                current, pending = get_division_matches(sheets, division_name, week)
                if not current and not pending:
                    continue
                msg = f"**🏆 {tourney['name']} - Division {division_name}**\n"
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
                    msg += f"\n**⏳ Pending matches from previous weeks:**\n```\n{format_table(rows, ['Week', 'Player 1', 'Player 2'], 'Pending')}\n```"
                for chunk in split_message(msg):
                    await ctx.send(chunk)
            except Exception as e:
                logger.error(f"Error in division_command ({tourney['name']}): {e}", exc_info=True)
                await ctx.send(f"❌ Error in {tourney['name']}: {str(e)}")

    # ------------------------------------------------------------------
    # Commands restricted to admins
    # ------------------------------------------------------------------

    @commands.has_permissions(administrator=True)
    @commands.command(name='sendto')
    async def sendto_command(self, ctx, player: str, week: int = None):
        if week is None:
            week = self.default_week
        await ctx.send(f"📬 Fetching matches for **{player}** (week {week})...")
        mapping = load_player_mapping(self.mapping_sheet_url)
        if player not in mapping:
            await ctx.send(f"❌ No Discord ID found for player **{player}**.")
            return
        discord_id = mapping[player]
        try:
            user = await self.bot.fetch_user(discord_id)
            mention = user.mention
        except Exception:
            mention = player
        success_count = 0
        for tourney in self.tournaments:
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets,
                    tourney.get('builds_sheet'),
                    tourney.get('builds_mapping')
                )
                messages, err = build_matches_message(tourney, player, week, force_refresh=False, builds=builds)
                if err:
                    await ctx.send(f"⚠️ Error in {tourney['name']}: {err}")
                elif messages:
                    for msg in messages:
                        for chunk in split_message(msg):
                            success = await send_dm_to_player(self.bot, discord_id, chunk)
                            if not success:
                                await ctx.send(f"⚠️ Could not DM {player} for {tourney['name']}.")
                                break
                        else:
                            success_count += 1
            except Exception as e:
                logger.error(f"Error in sendto_command ({tourney['name']}): {e}", exc_info=True)
                await ctx.send(f"❌ Unexpected error in {tourney['name']}: {str(e)}")
        if success_count > 0:
            await ctx.send(f"✅ DM(s) sent to **{player}** ({success_count} tournament(s)).")
        else:
            await ctx.send(
                f"❌ {mention}, I couldn't send you any DM.\n"
                f"👉 Please enable DMs from server members or check your privacy settings."
            )

    @commands.has_permissions(administrator=True)
    @commands.command(name='notify_all')
    async def notify_all_command(self, ctx, week: int = None):
        if week is None:
            week = self.default_week
        await ctx.send("🚀 Gathering players with pending matches...")
        mapping = load_player_mapping(self.mapping_sheet_url)
        if not mapping:
            await ctx.send("❌ No player mapping loaded. Cannot proceed.")
            return

        # Load all sheets once per tournament (avoid repeated fetches per player)
        tourney_data = {}
        for tourney in self.tournaments:
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets,
                    tourney.get('builds_sheet'),
                    tourney.get('builds_mapping')
                )
                tourney_data[tourney['name']] = {'tourney': tourney, 'sheets': sheets, 'builds': builds}
            except Exception as e:
                await ctx.send(f"⚠️ Error loading data for {tourney['name']}: {str(e)}")

        # Determine which players have pending matches
        players_with_pending = set()
        for player in mapping.keys():
            for td in tourney_data.values():
                try:
                    _, pending = get_player_matches(td['sheets'], player, week)
                    if pending:
                        players_with_pending.add(player)
                        break
                except Exception as e:
                    await ctx.send(f"⚠️ Error checking player {player}: {str(e)}")

        if not players_with_pending:
            await ctx.send("✅ No players have pending matches.")
            return

        await ctx.send(f"📬 Sending DMs to {len(players_with_pending)} players...")
        success_count = 0
        for player in players_with_pending:
            discord_id = mapping[player]
            pending_details = []
            for td in tourney_data.values():
                try:
                    _, pending = get_player_matches(td['sheets'], player, week)
                    if pending:
                        rows = []
                        builds = td['builds']
                        for m in pending:
                            if player in m['player1']:
                                player_hero, opponent = m['player1'], m['player2']
                            else:
                                player_hero, opponent = m['player2'], m['player1']
                            p_norm = normalize_name(player_hero)
                            o_norm = normalize_name(opponent)
                            player_disp = f"{player_hero} ({builds.get(p_norm, '?')})"
                            opponent_disp = f"{opponent} ({builds.get(o_norm, '?')})"
                            rows.append([m['week'], m['division'], player_disp, opponent_disp])
                        pending_details.append(
                            f"**{td['tourney']['name']}**\n```\n"
                            f"{format_table(rows, ['Week', 'Division', 'Your Hero', 'Opponent'], 'Pending matches')}\n```"
                        )
                except Exception as e:
                    pending_details.append(f"⚠️ Error in {td['tourney']['name']}: {str(e)}")
            if pending_details:
                message = f"⏳ **{player}**, you have pending matches from previous weeks:\n\n" + "\n".join(pending_details)
                for chunk in split_message(message):
                    success = await send_dm_to_player(self.bot, discord_id, chunk)
                    if not success:
                        await ctx.send(f"⚠️ Failed to send DM to {player}.")
                        break
                else:
                    success_count += 1
            await asyncio.sleep(1)
        await ctx.send(f"✅ DMs sent to {success_count} out of {len(players_with_pending)} players.")

    @commands.has_permissions(administrator=True)
    @commands.command(name='post_divisions')
    async def post_divisions(self, ctx, week: int = None, tournament_name_or_alias: str = "MA"):
        if week is None:
            week = self.default_week
        tourney = self.find_tournament(tournament_name_or_alias)
        if not tourney:
            await ctx.send(f"❌ Tournament '{tournament_name_or_alias}' not found.")
            return
        await ctx.send(f"🚀 Posting division matchups for **{tourney['name']}** (week {week}) to threads...")
        try:
            sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
            builds = load_hero_builds_from_sheets(
                sheets,
                tourney.get('builds_sheet'),
                tourney.get('builds_mapping')
            )
        except Exception as e:
            await ctx.send(f"❌ Error loading data: {e}")
            return

        excluded_keywords = ['formulierreacties', 'hero builds', 'leagues overview', 'format', 'scoresheet', 'arma heroum']
        division_sheets = [
            name for name in sheets.keys()
            if not any(kw in name.lower() for kw in excluded_keywords)
        ]
        if not division_sheets:
            await ctx.send("⚠️ No division sheets found. Check excluded keywords or sheet names.")
            return

        guild = ctx.guild
        thread_dict = {}
        for channel in guild.text_channels:
            for thread in channel.threads:
                thread_dict[thread.name.lower()] = thread

        success_count = 0
        not_found = []
        error_list = []
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
                    msg += f"\n**⏳ Pending matches from previous weeks:**\n```\n{format_table(rows, ['Week', 'Player 1', 'Player 2'], 'Pending')}\n```"
                for chunk in split_message(msg):
                    await thread.send(chunk)
                success_count += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error posting to thread '{div_name}': {e}", exc_info=True)
                error_list.append(f"{div_name}: {str(e)}")

        result_msg = f"✅ Posted to {success_count} divisions.\n"
        if not_found:
            result_msg += f"⚠️ Threads not found: {', '.join(not_found)}\n"
        if error_list:
            result_msg += f"❌ Errors: {', '.join(error_list)}\n"
        await ctx.send(result_msg)

    @commands.has_permissions(administrator=True)
    @commands.command(name='refresh')
    async def refresh_cache(self, ctx):
        await ctx.send("🔄 Refreshing cache... This may take a moment.")
        for tourney in self.tournaments:
            try:
                refresh_tournament_cache(tourney['url'])
                await ctx.send(f"✅ Refreshed {tourney['name']}")
            except Exception as e:
                await ctx.send(f"❌ Error refreshing {tourney['name']}: {str(e)}")
        try:
            load_player_mapping(self.mapping_sheet_url, force_refresh=True)
            await ctx.send("✅ Refreshed player mapping sheet")
        except Exception as e:
            await ctx.send(f"❌ Error refreshing mapping: {str(e)}")
        await ctx.send("🎉 Cache refresh complete!")

    # ------------------------------------------------------------------
    # Debug commands (admin only)
    # ------------------------------------------------------------------

    @commands.has_permissions(administrator=True)
    @commands.command(name='test_map')
    async def test_map(self, ctx):
        mapping = load_player_mapping(self.mapping_sheet_url)
        if not mapping:
            await ctx.send("❌ Mapping is empty.")
        else:
            await self._send_chunks(ctx, f"📋 Mapping loaded: {mapping}")

    @commands.has_permissions(administrator=True)
    @commands.command(name='test_id')
    async def test_id(self, ctx, player: str):
        mapping = load_player_mapping(self.mapping_sheet_url)
        if player in mapping:
            await ctx.send(f"🆔 ID for {player}: {mapping[player]}")
        else:
            await ctx.send(f"❌ Player {player} not found in mapping.")

    @commands.has_permissions(administrator=True)
    @commands.command(name='dmtest')
    async def dmtest(self, ctx, user_id: int, *, message: str):
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(message)
            await ctx.send("✅ DM sent.")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    @commands.command(name='help')
    async def help_command(self, ctx, command_name: str = None):
        if command_name is None:
            embed = discord.Embed(
                title="📖 Bot Commands",
                description="Use `!help <command>` for more details.",
                color=discord.Color.blue()
            )
            embed.add_field(name="!matches / !m", value="Show matches for a player in a given week.", inline=False)
            embed.add_field(name="!division / !d", value="Show all matchups for a division in a given week.", inline=False)
            embed.add_field(name="!tournaments", value="List all tournaments with their aliases.", inline=False)
            embed.add_field(name="── Admin only ──", value="​", inline=False)
            embed.add_field(name="!sendto", value="Send a DM to a player with their matches.", inline=False)
            embed.add_field(name="!notify_all", value="Send DMs to all players with pending matches.", inline=False)
            embed.add_field(name="!post_divisions", value="Post division matchups to threads.", inline=False)
            embed.add_field(name="!refresh", value="Refresh all cached data from Google Sheets.", inline=False)
            embed.add_field(name="!test_map / !test_id / !dmtest", value="Debug commands.", inline=False)
            embed.set_footer(text="Example: !matches Scorium 4")
            await ctx.send(embed=embed)
        else:
            cmd = command_name.lower()
            help_texts = {
                'matches':       ("!matches / !m",      "Show matches for a player in a given week.",                           "`!matches <player> [week]`",          "`!matches Scorium 4`",               "If week is omitted, uses the default week."),
                'sendto':        ("!sendto",             "Send a private DM to a player with their matches.",                   "`!sendto <player> [week]`",           "`!sendto Scorium 4`",                "Requires admin. Player must be in mapping sheet."),
                'notify_all':    ("!notify_all",         "Send DMs to every player who has pending matches.",                   "`!notify_all [week]`",                "`!notify_all 4`",                    "Requires admin. May be rate-limited."),
                'division':      ("!division / !d",      "Show all matchups for a division.",                                   "`!division <division> [week]`",       "`!division Bronze 4`",               ""),
                'post_divisions':("!post_divisions",     "Post division matchups to threads named after each division.",        "`!post_divisions [week] [alias]`",    "`!post_divisions 4 MA`",             "Requires admin. Tournament alias defaults to 'MA'."),
                'tournaments':   ("!tournaments",        "List all available tournaments with their aliases.",                  "`!tournaments`",                      "",                                   ""),
                'refresh':       ("!refresh",            "Clear and reload all cached data from Google Sheets.",                "`!refresh`",                          "",                                   "Requires admin."),
                'test_map':      ("!test_map",           "Debug: Show the loaded player mapping.",                              "`!test_map`",                         "",                                   "Requires admin."),
                'test_id':       ("!test_id",            "Debug: Show Discord ID for a specific player.",                      "`!test_id <player>`",                 "`!test_id Scorium`",                 "Requires admin."),
                'dmtest':        ("!dmtest",             "Debug: Send a test DM to a Discord user ID.",                        "`!dmtest <user_id> <message>`",       "`!dmtest 254177975417700352 Hello`", "Requires admin."),
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
                embed = discord.Embed(
                    title="Unknown Command",
                    description=f"`{command_name}` is not a valid command. Use `!help` to see all commands.",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)


async def setup(bot, tournaments, mapping_sheet_url, default_week):
    cog = TournamentCommands(bot, tournaments, mapping_sheet_url, default_week)
    await bot.add_cog(cog)
    return cog  # returned so SchedulerCog can hold a reference to it
