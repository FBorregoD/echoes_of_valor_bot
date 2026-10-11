import asyncio
import re
import threading
import time
import logging
import unicodedata
import json

import pandas as pd
import Googlexcel_noPassword as ggx
import discord

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# In-memory cache (TTL-based, safe for ephemeral filesystems)
# ------------------------------------------------------------
CACHE_TTL = 300  # seconds (5 minutes)
_memory_cache: dict[str, tuple] = {}

# get_tournament_sheets runs in a worker thread (via asyncio.to_thread from
# the async wrappers below), so several requests can now genuinely race on
# the same URL. Per-URL locks make concurrent misses on a cold/expired cache
# entry wait for a single in-flight fetch instead of stampeding Google Sheets.
_fetch_locks: dict[str, threading.Lock] = {}
_fetch_locks_guard = threading.Lock()


def _get_fetch_lock(url: str) -> threading.Lock:
    with _fetch_locks_guard:
        lock = _fetch_locks.get(url)
        if lock is None:
            lock = threading.Lock()
            _fetch_locks[url] = lock
        return lock


def load_cached_sheets(url: str):
    entry = _memory_cache.get(url)
    if entry is not None:
        data, timestamp = entry
        if (time.time() - timestamp) < CACHE_TTL:
            return data
    return None


def save_cached_sheets(url: str, data):
    _memory_cache[url] = (data, time.time())



def get_tournament_sheets(tournament_url: str, force_refresh: bool = False):
    if not force_refresh:
        cached = load_cached_sheets(tournament_url)
        if cached is not None:
            return cached

    with _get_fetch_lock(tournament_url):
        # Re-check: another thread may have just finished fetching while we
        # were waiting for the lock.
        if not force_refresh:
            cached = load_cached_sheets(tournament_url)
            if cached is not None:
                return cached

        logger.info(f"Fetching sheets from: {tournament_url}")
        max_retries = 2
        delay = 1  # segundos
        for attempt in range(max_retries):
            try:
                sheets = ggx.data_fromAllSheets(tournament_url)
                save_cached_sheets(tournament_url, sheets)
                return sheets
            except Exception as e:
                logger.warning(f"Attempt {attempt+1}/{max_retries} failed for {tournament_url}: {e}")
                if attempt == max_retries - 1:
                    logger.error(f"All retries failed for {tournament_url}: {e}", exc_info=True)
                    raise
                time.sleep(delay)
        return None  # no debería llegar


def refresh_tournament_cache(tournament_url: str):
    return get_tournament_sheets(tournament_url, force_refresh=True)


# ------------------------------------------------------------
# Async wrappers — the single place that offloads the blocking,
# timeout-less requests calls above onto a worker thread. Every async
# call site should go through these instead of calling the sync
# functions (or asyncio.to_thread) directly, so the offload + bounded
# wait behavior lives in one spot instead of being repeated at each
# call site.
#
# asyncio.wait_for bounds how long the *caller* waits — it does not (and
# cannot) cancel the underlying blocking call, since Googlexcel_noPassword
# exposes no timeout hook. A hung fetch still occupies a thread-pool worker
# until it eventually returns or errors; this only stops individual
# commands/tasks from hanging forever from the user's point of view.
# ------------------------------------------------------------
DEFAULT_FETCH_TIMEOUT = 30  # seconds


async def get_tournament_sheets_async(tournament_url: str, force_refresh: bool = False,
                                       timeout: float = DEFAULT_FETCH_TIMEOUT):
    return await asyncio.wait_for(
        asyncio.to_thread(get_tournament_sheets, tournament_url, force_refresh),
        timeout=timeout,
    )


async def refresh_tournament_cache_async(tournament_url: str, timeout: float = DEFAULT_FETCH_TIMEOUT):
    return await asyncio.wait_for(
        asyncio.to_thread(refresh_tournament_cache, tournament_url),
        timeout=timeout,
    )


# ------------------------------------------------------------
# Helper: normalize names
# ------------------------------------------------------------
def normalize_name(name: str) -> str:
    """
    Normalize player name: lowercase, collapse whitespace, normalize hyphens,
    fold Latin accents to their base letter (e.g. 'é' -> 'e'). Non-Latin
    scripts (CJK, Cyrillic, etc.) are preserved rather than stripped — the
    previous `.encode('ascii', 'ignore')` approach silently deleted them,
    collapsing every non-Latin name to the same empty string and colliding
    in anything keyed by this function (hero-build lookups, misreported-match
    dedup, !subscribe's per-player dedup key).
    """
    if not name:
        return ""
    name = str(name)
    # Normalize dash variants to a plain hyphen before any other processing —
    # doing this after the accent fold would be too late, since NFKD leaves
    # dash characters untouched (they have no decomposition).
    name = re.sub(r'[–—−]', '-', name)
    name = re.sub(r'\s*-\s*', '-', name)
    name = unicodedata.normalize('NFKD', name)
    name = ''.join(ch for ch in name if not unicodedata.combining(ch))
    return ' '.join(name.lower().split())


