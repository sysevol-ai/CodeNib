"""Shared visual system for compact, print-safe CodeMiner paper figures."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import matplotlib as mpl
from matplotlib import transforms
from matplotlib.lines import Line2D

SINGLE_COLUMN_WIDTH = 3.4
DOUBLE_COLUMN_WIDTH = 7.0
TWO_PANEL_HEIGHT = 2.18
THREE_PANEL_HEIGHT = 2.16
PAPER_DPI = 300

# Low-saturation pastels carry category identity.  Deep purple is reserved for
# the proposed/key configuration; brown-orange denotes baselines or reference
# executions.  Every color encoding is paired with a marker, line style, or
# hatch so that the figures remain legible in grayscale.
PRIMARY_COLOR = "#5B3A82"
PRIMARY_LIGHT = "#C8B8D6"
BASELINE_COLOR = "#9A5F37"
BASELINE_MID = "#C48452"
BASELINE_LIGHT = "#E4C3A2"
PASTEL_BLUE = "#8FB3CF"
PASTEL_ORANGE = "#D7A267"
PASTEL_GREEN = "#83B5A2"
PASTEL_MAUVE = "#B794B2"
PASTEL_TERRACOTTA = "#C98263"

MODEL_IDS = (
    "Salesforce/SweRankEmbed-Small",
    "Qwen/Qwen3-Embedding-0.6B",
    "jinaai/jina-code-embeddings-1.5b",
    "Qwen/Qwen3-Embedding-4B",
    "fishmingyu/SweRankEmbed-Large",
)
MODEL_LABELS = {
    "Salesforce/SweRankEmbed-Small": "SweRank-Small",
    "Qwen/Qwen3-Embedding-0.6B": "Qwen3-Embed-0.6B",
    "jinaai/jina-code-embeddings-1.5b": "Jina-Code-1.5B",
    "Qwen/Qwen3-Embedding-4B": "Qwen3-Embed-4B",
    "fishmingyu/SweRankEmbed-Large": "SweRank-Large",
}
MODEL_ORDER = tuple(MODEL_LABELS[model] for model in MODEL_IDS)
MODEL_COLORS = {
    "SweRank-Small": PASTEL_BLUE,
    "Qwen3-Embed-0.6B": PASTEL_ORANGE,
    "Jina-Code-1.5B": PASTEL_GREEN,
    "Qwen3-Embed-4B": PASTEL_MAUVE,
    "SweRank-Large": PASTEL_TERRACOTTA,
}
MODEL_MARKERS = {
    "SweRank-Small": "o",
    "Qwen3-Embed-0.6B": "s",
    "Jina-Code-1.5B": "D",
    "Qwen3-Embed-4B": "^",
    "SweRank-Large": "v",
}
MODEL_LINESTYLES = {
    "SweRank-Small": "-",
    "Qwen3-Embed-0.6B": "--",
    "Jina-Code-1.5B": "-.",
    "Qwen3-Embed-4B": ":",
    "SweRank-Large": (0, (4, 1, 1, 1)),
}
MODEL_HATCHES = {
    "SweRank-Small": "///",
    "Qwen3-Embed-0.6B": "\\\\\\",
    "Jina-Code-1.5B": "xx",
    "Qwen3-Embed-4B": "...",
    "SweRank-Large": "++",
}
MODEL_DIMENSIONS = {
    "SweRank-Small": 768,
    "Qwen3-Embed-0.6B": 1024,
    "Jina-Code-1.5B": 1536,
    "Qwen3-Embed-4B": 2560,
    "SweRank-Large": 3584,
}
# Rounded stored-parameter counts from the revision-bound
# dense-model-scale.json audit shipped with the paper artifact.
MODEL_PARAMETER_LABELS = {
    "SweRank-Small": "137M",
    "SweRank-Large": "7B",
}
MODEL_DISPLAY_LABELS = {
    "SweRank-Small": "SR-Small (137M)",
    "SweRank-Large": "SR-Large (7B)",
}

STATIC_COLOR = PRIMARY_COLOR
LIVE_COLOR = BASELINE_COLOR
GRAPH_COLOR = "#4D7775"
RERANK_COLOR = PRIMARY_COLOR
NEUTRAL_COLOR = "#4C4947"
MUTED_COLOR = "#77716D"
GRID_COLOR = "#E4E0DC"
PANEL_FILL = "#F7F4F1"

PAPER_FONT_RC = {
    "font.family": "sans-serif",
    "font.sans-serif": [
        "Arial",
        "Helvetica",
        "Liberation Sans",
        "DejaVu Sans",
    ],
}

PAPER_RC = {
    **PAPER_FONT_RC,
    "font.size": 7.0,
    "axes.titlesize": 8.1,
    "axes.titleweight": "semibold",
    "axes.labelsize": 7.4,
    "axes.edgecolor": NEUTRAL_COLOR,
    "axes.linewidth": 0.65,
    "axes.axisbelow": True,
    "axes.titlepad": 4.0,
    "axes.labelpad": 2.5,
    "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5,
    "xtick.major.size": 2.5,
    "ytick.major.size": 2.5,
    "xtick.major.width": 0.65,
    "ytick.major.width": 0.65,
    "legend.fontsize": 6.1,
    "legend.frameon": False,
    "legend.handlelength": 1.5,
    "legend.handletextpad": 0.35,
    "legend.columnspacing": 0.85,
    "grid.color": GRID_COLOR,
    "grid.linewidth": 0.5,
    "grid.alpha": 0.72,
    "lines.linewidth": 1.1,
    "lines.markersize": 3.6,
    "patch.linewidth": 0.65,
    "hatch.linewidth": 0.55,
    "hatch.color": NEUTRAL_COLOR,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "savefig.facecolor": "white",
    "savefig.bbox": "tight",
}


def apply_paper_style() -> None:
    """Install the shared Matplotlib defaults for a plotting process."""

    mpl.rcParams.update(PAPER_RC)


def style_axis(
    ax,
    *,
    grid_axis: str = "both",
    minor_grid: bool = False,
    open_frame: bool = False,
) -> None:
    """Apply shared grid and spine treatment to an axis."""

    ax.grid(True, axis=grid_axis, which="major")
    if minor_grid:
        ax.grid(True, axis=grid_axis, which="minor", alpha=0.35)
    else:
        ax.grid(False, which="minor")
    if open_frame:
        ax.spines[["top", "right"]].set_visible(False)


def panel_title(ax, label: str, title: str, *, pad: float = 6.0) -> None:
    """Set a consistently formatted, left-aligned panel title."""

    prefix = f"({label}) " if label else ""
    ax.set_title(f"{prefix}{title}", loc="left", pad=pad)


def aligned_panel_title(
    ax,
    label: str,
    title: str,
    *,
    pad: float = 6.0,
    label_width: float = 12.75,
) -> None:
    """Draw a panel label and title with a fixed-width physical label column."""

    figure = ax.figure
    y_offset = transforms.ScaledTranslation(0, pad / 72, figure.dpi_scale_trans)
    title_offset = transforms.ScaledTranslation(
        label_width / 72, pad / 72, figure.dpi_scale_trans
    )
    style = {
        "fontsize": mpl.rcParams["axes.titlesize"],
        "fontweight": mpl.rcParams["axes.titleweight"],
        "ha": "left",
        "va": "bottom",
        "clip_on": False,
    }
    ax.text(0, 1, f"({label})", transform=ax.transAxes + y_offset, **style)
    ax.text(0, 1, title, transform=ax.transAxes + title_offset, **style)


def embedding_legend_handles(
    labels: Iterable[str] = MODEL_ORDER,
    *,
    marker_size: float = 4.5,
    display_labels: dict[str, str] | None = None,
) -> list[Line2D]:
    """Return standard embedding-model legend handles."""

    return [
        Line2D(
            [],
            [],
            marker=MODEL_MARKERS[label],
            linestyle=MODEL_LINESTYLES[label],
            linewidth=1.05,
            color=MODEL_COLORS[label],
            markersize=marker_size,
            markerfacecolor=MODEL_COLORS[label],
            markeredgecolor=NEUTRAL_COLOR,
            markeredgewidth=0.35,
            label=(display_labels or {}).get(label, label),
        )
        for label in labels
    ]


def save_figure(
    fig,
    output_prefix: str | Path,
    *,
    formats: Sequence[str] = ("png", "pdf"),
    dpi: int = PAPER_DPI,
) -> list[Path]:
    """Save a paper figure with consistent raster and vector settings."""

    prefix = Path(output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    outputs = []
    for extension in formats:
        output = prefix.with_suffix(f".{extension}")
        # DPI also controls rasterized artists embedded in vector outputs.
        kwargs = {"bbox_inches": "tight", "facecolor": "white", "dpi": dpi}
        fig.savefig(output, **kwargs)
        outputs.append(output)
    return outputs
