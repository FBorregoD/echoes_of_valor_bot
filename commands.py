import asyncio
import io
import discord
from discord.ext import commands
import logging

from match_utils import (
    get_tournament_sheets_async,
    refresh_tournament_cache_async,
    get_division_matches,
    get_player_matches,
    player_matches,
    load_hero_builds_from_sheets,
    load_player_mapping_async,
    send_dm_to_player,
    build_matches_message_async,
    split_message,
    normalize_name,
    get_division_standings,
    format_table_messages,
    get_latest_week,
    get_latest_week_from_sheets,
    get_player_characters,
    find_player_division_rank,
    get_player_division_record,
)
from tournament_actions import (
    find_tournament,
    run_post_divisions,
    run_notify_all,
    get_threads_for_channel,
    send_division_image,
    run_post_standings
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


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th', 11-13 -> '11th'/'12th'/'13th', ..."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def require_dm():
    """Command check: reject if used inside a server — this command only works in DMs, for any user."""
    async def predicate(ctx):
        if ctx.guild is not None:
            raise commands.CheckFailure(
                "❌ This command only works in DMs. Please send me a direct message."
            )
        return True
    return commands.check(predicate)







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
    # ── Nuevo método de verificación ──────────────────────────────────────────────

    async def _ensure_dm_or_admin(self, ctx):
        """
        Raise CheckFailure if command is used in a guild by a non-admin.
        DMs are always allowed.
        """
        if ctx.guild is None:
            return  # DM always allowed
        if ctx.author.id in self.bot.admin_user_ids:
            return  # admin allowed in channels
        raise commands.CheckFailure(
            "This command is only available in DMs for regular users. "
            "Please send me a DM with your request."
        )
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
    
    def _tourneys_for_ctx(self, context: dict) -> list[dict]:
        if context['tournament']:
            return [context['tournament']]
        return self.tournaments

    async def _get_most_recent_week(self, context: dict) -> int:
        """Obtiene la semana más reciente con partidos en los torneos del contexto."""
        tourneys = self._tourneys_for_ctx(context)
        max_week = -1
        for tourney in tourneys:
            try:
                sheets = await get_tournament_sheets_async(tourney['url'], force_refresh=False)
                week = get_latest_week_from_sheets(sheets)
                if week > max_week:
                    max_week = week
            except Exception:
                continue
        return max_week if max_week > 0 else self.default_week

    async def _get_weeks_per_tournament(self, context: dict, guild_id: int = None, force_week: int = None) -> tuple[dict[str, int], dict[str, dict]]:
        """
        Returns (weeks, sheets_by_alias). sheets_by_alias holds whatever sheets
        were already fetched while resolving each tournament's week, so callers
        (e.g. matches_command) can reuse them instead of re-fetching the same
        URL a second time. Not every tourney will have an entry — force_week
        skips the fetch entirely, and a failed fetch leaves it absent too.
        """
        from scheduler import list_tasks
        import json
        tourneys = self._tourneys_for_ctx(context)
        weeks = {}
        sheets_by_alias = {}

        # Obtener tareas
        tasks = []
        if guild_id is not None:
            try:
                tasks = list_tasks(guild_id=guild_id)
                logger.debug(f"Found {len(tasks)} tasks for guild {guild_id}")
            except Exception as e:
                logger.warning(f"Could not fetch scheduler tasks for guild {guild_id}: {e}")
        else:
            # En DMs, listar todas las tareas (sin filtrar por guild)
            try:
                tasks = list_tasks()
                logger.debug(f"Found {len(tasks)} tasks globally (DM context)")
            except Exception as e:
                logger.warning(f"Could not fetch scheduler tasks globally: {e}")

        for tourney in tourneys:
            if force_week is not None:
                weeks[tourney['alias']] = force_week
                continue

            # Obtener última semana con partidos
            try:
                sheets = await get_tournament_sheets_async(tourney['url'], force_refresh=False)
                sheets_by_alias[tourney['alias']] = sheets
                latest = get_latest_week_from_sheets(sheets)
            except Exception as e:
                latest = -1

            # Buscar tarea para este torneo (por alias o nombre)
            matched_task = None
            for task in tasks:
                if task['action'] in ('post_divisions', 'notify_all'):
                    params = json.loads(task['params'])
                    task_tournament = params.get('tournament', '').lower()
                    # Comparar con alias y con nombre (case-insensitive)
                    if (task_tournament == tourney['alias'].lower() or
                        task_tournament == tourney['name'].lower()):
                        matched_task = task
                        logger.debug(f"Found task for {tourney['alias']}: action={task['action']}, "
                                    f"current_week={task['current_week']}, params={params}")
                        break

            week = None
            if matched_task:
                cw = matched_task['current_week']
                if cw is not None and cw != -1:
                    # Auto‑advance: mostrar la semana publicada (actual - 1)
                    week = max(1, cw - 1)
                    logger.debug(f"Auto‑advance: {tourney['alias']} current_week={cw} → showing {week}")
                elif cw == -1:
                    # Temporada finalizada: mostrar la siguiente semana después de la última con datos
                    week = latest + 1 if latest > 0 else self.default_week
                    logger.debug(f"Season finished: {tourney['alias']} → using latest+1={week}")
                else:
                    # Modo fijo: usar el 'week' del parámetro (resolviendo "default")
                    week_str = params.get('week', 'default')
                    if week_str.lower() == 'default':
                        week = self.default_week
                    else:
                        try:
                            week = int(week_str)
                        except ValueError:
                            week = self.default_week
                    logger.debug(f"Fixed week: {tourney['alias']} week={week}")
            else:
                # Sin tarea: mostrar la siguiente semana después de la última con partidos
                week = latest + 1 if latest > 0 else self.default_week
                logger.debug(f"No task for {tourney['alias']} → using latest+1={week}")

            weeks[tourney['alias']] = week

        return weeks, sheets_by_alias

    
    def _split_pending(self, pending: list) -> tuple[list, list]:
        normal = []
        misreported = []
        for m in pending:
            if m.get("misreported"):
                misreported.append(m)
            else:
                normal.append(m)
        return normal, misreported

    async def _send_matches_text(self, destination, player: str, tourney_results: list):
        """Envía resultados de partidos en texto plano (fallback o DM forzado)."""
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
                await destination.send(chunk)

    async def _build_and_send_player_matches(self, destination, player: str, force_text: bool,
                                              guild_id: int | None, context: dict):
        """
        Build a player's match results across the tournaments in `context`
        and send them to `destination` (anything with .send() — a ctx, a
        fetched discord.User, a DM channel) exactly like !m does. Shared by
        matches_command and the notify_subscribed_players scheduled action,
        which has no ctx/channel to resolve a context from (pass
        {'tournament': None} for a DM-equivalent, all-tournaments lookup).
        """
        weeks_per_tournament, sheets_by_alias = await self._get_weeks_per_tournament(
            context, guild_id=guild_id, force_week=None
        )
        tourneys = self._tourneys_for_ctx(context)

        tourney_results = []
        errors = []

        for tourney in tourneys:
            week = weeks_per_tournament.get(tourney['alias'], self.default_week)
            try:
                sheets = sheets_by_alias.get(tourney['alias'])
                if sheets is None:
                    sheets = await get_tournament_sheets_async(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                current, pending = get_player_matches(sheets, player, week)

                # Si no hay partidos y la semana supera la última con datos, considerar temporada completa
                if not current and not pending:
                    latest = get_latest_week_from_sheets(sheets)
                    if latest > 0 and week > latest:
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
                        # Sin datos, continuar
                        pass

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

                normal_pend, misreported = self._split_pending(pending)
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
                logger.error(f"_build_and_send_player_matches ({tourney['name']}): {e}", exc_info=True)
                errors.append(f"❌ Error in {tourney['name']}: could not load match data.")

        if errors:
            await destination.send("\n".join(errors))

        if not tourney_results and not errors:
            await destination.send(f"📅 No matches found for **{player}** in any tournament.")
            return

        if force_text:
            await self._send_matches_text(destination, player, tourney_results)
        else:
            # Siempre intentar imagen (a menos que se haya forzado texto)
            try:
                img_bytes = render_player_matches(player, tourney_results)
                filename = f"matches_{player.lower().replace(' ', '_')}.png"
                await destination.send(file=discord.File(io.BytesIO(img_bytes), filename=filename))
            except Exception as e:
                logger.exception(f"_build_and_send_player_matches image render failed: {e}")
                await self._send_matches_text(destination, player, tourney_results)

    async def _build_and_send_player_summary(self, destination, player: str, context: dict):
        """
        Build and send a per-tournament stats summary for `player`: every
        registered character (hero) with class/race, division, ranking
        position, and their played/points/win% record (only check=='OK'
        matches count), followed by a per-tournament total across all of
        that player's characters. Text-only for now. Shared by
        summary_command and notify_subscribed_players, which appends this
        after the matches DM (pass {'tournament': None} for a DM-equivalent,
        all-tournaments lookup, same convention as _build_and_send_player_matches).

        Matches are best-of-two games, so each one awards the player between
        0 and 2 points (their own score1/score2) rather than a binary
        win/loss — a 1-1 draw is possible. Win% is points / (played * 2).
        """
        tourneys = self._tourneys_for_ctx(context)
        sections = []

        for tourney in tourneys:
            try:
                sheets = await get_tournament_sheets_async(tourney['url'], force_refresh=False)
            except Exception as e:
                logger.error(f"_build_and_send_player_summary ({tourney['name']}): {e}", exc_info=True)
                continue

            characters = get_player_characters(
                sheets, player, tourney.get('builds_sheet'), tourney.get('builds_mapping')
            )
            if not characters:
                continue

            lines = [f"**🏆 {tourney['name']}**"]
            total_played = 0
            total_points = 0
            for char in characters:
                division, rank, total_ranked = find_player_division_rank(sheets, char['hero_key'])
                played, points = get_player_division_record(sheets, division, char['hero_key']) if division else (0, 0)
                total_played += played
                total_points += points

                build = f"{char['ancestry']} {char['class']}".strip() or "—"
                win_pct = f"{round(points / (played * 2) * 100)}%" if played else "—"
                if division:
                    rank_part = f" — {_ordinal(rank)} out of {total_ranked}"
                    div_label = division
                else:
                    rank_part = ""
                    div_label = "Unranked"
                lines.append(
                    f"**{div_label}** — {char['hero_name']} ({build}){rank_part} — "
                    f"{played} played, {points} points ({win_pct})"
                )

            total_pct = f"{round(total_points / (total_played * 2) * 100)}%" if total_played else "—"
            lines.append(f"**Total:** {total_played} played, {total_points} points ({total_pct})")
            sections.append("\n".join(lines))

        if not sections:
            await destination.send(f"📅 No characters found for **{player}** in any tournament.")
            return

        for chunk in split_message("\n\n".join(sections)):
            await destination.send(chunk)

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
        await self._ensure_dm_or_admin(ctx) 
        embed = discord.Embed(title="🏆 Available Tournaments", color=discord.Color.gold())
        for t in self._tourneys_for_ctx(context):
            embed.add_field(name=t['name'], value=f"Alias: `{t.get('alias', 'No alias')}`", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name='matches', aliases=['m'])
    async def matches_command(self, ctx, player: str = None, *args):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        await self._ensure_dm_or_admin(ctx)

        if not player:
            await ctx.send("❌ Please specify a player name. Example: `!m Scorium`")
            return

        # Solo permitir el flag 'text' (y solo en DMs)
        force_text = False
        is_dm = ctx.guild is None
        for arg in args:
            if arg.lower() == "text":
                if is_dm:
                    force_text = True
                else:
                    await ctx.send("ℹ️ The `text` flag only works in direct messages (DMs). Ignored.")
            else:
                # Cualquier otro argumento se ignora y se notifica
                await ctx.send(f"ℹ️ The `!m` command no longer accepts a week number. The week is determined automatically. Ignoring `{arg}`.")

        await ctx.send(f"🔍 Searching for **{player}**...")

        guild_id = ctx.guild.id if ctx.guild else None
        await self._build_and_send_player_matches(ctx, player, force_text and is_dm, guild_id, context)

    @require_dm()
    @commands.command(name='subscribe', aliases=['sub'])
    async def subscribe_command(self, ctx, player: str = None, *args):
        """DM-only. Subscribe to a player: get DM'd their matches whenever
        notify_subscribed_players fires, as if you'd run !m <player>."""
        if not player:
            await ctx.send("❌ Please specify a player name. Example: `!sub Scorium`")
            return

        force_text = False
        for arg in args:
            if arg.lower() == "text":
                force_text = True
            else:
                await ctx.send(f"ℹ️ Ignoring unknown option `{arg}`.")

        from scheduler import add_subscription, list_subscriptions, list_tasks_by_action, describe_schedule_gmt

        # Only one active subscription per user — check before anything else
        # so we don't bother hitting the mapping sheet if they're already subscribed.
        existing = list_subscriptions(ctx.author.id)
        if existing:
            await ctx.send(
                f"❌ You're already subscribed to **{existing[0]['player']}**. "
                f"Use `!unsub` first, then `!sub {player}` again to switch."
            )
            return

        # Validate the player against each tournament's hero-builds roster —
        # NOT the !sendto/!notify_all mapping sheet, which only lists players
        # who've linked a Discord ID (a small subset). !sub already has the
        # Discord ID directly from ctx.author.id, so that mapping is
        # irrelevant here; the roster that actually matters is "is this
        # player registered in a tournament at all", which the builds sheet
        # answers regardless of whether they've linked Discord.
        player_key = normalize_name(player)
        prefix = player_key + "-"
        found = False
        any_roster_loaded = False
        for tourney in self.tournaments:
            try:
                sheets = await get_tournament_sheets_async(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
            except Exception as e:
                logger.warning(f"subscribe_command: couldn't load roster for {tourney['name']}: {e}")
                continue
            any_roster_loaded = True
            # builds keys are normalize_name("Player - Hero"), e.g. "samwise-mordek" —
            # match the player portion by prefix so it doesn't also match a hero
            # name that happens to contain the search term (e.g. searching "ard"
            # shouldn't match a hero called "Standard").
            if any(key == player_key or key.startswith(prefix) for key in builds):
                found = True
                break

        if not found and not any_roster_loaded:
            await ctx.send("❌ Couldn't load the tournament rosters right now. Please try again in a moment.")
            return

        if not found:
            await ctx.send(
                f"❌ I couldn't find a player named **{player}**. "
                "Please make sure you typed the name exactly as it's registered in the tournaments."
            )
            return

        # The builds dict only stores normalized keys, so the original
        # spelling isn't recoverable from it — store what the user typed.
        add_subscription(ctx.author.id, player, force_text)

        tasks = list_tasks_by_action("notify_subscribed_players")
        if tasks:
            schedule_note = ", ".join(describe_schedule_gmt(t) for t in tasks)
        else:
            schedule_note = "no notification schedule is set up yet — check back once an admin creates one"

        format_note = "as plain text" if force_text else "as an image (falling back to text if that fails)"
        await ctx.send(
            f"✅ Subscribed to **{player}**. You'll be notified {schedule_note}, {format_note}. "
            f"Use `!unsub` to remove all your subscriptions."
        )

    @require_dm()
    @commands.command(name='unsubscribe', aliases=['unsub'])
    async def unsubscribe_command(self, ctx):
        """DM-only. Remove every player subscription for the calling user."""
        from scheduler import remove_all_subscriptions
        count = remove_all_subscriptions(ctx.author.id)
        if count:
            await ctx.send(f"🗑️ Removed {count} subscription(s).")
        else:
            await ctx.send("📭 You have no active subscriptions.")

    @require_dm()
    @commands.command(name='summary', aliases=['sum'])
    async def summary_command(self, ctx, player: str = None, *args):
        """DM-only. Show a player's characters and win/loss record across every tournament."""
        if not player:
            await ctx.send("❌ Please specify a player name. Example: `!summary Scorium`")
            return
        for arg in args:
            if arg.lower() != "text":
                await ctx.send(f"ℹ️ Ignoring unknown option `{arg}`.")

        await self._build_and_send_player_summary(ctx, player, context={'tournament': None})

    @is_bot_admin()
    @require_dm()
    @commands.command(name='show_sub')
    async def show_subscriptions_command(self, ctx):
        """Admin-only, DM-only. List every active subscription: who's subscribed to whom."""
        from scheduler import list_subscriptions
        subs = list_subscriptions()
        if not subs:
            await ctx.send("📭 No active subscriptions.")
            return

        entries = []
        for row in subs:
            try:
                user = await self.bot.fetch_user(row["user_id"])
                who = str(user)
            except Exception as e:
                logger.warning(f"show_sub: couldn't fetch user {row['user_id']}: {e}")
                who = str(row["user_id"])
            entries.append(f"{who} → {row['player']}")

        text = f"📋 **{len(entries)} active subscription(s):**\n" + ", ".join(entries)
        for chunk in split_message(text):
            await ctx.send(chunk)

    @commands.command(name='division', aliases=['d'])
    async def division_command(self, ctx, *args):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        await self._ensure_dm_or_admin(ctx)
        is_dm = ctx.guild is None
        force_text = False
        rest = list(args)

        # ── Parse "text" flag (only in DMs) ──
        if is_dm and rest and rest[-1].lower() == "text":
            force_text = True
            rest.pop()

        # ── Parse division and week ──
        division_name = None
        week = None
        if len(rest) >= 2:
            division_name = rest[0]
            try:
                week = int(rest[1])
            except ValueError:
                await ctx.send("❌ Week must be a number.")
                return
        elif len(rest) == 1:
            # Could be division or week
            try:
                week = int(rest[0])
            except ValueError:
                division_name = rest[0]

        # If no args, try to infer division from thread
        if not division_name:
            division_name = context.get('division')
            if not division_name:
                await ctx.send(
                    "❌ No division specified. "
                    "Run this command inside a division thread, or use `!d <division> [week]`."
                )
                return

        # If week not set, get the most recent
        if week is None:
            week = await self._get_most_recent_week(context)
            if week <= 0:
                week = self.default_week

        tourneys = self._tourneys_for_ctx(context)
        sent_any = False

        for tourney in tourneys:
            try:
                sheets = await get_tournament_sheets_async(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                current, pending = get_division_matches(sheets, division_name, week)

                if not current and not pending:
                    continue

                if force_text and is_dm:
                    # Texto directo
                    lines = [f"**🏆 {tourney['name']} · {division_name}** — Week {week}"]
                    for m in current:
                        status = " ✓" if m.get('check', '') == "OK" else ""
                        lines.append(f"{m['player1']} vs {m['player2']}{status}")
                    if pending:
                        lines.append("**⏳ Pending:**")
                        for m in pending:
                            lines.append(f"Wk {m['week']}: {m['player1']} vs {m['player2']}")
                    await ctx.send("\n".join(lines))
                    sent_any = True
                else:
                    # Intentar imagen
                    try:
                        await send_division_image(
                            ctx.channel, tourney['name'], division_name, week,
                            current, pending, builds
                        )
                        sent_any = True
                    except Exception as e:
                        logger.exception(f"division_command image render failed: {e}")
                        # Fallback a texto
                        lines = [f"**🏆 {tourney['name']} · {division_name}** — Week {week}"]
                        for m in current:
                            status = " ✓" if m.get('check', '') == "OK" else ""
                            lines.append(f"{m['player1']} vs {m['player2']}{status}")
                        if pending:
                            lines.append("**⏳ Pending:**")
                            for m in pending:
                                lines.append(f"Wk {m['week']}: {m['player1']} vs {m['player2']}")
                        await ctx.send("\n".join(lines))
                        sent_any = True
            except Exception as e:
                logger.error(f"division_command ({tourney['name']}): {e}", exc_info=True)
                await ctx.send(f"❌ Error in {tourney['name']}: {e}")

        if not sent_any:
            await ctx.send(f"⚠️ No matches found for division **{division_name}** in week **{week}**.")

    @commands.command(name='standings', aliases=['c'])
    async def standings_command(self, ctx, *args):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        await self._ensure_dm_or_admin(ctx)
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
                sheets = await get_tournament_sheets_async(tourney['url'], force_refresh=False)
                rows, headers = get_division_standings(sheets, division_name)

                if not rows:
                    await ctx.send(f"⚠️ No standings found for **{division_name}** in {tourney['name']}.")
                    continue

                # Load builds for enriching the text view
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                rows_with_build = [
                    row + [builds.get(normalize_name(row[1]), '')]
                    for row in rows
                ]
                text_headers = headers + ['Build'] if rows_with_build else headers

                if force_text and is_dm:
                    # Direct text
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
            week = await self._get_most_recent_week(context)
            if week <= 0:
                week = self.default_week

        await ctx.send(f"📬 Fetching matches for **{player}** (week {week})...")

        mapping = await load_player_mapping_async(self.mapping_sheet_url)
        if player not in mapping:
            await ctx.send(f"❌ No Discord ID found for player **{player}**.")
            return
        discord_id = mapping[player]

        success_count = 0
        for tourney in self._tourneys_for_ctx(context):
            try:
                sheets = await get_tournament_sheets_async(tourney['url'], force_refresh=False)
                builds = load_hero_builds_from_sheets(
                    sheets, tourney.get('builds_sheet'), tourney.get('builds_mapping')
                )
                messages, err = await build_matches_message_async(
                    tourney, player, week, force_refresh=False, builds=builds
                )
                if err:
                    await ctx.send(f"⚠️ Error in {tourney['name']}: {err}")
                    continue
                if not messages:
                    # No hay partidos para este torneo, pero no es error
                    continue

                # Enviar cada mensaje por DM
                for msg in messages:
                    for chunk in split_message(msg):
                        if not await send_dm_to_player(self.bot, discord_id, chunk):
                            await ctx.send(f"⚠️ Could not DM {player} for {tourney['name']}.")
                            break
                    else:
                        # Si todos los chunks se enviaron bien, sumamos éxito
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

        if week is None:
            week = await self._get_most_recent_week(context)
            if week <= 0:
                week = self.default_week

        await ctx.send(f"🚀 Gathering players with pending matches for week **{week}**...")

        success, total = await run_notify_all(
            bot=self.bot,
            destination=ctx,
            tournaments=self._tourneys_for_ctx(context),
            mapping_url=self.mapping_sheet_url,
            default_week=self.default_week,
            week_raw=week,
            force_refresh=False,
        )

        if total > 0:
            await ctx.send(f"✅ DMs sent to {success} out of {total} players.")
        else:
            await ctx.send("✅ No players with pending matches found.")

    @is_bot_admin()
    @commands.command(name='post_divisions')
    async def post_divisions_command(self, ctx, week: int = None, tournament_alias: str = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return

        if week is None:
            week = await self._get_most_recent_week(context)
            if week <= 0:
                week = self.default_week

        if tournament_alias is None:
            tournament_alias = context['tournament']['alias'] if context['tournament'] else "MA"

        tourney = find_tournament(self.tournaments, tournament_alias)
        if not tourney:
            await ctx.send(f"❌ Tournament `{tournament_alias}` not found.")
            return

        await ctx.send(f"🚀 Posting **{tourney['name']}** week {week} matchups to threads...")

        success, not_found, errors = await run_post_divisions(
            destination=ctx.channel,
            tournaments=self.tournaments,
            default_week=self.default_week,
            tournament_alias=tournament_alias,
            week_raw=week,
            force_refresh=False,
        )

        result = f"✅ Posted to {success} divisions."
        if not_found:
            result += f"\n⚠️ Threads not found: {', '.join(not_found)}"
        if errors:
            result += f"\n❌ Errors: {', '.join(errors)}"
        await ctx.send(result)

    @is_bot_admin()
    @commands.command(name='post_standings')
    async def post_standings_command(self, ctx, tournament_alias: str = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return

        if tournament_alias is None:
            tournament_alias = context['tournament']['alias'] if context['tournament'] else None

        if tournament_alias is None:
            await ctx.send("❌ No tournament specified. Use `!post_standings <tournament>` or run from a tournament channel.")
            return

        tourney = find_tournament(self.tournaments, tournament_alias)
        if not tourney:
            await ctx.send(f"❌ Tournament `{tournament_alias}` not found.")
            return

        await ctx.send(f"🚀 Posting **{tourney['name']}** standings to division threads...")

        success, not_found, errors = await run_post_standings(
            destination=ctx.channel,
            tournaments=self.tournaments,
            tournament_alias=tournament_alias,
            force_refresh=False,
        )

        result = f"✅ Standings posted to {success} divisions."
        if not_found:
            result += f"\n⚠️ Threads not found: {', '.join(not_found)}"
        if errors:
            result += f"\n❌ Errors: {', '.join(errors)}"
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
                await refresh_tournament_cache_async(tourney['url'])
                await ctx.send(f"✅ Refreshed {tourney['name']}")
            except Exception as e:
                await ctx.send(f"❌ Error refreshing {tourney['name']}: {e}")
        try:
            await load_player_mapping_async(self.mapping_sheet_url, force_refresh=True)
            await ctx.send("✅ Refreshed player mapping sheet")
        except Exception as e:
            await ctx.send(f"❌ Error refreshing mapping: {e}")
        await ctx.send("🎉 Cache refresh complete!")

    # ── Help ─────────────────────────────────────────────────────────
        
    @commands.command(name='help')
    async def help_command(self, ctx, command_name: str = None):
        context = self._ctx(ctx)
        if not context['allowed']:
            return
        bot_mention = f"@{ctx.bot.user.name}"
        embed = get_help_embed(bot_mention, command_name)
        await ctx.send(embed=embed)

async def setup(bot, tournaments, mapping_sheet_url, default_week):
    image_scale = bot.config.get('image_scale', 1.0) if hasattr(bot, 'config') else 1.0
    cog = TournamentCommands(bot, tournaments, mapping_sheet_url, default_week, image_scale)
    await bot.add_cog(cog)
    return cog