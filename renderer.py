"""Render BB-Terminal screens for the 7.5" 800x480 mono e-paper panel.

Mode '1' bitmap. Bit convention: 1 = white (no ink), 0 = black (ink) — matches
GxEPD2 and the UC8179 default mapping. PIL.Image.tobytes() on a mode '1' image
gives MSB-first packed pixels, which is what we send to the display.
"""
from __future__ import annotations

import datetime as dt
import io
from dataclasses import dataclass
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from data import Quote, Mover

W, H = 800, 480
TOP_BAR_H = 40
BOTTOM_BAR_H = 24
BODY_TOP = TOP_BAR_H
BODY_BOTTOM = H - BOTTOM_BAR_H

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


@dataclass
class Fonts:
    title: ImageFont.FreeTypeFont
    section: ImageFont.FreeTypeFont
    body: ImageFont.FreeTypeFont
    body_bold: ImageFont.FreeTypeFont
    small: ImageFont.FreeTypeFont
    big_num: ImageFont.FreeTypeFont


def _fonts() -> Fonts:
    return Fonts(
        title=ImageFont.truetype(FONT_BOLD, 22),
        section=ImageFont.truetype(FONT_BOLD, 14),
        body=ImageFont.truetype(FONT_REG, 13),
        body_bold=ImageFont.truetype(FONT_BOLD, 13),
        small=ImageFont.truetype(FONT_REG, 11),
        big_num=ImageFont.truetype(FONT_BOLD, 18),
    )


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 10000:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:,.2f}"
    return f"{v:,.2f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _sparkline(draw: ImageDraw.ImageDraw, values: Sequence[float], x: int, y: int, w: int, h: int) -> None:
    if len(values) < 2:
        return
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1.0
    pts: list[tuple[int, int]] = []
    n = len(values)
    for i, v in enumerate(values):
        px = x + int(i * (w - 1) / (n - 1))
        py = y + h - 1 - int((v - lo) / rng * (h - 1))
        pts.append((px, py))
    # Baseline (helps the eye)
    draw.line([(x, y + h - 1), (x + w - 1, y + h - 1)], fill=0, width=1)
    draw.line(pts, fill=0, width=1)
    # Mark the last point so the direction is obvious at small sizes
    lx, ly = pts[-1]
    draw.rectangle([(lx - 1, ly - 1), (lx + 1, ly + 1)], fill=0)


