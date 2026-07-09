# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Repository memory store for the Repository Guardian.

Provides a lightweight SQLite-backed append-only store that accumulates
cross-cycle state:

    MemoryStore.persist_cycle(report, graph)  ← called at end of every cycle
    MemoryStore.recent_findings(k)            ← paged read for the reflect prompt
    MemoryStore.load_prior_graph(memory_dir)  ← loads last saved CodeGraph snapshot

The ``readonly=True`` flag implements the ``--arm memoryless`` toggle: all reads
return empty / None, all writes are no-ops, so the same code path runs in both
arms and the only difference is what the reflect step receives.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator, List, Optional

from ..log_utils import get_logger

if TYPE_CHECKING:
    from ..graph.code_graph import CodeGraph
    from .report import GuardianReport

logger = get_logger(__name__)

_DB_FILENAME = "index.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    cycle_no      INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_sha    TEXT    NOT NULL,
    ts            TEXT    NOT NULL,
    token_cost    INTEGER DEFAULT 0,
    tests_summary TEXT    DEFAULT '',
    has_graph     INTEGER DEFAULT 0   -- 1 if a graph snapshot was saved this cycle
);

CREATE TABLE IF NOT EXISTS findings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_no  INTEGER NOT NULL REFERENCES cycles(cycle_no),
    kind      TEXT    NOT NULL,
    target    TEXT    NOT NULL DEFAULT '',
    title     TEXT    NOT NULL,
    detail    TEXT    DEFAULT '',
    narrative TEXT    DEFAULT '',
    confidence REAL   DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id        TEXT    NOT NULL,
    path             TEXT    NOT NULL DEFAULT '',
    first_seen_cycle INTEGER NOT NULL,
    last_seen_cycle  INTEGER NOT NULL,
    PRIMARY KEY (symbol_id)
);

CREATE TABLE IF NOT EXISTS edges (
    src_symbol  TEXT    NOT NULL,
    dst_symbol  TEXT    NOT NULL,
    edge_type   TEXT    NOT NULL,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    PRIMARY KEY (src_symbol, dst_symbol, edge_type)
);

