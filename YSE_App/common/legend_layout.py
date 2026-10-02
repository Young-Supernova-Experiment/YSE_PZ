"""Column layout for the Bokeh light-curve legends (#226, #369, #372).

Each legend column is one instrument (``PS1``, ``DECam`` ... see
``YSE_App/common/band_order.py`` for the order) and reads down, bluest band
first.  Bokeh 2.4.2 has no ``Legend.ncols`` and a horizontal ``Legend``
pads every cell to the same ``label_width``, so stacking horizontal rows
made every column as wide as the longest label on the plot and the legend
wrapped while there was still room (#372).  Instead each column is its own
vertical ``Legend`` placed ``below`` the plot at an explicit x offset, and
columns are packed left to right with their own widths, wrapping to a new
block only when the next column would not fit.  This module holds the pure
arithmetic so it can be unit-tested without Bokeh.

Column rule::

    label_px  = sum of CHAR_WIDTH_PX over the label's characters, rounded up
    column_px = GLYPH_WIDTH + LABEL_STANDOFF + max(label_px over the column)
    usable    = plot_width - LEFT_AXIS_WIDTH_PX - TOOLBAR_WIDTH_PX - RIGHT_MARGIN_PX
    a column starts at x = previous column's right edge + COLUMN_GAP, or on a
    new block at x = 0 when that right edge would pass ``usable``

``plot_width`` is the figure's nominal width: the page passes the real
container width as ``?w=`` (see :func:`requested_plot_width`) so the
columns match what the viewer sees instead of the 400 px default.

Vertical placement: every side-panel legend gets its own panel whose height
is ``legend_height + 2 * margin``, and a tuple ``location`` is measured from
that panel's bottom-left corner.  A column legend therefore uses
``margin = -height / 2`` (its panel collapses to zero height, so every
column of a block shares the same top edge) and ``location = (x, 2 * margin)``
(so it is drawn from the panel's top edge downward); an empty legend with a
positive margin follows each block to reserve the block's height
(:func:`legend_column_margin`, :func:`legend_block_height_px`).
"""

from __future__ import annotations

import math

# Advance widths of 13 px Helvetica/Arial (Bokeh's default legend font)
# glyphs, by width class, rounded up a little; anything else counts as
# DEFAULT_CHAR_WIDTH_PX.  A label's width is the sum, rounded up.
_CHAR_WIDTH_CLASSES = (
    (3.0, "ijl"),
    (3.7, "ftI ./:"),
    (4.4, "r()-"),
    (6.6, "cksvxyzJ"),
    (7.3, "abdeghnopqu0123456789L_"),
    (8.0, "FTZ+"),
    (8.8, "ABEKPSVXY"),
    (9.5, "wCDHNRU"),
    (10.2, "GOQ"),
    (10.9, "mM"),
    (12.4, "W"),
)
CHAR_WIDTH_PX = {char: width for width, chars in _CHAR_WIDTH_CLASSES for char in chars}
DEFAULT_CHAR_WIDTH_PX = 7.5
GLYPH_WIDTH = 20
# One legend row: label_height = glyph_height, no spacing between rows.
GLYPH_HEIGHT = 20
LABEL_STANDOFF = 5
# Horizontal gap between columns.
COLUMN_GAP = 8
# Vertical gap below each block of columns.
BLOCK_GAP = 6
# The legend panel starts at the frame's left edge, right of the y-axis
# ticks and label, and stops short of the right-hand toolbar.
LEFT_AXIS_WIDTH_PX = 50
TOOLBAR_WIDTH_PX = 30
RIGHT_MARGIN_PX = 4
# Accepted range for a page-supplied width, and the bucket it is rounded to
# (so the plot-HTML cache does not fragment on every pixel).
MIN_PLOT_WIDTH = 320
MAX_PLOT_WIDTH = 1600
PLOT_WIDTH_BUCKET = 40


def label_width_px(label) -> int:
    """Estimated pixel width of one legend label in the 13 px legend font."""
    return math.ceil(sum(CHAR_WIDTH_PX.get(char, DEFAULT_CHAR_WIDTH_PX) for char in str(label)))


def legend_label_width_px(labels) -> int:
    """Pixel width the labels of one column are padded to (its widest label)."""
    return max((label_width_px(label) for label in labels), default=0)


def legend_column_width_px(labels) -> int:
    """Pixel width of one legend column: marker, standoff and its longest label."""
    return GLYPH_WIDTH + LABEL_STANDOFF + legend_label_width_px(labels)


def legend_usable_width_px(plot_width: int) -> int:
    """Horizontal room for legend columns in a plot of nominal width ``plot_width``."""
    return max(1, int(plot_width) - LEFT_AXIS_WIDTH_PX - TOOLBAR_WIDTH_PX - RIGHT_MARGIN_PX)


def legend_column_blocks(columns, plot_width: int) -> list[list[tuple[int, int]]]:
    """Pack legend columns left to right; wrap only when the next one would not fit.

    ``columns`` is ``[[label, ...], ...]``, one list per instrument in legend
    order.  Returns blocks (rows of columns): ``[[(column_index, x_px), ...], ...]``
    where ``x_px`` is the column's offset from the legend's left edge.  A
    column wider than the whole plot still gets a block of its own.  Empty
    columns are skipped.
    """
    usable = legend_usable_width_px(plot_width)
    blocks: list[list[tuple[int, int]]] = []
    x = 0
    for index, labels in enumerate(columns):
        if not labels:
            continue
        width = legend_column_width_px(labels)
        if not blocks or (blocks[-1] and x + width > usable):
            blocks.append([])
            x = 0
        blocks[-1].append((index, x))
        x += width + COLUMN_GAP
    return blocks


def legend_column_height_px(n_items: int) -> int:
    """Height of a vertical column legend of ``n_items`` (Bokeh: rows x label height)."""
    return GLYPH_HEIGHT * max(0, int(n_items))


def legend_column_margin(n_items: int) -> int:
    """``Legend.margin`` that collapses a column's side panel to zero height."""
    return -(legend_column_height_px(n_items) // 2)


def legend_block_height_px(depth: int) -> int:
    """Vertical space one block of columns takes: its deepest column plus the gap below."""
    height = legend_column_height_px(depth) + BLOCK_GAP
    return height + height % 2  # spacer panels are 2 * margin tall, margin an int


def legend_blocks_height_px(depths) -> int:
    """Vertical space all blocks take below the plot, given each block's depth."""
    return sum(legend_block_height_px(depth) for depth in depths)


def requested_plot_width(raw_value, default: int) -> int:
    """Plot width from the page's ``?w=<container px>`` query parameter.

    Falls back to ``default`` when absent or unparsable, clamps to
    ``MIN_PLOT_WIDTH``..``MAX_PLOT_WIDTH`` and rounds down to a
    ``PLOT_WIDTH_BUCKET`` multiple so nearby widths share a cache entry.
    """
    try:
        width = int(float(raw_value))
    except (TypeError, ValueError):
        return default
    width = max(MIN_PLOT_WIDTH, min(MAX_PLOT_WIDTH, width))
    return width - width % PLOT_WIDTH_BUCKET