def _draw_top_bar(draw: ImageDraw.ImageDraw, fonts: Fonts, page_title: str, page_idx: int, page_total: int) -> None:
    # Inverted top bar: solid black band, white text
    draw.rectangle([(0, 0), (W, TOP_BAR_H)], fill=0)
    draw.text((10, 8), "BBTERMINAL", font=fonts.title, fill=1)
    draw.text((W // 2 - 60, 11), page_title, font=fonts.section, fill=1)
    ts = dt.datetime.now().strftime("%a %H:%M:%S")
    draw.text((W - 10 - int(fonts.section.getlength(ts)), 11), ts, font=fonts.section, fill=1)


def _draw_bottom_bar(draw: ImageDraw.ImageDraw, fonts: Fonts, page_idx: int, page_total: int, status: str = "") -> None:
    y = H - BOTTOM_BAR_H
    draw.line([(0, y), (W, y)], fill=0, width=1)
    page_txt = f"PG {page_idx + 1}/{page_total}"
    draw.text((10, y + 5), page_txt, font=fonts.small, fill=0)
    if status:
        draw.text((W // 2 - int(fonts.small.getlength(status)) // 2, y + 5), status, font=fonts.small, fill=0)
    gen = "GEN " + dt.datetime.now().strftime("%H:%M:%S")
    draw.text((W - 10 - int(fonts.small.getlength(gen)), y + 5), gen, font=fonts.small, fill=0)


def render_watch(quotes: Sequence[Quote], page_idx: int, page_total: int, status: str = "") -> Image.Image:
    img = Image.new("1", (W, H), 1)  # 1 = white
    draw = ImageDraw.Draw(img)
    fonts = _fonts()

    _draw_top_bar(draw, fonts, "WATCHLIST", page_idx, page_total)

    n = len(quotes)
    rows = (n + 1) // 2  # 2 columns
    cell_w = W // 2
    cell_h = (BODY_BOTTOM - BODY_TOP) // rows

    # Vertical divider between the two columns
    draw.line([(cell_w, BODY_TOP), (cell_w, BODY_BOTTOM)], fill=0, width=1)

    for i, q in enumerate(quotes):
        col = i % 2
        row = i // 2
        x = col * cell_w
        y = BODY_TOP + row * cell_h
        # Horizontal divider above each row (skip the first row)
        if row > 0:
            draw.line([(x + 2, y), (x + cell_w - 2, y)], fill=0, width=1)

        pad = 6
        # Layout within cell: SYMBOL (left, bold) | LAST | CHG% | SPARK
        sym_w = 90
        draw.text((x + pad, y + (cell_h - 18) // 2), q.sym, font=fonts.body_bold, fill=0)

        # Last price (right-aligned in a fixed slot)
        last_str = _fmt_price(q.last)
        price_x = x + sym_w + 75
        price_w = int(fonts.body.getlength(last_str))
        draw.text((price_x - price_w, y + (cell_h - 16) // 2), last_str, font=fonts.body, fill=0)

        # Change %
        pct_str = _fmt_pct(q.chg_pct)
        pct_x = x + sym_w + 145
        pct_w = int(fonts.body_bold.getlength(pct_str))
        # Invert background for movers >= 1% in either direction so the direction
        # punches through monochrome (we can't use color)
        if q.chg_pct is not None and abs(q.chg_pct) >= 1.0:
            bg = [(pct_x - pct_w - 4, y + 6), (pct_x + 2, y + cell_h - 6)]
            draw.rectangle(bg, fill=0)
            draw.text((pct_x - pct_w, y + (cell_h - 16) // 2), pct_str, font=fonts.body_bold, fill=1)
        else:
            draw.text((pct_x - pct_w, y + (cell_h - 16) // 2), pct_str, font=fonts.body_bold, fill=0)

        # Sparkline
        spark_x = x + sym_w + 155
        spark_w = cell_w - (sym_w + 155) - pad
        spark_h = cell_h - 12
        if q.series:
            _sparkline(draw, q.series, spark_x, y + 6, spark_w, spark_h)

    _draw_bottom_bar(draw, fonts, page_idx, page_total, status)
    return img


def render_cc(
    indices: Sequence[Quote],
    gainers: Sequence[Mover],
    losers: Sequence[Mover],
    page_idx: int,
    page_total: int,
    status: str = "",
) -> Image.Image:
    img = Image.new("1", (W, H), 1)
    draw = ImageDraw.Draw(img)
    fonts = _fonts()

    _draw_top_bar(draw, fonts, "COMMAND CENTER", page_idx, page_total)

    # --- US indices row (h=100) ---
    idx_top = BODY_TOP
    idx_bottom = idx_top + 100
    cell_w = W // max(len(indices), 1)
    for i, q in enumerate(indices):
        x = i * cell_w
        if i > 0:
            draw.line([(x, idx_top + 6), (x, idx_bottom - 6)], fill=0, width=1)
        draw.text((x + 10, idx_top + 6), q.name, font=fonts.section, fill=0)
        draw.text((x + 10, idx_top + 28), _fmt_price(q.last), font=fonts.big_num, fill=0)
        pct = _fmt_pct(q.chg_pct)
        if q.chg_pct is not None and abs(q.chg_pct) >= 0.5:
            pw = int(fonts.body_bold.getlength(pct))
            draw.rectangle([(x + 10, idx_top + 52), (x + 10 + pw + 6, idx_top + 70)], fill=0)
            draw.text((x + 13, idx_top + 54), pct, font=fonts.body_bold, fill=1)
        else:
            draw.text((x + 10, idx_top + 54), pct, font=fonts.body_bold, fill=0)
        if q.series:
            _sparkline(draw, q.series, x + 10, idx_top + 76, cell_w - 20, 18)
    draw.line([(0, idx_bottom), (W, idx_bottom)], fill=0, width=1)

    # --- Movers (h=316): two columns ---
    mov_top = idx_bottom
    mov_bottom = BODY_BOTTOM
    col_w = W // 2
    draw.line([(col_w, mov_top), (col_w, mov_bottom)], fill=0, width=1)

    for col, (label, items) in enumerate(
        [("TOP GAINERS", gainers), ("TOP LOSERS", losers)]
    ):
        x = col * col_w
        # Section header
        draw.rectangle([(x, mov_top), (x + col_w, mov_top + 22)], fill=0)
        draw.text((x + 10, mov_top + 3), label, font=fonts.section, fill=1)

        y = mov_top + 28
        row_h = (mov_bottom - mov_top - 30) // max(len(items), 1) if items else 30
        # Column x-coords within the cell: [sym 8..70] [name 70..n_end] [price ..pct_x] [pct .. col_w-6]
        sym_x = x + 8
        name_x = x + 70
        pct_pad = 6
        pct_box_w = 64  # fits "+99.99%" highlighted
        price_w = 60
        price_x_end = x + col_w - pct_pad - pct_box_w - 4
        name_x_end = price_x_end - price_w - 6
        max_name_chars = max(4, (name_x_end - name_x) // 8)  # ~8px per mono char at 13pt
        for j, m in enumerate(items):
            ry = y + j * row_h
            draw.text((sym_x, ry + 2), m.sym, font=fonts.body_bold, fill=0)
            name = m.name[: max_name_chars - 1] + ("…" if len(m.name) > max_name_chars else "")
            draw.text((name_x, ry + 4), name, font=fonts.body, fill=0)
            price = _fmt_price(m.price)
            pw_price = int(fonts.body.getlength(price))
            draw.text((price_x_end - pw_price, ry + 4), price, font=fonts.body, fill=0)
            pct = _fmt_pct(m.chg_pct)
            pw_pct = int(fonts.body_bold.getlength(pct))
            box_left = x + col_w - pct_pad - pw_pct - 6
            draw.rectangle([(box_left, ry + 1), (box_left + pw_pct + 6, ry + 22)], fill=0)
            draw.text((box_left + 3, ry + 4), pct, font=fonts.body_bold, fill=1)

    _draw_bottom_bar(draw, fonts, page_idx, page_total, status)
    return img


def image_to_packed_1bpp(img: Image.Image) -> bytes:
    """Return UC8179-style packed buffer: 48,000 bytes, MSB-first, 1=white 0=black."""
    if img.size != (W, H):
        img = img.resize((W, H))
    if img.mode != "1":
        img = img.convert("1")
    return img.tobytes()


def image_to_png_bytes(img: Image.Image) -> bytes:
    """Render as PNG for web preview (mode '1' is valid PNG)."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