CREATE TABLE IF NOT EXISTS test_deltas (
    cycle_no      INTEGER NOT NULL REFERENCES cycles(cycle_no),
    nodeid        TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    changed_from  TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_findings_cycle   ON findings(cycle_no);
CREATE INDEX IF NOT EXISTS idx_findings_target  ON findings(target);
CREATE INDEX IF NOT EXISTS idx_test_deltas_node ON test_deltas(nodeid);
"""


class MemoryStore:
    """Cross-cycle memory store backed by SQLite + graph snapshots on disk.

    Args:
        memory_dir: Directory that holds ``index.sqlite`` and the ``graph/``
            snapshot sub-directory.  Created on first write.
        readonly: When True, all writes are no-ops and all reads return empty
            results.  Use for ``--arm memoryless`` ablation runs.
    """

    def __init__(self, memory_dir: str, *, readonly: bool = False) -> None:
        self._dir = memory_dir
        self.readonly = readonly
        self._db_path = os.path.join(memory_dir, _DB_FILENAME)
        if not readonly:
            os.makedirs(memory_dir, exist_ok=True)
            self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def persist_cycle(
        self,
        report: "GuardianReport",
        *,
        graph: "Optional[CodeGraph]" = None,
        memory_dir: Optional[str] = None,
    ) -> int:
        """Write one cycle's report to the store and optionally save a graph snapshot.

        Args:
            report: The completed GuardianReport for this cycle.
            graph:  Optional CodeGraph to snapshot (skipped when None).
            memory_dir: Override the store's memory_dir for graph storage.
                        Defaults to the store's own directory.

        Returns:
            The new ``cycle_no`` assigned to this cycle.
        """
        if self.readonly:
            logger.debug("memory: readonly mode — persist_cycle is a no-op")
            return -1

        mdir = memory_dir or self._dir
        token_cost = 0
        if report.llm_usage is not None:
            token_cost = getattr(report.llm_usage, "total_tokens", 0) or 0

        # 1. Insert cycle row
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO cycles (commit_sha, ts, token_cost, tests_summary, has_graph) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    report.commit,
                    report.generated_at,
                    token_cost,
                    report.tests_summary or "",
                    0,  # updated below if graph saved
                ),
            )
            cycle_no = cur.lastrowid

            # 2. Insert findings
            for finding in report.findings:
                target = _target_from_finding(finding)
                conn.execute(
                    "INSERT INTO findings (cycle_no, kind, target, title, detail, narrative) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cycle_no,
                        finding.kind,
                        target,
                        finding.title,
                        finding.detail or "",
                        finding.narrative or "",
                    ),
                )
                # 3. Upsert symbol record for every finding target
                if target:
                    conn.execute(
                        "INSERT INTO symbols (symbol_id, path, first_seen_cycle, last_seen_cycle) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(symbol_id) DO UPDATE SET last_seen_cycle=excluded.last_seen_cycle",
                        (target, target, cycle_no, cycle_no),
                    )

        # 4. Save graph snapshot (outside the main transaction — can be large)
        has_graph = 0
        if graph is not None:
            try:
                from .graph_diff import save_snapshot
                save_snapshot(graph, mdir, report.commit)
                has_graph = 1
                logger.info("memory: saved graph snapshot for %s", report.commit[:12])
            except Exception as exc:  # noqa: BLE001
                logger.warning("memory: failed to save graph snapshot: %s", exc)

        # 5. Update has_graph flag
        if has_graph:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE cycles SET has_graph=1 WHERE cycle_no=?", (cycle_no,)
                )

        logger.info(
            "memory: persisted cycle #%d (commit=%s, %d finding(s), tokens=%d)",
            cycle_no, report.commit[:12], len(report.findings), token_cost,
        )
        return cycle_no

    def persist_edge_changes(
        self,
        cycle_no: int,
        edge_changes: list,
    ) -> None:
        """Upsert edge-level changes from a graph diff into the edges table.

        Args:
            cycle_no:     The current cycle number (from persist_cycle).
            edge_changes: List of EdgeChange objects from graph_diff.diff_graphs.
        """
        if self.readonly or not edge_changes:
            return
        with self._connect() as conn:
            for ch in edge_changes:
                conn.execute(
                    "INSERT INTO edges (src_symbol, dst_symbol, edge_type, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(src_symbol, dst_symbol, edge_type) "
                    "DO UPDATE SET last_seen=excluded.last_seen",
                    (ch.src, ch.dst, ch.edge_type, cycle_no, cycle_no),
                )

    # ------------------------------------------------------------------
    # Read API  (all return empty / None when readonly=True)
    # ------------------------------------------------------------------

    def recent_findings(self, k: int = 5) -> List[dict]:
        """Return the k most recent Finding rows as plain dicts.

        Results are sorted newest-first and capped at ``k`` to keep the
        reflect-prompt context bounded.  Returns [] when readonly or no data.
        """
        if self.readonly or not os.path.exists(self._db_path):
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT f.kind, f.target, f.title, f.detail, f.narrative, "
                    "       c.commit_sha, c.ts "
                    "FROM findings f "
                    "JOIN cycles c ON c.cycle_no = f.cycle_no "
                    "ORDER BY f.id DESC "
                    "LIMIT ?",
                    (k,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory: recent_findings failed: %s", exc)
            return []

    def load_prior_graph(self, memory_dir: Optional[str] = None) -> "Optional[CodeGraph]":
        """Load the CodeGraph snapshot from the most recent cycle that saved one.

        Returns None when readonly, no snapshot exists, or loading fails.
        """
        if self.readonly or not os.path.exists(self._db_path):
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT commit_sha FROM cycles WHERE has_graph=1 ORDER BY cycle_no DESC LIMIT 1"
                ).fetchone()
            if row is None:
                return None
            commit = row["commit_sha"]
            mdir = memory_dir or self._dir
            from .graph_diff import load_snapshot
            graph = load_snapshot(mdir, commit)
            if graph is not None:
                logger.info("memory: loaded prior graph for commit %s", commit[:12])
            return graph
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory: load_prior_graph failed: %s", exc)
            return None

    def symbol_history(self, symbol_id: str) -> dict:
        """Return first/last seen cycle info for a symbol, or {} if unknown."""
        if self.readonly or not os.path.exists(self._db_path):
            return {}
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT first_seen_cycle, last_seen_cycle FROM symbols WHERE symbol_id=?",
                    (symbol_id,),
                ).fetchone()
            return dict(row) if row else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory: symbol_history failed: %s", exc)
            return {}

    def edge_drift(self, since_cycle: int) -> List[dict]:
        """Return edge records first seen at or after ``since_cycle``."""
        if self.readonly or not os.path.exists(self._db_path):
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT src_symbol, dst_symbol, edge_type, first_seen, last_seen "
                    "FROM edges WHERE first_seen >= ?",
                    (since_cycle,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory: edge_drift failed: %s", exc)
            return []

    def cycle_count(self) -> int:
        """Number of completed cycles recorded in the store."""
        if self.readonly or not os.path.exists(self._db_path):
            return 0
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()
            return row[0] if row else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory: cycle_count failed: %s", exc)
            return 0

    def recent_cycles(self, k: int = 10) -> List[dict]:
        """Return the k most recent cycle summary rows as dicts."""
        if self.readonly or not os.path.exists(self._db_path):
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cycle_no, commit_sha, ts, token_cost, tests_summary, has_graph "
                    "FROM cycles ORDER BY cycle_no DESC LIMIT ?",
                    (k,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory: recent_cycles failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def assert_cross_cycle(self) -> None:
        """Assert that at least two cycles exist and findings from cycle k-1 are readable.

        Raises AssertionError if the M2 invariant is not satisfied.
        """
        count = self.cycle_count()
        assert count >= 2, (
            f"Cross-cycle assertion failed: expected >= 2 cycles, found {count}"
        )
        findings = self.recent_findings(k=20)
        assert len(findings) > 0, "Cross-cycle assertion failed: no findings readable"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _target_from_finding(finding: "object") -> str:
    """Extract a target path/symbol from a Finding for the symbols table."""
    title: str = getattr(finding, "title", "") or ""
    detail: str = getattr(finding, "detail", "") or ""

    # "High-churn file: codeminer/foo.py" → "codeminer/foo.py"
    if ":" in title:
        candidate = title.split(":", 1)[-1].strip()
        if candidate:
            return candidate

    # Fallback: first token of detail that looks like a path
    for token in detail.split():
        if "/" in token or token.endswith(".py"):
            return token.strip("*`.,")

    return title.strip()


def format_findings_for_prompt(findings: List[dict], max_findings: int = 5) -> str:
    """Render recent findings as a compact plain-text block for the reflect prompt.

    Keeps output bounded so it never blows the model context.
    """
    if not findings:
        return "(no prior findings in memory)"
    lines = []
    for f in findings[:max_findings]:
        commit = (f.get("commit_sha") or f.get("commit") or "?")[:8]
        kind = f.get("kind", "?")
        title = f.get("title", "")
        detail = (f.get("detail") or "")[:120]
        lines.append(f"[{commit}] ({kind}) {title}")
        if detail:
            lines.append(f"  {detail}")
    if len(findings) > max_findings:
        lines.append(f"  … ({len(findings) - max_findings} more finding(s) not shown)")
    return "\n".join(lines)
