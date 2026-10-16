"""
image_render.py — Generate Discord-ready matchup/standings images with Pillow.

Fonts are loaded from a 'fonts/' subdirectory next to this file,
with fallback to common system font paths. This ensures the bot works
on any deployment environment (Railway, Render, Fly.io, bare Linux, etc.).

Public API:
    render_matchups(title, week_label, current_rows, pending_rows, misreported_rows) -> bytes (PNG)
    render_standings(title, rows, relegation_start) -> bytes (PNG)
    set_scale(scale)  — configure global image scale factor (config)
"""

from __future__ import annotations
import io
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import math
import logging

_log = logging.getLogger(__name__)

# ── Font discovery ────────────────────────────────────────────────────────────

def _find_font(filename: str) -> str:
    here = Path(__file__).parent
    bundled = here / "fonts" / filename
    if bundled.exists():
        return str(bundled)

    system_dirs = [
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/truetype/liberation",
        "/usr/share/fonts/truetype/freefont",
        "/usr/share/fonts/TTF",
        "/usr/share/fonts/dejavu",
        "/System/Library/Fonts",          # macOS
        "C:/Windows/Fonts",               # Windows
    ]
    for d in system_dirs:
        p = Path(d) / filename
        if p.exists():
            return str(p)

    raise FileNotFoundError(
        f"Font '{filename}' not found. "
        f"Place DejaVuSans.ttf and DejaVuSans-Bold.ttf in the fonts/ directory."
    )

try:
    _PATH_R = _find_font("DejaVuSans.ttf")
    _PATH_B = _find_font("DejaVuSans-Bold.ttf")
except FileNotFoundError:
    try:
        _PATH_R = _find_font("LiberationSans-Regular.ttf")
        _PATH_B = _find_font("LiberationSans-Bold.ttf")
    except FileNotFoundError:
        _PATH_R = _PATH_B = None

if _PATH_R:
    _log.info(f"image_render: using font {_PATH_R}")
else:
    _log.warning("image_render: no TrueType font found, falling back to bitmap font")

# ── Global scale factor ───────────────────────────────────────────────────────
IMAGE_SCALE = 1.0

def set_scale(scale: float):
    global IMAGE_SCALE
    IMAGE_SCALE = max(0.5, min(2.0, scale))

# ── Palette ───────────────────────────────────────────────────────────────────
BG          = (49,  51,  56)
BG_ALT      = (43,  45,  49)
BG_HEAD     = (35,  36,  40)
ACCENT      = (88, 101, 242)
GOLD        = (255, 184,   0)
ORANGE      = (240, 160,   0)
TEXT        = (220, 221, 222)
TEXT_BUILD  = (163, 166, 170)
TEXT_WHITE  = (255, 255, 255)
CHECK_GREEN = (87, 242, 135)
MISR_COLOR  = (220, 50, 50)          # for misreported matches

# Division labels in !m images: a different blue per division so a player's
# matches can be told apart at a glance. Ordered lightest (top division) to
# deepest (bottom); divisions take them in the sheet's tab order.
DIVISION_BLUES = [
    (200, 230, 255),   # ice
    (135, 206, 250),   # sky
    ( 64, 196, 245),   # cyan
    ( 80, 160, 255),   # azure
    (110, 140, 250),   # cornflower
    (140, 130, 255),   # periwinkle
    ( 95, 110, 245),   # blurple
    ( 70, 120, 220),   # steel
]


def division_color(division: str, division_order: list[str]) -> tuple:
    """Blue for `division` by its position in `division_order` (the
    tournament's division sheets, top first), spread across DIVISION_BLUES.
    Unknown divisions fall back to ACCENT."""
    names = [d.lower() for d in division_order]
    if division.lower() not in names:
        return ACCENT
    i, n = names.index(division.lower()), len(names)
    if n == 1:
        return DIVISION_BLUES[0]
    return DIVISION_BLUES[round(i * (len(DIVISION_BLUES) - 1) / (n - 1))]

# ── Layout constants (scaled) ──────────────────────────────────────────────────
def _s(val: int) -> int:
    return max(1, round(val * IMAGE_SCALE))

