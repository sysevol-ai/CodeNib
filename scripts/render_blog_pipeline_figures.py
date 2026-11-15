#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Render the pipeline figures of three earlier blog posts.

Writes to ``landing/assets/blogs/``:

- ``local-code-intelligence-dgx-spark.svg``: one repository compiled into
  independent views and published through one manifest on a DGX Spark.
- ``fact-query-index-fact-batch.svg``: provider artifacts, the shared facts
  contract, and the three paths behind it.
- ``jev-retrieval-routes.svg``: four retrieval routes feeding one candidate
  pool and two rerankers.

They are drawn the way the thread context figures are: one surface, stages
side by side with an arrow between them, rules instead of boxes, and the
site's font embedded so they read the same in every browser. The wording and
the numbers are those of the figures they replace. Each is also its post's
sharing image::

    python scripts/render_blog_pipeline_figures.py
    node scripts/render_svg_png.mjs landing/assets/blogs/<name>.svg \\
        landing/assets/blogs/<name>.png
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from blog_figure_theme import dark_figure
from render_contextbench_blog_figures import (
    ACCENT_DARK,
    INK_2,
    INK_3,
    LINE,
    OUT,
    ROOT,
    TEAL_DARK,
    Figure,
)

STYLES = {
    "head": {"size": 34, "weight": 650, "spacing": -0.01},
    "head-dim": {"size": 34, "weight": 650, "spacing": -0.01, "fill": INK_3},
    "sub": {"size": 18, "fill": INK_2},
    "col": {"size": 20, "weight": 600},
    "col-b": {"size": 20, "weight": 600, "fill": ACCENT_DARK},
    "col-t": {"size": 20, "weight": 600, "fill": TEAL_DARK},
    "strong": {"size": 17.5, "weight": 600},
    "text": {"size": 16.5, "fill": INK_2},
    "mono": {"size": 15, "weight": 500, "family": "mono"},
    "mono-t": {"size": 15, "weight": 500, "family": "mono", "fill": TEAL_DARK},
    "mono-dim": {"size": 14.5, "family": "mono", "fill": INK_3},
    "foot": {"size": 16, "fill": INK_2},
    "foot-b": {"size": 17, "weight": 600},
}


def rule(fig: Figure, x: float, y: float, width: float) -> None:
    fig.path(f"M {x:.1f} {y:.1f} H {x + width:.1f}", stroke=LINE, sw=1.5)


def arrow(fig: Figure, x: float, y: float, to: float, color="gray", dash=None) -> None:
    stroke = {"gray": INK_3, "blue": "#2563eb", "teal": "#0d9488"}[color]
    fig.path(
        f"M {x:.1f} {y:.1f} H {to:.1f}",
        stroke=stroke,
        sw=1.8,
        marker=f"arrow-{color}",
        dash=dash,
    )


def para(fig: Figure, x: float, y: float, text: str, width: float, style="text"):
    """Wrapped text; returns the baseline after the last line."""
    for line in fig.wrap(text, style, width):
        fig.text(x, y, line, style)
        y += 24
    return y


# --------------------------------------------------------------------------- #
# One repository index on one DGX Spark

SPARK_TITLE = "One repository index on one DGX Spark"
SPARK_DESC = (
    "A repository at a verified commit is compiled once into independent "
    "BM25, dense, source and graph views, which are incremental and "
    "capability-bearing. One verified manifest publishes them as a Wiki for "
    "people, Ask through Qwen, and MCP for agents, with exact paths, line "
    "ranges and commit identity. The stack runs on loopback on an NVIDIA DGX "
    "Spark, GB10, Arm64, 128 GB unified memory, with Qwen3.6 on vLLM at port "
    "8080. Generation and embeddings can stay local; BM25 and CodeGraph "
    "remain model-free."
)


