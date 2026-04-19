import re
import time
import logging
import unicodedata

import pandas as pd
import Googlexcel_noPassword as ggx
import discord

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# In-memory cache (TTL-based, safe for ephemeral filesystems)
# ------------------------------------------------------------
CACHE_TTL = 300  # seconds (5 minutes)
_memory_cache: dict[str, tuple] = {}


def load_cached_sheets(url: str):
    entry = _memory_cache.get(url)
    if entry is not None:
        data, timestamp = entry
        if (time.time() - timestamp) < CACHE_TTL:
            return data
    return None


def save_cached_sheets(url: str, data):
    _memory_cache[url] = (data, time.time())


def invalidate_cache(url: str):
    _memory_cache.pop(url, None)


def get_tournament_sheets(tournament_url: str, force_refresh: bool = False):
    if not force_refresh:
        cached = load_cached_sheets(tournament_url)
        if cached is not None:
            return cached
    logger.info(f"Fetching sheets from: {tournament_url}")
    sheets = ggx.data_fromAllSheets(tournament_url)
    save_cached_sheets(tournament_url, sheets)
    return sheets


def refresh_tournament_cache(tournament_url: str):
    return get_tournament_sheets(tournament_url, force_refresh=True)


# ------------------------------------------------------------
# Helper: normalize names
# ------------------------------------------------------------
def normalize_name(name: str) -> str:
    """Normalize player name: lowercase, remove extra spaces, normalize hyphens."""
    if not name:
        return ""
    name = unicodedata.normalize('NFKD', str(name))
    name = name.encode('ascii', 'ignore').decode('ascii')
    name = re.sub(r'[–—−]', '-', name)
    name = re.sub(r'\s*-\s*', '-', name)
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


def load_hero_builds_from_sheets(sheets_dict: dict, sheet_name: str = None, column_mapping: dict = None):
    builds = {}
    target_sheet = find_hero_builds_sheet(sheets_dict, sheet_name)
    if not target_sheet:
        return builds
    df = sheets_dict[target_sheet]

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

    if col_indices.get('player_col') is None:
        return builds

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

        norm_key = normalize_name(player_hero)
        builds[norm_key] = f"{ancestry} {class_}".strip()

    return builds


# ------------------------------------------------------------
# Match extraction and formatting
# ------------------------------------------------------------
def parse_week_number(week_str: str):
    match = re.search(r'\d+', str(week_str))
    return int(match.group()) if match else None


def get_player_matches(sheets_dict: dict, player: str, target_week: int):
    current_matches = []
    pending_matches = []
    player_lower = player.lower()
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
            check = str(row.iloc[6]).strip() if len(row) > 6 and pd.notna(row.iloc[6]) else ""
            match = {
                "week": current_week,
                "player1": player1,
                "player2": player2,
                "division": sheet_name
            }
            if player_lower in player1.lower() or player_lower in player2.lower():
                if current_week == target_week:
                    current_matches.append(match)
                elif current_week < target_week and check != "OK":
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
        check = str(row.iloc[6]).strip() if len(row) > 6 and pd.notna(row.iloc[6]) else ""
        pair = tuple(sorted([normalize_name(player1), normalize_name(player2)]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        match = {
            "week": current_week,
            "player1": player1,
            "player2": player2,
            "division": target_sheet,
            "check": check
        }
        if current_week == target_week:
            current.append(match)
        elif current_week < target_week and check != "OK":
            pending.append(match)
    return current, pending


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
                ph, opp = (m['player1'], m['player2']) if player in m['player1'] else (m['player2'], m['player1'])
                lines.append(f"**{m['division']}** · {fmt(ph)} vs {fmt(opp)}")
        else:
            lines.append("📅 No matches found for this week.")

        if pending:
            lines.append("")
            lines.append("**⏳ Pending matches:**")
            for m in pending:
                ph, opp = (m['player1'], m['player2']) if player in m['player1'] else (m['player2'], m['player1'])
                lines.append(f"Wk {m['week']} · **{m['division']}** · {fmt(ph)} vs {fmt(opp)}")

        return split_message("\n".join(lines)), None
    except Exception as e:
        logger.error(f"Error building matches message for {tournament['name']}: {e}", exc_info=True)
        return None, f"Error in {tournament['name']}: {e}"


# ------------------------------------------------------------
# Player mapping
# ------------------------------------------------------------
def load_player_mapping(sheet_url: str, force_refresh: bool = False) -> dict:
    mapping = {}
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



def get_max_week(sheets_dict: dict) -> int | None:
    """Return the highest week number found across all division sheets."""
    max_week = None
    for sheet_name, df in sheets_dict.items():
        if not is_division_sheet(df):
            continue
        for idx, row in df.iterrows():
            cell = row.iloc[0]
            if pd.notna(cell) and str(cell).strip().startswith("Week"):
                w = parse_week_number(str(cell))
                if w is not None:
                    max_week = w if max_week is None else max(max_week, w)
    return max_week


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
    Reads the standings table from a division sheet.

    Handles two formats produced by different DataFrame sources:
      A) ggx / pd.read_csv — Google Sheets CSV omits the blank top row, so the
         row containing 'Rank', 'Hero', 'Points', 'Matches played' becomes the
         DataFrame column names.  Data rows start at index 0.
      B) pd.read_excel / header=None — 'Rank' etc. appear as cell values in a
         data row (row ~1).  Data rows start after that header row.

    The function detects the format automatically.
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
        return col_map

    # Format A: Rank/Hero are column names (ggx CSV format)
    col_map = _map_cols(df.columns)
    data_start = 0

    # Format B: Rank/Hero appear as cell values in a row (xlsx / header=None)
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

    logger.debug(
        f"get_division_standings({division_name!r}): "
        f"col_map={col_map}, data_start={data_start}"
    )

    standings = []
    rows_iter = (
        df.iterrows() if data_start == 0
        else ((i, df.iloc[i]) for i in range(data_start, len(df)))
    )
    for _, row in rows_iter:
        hero = _clean(row.iloc[col_map['hero']])
        if not hero:
            break
        rank   = _clean(row.iloc[col_map['rank']])
        points = _clean(row.iloc[col_map['points']]) if 'points' in col_map else ""
        played = _clean(row.iloc[col_map['played']]) if 'played' in col_map else ""
        standings.append([rank, hero, played, points])

    return standings, ["#", "Player", "Played", "Pts"]