def is_division_sheet(df) -> bool:
    """
    Return True only if the sheet is a real division sheet.
    Criteria:
      1. Has a 'SCHEDULE' marker in column 0.
      2. At least one match row (below a 'Week N' header) where both player
         cells follow the 'PlayerName - HeroName' pattern.
    This filters out meta-sheets like 'Format Scoresheet', 'Legions', etc.
    """
    schedule_found = False
    week_found = False
    for idx, row in df.iterrows():
        cell = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        if cell.upper() == "SCHEDULE":
            schedule_found = True
        if schedule_found and cell.startswith("Week"):
            week_found = True
        if week_found:
            p1 = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""
            p2 = str(row.iloc[3]).strip() if len(row) > 3 and pd.notna(row.iloc[3]) else ""
            if " - " in p1 and " - " in p2:
                return True
    return False


# ------------------------------------------------------------
# Player name matching
# ------------------------------------------------------------

def _player_name(full: str) -> str:
    """Extract 'PlayerName' from 'PlayerName - HeroName'."""
    if ' - ' in full:
        return full.split(' - ')[0].strip()
    return full.strip()


def _hero_name(full: str) -> str:
    """Extract 'HeroName' from 'PlayerName - HeroName' (split once, so a
    hero name that itself contains ' - ' isn't cut short)."""
    if ' - ' in full:
        return full.split(' - ', 1)[1].strip()
    return full.strip()


def player_matches(search: str, full_name: str) -> bool:
    """
    True if `search` matches the player-name portion of `full_name`.
    Rules (in order):
      1. Exact match on player name (case-insensitive)
      2. Player name starts with the search term
      3. Search term appears as a whole word in the player name
    Never matches mid-word, so 'omsk' does NOT match 'Ohmsk'.
    """
    import re as _re
    name = _player_name(full_name).lower()
    s = search.lower().strip()
    if not s:
        return False
    if name == s:
        return True
    if name.startswith(s):
        return True
    if _re.search(r'\b' + _re.escape(s) + r'\b', name):
        return True
    return False


# ------------------------------------------------------------
# Hero builds loading
# ------------------------------------------------------------
def find_hero_builds_sheet(sheets_dict: dict, preferred_name: str = None):
    if preferred_name:
        for name in sheets_dict.keys():
            if name.lower() == preferred_name.lower():
                return name
    for name in sheets_dict.keys():
        lower_name = name.lower()
        if 'hero' in lower_name or 'build' in lower_name:
            return name
    return None


def _resolve_builds_layout(df, column_mapping: dict = None) -> tuple[dict, int]:
    """
    Shared header-row/column detection for the hero-builds sheet. Returns
    (col_indices, start_row). Split out of load_hero_builds_from_sheets so
    get_player_characters() can reuse the exact same layout logic instead of
    re-implementing it.
    """
    header_row = 0
    start_row = 0
    if column_mapping and any(isinstance(v, str) for v in column_mapping.values()):
        string_cols = [v.lower() for v in column_mapping.values() if isinstance(v, str)]
        for idx in range(min(10, len(df))):
            row_vals = [str(cell).strip().lower() for cell in df.iloc[idx] if pd.notna(cell)]
            if all(any(col in rv for rv in row_vals) for col in string_cols):
                header_row = idx
                start_row = idx + 1
                break
    else:
        for idx in range(min(5, len(df))):
            first_cell = str(df.iloc[idx, 0]).strip().lower() if pd.notna(df.iloc[idx, 0]) else ""
            if 'tier' in first_cell or 'player' in first_cell:
                header_row = idx
                start_row = idx + 1
                break
        else:
            header_row = -1
            start_row = 0

    col_indices = {}
    if column_mapping:
        for key, col_spec in column_mapping.items():
            if isinstance(col_spec, int):
                col_indices[key] = col_spec
            elif isinstance(col_spec, str):
                found = False
                if header_row >= 0:
                    for i, cell in enumerate(df.iloc[header_row]):
                        if pd.notna(cell) and str(cell).strip().lower() == col_spec.lower():
                            col_indices[key] = i
                            found = True
                            break
                    if not found:
                        for i, cell in enumerate(df.iloc[header_row]):
                            if pd.notna(cell) and col_spec.lower() in str(cell).strip().lower():
                                col_indices[key] = i
                                found = True
                                break
                if not found:
                    col_indices[key] = 0
    else:
        col_indices = {'player_col': 3, 'ancestry_col': 4, 'class_col': 5}

    return col_indices, start_row