def spark() -> str:
    fig = Figure(1200, 630, STYLES)
    fig.text(56, 82, "One repository index.", "head")
    fig.text(56, 124, "One DGX Spark.", "head-dim")
    fig.text(56, 160, "Source-linked Wiki · hybrid retrieval · CodeGraph · MCP", "sub")
    fig.text(1144, 76, "NVIDIA DGX Spark", "col", "end")
    fig.text(1144, 102, "GB10 · Arm64 · 128 GB unified", "text", "end")
    fig.text(1144, 128, "Qwen3.6 + vLLM  :8080", "mono", "end")
    fig.text(1144, 154, "local on loopback", "mono-t", "end")
    rule(fig, 0, 190, 1200)

    fig.text(56, 238, "Repository", "col")
    fig.text(56, 264, "The input: commit + fingerprint", "text")
    fig.text(56, 318, "source verified", "mono-t")
    arrow(fig, 350, 232, 394)

    fig.text(416, 238, "Independent views", "col")
    fig.text(416, 264, "Compiled once", "text")
    for i, view in enumerate(["BM25", "dense", "source", "graph"]):
        fig.text(416 + (i % 2) * 150, 318 + (i // 2) * 32, view, "mono")
    fig.text(416, 400, "Incremental · capability-bearing", "text")
    arrow(fig, 750, 232, 794)

    fig.text(816, 238, "One verified manifest", "col-t")
    fig.text(816, 264, "Exact paths · line ranges · commit identity", "text")
    for i, (name, who) in enumerate(
        [("Wiki", "humans"), ("Ask", "Qwen"), ("MCP", "agents")]
    ):
        fig.text(816, 318 + i * 32, name, "strong")
        fig.text(890, 318 + i * 32, who, "text")
    rule(fig, 0, 460, 1200)

    fig.text(
        56,
        514,
        "Generation and embeddings can stay local. BM25 and CodeGraph remain "
        "model-free.",
        "foot",
    )
    fig.text(
        56,
        552,
        "Index once. Explore it as a Wiki. Serve it to any coding agent.",
        "foot-b",
    )
    fig.text(1144, 552, "codenib.ai", "foot", "end")
    return fig.svg(SPARK_TITLE, SPARK_DESC)


# --------------------------------------------------------------------------- #
# CodeGraph paths

FACTS_TITLE = "CodeGraph paths and materialization boundaries"
FACTS_DESC = (
    "Existing analyzer output from SCIP (index.decoded) and clangd (idx "
    "shards) is adapted into provider facts, the shared contract: definitions "
    "and references, occurrences and ranges, adjacency and order, roles and "
    "diagnostics, as paths, ranges and roles. Three paths sit behind it. The "
    "native query path is FactQueryIndex with an adapter: integer postings, "
    "provider-gated capabilities, symbols, optional positions and a route "
    "adapter, format v3, no graphs. The provider-neutral model is FactBatch "
    "v1 in one file: validated per-file facts with no database lifecycle, "
    "with source digest, profile digest and provenance in a native buffer. "
    "The compatibility authority is the complete CodeGraph on igraph, by "
    "direct selection or request fallback. Identity failures fail closed, and "
    "external analyzer generation is excluded."
)


def facts() -> str:
    fig = Figure(1200, 630, STYLES)
    fig.text(56, 72, "CodeGraph paths and materialization boundaries.", "head")
    fig.text(
        56,
        104,
        "Provider-specific representations stay behind explicit capability and "
        "identity gates.",
        "sub",
    )
    rule(fig, 0, 132, 1200)

    fig.text(56, 178, "Provider artifacts", "col")
    fig.text(56, 224, "SCIP", "strong")
    fig.text(56, 247, "index.decoded", "mono-dim")
    fig.text(56, 290, "clangd", "strong")
    fig.text(56, 313, "*.idx shards", "mono-dim")
    fig.text(56, 364, "Existing analyzer output.", "text")
    fig.text(56, 388, "Analyzer generation excluded.", "text")
    fig.text(323, 160, "adapt", "mono-dim", "middle")
    arrow(fig, 302, 172, 346)

    fig.text(368, 178, "Provider facts", "col")
    fig.text(368, 204, "The shared contract", "text")
    for i, line in enumerate(
        [
            "definitions + references",
            "occurrences + ranges",
            "adjacency + order",
            "roles + diagnostics",
        ]
    ):
        fig.text(368, 250 + i * 30, line, "mono")
    fig.text(368, 388, "paths · ranges · roles", "mono-t")

    arrow(fig, 640, 172, 684, "blue")
    fig.text(706, 178, "FactQueryIndex + adapter", "col-b")
    para(
        fig,
        706,
        204,
        "Native query path. Integer postings; capabilities are provider-gated.",
        440,
    )
    fig.text(706, 252, "symbols · opt. positions · route adapter", "mono")
    fig.text(706, 274, "fmt v3 · 0 graphs", "mono-dim")
    rule(fig, 706, 296, 494)

    arrow(fig, 640, 332, 684, "teal")
    fig.text(706, 338, "FactBatch v1 · one file", "col-t")
    para(
        fig,
        706,
        364,
        "Provider-neutral model. Validated per-file facts with no database "
        "lifecycle.",
        440,
    )
    fig.text(706, 412, "source digest · profile digest · provenance", "mono")
    fig.text(706, 434, "native buffer · no DB", "mono-dim")
    rule(fig, 706, 456, 494)

    arrow(fig, 640, 492, 684, "gray", "5 5")
    fig.text(706, 498, "Complete CodeGraph / igraph", "col")
    fig.text(
        706,
        524,
        "Compatibility authority. Direct selection · request fallback.",
        "text",
    )
    rule(fig, 0, 552, 1200)
    fig.text(
        56,
        592,
        "Existing artifacts · provider-specific receipts · identity failures "
        "fail closed · external analyzer generation excluded",
        "foot",
    )
    return fig.svg(FACTS_TITLE, FACTS_DESC)


# --------------------------------------------------------------------------- #
# Retrieval routes

ROUTES_TITLE = "Retrieval decides what can be found. The reranker only orders it."
ROUTES_DESC = (
    "The full issue text, on the same source snapshot and with no patch, "
    "hints, target files or target blocks, goes to one of the routes. BM25: "
    "a BM25 index over tree-sitter chunks, top 100 by lexical score, K 100, "
    "coverage 63.78%. Model-planned grep: Sonnet 4.6 in one call plans up to "
    "six regex or glob actions, rg runs them locally with match caps per "
    "file, the smallest code chunk around each match is taken, and they are "
    "merged round-robin and deduplicated to at most 100; a mean of 17.47 "
    "chunks, coverage 75.83%. Dense and hybrid: a CodeRankEmbed query vector, "
    "then FAISS top 100, or with BM25 through RRF top 100; K 100, coverage "
    "71.98% and 75.28%. A route's pool holds code chunks of up to 3,000 "
    "characters each; BM25 and grep use no embeddings. A replaceable reranker "
    "orders it: Qwen3-Reranker, 4B on every route and 0.6B on BM25 and grep "
    "only, scoring query-code pairs on a local H100; or Jev 1.13 Decisions, "
    "one four-level relevance question per chunk, ten per request, with "
    "Python sorting the scores and keeping failed batches in order. The top "
    "five results are compared as complete paths on Recall@5, latency and "
    "cost. Coverage is the ceiling: a reranker cannot recover code that "
    "never entered the pool."
)
GREP_STEPS = [
    ("Sonnet 4.6", "1 call, up to 6 regex or glob actions"),
    ("rg", "runs locally with match caps per file"),
    ("Chunk", "smallest code chunk around each match"),
    ("Merge", "round-robin, dedupe, keep at most 100"),
]


def _then(fig: Figure, x: float, y: float, first: str, second: str) -> None:
    fig.text(x, y, first, "text")
    at = x + fig.measure(first, "text") + 14
    arrow(fig, at, y - 5, at + 30)
    fig.text(at + 44, y, second, "text")


def routes() -> str:
    fig = Figure(1600, 840, STYLES)
    fig.text(64, 78, ROUTES_TITLE, "head")
    rule(fig, 0, 118, 1600)

    fig.text(64, 172, "Full issue text", "col")
    fig.text(64, 200, "Same source snapshot.", "text")
    para(fig, 64, 244, "No patch, hints, target files, or target blocks.", 230)
    arrow(fig, 300, 166, 334)
    fig.path("M 346 150 V 616", stroke=LINE, sw=1.5)

    fig.text(372, 172, "BM25", "col")
    fig.text(1010, 172, "K = 100 · coverage 63.78%", "mono-dim", "end")
    _then(
        fig, 372, 220, "BM25 index over tree-sitter chunks", "Top 100 by lexical score"
    )
    rule(fig, 372, 268, 638)

    fig.text(372, 322, "Model-planned grep", "col-b")
    fig.text(1010, 322, "mean 17.47 chunks · coverage 75.83%", "mono-dim", "end")
    for i, (name, does) in enumerate(GREP_STEPS):
        x = 372 + i * 164
        fig.text(x, 370, name, "strong")
        para(fig, x, 396, does, 136)
        if i:
            arrow(fig, x - 34, 364, x - 10)
    rule(fig, 372, 500, 638)

    fig.text(372, 554, "Dense and hybrid", "col-t")
    fig.text(1010, 554, "K = 100 · coverage 71.98% / 75.28%", "mono-dim", "end")
    _then(
        fig,
        372,
        602,
        "CodeRankEmbed query vector",
        "FAISS top 100, or + BM25 with RRF top 100",
    )
    fig.path("M 1034 150 V 616", stroke=LINE, sw=1.5)
    arrow(fig, 1046, 166, 1074)

    fig.text(1092, 172, "One route's pool", "col")
    para(fig, 1092, 200, "Code chunks, up to 3,000 characters each.", 190)
    para(fig, 1092, 272, "BM25 and grep use no embeddings.", 190)
    arrow(fig, 1290, 166, 1334)

    fig.text(1356, 172, "Reranker, replaceable", "col")
    fig.text(1356, 220, "Qwen3-Reranker", "strong")
    for i, line in enumerate(
        [
            "4B on every route",
            "0.6B: BM25 / grep only",
            "scores query–code pairs",
            "local H100",
        ]
    ):
        fig.text(1356, 246 + i * 24, line, "text")
    fig.text(1356, 370, "Jev 1.13 · Decisions", "strong", fill=ACCENT_DARK)
    para(
        fig,
        1356,
        396,
        "One four-level relevance question per chunk, 10 per request. Python "
        "sorts the scores and keeps failed batches in order.",
        184,
    )
    rule(fig, 1356, 560, 244)
    fig.text(1356, 606, "Top 5 results", "col")
    para(fig, 1356, 634, "Compare complete paths: Recall@5, latency and cost.", 184)

    rule(fig, 0, 704, 1600)
    fig.text(
        64,
        752,
        "Coverage is the ceiling: a reranker cannot recover code that never "
        "entered the pool.",
        "foot-b",
    )
    fig.text(
        64,
        786,
        "All four routes use base-aligned labels on the same 100 CodeNib Base "
        "issues. Each route's candidate pool is frozen and shared by its "
        "rerankers.",
        "foot",
    )
    return fig.svg(ROUTES_TITLE, ROUTES_DESC)


# --------------------------------------------------------------------------- #
# Accuracy by route

ACCURACY_TITLE = "Code-block Recall@5 on four retrieval routes"
ACCURACY_DESC = (
    "Dot plot of code-block Recall@5 on 100 CodeNib Base issues, by retrieval "
    "route: with no reranker, with Qwen3-Reranker-4B, with Jev 1.13, and the "
    "candidate coverage that no reranker can exceed. BM25 top 100: 28.78%, "
    "58.30%, 59.35%, coverage 63.78%. Dense, CodeRankEmbed with FAISS: "
    "39.75%, 63.40%, 63.05%, coverage 71.98%. Hybrid, BM25 with dense "
    "through RRF: 32.02%, 65.03%, 67.92%, coverage 75.28%. Model-planned "
    "grep, 17.47 chunks on average: 58.62%, 68.07%, 71.40%, coverage 75.83%."
)
QWEN = "#eb6834"
# (route, how, no reranker, Qwen3-Reranker-4B, Jev 1.13, candidate coverage)
ACCURACY = [
    ("BM25", "top 100", 28.78, 58.30, 59.35, 63.78),
    ("Dense", "CodeRankEmbed + FAISS", 39.75, 63.40, 63.05, 71.98),
    ("Hybrid", "BM25 + dense, RRF", 32.02, 65.03, 67.92, 75.28),
    ("Model-planned grep", "17.47 chunks on average", 58.62, 68.07, 71.40, 75.83),
]


def _ring(fig: Figure, x: float, y: float) -> None:
    fig.circle(x, y, 9, INK_3)
    fig.circle(x, y, 6, "#ffffff")


def _diamond(fig: Figure, x: float, y: float) -> None:
    fig.path(f"M {x:.1f} {y - 11:.1f} l 10 11 l -10 11 l -10 -11 z", fill="#2563eb")


def accuracy() -> str:
    fig = Figure(1600, 700, STYLES)
    fig.text(64, 78, ACCURACY_TITLE, "head")
    rule(fig, 0, 118, 1600)

    # Legend.
    x = 64
    _ring(fig, x + 9, 160)
    fig.text(x + 28, 166, "No reranker", "text")
    x += 28 + fig.measure("No reranker", "text") + 36
    fig.circle(x + 9, 160, 8, QWEN)
    fig.text(x + 28, 166, "Qwen3-Reranker-4B", "text")
    x += 28 + fig.measure("Qwen3-Reranker-4B", "text") + 36
    _diamond(fig, x + 10, 160)
    fig.text(x + 30, 166, "Jev 1.13", "text")
    x += 30 + fig.measure("Jev 1.13", "text") + 36
    fig.path(f"M {x + 2:.1f} 146 V 174", stroke=INK_2, sw=3)
    fig.text(x + 18, 166, "Candidate coverage, the ceiling for reranking", "text")

    def at(percent: float) -> float:
        return 400 + (percent - 20) * 19

    for tick in range(20, 81, 10):
        fig.path(f"M {at(tick):.1f} 204 V 600", stroke=LINE, sw=1.5)
        fig.text(at(tick), 628, f"{tick}%", "mono-dim", "middle")
    for i, (route, how, plain, qwen, jev, coverage) in enumerate(ACCURACY):
        y = 256 + i * 96
        fig.text(64, y - 2, route, "col")
        fig.text(64, y + 24, how, "text")
        fig.path(f"M {at(plain):.1f} {y} H {at(jev):.1f}", stroke=INK_3, sw=2)
        fig.path(f"M {at(coverage):.1f} {y - 20} V {y + 20}", stroke=INK_2, sw=3)
        fig.text(at(coverage), y - 30, f"{coverage:.2f}", "mono", "middle")
        _ring(fig, at(plain), y)
        fig.text(at(plain), y + 34, f"{plain:.2f}", "mono-dim", "middle")
        fig.circle(at(qwen), y, 8, QWEN)
        fig.text(at(qwen), y + 34, f"{qwen:.2f}", "mono-dim", "middle")
        _diamond(fig, at(jev), y)
        fig.text(at(jev), y - 20, f"{jev:.2f}", "mono", "middle", fill=ACCENT_DARK)
    rule(fig, 0, 648, 1600)
    fig.text(
        64,
        680,
        "100 CodeNib Base issues, labels aligned to base_commit. Each route keeps "
        "at most 100 candidates; coverage counts targets anywhere in the pool.",
        "foot",
    )
    return fig.svg(ACCURACY_TITLE, ACCURACY_DESC)


FIGURES = {
    "jev-accuracy-by-route.svg": accuracy,
    "local-code-intelligence-dgx-spark.svg": spark,
    "fact-query-index-fact-batch.svg": facts,
    "jev-retrieval-routes.svg": routes,
}


def main() -> None:
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, render in FIGURES.items():
        path = args.out / name
        path.write_text(dark_figure(render()), encoding="utf-8")
        shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        print(f"{shown}  {path.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