PAD      = lambda: _s(16)
ROW_H    = lambda: _s(26)
# Match rows (pairings, !m) normally show each player on two lines — name,
# then build below it — so images stay ~550px wide: Discord shrinks anything
# wider to fit, which made the text of wide one-line images much smaller.
# Two lines double the height, though, so images with more rows than this
# fall back to the compact one-line layout ("Player (build)").
MATCH_ROW_H = lambda: _s(40)
TWO_LINE_MAX_ROWS = 15
HDR_H    = lambda: _s(42)
SEC_H    = lambda: _s(24)
COL_GAP  = lambda: _s(12)
VS_PADX  = lambda: _s(6)
CORNER   = lambda: _s(4)
BASE     = lambda: _s(14)

# ── Font cache ─────────────────────────────────────────────────────────────────
_font_cache: dict = {}

def _f(style: str, size: int) -> ImageFont.FreeTypeFont:
    scaled_size = _s(size)
    key = (style, scaled_size)
    if key not in _font_cache:
        path = _PATH_B if style == 'bold' else _PATH_R
        if path:
            _font_cache[key] = ImageFont.truetype(path, scaled_size)
        else:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]

# ── Drawing helpers ────────────────────────────────────────────────────────────

def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))

def _dummy_draw():
    img = Image.new('RGB', (1, 1))
    return img, ImageDraw.Draw(img)

def _cell_w(draw, name: str, build: str, two_line: bool = True) -> int:
    """Width of a player cell: two-line = the wider of name and build;
    one-line = "Name (build)"."""
    if two_line:
        return max(_tw(draw, name, _f('bold', BASE())),
                   _tw(draw, build, _f('regular', BASE() - 1)))
    return (_tw(draw, name, _f('bold', BASE()))
            + _s(5)
            + _tw(draw, f"({build})", _f('regular', BASE() - 1)))

def _draw_name_build(draw, x: int, y: int, name: str, build: str, two_line: bool = True):
    """Player name, with the build below it (two-line) or after it in brackets."""
    if two_line:
        draw.text((x, y + _s(4)), name, font=_f('bold', BASE()), fill=TEXT)
        draw.text((x, y + _s(21)), build, font=_f('regular', BASE() - 1), fill=TEXT_BUILD)
        return
    draw.text((x, y + _s(5)), name, font=_f('bold', BASE()), fill=TEXT)
    nw = _tw(draw, name, _f('bold', BASE()))
    draw.text((x + nw + _s(4), y + _s(6)), f"({build})", font=_f('regular', BASE() - 1), fill=TEXT_BUILD)

def _status_mark(finished: bool, misreported: bool) -> tuple[str, tuple] | None:
    """End-of-row mark for a current-week match: ✓ if finished, ⚠ if its
    result is misreported, nothing otherwise."""
    if finished:
        return " ✓", CHECK_GREEN
    if misreported:
        return " ⚠", MISR_COLOR
    return None

def _match_row_h(two_line: bool) -> int:
    return MATCH_ROW_H() if two_line else ROW_H()

def _row_text_y(y: int, row_h: int) -> int:
    """Top y for a single line of small text vertically centred in a row."""
    return y + (row_h - _s(15)) // 2