def _iter_builds_rows(df, col_indices: dict, start_row: int):
    """Yield (player_hero_raw, ancestry, class_) for each populated row below start_row."""
    if col_indices.get('player_col') is None:
        return
    for idx in range(start_row, len(df)):
        row = df.iloc[idx]
        if col_indices['player_col'] >= len(row):
            continue
        player_cell = row.iloc[col_indices['player_col']]
        if pd.isna(player_cell):
            continue
        player_hero = str(player_cell).strip()
        if not player_hero:
            continue

        ancestry = ""
        if col_indices.get('ancestry_col') is not None and col_indices['ancestry_col'] < len(row):
            anc_cell = row.iloc[col_indices['ancestry_col']]
            if not pd.isna(anc_cell):
                ancestry = str(anc_cell).strip()

        class_ = ""
        if col_indices.get('class_col') is not None and col_indices['class_col'] < len(row):
            cls_cell = row.iloc[col_indices['class_col']]
            if not pd.isna(cls_cell):
                class_ = str(cls_cell).strip()

        yield player_hero, ancestry, class_


def load_hero_builds_from_sheets(sheets_dict: dict, sheet_name: str = None, column_mapping: dict = None):
    builds = {}
    target_sheet = find_hero_builds_sheet(sheets_dict, sheet_name)
    if not target_sheet:
        return builds
    df = sheets_dict[target_sheet]
    col_indices, start_row = _resolve_builds_layout(df, column_mapping)

    for player_hero, ancestry, class_ in _iter_builds_rows(df, col_indices, start_row):
        norm_key = normalize_name(player_hero)
        builds[norm_key] = f"{ancestry} {class_}".strip()

    return builds


def get_player_characters(sheets_dict: dict, player_name: str,
                           sheet_name: str = None, column_mapping: dict = None) -> list[dict]:
    """
    Every hero-builds-sheet row registered to `player_name`, matched by
    normalized prefix — the same convention !subscribe uses to validate a
    player against the roster. A player can have more than one row (e.g.
    registered in more than one division), so this returns a list, in sheet
    order: [{"hero_key": "samwise-mordek", "display_name": "Samwise - Mordek",
    "hero_name": "Mordek", "ancestry": ..., "class": ...}, ...].
    """
    characters = []
    target_sheet = find_hero_builds_sheet(sheets_dict, sheet_name)
    if not target_sheet:
        return characters
    df = sheets_dict[target_sheet]
    col_indices, start_row = _resolve_builds_layout(df, column_mapping)

    player_key = normalize_name(player_name)
    prefix = player_key + "-"

    for player_hero, ancestry, class_ in _iter_builds_rows(df, col_indices, start_row):
        hero_key = normalize_name(player_hero)
        if hero_key == player_key or hero_key.startswith(prefix):
            characters.append({
                "hero_key": hero_key,
                "display_name": player_hero,
                "hero_name": _hero_name(player_hero),
                "ancestry": ancestry,
                "class": class_,
            })

    return characters


# ------------------------------------------------------------
# Match extraction and formatting
# ------------------------------------------------------------
def parse_week_number(week_str: str):
    match = re.search(r'\d+', str(week_str))
    return int(match.group()) if match else None


VALID_GAME_SCORES = (0, 1, 2)


def read_match_result(row) -> dict:
    """
    Decide a match row's state from its scores alone (columns E/F).

    The sheet's "check" column (G) is deliberately ignored: it's edited by
    hand and admins sometimes get it wrong, so the scores are the truth.
    Matches are best-of-two, so:
      - finished:    both scores in 0/1/2 and they add up to 2 (2-0, 0-2, 1-1)
      - not started: 0-0 or both cells empty
      - misreported: anything else (2-2, 1-0, one cell empty, non-numeric, ...)

    Returns {"score1", "score2", "total", "finished", "misreported"}; scores
    are 0 and total is None when the cells can't be read as numbers.
    """
    raw = [row.iloc[i] if len(row) > i else None for i in (4, 5)]
    blank = [v is None or pd.isna(v) or str(v).strip() == "" for v in raw]
    unreadable = {"score1": 0, "score2": 0, "total": None, "finished": False}

    if all(blank):
        return {**unreadable, "misreported": False}
    if any(blank):
        return {**unreadable, "misreported": True}
    try:
        score1 = float(raw[0])
        score2 = float(raw[1])
    except (ValueError, TypeError):
        return {**unreadable, "misreported": True}

    total = score1 + score2
    finished = total == 2 and score1 in VALID_GAME_SCORES and score2 in VALID_GAME_SCORES
    not_started = score1 == 0 and score2 == 0
    return {
        "score1": score1,
        "score2": score2,
        "total": total,
        "finished": finished,
        "misreported": not finished and not not_started,
    }

