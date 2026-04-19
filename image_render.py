"""
image_render.py — Generate Discord-ready matchup/standings images with Pillow.

All public functions return bytes (PNG) ready to pass to discord.File().
"""

from __future__ import annotations
import io
from PIL import Image, ImageDraw, ImageFont

# ── Palette (Discord dark theme) ──────────────────────────────────────────────
BG         = (49,  51,  56)
BG_ALT     = (43,  45,  49)
BG_HEAD    = (35,  36,  40)
ACCENT     = (88, 101, 242)
GOLD       = (255, 184,   0)
ORANGE     = (240, 160,   0)
GREEN      = (87,  242, 135)
TEXT       = (220, 221, 222)
TEXT_BUILD = (163, 166, 170)
TEXT_WHITE = (255, 255, 255)

# ── Fonts ─────────────────────────────────────────────────────────────────────
_FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

_font_cache: dict = {}

def _f(style: str, size: int) -> ImageFont.FreeTypeFont:
    key = (style, size)
    if key not in _font_cache:
        path = _FONT_B if style == 'bold' else _FONT_R
        _font_cache[key] = ImageFont.truetype(path, size)
    return _font_cache[key]


# ── Layout constants ──────────────────────────────────────────────────────────
PAD      = 16
ROW_H    = 26
HDR_H    = 42
SEC_H    = 24
COL_GAP  = 12
VS_PADX  = 6
CORNER   = 4
BASE     = 14   # base font size


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def _dummy_draw() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new('RGB', (1, 1))
    return img, ImageDraw.Draw(img)


def _cell_w(draw, name: str, build: str) -> int:
    nw = _tw(draw, name, _f('bold', BASE))
    bw = _tw(draw, f"({build})", _f('regular', BASE - 1))
    return nw + 5 + bw


def _draw_name_build(draw, x: int, y: int, name: str, build: str):
    draw.text((x, y + 5), name, font=_f('bold', BASE), fill=TEXT)
    nw = _tw(draw, name, _f('bold', BASE))
    draw.text((x + nw + 4, y + 6), f"({build})", font=_f('regular', BASE - 1), fill=TEXT_BUILD)


