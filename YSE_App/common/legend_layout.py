"""Column layout for the Bokeh light-curve legends (#226).

Bokeh 2.4.2 has no ``Legend.ncols``, so a multi-column legend is built from
several horizontal ``Legend`` rows stacked ``below`` the plot, each holding
``ncols`` items.  Every label is padded to the same width (``label_width``)
so the columns line up across rows.  This module holds the pure arithmetic
so it can be unit-tested without Bokeh.

Column rule::

    entry_px = GLYPH_WIDTH + LABEL_STANDOFF + max_label_chars * PX_PER_CHAR + SPACING
    usable   = plot_width - TOOLBAR_WIDTH_PX - 2 * PADDING
    ncols    = clamp(usable // entry_px, 1, min(MAX_COLUMNS, n_items))
    nrows    = ceil(n_items / ncols)

Short labels (``PS1 g``) pack into up to ``MAX_COLUMNS`` columns; one long
label forces fewer, wider columns so nothing overlaps.  ``plot_width`` is
the figure's nominal width: the page passes the real container width as
``?w=`` (see :func:`requested_plot_width`) so the columns match what the
viewer sees instead of the 400 px default.
"""

from __future__ import annotations

import math

# 13 px sans-serif (Bokeh's default legend font) averages ~6.5 px per glyph;
# round up so an estimate that is slightly too wide never overlaps.
PX_PER_CHAR = 7
GLYPH_WIDTH = 20
GLYPH_HEIGHT = 20
LABEL_STANDOFF = 5
SPACING = 6
PADDING = 4
MAX_COLUMNS = 6
# The figure's right-hand toolbar sits inside plot_width; the legend cannot use it.
TOOLBAR_WIDTH_PX = 30
# Accepted range for a page-supplied width, and the bucket it is rounded to
# (so the plot-HTML cache does not fragment on every pixel).
MIN_PLOT_WIDTH = 320
MAX_PLOT_WIDTH = 1600
PLOT_WIDTH_BUCKET = 40
# One horizontal legend row: label/glyph height plus padding above and below.
ROW_HEIGHT_PX = GLYPH_HEIGHT + 2 * PADDING


def legend_label_width_px(labels, *, px_per_char: int = PX_PER_CHAR) -> int:
    """Pixel width every label cell is padded to (the longest label)."""
    longest = max((len(str(label)) for label in labels), default=0)
    return longest * px_per_char


def legend_column_count(labels, plot_width: int, *, max_columns: int = MAX_COLUMNS) -> int:
    """Number of legend columns that fit ``plot_width`` given the longest label."""
    labels = list(labels)
    if not labels:
        return 1
    entry_px = GLYPH_WIDTH + LABEL_STANDOFF + legend_label_width_px(labels) + SPACING
    usable = plot_width - TOOLBAR_WIDTH_PX - 2 * PADDING
    fit = usable // entry_px
    return max(1, min(int(fit), max_columns, len(labels)))


def legend_rows(items, ncols: int) -> list[list]:
    """Split legend items into row-major rows of ``ncols`` (last row may be short)."""
    items = list(items)
    ncols = max(1, int(ncols))
    return [items[i:i + ncols] for i in range(0, len(items), ncols)]


def legend_row_count(n_items: int, ncols: int) -> int:
    return max(1, math.ceil(n_items / max(1, ncols))) if n_items else 0


def legend_height_px(n_items: int, ncols: int) -> int:
    """Vertical space the stacked legend rows take below the plot."""
    return ROW_HEIGHT_PX * legend_row_count(n_items, ncols)


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
