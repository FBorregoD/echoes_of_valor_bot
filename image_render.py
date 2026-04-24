"""
image_render.py — Generate Discord-ready matchup/standings images with Pillow.

Fonts are loaded from a 'fonts/' subdirectory next to this file,
with fallback to common system font paths. This ensures the bot works
on any deployment environment (Railway, Render, Fly.io, bare Linux, etc.).

Public API:
    render_matchups(title, week_label, current_rows, pending_rows) -> bytes (PNG)
    render_standings(title, rows, relegation_start) -> bytes (PNG)
    render_player_matches(player, week, tourney_results) -> bytes (PNG)
"""

from __future__ import annotations
import io
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# ── Font discovery ────────────────────────────────────────────────────────────

def _find_font(filename: str) -> str:
    """Locate a font file. Search order: bundled fonts/ dir, then system paths."""
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

# Resolve once at import time so errors are caught early
try:
    _PATH_R = _find_font("DejaVuSans.ttf")
    _PATH_B = _find_font("DejaVuSans-Bold.ttf")
except FileNotFoundError:
    # Try Liberation as fallback
    try:
        _PATH_R = _find_font("LiberationSans-Regular.ttf")
        _PATH_B = _find_font("LiberationSans-Bold.ttf")
    except FileNotFoundError:
        _PATH_R = _PATH_B = None   # will use Pillow's built-in bitmap font

import logging
_log = logging.getLogger(__name__)
if _PATH_R:
    _log.info(f"image_render: using font {_PATH_R}")
else:
    _log.warning("image_render: no TrueType font found, falling back to bitmap font")


# ── Palette (Discord dark theme) ──────────────────────────────────────────────
BG         = (49,  51,  56)
BG_ALT     = (43,  45,  49)
BG_HEAD    = (35,  36,  40)
ACCENT     = (88, 101, 242)
GOLD       = (255, 184,   0)
ORANGE     = (240, 160,   0)
TEXT       = (220, 221, 222)
TEXT_BUILD = (163, 166, 170)
TEXT_WHITE = (255, 255, 255)

# ── Layout constants ──────────────────────────────────────────────────────────
PAD      = 16
ROW_H    = 26
HDR_H    = 42
SEC_H    = 24
COL_GAP  = 12
VS_PADX  = 6
CORNER   = 4
BASE     = 14


# ── Font cache ────────────────────────────────────────────────────────────────
_font_cache: dict = {}

def _f(style: str, size: int) -> ImageFont.FreeTypeFont:
    key = (style, size)
    if key not in _font_cache:
        path = _PATH_B if style == 'bold' else _PATH_R
        if path:
            _font_cache[key] = ImageFont.truetype(path, size)
        else:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


# ── Drawing helpers ───────────────────────────────────────────────────────────

def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))

def _dummy_draw():
    img = Image.new('RGB', (1, 1))
    return img, ImageDraw.Draw(img)

def _cell_w(draw, name: str, build: str) -> int:
    """Width of 'name (build)' pair."""
    return (_tw(draw, name, _f('bold', BASE))
            + 5
            + _tw(draw, f"({build})", _f('regular', BASE - 1)))

def _draw_name_build(draw, x: int, y: int, name: str, build: str):
    """Draw 'name (build)' at position (x, y), used by matchups and standings."""
    draw.text((x, y + 5), name, font=_f('bold', BASE), fill=TEXT)
    nw = _tw(draw, name, _f('bold', BASE))
    draw.text((x + nw + 4, y + 6), f"({build})", font=_f('regular', BASE - 1), fill=TEXT_BUILD)