def _draw_vs(draw, cx: int, y: int, badge_w: int):
    bh = ROW_H - 8
    x0 = cx - badge_w // 2
    x1 = x0 + badge_w
    y0 = y + (ROW_H - bh) // 2
    y1 = y0 + bh
    draw.rounded_rectangle([x0, y0, x1, y1], radius=CORNER, fill=ACCENT)
    vw = _tw(draw, "vs", _f('bold', BASE - 1))
    draw.text((x0 + (badge_w - vw) // 2, y0 + 2), "vs", font=_f('bold', BASE - 1), fill=TEXT_WHITE)


def _draw_section_header(draw, width: int, y: int, label: str, color: tuple):
    draw.rectangle([0, y, width, y + SEC_H], fill=BG_HEAD)
    draw.rectangle([0, y, 4, y + SEC_H], fill=color)
    draw.text((PAD + 8, y + 4), label, font=_f('bold', BASE - 1), fill=color)


# ── Main render functions ─────────────────────────────────────────────────────

def render_matchups(
    title: str,
    week_label: str,
    current_rows: list[tuple],  # (p1_name, p1_build, p2_name, p2_build)
    pending_rows: list[tuple],  # (week_num, p1_name, p1_build, p2_name, p2_build)
    min_width: int = 480,
) -> bytes:
    """
    Render a matchup card image and return PNG bytes.
    Pass to discord.File(io.BytesIO(bytes), filename='matchups.png').
    """
    _, dd = _dummy_draw()

    wk_col_w = _tw(dd, "Wk 9 ", _f('bold', BASE - 1)) + 6

    all_p1 = (
        [_cell_w(dd, r[0], r[1]) for r in current_rows] +
        [_cell_w(dd, r[1], r[2]) for r in pending_rows]
    )
    all_p2 = (
        [_cell_w(dd, r[2], r[3]) for r in current_rows] +
        [_cell_w(dd, r[3], r[4]) for r in pending_rows]
    )
    max_p1 = max(all_p1, default=120)
    max_p2 = max(all_p2, default=120)

    vs_w = max(_tw(dd, "vs", _f('bold', BASE - 1)) + VS_PADX * 2, 26)

    row_w    = max_p1 + COL_GAP + vs_w + COL_GAP + max_p2
    pend_w   = wk_col_w + row_w
    width    = max(min_width, max(row_w, pend_w) + PAD * 2)

    # height
    h = HDR_H
    if current_rows: h += SEC_H + len(current_rows) * ROW_H + 4
    if pending_rows: h += SEC_H + len(pending_rows) * ROW_H + 4
    h += 6  # bottom accent bar

    img  = Image.new('RGB', (width, h), BG)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle([0, 0, width, HDR_H], fill=BG_HEAD)
    draw.rectangle([0, 0, 4, HDR_H], fill=ACCENT)
    draw.text((PAD + 8, HDR_H // 2 - 10), title, font=_f('bold', BASE + 3), fill=TEXT_WHITE)
    wlw = _tw(draw, week_label, _f('bold', BASE))
    draw.text((width - PAD - wlw, HDR_H // 2 - 9), week_label, font=_f('bold', BASE), fill=GOLD)

    y = HDR_H

    def draw_rows(rows, is_pending: bool, section_label: str, color: tuple):
        nonlocal y
        _draw_section_header(draw, width, y, section_label, color)
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

    # Bottom accent bar
    draw.rectangle([0, h - 4, width, h], fill=ACCENT)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def render_standings(
    title: str,
    rows: list[tuple],   # (rank, hero_name, played, points)
    relegation_start: int | None = None,  # rank number where relegation begins
    min_width: int = 380,
) -> bytes:
    """
    Render a standings table image and return PNG bytes.
    rows: list of (rank, player_name, played, pts)
    """
    _, dd = _dummy_draw()

    # Column widths
    rank_w  = max(_tw(dd, str(r[0]), _f('bold', BASE)) for r in rows) + 8
    name_w  = max(_tw(dd, r[1], _f('bold', BASE)) for r in rows) + 12
    play_w  = max(_tw(dd, "Played", _f('bold', BASE - 1)),
                  max(_tw(dd, str(r[2]), _f('regular', BASE)) for r in rows)) + 12
    pts_w   = max(_tw(dd, "Pts", _f('bold', BASE - 1)),
                  max(_tw(dd, str(r[3]), _f('bold', BASE)) for r in rows)) + 12

    content_w = rank_w + name_w + play_w + pts_w
    width = max(min_width, content_w + PAD * 2)

    # Stretch name column to fill available width
    name_w += width - (content_w + PAD * 2)

    h = HDR_H + SEC_H + len(rows) * ROW_H + 10

    img  = Image.new('RGB', (width, h), BG)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle([0, 0, width, HDR_H], fill=BG_HEAD)
    draw.rectangle([0, 0, 4, HDR_H], fill=ACCENT)
    draw.text((PAD + 8, HDR_H // 2 - 10), title, font=_f('bold', BASE + 3), fill=TEXT_WHITE)

    # Column headers
    y = HDR_H
    _draw_section_header(draw, width, y, "", ACCENT)
    cx = PAD
    draw.text((cx, y + 4), "#", font=_f('bold', BASE - 1), fill=ACCENT)
    cx += rank_w
    draw.text((cx, y + 4), "Player", font=_f('bold', BASE - 1), fill=ACCENT)
    draw.text((width - PAD - pts_w - play_w, y + 4), "Played", font=_f('bold', BASE - 1), fill=ACCENT)
    draw.text((width - PAD - pts_w, y + 4), "Pts", font=_f('bold', BASE - 1), fill=ACCENT)
    y += SEC_H

    rel_drawn = False
    for i, (rank, name, played, pts) in enumerate(rows):
        # Relegation zone divider
        if relegation_start and int(rank) >= relegation_start and not rel_drawn:
            draw.rectangle([PAD, y, width - PAD, y + 1], fill=(200, 60, 60))
            rel_drawn = True

        bg = BG_ALT if i % 2 == 0 else BG
        draw.rectangle([0, y, width, y + ROW_H], fill=bg)

        cx = PAD
        # Rank badge
        rw = _tw(draw, str(rank), _f('bold', BASE))
        draw.text((cx + (rank_w - rw) // 2, y + 5), str(rank), font=_f('bold', BASE), fill=TEXT_BUILD)
        cx += rank_w

        # Name
        draw.text((cx, y + 5), name, font=_f('bold', BASE), fill=TEXT)
        cx += name_w

        # Played (right-aligned in its column)
        pw = _tw(draw, str(played), _f('regular', BASE))
        draw.text((cx + (play_w - pw) // 2, y + 5), str(played), font=_f('regular', BASE), fill=TEXT_BUILD)

        # Points (bold, right-aligned)
        ptw = _tw(draw, str(pts), _f('bold', BASE))
        draw.text((width - PAD - pts_w + (pts_w - ptw) // 2, y + 5), str(pts), font=_f('bold', BASE), fill=GOLD)

        y += ROW_H

    draw.rectangle([0, h - 4, width, h], fill=ACCENT)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()