def get_player_matches(sheets_dict: dict, player: str, target_week: int):
    current_matches = []
    pending_matches = []
    for sheet_name, df in sheets_dict.items():
        if not is_division_sheet(df):
            continue
        start_row = None
        for idx, row in df.iterrows():
            if pd.notna(row.iloc[0]) and str(row.iloc[0]).strip().startswith("Week"):
                start_row = idx
                break
        if start_row is None:
            continue
        current_week = None
        for idx in range(start_row, len(df)):
            row = df.iloc[idx]
            week_val = row.iloc[0]
            week_str = str(week_val).strip() if pd.notna(week_val) else ""
            if week_str.startswith("Week"):
                new_week = parse_week_number(week_str)
                if new_week is not None:
                    current_week = new_week
            if current_week is None:
                continue
            player1 = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""
            player2 = str(row.iloc[3]).strip() if len(row) > 3 and pd.notna(row.iloc[3]) else ""
            if not player1 and not player2:
                continue
            if player_matches(player, player1) or player_matches(player, player2):
                match = {
                    "week": current_week,
                    "player1": player1,
                    "player2": player2,
                    "division": sheet_name,
                    **read_match_result(row),
                }
                if current_week == target_week:
                    current_matches.append(match)
                elif current_week < target_week and not match["finished"]:
                    pending_matches.append(match)
    return current_matches, pending_matches