def _draw_vs(draw, cx: int, y: int, badge_w: int, row_h: int | None = None):
    row_h = row_h or ROW_H()
    bh = _s(18)
    x0, x1 = cx - badge_w // 2, cx + badge_w // 2
    y0, y1 = y + (row_h - bh) // 2, y + (row_h - bh) // 2 + bh
    draw.rounded_rectangle([x0, y0, x1, y1], radius=CORNER(), fill=ACCENT)
    vw = _tw(draw, "vs", _f('bold', BASE() - 1))
    draw.text((x0 + (badge_w - vw) // 2, y0 + _s(2)), "vs", font=_f('bold', BASE() - 1), fill=TEXT_WHITE)

def _draw_section_header(draw, width: int, y: int, label: str, color: tuple):
    sec_h = SEC_H()
    draw.rectangle([0, y, width, y + sec_h], fill=BG_HEAD)
    draw.rectangle([0, y, _s(4), y + sec_h], fill=color)
    if label:
        draw.text((PAD() + _s(8), y + _s(4)), label, font=_f('bold', BASE() - 1), fill=color)

# ── Public render functions ────────────────────────────────────────────────────

def render_matchups(
    title: str,
    week_label: str,
    current_rows: list[tuple],   # (p1_name, p1_build, p2_name, p2_build, finished)
    pending_rows: list[tuple],   # (week_num, p1_name, p1_build, p2_name, p2_build)
    misreported_rows: list[tuple] | None = None,  # same format as pending
    min_width: int | None = None,
) -> bytes:
    if misreported_rows is None:
        misreported_rows = []

    _, dd = _dummy_draw()
    two_line = len(current_rows) + len(pending_rows) + len(misreported_rows) <= TWO_LINE_MAX_ROWS
    row_h = _match_row_h(two_line)

    wk_col_w = _tw(dd, "Wk 9 ", _f('bold', BASE() - 1)) + _s(6)
    all_p1 = ([_cell_w(dd, r[0], r[1], two_line) for r in current_rows] +
              [_cell_w(dd, r[1], r[2], two_line) for r in pending_rows] +
              [_cell_w(dd, r[1], r[2], two_line) for r in misreported_rows])
    all_p2 = ([_cell_w(dd, r[2], r[3], two_line) for r in current_rows] +
              [_cell_w(dd, r[3], r[4], two_line) for r in pending_rows] +
              [_cell_w(dd, r[3], r[4], two_line) for r in misreported_rows])
    max_p1 = max(all_p1, default=_s(120))
    max_p2 = max(all_p2, default=_s(120))
    vs_w   = max(_tw(dd, "vs", _f('bold', BASE() - 1)) + VS_PADX() * 2, _s(26))

    row_w  = max_p1 + COL_GAP() + vs_w + COL_GAP() + max_p2
    check_w = _tw(dd, " ✓", _f('bold', BASE())) if any(r[4] for r in current_rows if len(r) > 4) else 0
    if check_w:
        row_w += check_w + _s(4)
    pend_w = wk_col_w + row_w
    width  = max(_s(520), max(row_w, pend_w) + PAD() * 2)

    h = HDR_H()
    if current_rows:      h += SEC_H() + len(current_rows) * row_h + _s(4)
    if pending_rows:      h += SEC_H() + len(pending_rows) * row_h + _s(4)
    if misreported_rows:  h += SEC_H() + len(misreported_rows) * row_h + _s(4)
    h += _s(6)

    img  = Image.new('RGB', (width, h), BG)
    draw = ImageDraw.Draw(img)

    # Header
    header_h = HDR_H()
    draw.rectangle([0, 0, width, header_h], fill=BG_HEAD)
    draw.rectangle([0, 0, _s(4), header_h], fill=ACCENT)
    draw.text((PAD() + _s(8), header_h // 2 - _s(10)), title, font=_f('bold', BASE() + 3), fill=TEXT_WHITE)
    wlw = _tw(draw, week_label, _f('bold', BASE()))
    draw.text((width - PAD() - wlw, header_h // 2 - _s(9)), week_label, font=_f('bold', BASE()), fill=GOLD)

    y = header_h

    def draw_rows(rows, is_pending: bool, label: str, color: tuple):
        nonlocal y
        _draw_section_header(draw, width, y, label, color)
        y += SEC_H()
        p1_x = PAD() + (wk_col_w if is_pending else 0)
        vs_cx = p1_x + max_p1 + COL_GAP() + vs_w // 2
        p2_x  = vs_cx + vs_w // 2 + COL_GAP()
        for i, row in enumerate(rows):
            draw.rectangle([0, y, width, y + row_h], fill=BG_ALT if i % 2 == 0 else BG)
            if is_pending:
                draw.text((PAD(), _row_text_y(y, row_h)), f"Wk {row[0]}", font=_f('bold', BASE() - 1), fill=color)
                p1n, p1b, p2n, p2b = row[1], row[2], row[3], row[4]
                finished = False
            else:
                p1n, p1b, p2n, p2b = row[0], row[1], row[2], row[3]
                finished = row[4] if len(row) > 4 else False
            _draw_name_build(draw, p1_x, y, p1n, p1b, two_line)
            _draw_vs(draw, vs_cx, y, vs_w, row_h)
            _draw_name_build(draw, p2_x, y, p2n, p2b, two_line)
            if finished and not is_pending:
                draw.text((p2_x + max_p2 + _s(4), _row_text_y(y, row_h)),
                          " ✓", font=_f('bold', BASE()), fill=CHECK_GREEN)
            y += row_h
        y += _s(4)

    if current_rows:
        draw_rows(current_rows, False, f"Pairings — {week_label}", GOLD)
    if pending_rows:
        draw_rows(pending_rows, True, "Pending matches", ORANGE)
    if misreported_rows:
        draw_rows(misreported_rows, True, "Misreported matches", MISR_COLOR)

    draw.rectangle([0, h - _s(4), width, h], fill=ACCENT)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def render_standings(
    title: str,
    rows: list[tuple],
    # rows: (rank, player_name, played, pts) or (rank, player_name, played, pts, build)
    relegation_start: int | None = None,
    min_width: int | None = None,
) -> bytes:
    if min_width is None:
        min_width = _s(380)
    _, dd = _dummy_draw()

    row_h = ROW_H()

    has_build = any(len(r) >= 5 and r[4] for r in rows)

    def player_display(r):
        name = r[1]
        build = r[4] if len(r) >= 5 and r[4] else ""
        if build:
            return f"{name} ({build})"
        return name

    rank_w = max(_tw(dd, str(r[0]), _f('bold', BASE())) for r in rows) + _s(8)
    name_w = max(_tw(dd, player_display(r), _f('bold', BASE())) for r in rows) + _s(12)

    play_w = max(
        _tw(dd, "Played", _f('bold', BASE() - 1)),
        max(_tw(dd, str(r[2]), _f('regular', BASE())) for r in rows)
    ) + _s(12)
    pts_w = max(
        _tw(dd, "Pts", _f('bold', BASE() - 1)),
        max(_tw(dd, str(r[3]), _f('bold', BASE())) for r in rows)
    ) + _s(12)

    content_w = rank_w + name_w + play_w + pts_w
    width = max(min_width, content_w + PAD() * 2)
    name_w += width - (content_w + PAD() * 2)

    h = HDR_H() + SEC_H() + len(rows) * row_h + _s(10)

    img  = Image.new('RGB', (width, h), BG)
    draw = ImageDraw.Draw(img)

    header_h = HDR_H()
    draw.rectangle([0, 0, width, header_h], fill=BG_HEAD)
    draw.rectangle([0, 0, _s(4), header_h], fill=ACCENT)
    draw.text((PAD() + _s(8), header_h // 2 - _s(10)), title, font=_f('bold', BASE() + 3), fill=TEXT_WHITE)

    y = header_h
    _draw_section_header(draw, width, y, "", ACCENT)
    sec_h = SEC_H()
    draw.text((PAD(),                          y + _s(4)), "#",      font=_f('bold', BASE() - 1), fill=ACCENT)
    draw.text((PAD() + rank_w,                 y + _s(4)), "Player", font=_f('bold', BASE() - 1), fill=ACCENT)
    draw.text((width - PAD() - pts_w - play_w, y + _s(4)), "Played", font=_f('bold', BASE() - 1), fill=ACCENT)
    draw.text((width - PAD() - pts_w,          y + _s(4)), "Pts",    font=_f('bold', BASE() - 1), fill=ACCENT)
    y += sec_h

    rel_drawn = False
    for i, r in enumerate(rows):
        rank   = r[0]
        name_display = player_display(r)
        played = r[2]
        pts    = r[3]

        if relegation_start and not rel_drawn:
            try:
                if int(rank) >= relegation_start:
                    draw.rectangle([PAD(), y, width - PAD(), y + _s(1)], fill=(200, 60, 60))
                    rel_drawn = True
            except ValueError:
                pass

        draw.rectangle([0, y, width, y + row_h], fill=BG_ALT if i % 2 == 0 else BG)

        rw = _tw(draw, str(rank), _f('bold', BASE()))
        ry = y + (row_h - BASE()) // 2 - _s(1)
        draw.text((PAD() + (rank_w - rw) // 2, ry), str(rank), font=_f('bold', BASE()), fill=TEXT_BUILD)

        nx = PAD() + rank_w
        draw.text((nx, ry), name_display, font=_f('bold', BASE()), fill=TEXT)

        pw = _tw(draw, str(played), _f('regular', BASE()))
        draw.text((width - PAD() - pts_w - play_w + (play_w - pw) // 2, ry),
                  str(played), font=_f('regular', BASE()), fill=TEXT_BUILD)

        ptw = _tw(draw, str(pts), _f('bold', BASE()))
        draw.text((width - PAD() - pts_w + (pts_w - ptw) // 2, ry),
                  str(pts), font=_f('bold', BASE()), fill=GOLD)

        y += row_h

    draw.rectangle([0, h - _s(4), width, h], fill=ACCENT)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def render_player_matches(
    player: str,
    tourney_results: list[dict],
) -> bytes:
    """
    Render a player's matches across multiple tournaments.

    tourney_results: list of dicts with keys:
        {
          'tourney_name': str,
          'week': int,                    # week of this tournament
          'season_complete': bool,        # True if no more matches
          'current': [(division, p1_name, p1_build, p2_name, p2_build, finished[, misreported]), ...],
          'pending': [(week_num, division, p1_name, p1_build, p2_name, p2_build), ...],
          'misreported': [...],
        }
    """
    _log.debug(f"render_player_matches called for {player} with {len(tourney_results)} tournaments")

    _, dd = _dummy_draw()
    total_rows = sum(
        len(t.get('current', [])) + len(t.get('pending', [])) + len(t.get('misreported', []))
        for t in tourney_results if not t.get('season_complete', False)
    )
    two_line = total_rows <= TWO_LINE_MAX_ROWS
    row_h = _match_row_h(two_line)

    # Calculate widths
    div_col_w = 0
    all_p1, all_p2 = [], []

    for t in tourney_results:
        _log.debug(f"Processing tournament: {t.get('tourney_name')}, season_complete={t.get('season_complete')}")

        if t.get('season_complete', False):
            continue

        current = t.get('current', [])
        pending = t.get('pending', [])
        misreported = t.get('misreported', [])

        for r in current:
            if len(r) < 5:
                _log.warning(f"Current row too short: {r}")
                continue
            div_col_w = max(div_col_w, _tw(dd, r[0], _f('bold', BASE() - 1)) + _s(8))
            all_p1.append(_cell_w(dd, r[1], r[2], two_line))
            all_p2.append(_cell_w(dd, r[3], r[4], two_line))

        for r in pending:
            if len(r) < 6:
                _log.warning(f"Pending row too short: {r}")
                continue
            div_col_w = max(div_col_w, _tw(dd, r[1], _f('bold', BASE() - 1)) + _s(8))
            all_p1.append(_cell_w(dd, r[2], r[3], two_line))
            all_p2.append(_cell_w(dd, r[4], r[5], two_line))

        for r in misreported:
            if len(r) < 6:
                _log.warning(f"Misreported row too short: {r}")
                continue
            div_col_w = max(div_col_w, _tw(dd, r[1], _f('bold', BASE() - 1)) + _s(8))
            all_p1.append(_cell_w(dd, r[2], r[3], two_line))
            all_p2.append(_cell_w(dd, r[4], r[5], two_line))

    wk_col_w = _tw(dd, "Wk 9 ", _f('bold', BASE() - 1)) + _s(6)
    max_p1 = max(all_p1, default=_s(120))
    max_p2 = max(all_p2, default=_s(120))
    vs_w = max(_tw(dd, "vs", _f('bold', BASE() - 1)) + VS_PADX() * 2, _s(26))

    # Two-line layout: pending/misreported rows stack "Wk N" above the
    # division in the same column, so they're no wider than current rows.
    # One-line layout: "Wk N" gets its own column before the division.
    div_col_w = (max(div_col_w, wk_col_w) if two_line else div_col_w) + COL_GAP()
    has_wk_rows = any(t.get('pending') or t.get('misreported')
                      for t in tourney_results if not t.get('season_complete', False))

    row_w = div_col_w + max_p1 + COL_GAP() + vs_w + COL_GAP() + max_p2
    check_w = 0
    for t in tourney_results:
        for r in t.get('current', []):
            mark = _status_mark(len(r) > 5 and r[5], len(r) > 6 and r[6])
            if mark:
                check_w = max(check_w, _tw(dd, mark[0], _f('bold', BASE())))
    if check_w:
        row_w += check_w + _s(4)
    if not two_line and has_wk_rows:
        row_w += wk_col_w
    width = max(_s(520), row_w + PAD() * 2)

    # Calculate height
    h = HDR_H()
    for t in tourney_results:
        n_cur = len(t.get('current', []))
        n_pend = len(t.get('pending', []))
        n_mis = len(t.get('misreported', []))

        if t.get('season_complete', False):
            h += SEC_H()  # only the complete message
        elif n_cur or n_pend or n_mis:
            h += SEC_H()  # tournament label
            if n_cur:
                h += SEC_H() + n_cur * row_h + _s(2)
            if n_pend:
                h += SEC_H() + n_pend * row_h + _s(2)
            if n_mis:
                h += SEC_H() + n_mis * row_h + _s(2)
    h += _s(6)

    _log.debug(f"Image dimensions: {width}x{h}")

    # Create image
    img = Image.new('RGB', (width, h), BG)
    draw = ImageDraw.Draw(img)

    # Header
    header_h = HDR_H()
    draw.rectangle([0, 0, width, header_h], fill=BG_HEAD)
    draw.rectangle([0, 0, _s(4), header_h], fill=ACCENT)
    title_str = f"Matches for {player}"
    draw.text((PAD() + _s(8), header_h // 2 - _s(10)), title_str, font=_f('bold', BASE() + 3), fill=TEXT_WHITE)

    y = header_h

    def draw_match_row(row_data, is_pending: bool, i: int, division_order: list[str], color=ORANGE):
        nonlocal y
        draw.rectangle([0, y, width, y + row_h], fill=BG_ALT if i % 2 == 0 else BG)
        x = PAD()
        small = _f('bold', BASE() - 1)
        if is_pending:
            div, p1n, p1b, p2n, p2b = row_data[1], row_data[2], row_data[3], row_data[4], row_data[5]
            finished = misreported = False
            wk = f"Wk {row_data[0]}"
            if two_line:
                draw.text((x + (div_col_w - _tw(draw, wk, small)) // 2, y + _s(4)), wk, font=small, fill=color)
                div_y = y + _s(21)
            else:
                draw.text((x, _row_text_y(y, row_h)), wk, font=small, fill=color)
                x += wk_col_w
                div_y = _row_text_y(y, row_h)
        else:
            div, p1n, p1b, p2n, p2b = row_data[0], row_data[1], row_data[2], row_data[3], row_data[4]
            finished = row_data[5] if len(row_data) > 5 else False
            misreported = row_data[6] if len(row_data) > 6 else False
            div_y = _row_text_y(y, row_h)

        draw.text((x + (div_col_w - _tw(draw, div, small)) // 2, div_y), div, font=small,
                  fill=division_color(div, division_order))
        x += div_col_w

        vs_cx = x + max_p1 + COL_GAP() + vs_w // 2
        p2_x = vs_cx + vs_w // 2 + COL_GAP()
        _draw_name_build(draw, x, y, p1n, p1b, two_line)
        _draw_vs(draw, vs_cx, y, vs_w, row_h)
        _draw_name_build(draw, p2_x, y, p2n, p2b, two_line)
        mark = None if is_pending else _status_mark(finished, misreported)
        if mark:
            draw.text((p2_x + max_p2 + _s(4), _row_text_y(y, row_h)),
                      mark[0], font=_f('bold', BASE()), fill=mark[1])
        y += row_h

    # Draw tournaments
    for t in tourney_results:
        _log.debug(f"Drawing tournament: {t.get('tourney_name')}")

        if t.get('season_complete', False):
            sec_h = SEC_H()
            draw.rectangle([0, y, width, y + sec_h], fill=BG_HEAD)
            draw.rectangle([0, y, _s(4), y + sec_h], fill=ACCENT)
            draw.text((PAD() + _s(8), y + _s(4)),
                     f"{t.get('tourney_name', 'Unknown')} — 🏁 Season complete",
                     font=_f('bold', BASE()), fill=TEXT_WHITE)
            y += sec_h
            continue

        cur = t.get('current', [])
        pend = t.get('pending', [])
        mis = t.get('misreported', [])

        if not cur and not pend and not mis:
            continue

        # Tournament section header with week
        sec_h = SEC_H()
        draw.rectangle([0, y, width, y + sec_h], fill=BG_HEAD)
        draw.rectangle([0, y, _s(4), y + sec_h], fill=ACCENT)
        week = t.get('week', '?')
        draw.text((PAD() + _s(8), y + _s(4)),
                 f"{t.get('tourney_name', 'Unknown')} — Week {week}",
                 font=_f('bold', BASE()), fill=TEXT_WHITE)
        y += sec_h

        division_order = t.get('division_order', [])
        if cur:
            _draw_section_header(draw, width, y, f"Week {week} matches", GOLD)
            y += SEC_H()
            for i, row in enumerate(cur):
                draw_match_row(row, False, i, division_order)
            y += _s(2)

        if pend:
            _draw_section_header(draw, width, y, "Pending matches", ORANGE)
            y += SEC_H()
            for i, row in enumerate(pend):
                draw_match_row(row, True, i, division_order, color=ORANGE)
            y += _s(2)

        if mis:
            _draw_section_header(draw, width, y, "Misreported matches", MISR_COLOR)
            y += SEC_H()
            for i, row in enumerate(mis):
                draw_match_row(row, True, i, division_order, color=MISR_COLOR)
            y += _s(2)

    draw.rectangle([0, h - _s(4), width, h], fill=ACCENT)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    _log.debug("Image rendered successfully")
    return buf.getvalue()