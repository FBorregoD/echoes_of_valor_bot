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
    get_latest_week,
    get_latest_week_from_sheets,  
)
from tournament_actions import (
    find_tournament,
    run_post_divisions,
    run_notify_all,
    get_threads_for_channel,
    send_division_image,
)
from channel_context import resolve_context, parse_division_args
from image_render import render_standings, render_player_matches, set_scale
from help import get_help_embed

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


# ── Helper global (no necesita self) ──────────────────────────────────────────

def _split_pending(pending: list) -> tuple[list, list]:
    normal = []
    misreported = []
    for m in pending:
        if m.get("misreported"):
            misreported.append(m)
        else:
            normal.append(m)
    return normal, misreported


# ── Cog ────────────────────────────────────────────────────────────────────────

class TournamentCommands(commands.Cog):
    def __init__(self, bot, tournaments, mapping_sheet_url, default_week, image_scale=1.0):
        self.bot = bot
        self.tournaments = tournaments
        self.mapping_sheet_url = mapping_sheet_url
        self.default_week = default_week
        self.image_scale = image_scale
        set_scale(image_scale)

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

    async def _get_most_recent_week(self, context: dict) -> int:
        """Mantener para comandos que no sean !matches (global)."""
        tourneys = self._tourneys_for_ctx(context)
        guild = context['channel'].guild if context['channel'] else None
        guild_id = guild.id if guild else 0
        latest = get_latest_week(tourneys, guild_id)
        return latest if latest > 0 else self.default_week

    async def _get_weeks_per_tournament(self, context: dict, force_week: int = None) -> dict[str, int]:
        """Usar solo para !matches."""
        tourneys = self._tourneys_for_ctx(context)
        weeks = {}

        for tourney in tourneys:
            if force_week is not None:
                weeks[tourney['alias']] = force_week
                continue

            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                latest = get_latest_week_from_sheets(sheets)
                if latest > 0:
                    weeks[tourney['alias']] = latest
                else:
                    weeks[tourney['alias']] = self.default_week
            except Exception:
                weeks[tourney['alias']] = self.default_week

        return weeks

    async def _send_matches_text(self, ctx, player: str, tourney_results: list):
        """Send match results as plain text (used in DMs and fallback)."""
        for t in tourney_results:
            lines = [f"**🏆 {t['tourney_name']}**"]
            if t.get('season_complete', False):
                lines.append("🏁 Season complete — no new matches.")
            else:
                week = t['week']
                if t['current']:
                    lines.append(f"**Week {week} matches:**")
                    for r in t['current']:
                        status = " ✓" if r[5] else ""
                        lines.append(f"**{r[0]}** · {r[1]} ({r[2]}) vs {r[3]} ({r[4]}){status}")
                if t['pending']:
                    lines.append("\n**⏳ Pending matches:**")
                    for r in t['pending']:
                        lines.append(f"Wk {r[0]} · **{r[1]}** · {r[2]} ({r[3]}) vs {r[4]} ({r[5]})")
                if t['misreported']:
                    lines.append("\n**⚠️ Misreported matches:**")
                    for r in t['misreported']:
                        lines.append(f"Wk {r[0]} · **{r[1]}** · {r[2]} ({r[3]}) vs {r[4]} ({r[5]})")
                if not t['current'] and not t['pending'] and not t['misreported']:
                    lines.append(f"📅 No matches found for week {week}.")
            for chunk in split_message("\n".join(lines)):
                await ctx.send(chunk)

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
    async def matches_command(self, ctx, *args):
        context = self._ctx(ctx)
        if not context['allowed']:
            return

        if not args:
            await ctx.send("❌ Please specify a player name. Example: `!m Scorium 9`")
            return

        player = args[0]
        force_week = None
        force_text = False
        rest = args[1:]

        for token in rest:
            if token.lower() == "text":
                force_text = True
            else:
                try:
                    force_week = int(token)
                except ValueError:
                    pass

        # Get weeks per tournament
        weeks_per_tournament = await self._get_weeks_per_tournament(context, force_week)

        is_dm = ctx.guild is None
        tourneys = self._tourneys_for_ctx(context)

        await ctx.send(f"🔍 Searching for **{player}**...")

        tourney_results = []
        errors = []

        for tourney in tourneys:
            week = weeks_per_tournament.get(tourney['alias'], self.default_week)
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                current, pending = get_player_matches(sheets, player, week)

                # If no matches and week is beyond latest, check if season complete
                if not current and not pending:
                    latest = get_latest_week_from_sheets(sheets)
                    if latest > 0 and week > latest:
                        # Season complete for this tournament
                        tourney_results.append({
                            'tourney_name': tourney['name'],
                            'season_complete': True,
                            'week': week,
                            'current': [],
                            'pending': [],
                            'misreported': [],
                        })
                        continue
                    elif latest <= 0:
                        # No data at all, use default week but no matches
                        pass

                # Build rows as before
                def fmt(name, b=builds):
                    return (name, b.get(normalize_name(name), '?'))

                def pick(m):
                    if player_matches(player, m['player1']):
                        return m['player1'], m['player2']
                    return m['player2'], m['player1']

                cur_rows = []
                for m in current:
                    ph, opp = pick(m)
                    finished = m.get('check', '') == 'OK'
                    cur_rows.append((m['division'], *fmt(ph), *fmt(opp), finished))

                normal_pend, misreported = _split_pending(pending)  # <-- SIN self.

                pend_rows = []
                for m in normal_pend:
                    ph, opp = pick(m)
                    pend_rows.append((m['week'], m['division'], *fmt(ph), *fmt(opp)))

                mis_rows = []
                for m in misreported:
                    ph, opp = pick(m)
                    mis_rows.append((m['week'], m['division'], *fmt(ph), *fmt(opp)))

                if cur_rows or pend_rows or mis_rows:
                    tourney_results.append({
                        'tourney_name': tourney['name'],
                        'season_complete': False,
                        'week': week,
                        'current': cur_rows,
                        'pending': pend_rows,
                        'misreported': mis_rows,
                    })
            except Exception as e:
                logger.error(f"matches_command ({tourney['name']}): {e}", exc_info=True)
                errors.append(f"❌ Error in {tourney['name']}: could not load match data.")

        # Send results
        if errors:
            await ctx.send("\n".join(errors))

        if not tourney_results and not errors:
            # No matches found in any tournament
            if force_week is not None:
                await ctx.send(f"📅 No matches found for **{player}** in week **{force_week}**.")
            else:
                await ctx.send(f"📅 No matches found for **{player}** in any tournament.")
            return

        
        if force_text:
            await self._send_matches_text(ctx, player, tourney_results)
        else:
            # Siempre intentar imagen, incluso en DMs
            try:
                img_bytes = render_player_matches(player, tourney_results)
                filename = f"matches_{player.lower().replace(' ', '_')}.png"
                await ctx.send(file=discord.File(io.BytesIO(img_bytes), filename=filename))
            except Exception as e:
                logger.exception(f"matches_command image render failed: {e}")
                await self._send_matches_text(ctx, player, tourney_results)

    @commands.command(name='division', aliases=['d'])
    async def division_command(self, ctx, *args):
        context = self._ctx(ctx)
        if not context['allowed']:
            return

        division_name, week = parse_division_args(args, context['division'])
        if week is None:
            week = await self._get_most_recent_week(context)  # <-- restaurado

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

        is_dm = ctx.guild is None
        force_text = False
        rest = list(args)

        # ── Parse "text" flag (only in DMs) ──
        if is_dm and rest and rest[-1].lower() == "text":
            force_text = True
            rest.pop()

        # ── Parse tournament and division ──
        tourney_alias = None
        division_name = None
        if len(rest) >= 2:
            tourney_alias = rest[0]
            division_name = rest[1]
        elif len(rest) == 1:
            # Could be tournament or division
            if find_tournament(self.tournaments, rest[0]):
                tourney_alias = rest[0]
            else:
                division_name = rest[0]

        # Determine which tournaments to process
        if tourney_alias:
            tourney = find_tournament(self.tournaments, tourney_alias)
            if not tourney:
                await ctx.send(f"❌ Tournament `{tourney_alias}` not found.")
                return
            tourneys = [tourney]
        else:
            tourneys = self._tourneys_for_ctx(context)

        # Division name: if not given, try to infer from thread context
        if not division_name:
            division_name = context.get('division')
            if not division_name:
                await ctx.send(
                    "❌ No division specified. "
                    "Use `!c [tournament] <division>` or run inside a division thread."
                )
                return

        sent_any = False
        for tourney in tourneys:
            try:
                sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
                rows, headers = get_division_standings(sheets, division_name)

                if not rows:
                    await ctx.send(f"⚠️ No standings found for **{division_name}** in {tourney['name']}.")
                    continue

                # Load builds for enriching the text view (optional, but nice)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                rows_with_build = [
                    row + [builds.get(normalize_name(row[1]), '')]
                    for row in rows
                ]
                text_headers = headers + ['Build'] if rows_with_build else headers

                if force_text:
                    # Direct text (only in DMs)
                    chunks = format_table_messages(rows_with_build, text_headers, f"🏆 {tourney['name']} · {division_name}")
                    for chunk in chunks:
                        await ctx.send(chunk)
                    sent_any = True
                else:
                    # Try image
                    try:
                        img_bytes = render_standings(
                            title=f"{tourney['name']} · {division_name}",
                            rows=rows_with_build,
                        )
                        await ctx.send(
                            file=discord.File(io.BytesIO(img_bytes), filename=f"standings_{division_name.lower()}.png")
                        )
                        sent_any = True
                    except Exception as e:
                        logger.exception(f"standings_command image render failed: {e}")
                        # Fallback to text
                        chunks = format_table_messages(rows_with_build, text_headers, f"🏆 {tourney['name']} · {division_name}")
                        for chunk in chunks:
                            await ctx.send(chunk)
                        sent_any = True
            except Exception as e:
                logger.error(f"standings_command ({tourney['name']}): {e}", exc_info=True)
                await ctx.send(f"❌ Error in {tourney['name']}: {e}")

        if not sent_any:
            await ctx.send(f"⚠️ No standings could be displayed for **{division_name}**.")

    # ── Admin commands ─────────────────────────────────────────────────────────

    @is_bot_admin()
    @commands.command(name='sendto')
    async def sendto_command(self, ctx, player: str, week: int = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        if week is None:
            week = await self._get_most_recent_week(context)  # <-- restaurado
        # ... resto igual ...

    @is_bot_admin()
    @commands.command(name='notify_all')
    async def notify_all_command(self, ctx, week: int = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        if week is None:
            week = await self._get_most_recent_week(context)  # <-- restaurado
        # ... resto igual ...

    @is_bot_admin()
    @commands.command(name='post_divisions')
    async def post_divisions_command(self, ctx, week: int = None, tournament_alias: str = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        if week is None:
            week = await self._get_most_recent_week(context)  # <-- restaurado
        # ... resto igual ...

    # ... resto de comandos (post_standings, refresh, debug, help) sin cambios ...


async def setup(bot, tournaments, mapping_sheet_url, default_week):
    image_scale = bot.config.get('image_scale', 1.0) if hasattr(bot, 'config') else 1.0
    cog = TournamentCommands(bot, tournaments, mapping_sheet_url, default_week, image_scale)
    await bot.add_cog(cog)
    return cog