def get_division_matches(sheets_dict: dict, division_name: str, target_week: int):
    current = []
    pending = []
    seen_pairs = set()
    target_sheet = None
    for sheet_name in sheets_dict.keys():
        if sheet_name.lower() == division_name.lower():
            target_sheet = sheet_name
            break
    if not target_sheet:
        return current, pending
    df = sheets_dict[target_sheet]
    start_row = None
    for idx, row in df.iterrows():
        if pd.notna(row.iloc[0]) and str(row.iloc[0]).strip().startswith("Week"):
            start_row = idx
            break
    if start_row is None:
        return current, pending
    current_week = None
    for idx in range(start_row, len(df)):
        row = df.iloc[idx]
        week_val = row.iloc[0]
        week_str = str(week_val).strip() if pd.notna(week_val) else ""
        if week_str.startswith("Week"):
            new_week = parse_week_number(week_str)
            if new_week is not None:
                current_week = new_week
        if current_week is None:
            continue
        player1 = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""
        player2 = str(row.iloc[3]).strip() if len(row) > 3 and pd.notna(row.iloc[3]) else ""
        if not player1 and not player2:
            continue
        pair = tuple(sorted([normalize_name(player1), normalize_name(player2)]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        match = {
            "week": current_week,
            "player1": player1,
            "player2": player2,
            "division": target_sheet,
            **read_match_result(row),
        }
        if current_week == target_week:
            current.append(match)
        elif current_week < target_week and not match["finished"]:
            pending.append(match)
    return current, pending

def get_all_misreported_matches(sheets_dict: dict) -> list[dict]:
    mis = []
    division_names = [name for name, df in sheets_dict.items() if is_division_sheet(df)]
    latest_week = get_latest_week_from_sheets(sheets_dict)
    if latest_week <= 0:
        return mis

    for div in division_names:
        for week in range(1, latest_week + 1):
            current, pending = get_division_matches(sheets_dict, div, week)
            for match in current + pending:
                if match.get("misreported"):
                    mis.append(match)

    # Deduplicar por (semana, división, jugador1_norm, jugador2_norm)
    seen = set()
    unique = []
    for m in mis:
        key = (m['week'], m['division'], normalize_name(m['player1']), normalize_name(m['player2']))
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique

# Maximum Discord code-block width before switching to card layout
_TABLE_MAX_WIDTH = 80
# Discord message character limit (leaving room for markdown wrappers)
_MSG_MAX_CHARS = 1900
# Max rows per code-block chunk (keeps blocks short and readable)
_TABLE_CHUNK_ROWS = 15


def _table_total_width(col_widths: list[int]) -> int:
    return sum(col_widths) + 3 * len(col_widths) + 1


def _ascii_table_lines(rows: list, headers: list) -> list[str]:
    """Build lines for a fixed-width ASCII table (header + data rows, no footer repeat)."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))
    sep   = "┌" + "┬".join("─" * (w + 2) for w in col_widths) + "┐"
    head  = "│ " + " │ ".join(h.center(w) for h, w in zip(headers, col_widths)) + " │"
    div   = "├" + "┼".join("─" * (w + 2) for w in col_widths) + "┤"
    foot  = "└" + "┴".join("─" * (w + 2) for w in col_widths) + "┘"
    lines = [sep, head, div]
    for row in rows:
        lines.append("│ " + " │ ".join(str(cell).ljust(w) for cell, w in zip(row, col_widths)) + " │")
    lines.append(foot)
    return lines


def _card_lines(rows: list, headers: list) -> list[str]:
    """
    Render each row as a single line:
      [**Wk N · Division** · ] Player1 ⚔️ Player2
    Falls back to label: value pairs for non-match column layouts.
    """
    h_lower = [x.lower() for x in headers]
    has_week = 'week' in h_lower
    has_div  = 'division' in h_lower
    wi = h_lower.index('week')     if has_week else None
    di = h_lower.index('division') if has_div  else None

    skip = {i for i in (wi, di) if i is not None}
    player_cols = [i for i in range(len(headers)) if i not in skip]

    lines = []
    for row in rows:
        prefix_parts = []
        if has_week:
            prefix_parts.append(f"Wk {row[wi]}")
        if has_div:
            prefix_parts.append(str(row[di]))
        prefix = "**" + " · ".join(prefix_parts) + "** · " if prefix_parts else ""

        if len(player_cols) == 2:
            p1 = row[player_cols[0]]
            p2 = row[player_cols[1]]
            lines.append(f"{prefix}{p1} ⚔️ {p2}")
        else:
            body = " | ".join(f"{headers[i]}: {row[i]}" for i in player_cols)
            lines.append(f"{prefix}{body}")
    return lines


def format_table(rows: list, headers: list, title: str) -> str:
    """
    Legacy single-string renderer (used by build_matches_message in match_utils).
    Automatically picks ASCII table or card layout based on width.
    For multi-message sending prefer format_table_messages().
    """
    if not rows:
        return f"{title}\nNo data.\n"
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))
    if _table_total_width(col_widths) <= _TABLE_MAX_WIDTH:
        return "\n".join(_ascii_table_lines(rows, headers))
    return "\n".join(_card_lines(rows, headers))


def format_table_messages(rows: list, headers: list, title: str) -> list[str]:
    """
    Return a list of Discord-ready strings, each fitting within _MSG_MAX_CHARS.
    Uses ASCII table for narrow data, card layout for wide data.
    Splits at row boundaries — never mid-row — and re-emits the header on each chunk.
    """
    if not rows:
        return [f"{title}\n*(no data)*"]

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))

    use_cards = _table_total_width(col_widths) > _TABLE_MAX_WIDTH

    messages = []
    chunk_rows = []

    def flush(chunk, is_first: bool):
        if not chunk:
            return
        header = f"**{title}**\n" if is_first else f"**{title} (cont.)**\n"
        if use_cards:
            body = "\n".join(_card_lines(chunk, headers))
        else:
            body = "```\n" + "\n".join(_ascii_table_lines(chunk, headers)) + "\n```"
        messages.append(header + body)

    first = True
    for row in rows:
        chunk_rows.append(row)
        # Check size: render trial and measure
        trial_lines = _card_lines(chunk_rows, headers) if use_cards else _ascii_table_lines(chunk_rows, headers)
        trial = "\n".join(trial_lines)
        if len(trial) + 30 > _MSG_MAX_CHARS or len(chunk_rows) >= _TABLE_CHUNK_ROWS:
            # Flush all but the last row, then start new chunk with that row
            flush(chunk_rows[:-1], first)
            first = False
            chunk_rows = [row]

    flush(chunk_rows, first)
    return messages


def split_message(text: str, max_length: int = 1900) -> list[str]:
    """Split a plain text message at line boundaries, never exceeding max_length."""
    if len(text) <= max_length:
        return [text]
    chunks, current, length = [], [], 0
    for line in text.split('\n'):
        if length + len(line) + 1 > max_length:
            if current:
                chunks.append('\n'.join(current))
            current, length = [line], len(line) + 1
        else:
            current.append(line)
            length += len(line) + 1
    if current:
        chunks.append('\n'.join(current))
    return chunks


def build_matches_message(tournament: dict, player: str, week: int, force_refresh: bool = False, builds: dict = None):
    """
    Build match info as plain-text lines (used for DMs and inline replies).
    Always uses single-line format: Division · Player (build) vs Opponent (build)
    Adds a ✓ suffix if the match is finished (scores add up to 2 — see read_match_result).
    """
    try:
        sheets = get_tournament_sheets(tournament['url'], force_refresh=force_refresh)
        current, pending = get_player_matches(sheets, player, week)
        lines = [f"**🏆 {tournament['name']}**"]

        def fmt(name):
            if not builds:
                return name
            return f"{name} ({builds.get(normalize_name(name), '?')})"

        if current:
            lines.append(f"**Week {week} matches:**")
            for m in current:
                ph, opp = (m['player1'], m['player2']) if player_matches(player, m['player1']) else (m['player2'], m['player1'])
                status = " ✓" if m.get('finished') else ""
                lines.append(f"**{m['division']}** · {fmt(ph)} vs {fmt(opp)}{status}")
        else:
            lines.append("📅 No matches found for this week.")

        if pending:
            lines.append("")
            lines.append("**⏳ Pending matches:**")
            for m in pending:
                ph, opp = (m['player1'], m['player2']) if player_matches(player, m['player1']) else (m['player2'], m['player1'])
                lines.append(f"Wk {m['week']} · **{m['division']}** · {fmt(ph)} vs {fmt(opp)}")

        return split_message("\n".join(lines)), None
    except Exception as e:
        logger.error(f"Error building matches message for {tournament['name']}: {e}", exc_info=True)
        return None, f"Error in {tournament['name']}: {e}"


async def build_matches_message_async(tournament: dict, player: str, week: int,
                                       force_refresh: bool = False, builds: dict = None,
                                       timeout: float = DEFAULT_FETCH_TIMEOUT):
    return await asyncio.wait_for(
        asyncio.to_thread(build_matches_message, tournament, player, week, force_refresh, builds),
        timeout=timeout,
    )


# ------------------------------------------------------------
# Player mapping
# ------------------------------------------------------------
def load_player_mapping(sheet_url: str, force_refresh: bool = False) -> dict:
    mapping = {}
    if not force_refresh:
        cached = load_cached_sheets(sheet_url)
        if cached is not None and isinstance(cached, dict):
            return cached
    with _get_fetch_lock(sheet_url):
        if not force_refresh:
            cached = load_cached_sheets(sheet_url)
            if cached is not None and isinstance(cached, dict):
                return cached
        try:
            sheets = ggx.data_fromAllSheets(sheet_url)
            if not sheets:
                return mapping
            df = next(iter(sheets.values()))
            for idx, row in df.iterrows():
                player = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else None
                id_val = row.iloc[1]
                if pd.isna(id_val):
                    continue
                try:
                    discord_id = int(float(id_val))
                except (ValueError, TypeError):
                    id_str = str(id_val).strip()
                    if id_str.isdigit():
                        discord_id = int(id_str)
                    else:
                        continue
                if player and discord_id and player.lower() not in ["player name", "playername"]:
                    mapping[player] = discord_id
            save_cached_sheets(sheet_url, mapping)
        except Exception as e:
            logger.error(f"Error loading player mapping: {e}", exc_info=True)
    return mapping


async def load_player_mapping_async(sheet_url: str, force_refresh: bool = False,
                                     timeout: float = DEFAULT_FETCH_TIMEOUT) -> dict:
    return await asyncio.wait_for(
        asyncio.to_thread(load_player_mapping, sheet_url, force_refresh),
        timeout=timeout,
    )



def week_has_matches(sheets_dict: dict, week: int) -> bool:
    """Return True if at least one match row exists for the given week number."""
    for sheet_name, df in sheets_dict.items():
        if not is_division_sheet(df):
            continue
        current_week = None
        for idx, row in df.iterrows():
            cell = row.iloc[0]
            if pd.notna(cell) and str(cell).strip().startswith("Week"):
                current_week = parse_week_number(str(cell))
            if current_week != week:
                continue
            p1 = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""
            p2 = str(row.iloc[3]).strip() if len(row) > 3 and pd.notna(row.iloc[3]) else ""
            if p1 or p2:
                return True
    return False

async def send_dm_to_player(bot, discord_id: int, message_content: str) -> bool:
    try:
        user = await bot.fetch_user(discord_id)
        await user.send(message_content)
        return True
    except discord.Forbidden:
        return False
    except Exception as e:
        logger.error(f"Error sending DM to {discord_id}: {e}", exc_info=True)
        return False
    
# ------------------------------------------------------------
# Division standings
# ------------------------------------------------------------

def get_division_standings(sheets_dict: dict, division_name: str) -> tuple[list[list], list[str]]:
    """
    Reads the standings table from a division sheet, then re‑ranks players by:
      1. Points (descending)
      2. Matches played (ascending)
      3. Hero name (case‑insensitive alphabetical)

    Returns (sorted_standings, headers) where each row is [rank_str, hero, played_str, pts_str].
    """

    target_sheet = None
    for sheet_name in sheets_dict.keys():
        if sheet_name.lower() == division_name.lower():
            target_sheet = sheet_name
            break

    if not target_sheet:
        return None, None

    df = sheets_dict[target_sheet]

    def _clean(val) -> str:
        if not pd.notna(val):
            return ""
        s = str(val).strip()
        if s.lower() in ('nan', ''):
            return ""
        if s.endswith(".0") and s[:-2].lstrip('-').isdigit():
            s = s[:-2]
        return s

    def _map_cols(names_iter) -> dict:
        col_map = {}
        for c_idx, raw in enumerate(names_iter):
            v = str(raw).strip().lower() if pd.notna(raw) else ""
            if v == "rank":
                col_map['rank'] = c_idx
            elif v in ("hero", "hero name", "player & hero name", "player + hero"):
                col_map['hero'] = c_idx
            elif "points" in v or v == "pts":
                col_map['points'] = c_idx
            elif "played" in v or "matches" in v:
                col_map['played'] = c_idx
            elif v == "ancestry":
                col_map['ancestry'] = c_idx
            elif v in ("class", "hero class"):
                col_map['class'] = c_idx
        return col_map

    # Format A: column names already set (ggx CSV)
    col_map = _map_cols(df.columns)
    data_start = 0

    # Format B: header row is inside the data (xlsx / header=None)
    if 'rank' not in col_map or 'hero' not in col_map:
        col_map = {}
        data_start = None
        for idx, row in df.iterrows():
            trial = _map_cols(row)
            if 'rank' in trial and 'hero' in trial:
                col_map = trial
                data_start = idx + 1
                break

    if not col_map or 'rank' not in col_map or 'hero' not in col_map:
        logger.debug(
            f"get_division_standings({division_name!r}): "
            f"could not locate Rank/Hero header in sheet {target_sheet!r}"
        )
        return [], []

    standings = []
    rows_iter = (
        df.iterrows() if data_start == 0
        else ((i, df.iloc[i]) for i in range(data_start, len(df)))
    )
    for _, row in rows_iter:
        hero   = _clean(row.iloc[col_map['hero']])
        if not hero:
            break          # stop at blank hero row
        points = _clean(row.iloc[col_map['points']]) if 'points' in col_map else "0"
        played = _clean(row.iloc[col_map['played']]) if 'played' in col_map else "0"

        # safely convert to integers, fallback to 0
        try:
            pts_int = int(points) if points else 0
        except ValueError:
            pts_int = 0
        try:
            played_int = int(played) if played else 0
        except ValueError:
            played_int = 0

        standings.append([hero, played_int, pts_int])

    # Sort: pts desc, played asc, hero alpha asc
    standings.sort(key=lambda x: (-x[2], x[1], x[0].lower()))

    # Assign new ranks and convert back to string format
    ranked = []
    for i, (hero, played_int, pts_int) in enumerate(standings, start=1):
        ranked.append([str(i), hero, str(played_int), str(pts_int)])

    return ranked, ["#", "Player", "Played", "Pts"]


def find_player_division_rank(sheets_dict: dict, hero_key: str) -> tuple[str | None, int | None, int | None]:
    """
    Search every division sheet's standings for `hero_key` (a normalized
    "player-hero" string, e.g. from get_player_characters). Returns
    (division_name, rank, total_ranked) for the first division whose
    standings list contains it — total_ranked is how many entries that
    division's standings has, for display like "3rd out of 8" — or
    (None, None, None) if it isn't ranked in any division yet (e.g.
    registered but with no scored matches so far).
    """
    for sheet_name, df in sheets_dict.items():
        if not is_division_sheet(df):
            continue
        rows, _ = get_division_standings(sheets_dict, sheet_name)
        if not rows:
            continue
        for rank_str, hero, _played, _pts in rows:
            if normalize_name(hero) == hero_key:
                return sheet_name, int(rank_str), len(rows)
    return None, None, None


def get_player_division_record(sheets_dict: dict, division_name: str, hero_key: str) -> tuple[int, int]:
    """
    Count (played, points) for `hero_key` (a normalized "player-hero" string)
    across every week of `division_name`'s match schedule. A match counts as
    played only if read_match_result says it's finished (both scores in 0/1/2
    adding up to 2 — matches are best-of-two games). The "check" column is
    intentionally NOT used — it's edited by hand and isn't reliable.

    Each match awards the player their own score1/score2 value (0, 1, or 2 —
    a 1-1 draw is possible, not just a binary win/loss), so `points` is a
    sum, not a win count. Win% is `points / (played * 2)`, since 2 is the
    max a player can earn per match.
    """
    played = 0
    points = 0
    target_sheet = None
    for sheet_name in sheets_dict.keys():
        if sheet_name.lower() == division_name.lower():
            target_sheet = sheet_name
            break
    if not target_sheet:
        return played, points
    df = sheets_dict[target_sheet]

    start_row = None
    for idx, row in df.iterrows():
        if pd.notna(row.iloc[0]) and str(row.iloc[0]).strip().startswith("Week"):
            start_row = idx
            break
    if start_row is None:
        return played, points

    for idx in range(start_row, len(df)):
        row = df.iloc[idx]
        player1 = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""
        player2 = str(row.iloc[3]).strip() if len(row) > 3 and pd.notna(row.iloc[3]) else ""
        if not player1 and not player2:
            continue

        is_p1 = normalize_name(player1) == hero_key
        is_p2 = normalize_name(player2) == hero_key
        if not (is_p1 or is_p2):
            continue

        result = read_match_result(row)
        if not result["finished"]:
            continue

        played += 1
        my_score = result["score1"] if is_p1 else result["score2"]
        points += int(my_score)

    return played, points


# ------------------------------------------------------------
# Auto-detect most recent week from tournament sheets
# ------------------------------------------------------------

def get_latest_week_from_sheets(sheets: dict) -> int:
    """
    Scan all division sheets in a tournament and return the highest week number
    that contains at least one match row.
    Returns -1 if no matches found in any sheet.
    """
    max_week = -1
    for sheet_name, df in sheets.items():
        if not is_division_sheet(df):
            continue
        current_week = None
        for idx, row in df.iterrows():
            cell = row.iloc[0]
            if pd.notna(cell) and str(cell).strip().startswith("Week"):
                try:
                    current_week = parse_week_number(str(cell))
                except Exception:
                    continue
            if current_week is not None:
                p1 = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""
                p2 = str(row.iloc[3]).strip() if len(row) > 3 and pd.notna(row.iloc[3]) else ""
                if p1 or p2:
                    max_week = max(max_week, current_week)
    return max_week

def get_latest_week(tournaments: list[dict], guild_id: int) -> int:
    """
    Return the most recent week that has matches.
    Priority:
      1. Scheduled tasks (if any) for this guild.
      2. Fallback: scan all tournament sheets and take the max week.
    Falls back to -1 if no weeks found.
    """
    # ── Primary: scheduled tasks ──────────────────────────────────────
    try:
        from scheduler import list_tasks as get_scheduled_tasks
    except Exception:
        get_scheduled_tasks = None

    if get_scheduled_tasks is not None:
        try:
            tasks = get_scheduled_tasks(guild_id=guild_id)
            aliases = {t.get('alias', '').lower() for t in tournaments}
            aliases.update({t['name'].lower() for t in tournaments})

            max_week_from_schedule = -1
            for task in tasks:
                if task["current_week"] is not None and task["current_week"] != -1:
                    params = json.loads(task["params"])
                    task_tournament = params.get("tournament", "").lower()
                    if task_tournament in aliases:
                        max_week_from_schedule = max(max_week_from_schedule, task["current_week"])
            if max_week_from_schedule >= 0:
                return max_week_from_schedule
        except Exception as e:
            logger.warning(f"Could not read scheduler tasks for latest week: {e}")

    # ── Fallback: scan all sheets ──────────────────────────────────────
    max_week = -1
    for tourney in tournaments:
        try:
            sheets = get_tournament_sheets(tourney['url'], force_refresh=False)
            week = get_latest_week_from_sheets(sheets)
            if week > max_week:
                max_week = week
        except Exception as e:
            logger.warning(f"Error scanning for latest week in {tourney['name']}: {e}")
    return max_week