def _draw_vs(draw, cx: int, y: int, badge_w: int):
    bh = ROW_H - 8
    x0, x1 = cx - badge_w // 2, cx + badge_w // 2
    y0, y1 = y + (ROW_H - bh) // 2, y + (ROW_H - bh) // 2 + bh
    draw.rounded_rectangle([x0, y0, x1, y1], radius=CORNER, fill=ACCENT)
    vw = _tw(draw, "vs", _f('bold', BASE - 1))
    draw.text((x0 + (badge_w - vw) // 2, y0 + 2), "vs", font=_f('bold', BASE - 1), fill=TEXT_WHITE)

def _draw_section_header(draw, width: int, y: int, label: str, color: tuple):
    draw.rectangle([0, y, width, y + SEC_H], fill=BG_HEAD)
    draw.rectangle([0, y, 4, y + SEC_H], fill=color)
    if label:
        draw.text((PAD + 8, y + 4), label, font=_f('bold', BASE - 1), fill=color)


# ── Public render functions ───────────────────────────────────────────────────

def render_matchups(
    title: str,
    week_label: str,
    current_rows: list[tuple],  # (p1_name, p1_build, p2_name, p2_build)
    pending_rows: list[tuple],  # (week_num, p1_name, p1_build, p2_name, p2_build)
    min_width: int = 480,
) -> bytes:
    """Render matchup card. Returns PNG bytes."""
    _, dd = _dummy_draw()

    wk_col_w = _tw(dd, "Wk 9 ", _f('bold', BASE - 1)) + 6
    all_p1 = ([_cell_w(dd, r[0], r[1]) for r in current_rows] +
              [_cell_w(dd, r[1], r[2]) for r in pending_rows])
    all_p2 = ([_cell_w(dd, r[2], r[3]) for r in current_rows] +
              [_cell_w(dd, r[3], r[4]) for r in pending_rows])
    max_p1 = max(all_p1, default=120)
    max_p2 = max(all_p2, default=120)
    vs_w   = max(_tw(dd, "vs", _f('bold', BASE - 1)) + VS_PADX * 2, 26)

    row_w  = max_p1 + COL_GAP + vs_w + COL_GAP + max_p2
    pend_w = wk_col_w + row_w
    width  = max(min_width, max(row_w, pend_w) + PAD * 2)

    h = HDR_H
    if current_rows: h += SEC_H + len(current_rows) * ROW_H + 4
    if pending_rows: h += SEC_H + len(pending_rows) * ROW_H + 4
    h += 6

    img  = Image.new('RGB', (width, h), BG)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle([0, 0, width, HDR_H], fill=BG_HEAD)
    draw.rectangle([0, 0, 4, HDR_H], fill=ACCENT)
    draw.text((PAD + 8, HDR_H // 2 - 10), title,      font=_f('bold', BASE + 3), fill=TEXT_WHITE)
    wlw = _tw(draw, week_label, _f('bold', BASE))
    draw.text((width - PAD - wlw, HDR_H // 2 - 9), week_label, font=_f('bold', BASE), fill=GOLD)

    y = HDR_H

    def draw_rows(rows, is_pending: bool, label: str, color: tuple):
        nonlocal y
        _draw_section_header(draw, width, y, label, color)
        y += SEC_H
        p1_x = PAD + (wk_col_w if is_pending else 0)
        vs_cx = p1_x + max_p1 + COL_GAP + vs_w // 2
        p2_x  = vs_cx + vs_w // 2 + COL_GAP
        for i, row in enumerate(rows):
            draw.rectangle([0, y, width, y + ROW_H], fill=BG_ALT if i % 2 == 0 else BG)
            if is_pending:
                draw.text((PAD, y + 6), f"Wk {row[0]}", font=_f('bold', BASE - 1), fill=ORANGE)
                p1n, p1b, p2n, p2b = row[1], row[2], row[3], row[4]
            else:
                p1n, p1b, p2n, p2b = row[0], row[1], row[2], row[3]
            _draw_name_build(draw, p1_x, y, p1n, p1b)
            _draw_vs(draw, vs_cx, y, vs_w)
            _draw_name_build(draw, p2_x, y, p2n, p2b)
            y += ROW_H
        y += 4

    if current_rows:
        draw_rows(current_rows, False, f"Pairings — {week_label}", GOLD)
    if pending_rows:
        draw_rows(pending_rows, True, "Pending matches", ORANGE)

    draw.rectangle([0, h - 4, width, h], fill=ACCENT)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def render_standings(
    title: str,
    rows: list[tuple],
    # rows: (rank, player_name, played, pts) or (rank, player_name, played, pts, build)
    relegation_start: int | None = None,
    min_width: int = 380,
) -> bytes:
    """
    Render standings table as PNG. Build is shown on the same line as the name,
    e.g. 'Player (Ancestry Class)'. If no build info, only the name is shown.
    """
    _, dd = _dummy_draw()

    # Always use single-line row height
    row_h = ROW_H

    # Column widths
    rank_w = max(_tw(dd, str(r[0]), _f('bold', BASE)) for r in rows) + 8

    # Name column: if build is present, measure combined width
    has_build = any(len(r) >= 5 and r[4] for r in rows)
    name_widths = []
    for r in rows:
        name = r[1]
        build = r[4] if len(r) >= 5 else ""
        if build:
            name_widths.append(_cell_w(dd, name, build))
        else:
            name_widths.append(_tw(dd, name, _f('bold', BASE)))
    name_w = max(name_widths) + 12

    play_w = max(
        _tw(dd, "Played", _f('bold', BASE - 1)),
        max(_tw(dd, str(r[2]), _f('regular', BASE)) for r in rows)
    ) + 12
    pts_w = max(
        _tw(dd, "Pts", _f('bold', BASE - 1)),
        max(_tw(dd, str(r[3]), _f('bold', BASE)) for r in rows)
    ) + 12

    content_w = rank_w + name_w + play_w + pts_w
    width = max(min_width, content_w + PAD * 2)
    name_w += width - (content_w + PAD * 2)   # stretch name column to fill

    h = HDR_H + SEC_H + len(rows) * row_h + 10

    img  = Image.new('RGB', (width, h), BG)
    draw = ImageDraw.Draw(img)

    # Title header
    draw.rectangle([0, 0, width, HDR_H], fill=BG_HEAD)
    draw.rectangle([0, 0, 4, HDR_H], fill=ACCENT)
    draw.text((PAD + 8, HDR_H // 2 - 10), title, font=_f('bold', BASE + 3), fill=TEXT_WHITE)

    # Column header bar
    y = HDR_H
    _draw_section_header(draw, width, y, "", ACCENT)
    draw.text((PAD,                          y + 4), "#",      font=_f('bold', BASE - 1), fill=ACCENT)
    draw.text((PAD + rank_w,                 y + 4), "Player", font=_f('bold', BASE - 1), fill=ACCENT)
    draw.text((width - PAD - pts_w - play_w, y + 4), "Played", font=_f('bold', BASE - 1), fill=ACCENT)
    draw.text((width - PAD - pts_w,          y + 4), "Pts",    font=_f('bold', BASE - 1), fill=ACCENT)
    y += SEC_H

    rel_drawn = False
    for i, row_data in enumerate(rows):
        rank   = row_data[0]
        name   = row_data[1]
        played = row_data[2]
        pts    = row_data[3]
        build  = row_data[4] if len(row_data) >= 5 else ""

        # Relegation divider
        if relegation_start and not rel_drawn:
            try:
                if int(rank) >= relegation_start:
                    draw.rectangle([PAD, y, width - PAD, y + 1], fill=(200, 60, 60))
                    rel_drawn = True
            except ValueError:
                pass

        draw.rectangle([0, y, width, y + row_h], fill=BG_ALT if i % 2 == 0 else BG)

        # Rank
        rw = _tw(draw, str(rank), _f('bold', BASE))
        draw.text((PAD + (rank_w - rw) // 2, y + 5), str(rank), font=_f('bold', BASE), fill=TEXT_BUILD)

        # Player name (+ build in same line)
        nx = PAD + rank_w
        if build:
            _draw_name_build(draw, nx, y, name, build)
        else:
            draw.text((nx, y + 5), name, font=_f('bold', BASE), fill=TEXT)

        # Played
        pw = _tw(draw, str(played), _f('regular', BASE))
        draw.text((width - PAD - pts_w - play_w + (play_w - pw) // 2, y + 5),
                  str(played), font=_f('regular', BASE), fill=TEXT_BUILD)

        # Pts
        ptw = _tw(draw, str(pts), _f('bold', BASE))
        draw.text((width - PAD - pts_w + (pts_w - ptw) // 2, y + 5),
                  str(pts), font=_f('bold', BASE), fill=GOLD)

        y += row_h

    draw.rectangle([0, h - 4, width, h], fill=ACCENT)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def render_player_matches(
    player: str,
    week: int,
    tourney_results: list[dict],
) -> bytes:
    """
    Render per-player match results across tournaments as a PNG image.
    """
    _, dd = _dummy_draw()

    div_col_w = 0
    all_p1, all_p2 = [], []
    for t in tourney_results:
        for r in t['current']:
            div_col_w = max(div_col_w, _tw(dd, r[0], _f('bold', BASE - 1)) + 8)
            all_p1.append(_cell_w(dd, r[1], r[2]))
            all_p2.append(_cell_w(dd, r[3], r[4]))
        for r in t['pending']:
            div_col_w = max(div_col_w, _tw(dd, r[1], _f('bold', BASE - 1)) + 8)
            all_p1.append(_cell_w(dd, r[2], r[3]))
            all_p2.append(_cell_w(dd, r[4], r[5]))

    wk_col_w = _tw(dd, "Wk 9 ", _f('bold', BASE - 1)) + 6
    max_p1   = max(all_p1, default=120)
    max_p2   = max(all_p2, default=120)
    vs_w     = max(_tw(dd, "vs", _f('bold', BASE - 1)) + VS_PADX * 2, 26)

    row_w   = div_col_w + max_p1 + COL_GAP + vs_w + COL_GAP + max_p2
    pend_w  = wk_col_w + row_w
    width   = max(520, max(row_w, pend_w) + PAD * 2)

    h = HDR_H
    for t in tourney_results:
        n_cur  = len(t['current'])
        n_pend = len(t['pending'])
        if n_cur or n_pend:
            h += SEC_H
            if n_cur:  h += SEC_H + n_cur  * ROW_H + 2
            if n_pend: h += SEC_H + n_pend * ROW_H + 2
    h += 6

    img  = Image.new('RGB', (width, h), BG)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle([0, 0, width, HDR_H], fill=BG_HEAD)
    draw.rectangle([0, 0, 4, HDR_H], fill=ACCENT)
    title = f"Matches for {player}"
    draw.text((PAD + 8, HDR_H // 2 - 10), title, font=_f('bold', BASE + 3), fill=TEXT_WHITE)
    wl = f"Week {week}"
    wlw = _tw(draw, wl, _f('bold', BASE))
    draw.text((width - PAD - wlw, HDR_H // 2 - 9), wl, font=_f('bold', BASE), fill=GOLD)

    y = HDR_H

    def draw_match_row(row_data, is_pending: bool, i: int):
        nonlocal y
        draw.rectangle([0, y, width, y + ROW_H], fill=BG_ALT if i % 2 == 0 else BG)
        x = PAD
        if is_pending:
            draw.text((x, y + 6), f"Wk {row_data[0]}", font=_f('bold', BASE - 1), fill=ORANGE)
            x += wk_col_w
            div, p1n, p1b, p2n, p2b = row_data[1], row_data[2], row_data[3], row_data[4], row_data[5]
        else:
            div, p1n, p1b, p2n, p2b = row_data[0], row_data[1], row_data[2], row_data[3], row_data[4]

        dw = _tw(draw, div, _f('bold', BASE - 1))
        draw.text((x + (div_col_w - dw) // 2, y + 6), div, font=_f('bold', BASE - 1), fill=ACCENT)
        x += div_col_w

        vs_cx = x + max_p1 + COL_GAP + vs_w // 2
        p2_x  = vs_cx + vs_w // 2 + COL_GAP
        _draw_name_build(draw, x, y, p1n, p1b)
        _draw_vs(draw, vs_cx, y, vs_w)
        _draw_name_build(draw, p2_x, y, p2n, p2b)
        y += ROW_H

    for t in tourney_results:
        cur, pend = t['current'], t['pending']
        if not cur and not pend:
            continue

        draw.rectangle([0, y, width, y + SEC_H], fill=BG_HEAD)
        draw.rectangle([0, y, 4, y + SEC_H], fill=ACCENT)
        draw.text((PAD + 8, y + 4), t['tourney_name'], font=_f('bold', BASE), fill=TEXT_WHITE)
        y += SEC_H

        if cur:
            _draw_section_header(draw, width, y, f"Week {week} matches", GOLD)
            y += SEC_H
            for i, row in enumerate(cur):
                draw_match_row(row, False, i)
            y += 2

        if pend:
            _draw_section_header(draw, width, y, "Pending matches", ORANGE)
            y += SEC_H
            for i, row in enumerate(pend):
                draw_match_row(row, True, i)
            y += 2

    draw.rectangle([0, h - 4, width, h], fill=ACCENT)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()