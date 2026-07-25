# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""SQLite persistence for Guardian signals and hypothesis trajectories."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Generator, List, Optional

from ...log_utils import get_logger

if TYPE_CHECKING:
    from ...graph.code_graph import CodeGraph
    from ..loop.state import CycleState
    from ..report import GuardianReport

logger = get_logger(__name__)

_DB_FILENAME = "index.sqlite"
_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    cycle_no          INTEGER PRIMARY KEY,
    commit_sha        TEXT NOT NULL,
    ts                TEXT NOT NULL,
    token_cost        INTEGER DEFAULT 0,
    tests_summary     TEXT DEFAULT '',
    has_graph         INTEGER DEFAULT 0,
    exit_reason       TEXT DEFAULT '',
    degraded          INTEGER DEFAULT 0,
    report_summary    TEXT DEFAULT '',
    compaction_events INTEGER DEFAULT 0,
    decision_log_json TEXT DEFAULT '[]',
    carried_from      INTEGER
);

CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id         TEXT NOT NULL,
    cycle_no              INTEGER NOT NULL REFERENCES cycles(cycle_no),
    claim                 TEXT NOT NULL,
    consequence           TEXT NOT NULL,
    remedy                TEXT NOT NULL,
    origin                TEXT NOT NULL,
    grade                 TEXT NOT NULL,
    locus_json            TEXT NOT NULL,
    evidence_json         TEXT NOT NULL,
    confidence            REAL NOT NULL,
    attempts              INTEGER NOT NULL,
    spent_tokens          INTEGER NOT NULL,
    first_seen_cycle      INTEGER NOT NULL,
    last_touched_cycle    INTEGER NOT NULL,
    supersedes_json       TEXT NOT NULL,
    PRIMARY KEY (hypothesis_id, cycle_no)
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id    TEXT NOT NULL,
    cycle_no     INTEGER NOT NULL REFERENCES cycles(cycle_no),
    kind         TEXT NOT NULL,
    locus_json   TEXT NOT NULL,
    detail       TEXT NOT NULL,
    value_json   TEXT NOT NULL,
    PRIMARY KEY (signal_id, cycle_no)
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id        TEXT NOT NULL PRIMARY KEY,
    path             TEXT NOT NULL DEFAULT '',
    first_seen_cycle INTEGER NOT NULL,
    last_seen_cycle  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    src_symbol TEXT NOT NULL,
    dst_symbol TEXT NOT NULL,
    edge_type  TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    PRIMARY KEY (src_symbol, dst_symbol, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_hypotheses_cycle
    ON hypotheses(cycle_no);
CREATE INDEX IF NOT EXISTS idx_hypotheses_grade
    ON hypotheses(grade);
CREATE INDEX IF NOT EXISTS idx_signals_cycle
    ON signals(cycle_no);
CREATE INDEX IF NOT EXISTS idx_signals_kind
    ON signals(kind);
"""


class MemoryStore:
    """Cross-cycle memory store.

    In ``readonly`` mode every read returns empty and every write is a no-op.
    The API and tool surface therefore remain identical in both ablation arms.
    """

    def __init__(self, memory_dir: str, *, readonly: bool = False) -> None:
        self._dir = memory_dir
        self.readonly = readonly
        self._db_path = os.path.join(memory_dir, _DB_FILENAME)
        if not readonly:
            os.makedirs(memory_dir, exist_ok=True)
            self._init_db()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='findings'"
            ).fetchone()
            version_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone()
            if existing is not None and version_table is None:
                raise RuntimeError(
                    "Guardian memory uses the obsolete findings-as-signals schema; "
                    "discard index.sqlite before running outer-loop blueprint v2"
                )
            connection.executescript(_SCHEMA)
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row is not None and int(row["value"]) != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported Guardian memory schema {row['value']}; "
                    f"expected {_SCHEMA_VERSION}"
                )
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(_SCHEMA_VERSION),),
            )

    def next_cycle_no(self) -> int:
        if self.readonly or not os.path.exists(self._db_path):
            return 1
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(cycle_no) FROM cycles").fetchone()
        return int(row[0] or 0) + 1

    def load_hypotheses(self) -> list:
        """Load the latest snapshot of every carried hypothesis."""
        if self.readonly or not os.path.exists(self._db_path):
            return []
        from ..loop.state import Hypothesis

        query = """
            SELECT h.*
            FROM hypotheses h
            JOIN (
                SELECT hypothesis_id, MAX(cycle_no) AS latest_cycle
                FROM hypotheses GROUP BY hypothesis_id
            ) latest
              ON latest.hypothesis_id = h.hypothesis_id
             AND latest.latest_cycle = h.cycle_no
            ORDER BY h.first_seen_cycle, h.hypothesis_id
        """
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [
            Hypothesis(
                id=row["hypothesis_id"],
                claim=row["claim"],
                consequence=row["consequence"],
                remedy=row["remedy"],
                grade=row["grade"],
                origin=row["origin"],
                locus=json.loads(row["locus_json"]),
                evidence=json.loads(row["evidence_json"]),
                confidence=float(row["confidence"]),
                attempts=int(row["attempts"]),
                spent_tokens=int(row["spent_tokens"]),
                first_seen_cycle=int(row["first_seen_cycle"]),
                last_touched_cycle=int(row["last_touched_cycle"]),
                supersedes=json.loads(row["supersedes_json"]),
            )
            for row in rows
        ]

    def persist_state(
        self,
        state: "CycleState",
        report: "GuardianReport",
        *,
        graph: "Optional[CodeGraph]" = None,
        memory_dir: Optional[str] = None,
    ) -> int:
        """Atomically persist one cycle boundary and all carried records."""
        if self.readonly:
            return -1
        token_cost = int(state.budget_spent)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cycles (
                    cycle_no, commit_sha, ts, token_cost, tests_summary,
                    has_graph, exit_reason, degraded, report_summary,
                    compaction_events, decision_log_json, carried_from
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.cycle_no,
                    state.commit,
                    report.generated_at,
                    token_cost,
                    report.tests_summary,
                    state.exit_reason or "",
                    int(state.degraded),
                    state.report_summary,
                    state.compaction_events,
                    json.dumps(state.decision_log, sort_keys=True),
                    state.carried_from,
                ),
            )
            for hypothesis in state.hypotheses:
                connection.execute(
                    """
                    INSERT INTO hypotheses (
                        hypothesis_id, cycle_no, claim, consequence, remedy,
                        origin, grade, locus_json, evidence_json, confidence,
                        attempts, spent_tokens, first_seen_cycle,
                        last_touched_cycle, supersedes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hypothesis.id,
                        state.cycle_no,
                        hypothesis.claim,
                        hypothesis.consequence,
                        hypothesis.remedy,
                        hypothesis.origin,
                        hypothesis.grade,
                        json.dumps(hypothesis.locus, sort_keys=True),
                        json.dumps(hypothesis.evidence, sort_keys=True),
                        hypothesis.confidence,
                        hypothesis.attempts,
                        hypothesis.spent_tokens,
                        hypothesis.first_seen_cycle,
                        hypothesis.last_touched_cycle,
                        json.dumps(hypothesis.supersedes, sort_keys=True),
                    ),
                )
                for locus in hypothesis.locus:
                    connection.execute(
                        """
                        INSERT INTO symbols (
                            symbol_id, path, first_seen_cycle, last_seen_cycle
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(symbol_id) DO UPDATE SET
                            last_seen_cycle=excluded.last_seen_cycle
                        """,
                        (
                            locus,
                            locus,
                            hypothesis.first_seen_cycle,
                            state.cycle_no,
                        ),
                    )
            for signal in state.signals:
                connection.execute(
                    """
                    INSERT INTO signals (
                        signal_id, cycle_no, kind, locus_json, detail, value_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal.id,
                        state.cycle_no,
                        signal.kind,
                        json.dumps(signal.locus, sort_keys=True),
                        signal.detail,
                        json.dumps(signal.value, sort_keys=True),
                    ),
                )

        if graph is not None:
            try:
                from ..signals.graph_diff import save_snapshot

                save_snapshot(graph, memory_dir or self._dir, state.commit)
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE cycles SET has_graph=1 WHERE cycle_no=?",
                        (state.cycle_no,),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("memory: failed to save graph snapshot: %s", exc)
        return state.cycle_no

    def recall(
        self,
        *,
        query: str = "",
        kind: Optional[str] = None,
        locus: Optional[str] = None,
        k: int = 10,
    ) -> List[dict]:
        """Pull semantic, locus, recent, or trajectory-shaped memory records."""
        if self.readonly or not os.path.exists(self._db_path):
            return []
        limit = max(1, min(int(k), 50))
        where = []
        parameters: list = []
        if locus:
            where.append("h.locus_json LIKE ?")
            parameters.append(f"%{locus}%")
        semantic_query = bool(query and kind in {None, "semantic"})
        if query and not semantic_query:
            where.append(
                "(h.hypothesis_id LIKE ? OR h.claim LIKE ? OR "
                "h.consequence LIKE ? OR h.remedy LIKE ? "
                "OR h.evidence_json LIKE ?)"
            )
            term = f"%{query}%"
            parameters.extend([term, term, term, term, term])
        clause = " WHERE " + " AND ".join(where) if where else ""
        order = (
            "h.hypothesis_id, h.cycle_no"
            if kind == "trajectory"
            else "h.cycle_no DESC, h.hypothesis_id"
        )
        sql = f"""
            SELECT h.*, c.commit_sha, c.exit_reason, c.degraded
            FROM hypotheses h
            JOIN cycles c ON c.cycle_no = h.cycle_no
            {clause}
            ORDER BY {order}
            LIMIT ?
        """
        sql_limit = 200 if semantic_query else limit
        parameters.append(sql_limit)
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        records = [self._memory_row(row) for row in rows]
        if semantic_query:
            records.sort(
                key=lambda record: self._semantic_score(query, record),
                reverse=True,
            )
        if kind == "trajectory":
            grouped: dict[str, list[dict]] = {}
            for record in records:
                grouped.setdefault(record["hypothesis_id"], []).append(record)
            for trajectory in grouped.values():
                resolved = [
                    item
                    for item in trajectory
                    if item["grade"] in {"finding", "supported", "refuted"}
                ]
                confirmation_rate = (
                    sum(item["grade"] == "finding" for item in resolved)
                    / len(resolved)
                    if resolved
                    else 0.0
                )
                grade_history = [
                    {
                        "cycle_no": item["cycle_no"],
                        "grade": item["grade"],
                        "confidence": item["confidence"],
                        "spent_tokens": item["spent_tokens"],
                    }
                    for item in trajectory
                ]
                for item in trajectory:
                    item["recurrence_count"] = len(trajectory)
                    item["confirmation_rate"] = confirmation_rate
                    item["grade_history"] = grade_history
        return records[:limit]

    @staticmethod
    def _semantic_score(query: str, record: dict) -> float:
        """Dependency-free relevance score for the semantic recall shape."""
        document = " ".join(
            [
                record["claim"],
                record["consequence"],
                record["remedy"],
                " ".join(record["locus"]),
            ]
        ).lower()
        query_normalized = query.lower().strip()
        query_terms = set(re.findall(r"[a-z0-9_]+", query_normalized))
        document_terms = set(re.findall(r"[a-z0-9_]+", document))
        overlap = (
            len(query_terms & document_terms) / len(query_terms)
            if query_terms
            else 0.0
        )
        sequence = SequenceMatcher(
            None, query_normalized, document[:2_000]
        ).ratio()
        return (0.8 * overlap) + (0.2 * sequence)

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> dict:
        return {
            "hypothesis_id": row["hypothesis_id"],
            "cycle_no": row["cycle_no"],
            "commit": row["commit_sha"],
            "claim": row["claim"],
            "consequence": row["consequence"],
            "remedy": row["remedy"],
            "origin": row["origin"],
            "grade": row["grade"],
            "locus": json.loads(row["locus_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "confidence": row["confidence"],
            "attempts": row["attempts"],
            "spent_tokens": row["spent_tokens"],
            "supersedes": json.loads(row["supersedes_json"]),
            "exit_reason": row["exit_reason"],
            "degraded": bool(row["degraded"]),
        }

    def recent_findings(self, k: int = 5) -> List[dict]:
        """Compatibility query: return recent grade=finding hypotheses."""
        return [
            row
            for row in self.recall(kind="recent", k=max(k * 3, k))
            if row["grade"] == "finding"
        ][:k]

    def recent_cycles(self, k: int = 10) -> List[dict]:
        if self.readonly or not os.path.exists(self._db_path):
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM cycles ORDER BY cycle_no DESC LIMIT ?", (k,)
            ).fetchall()
        return [dict(row) for row in rows]

    def cycle_count(self) -> int:
        if self.readonly or not os.path.exists(self._db_path):
            return 0
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM cycles").fetchone()
        return int(row[0] or 0)

    def load_prior_graph(
        self, memory_dir: Optional[str] = None
    ) -> "Optional[CodeGraph]":
        if self.readonly or not os.path.exists(self._db_path):
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT commit_sha FROM cycles WHERE has_graph=1 "
                "ORDER BY cycle_no DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        from ..signals.graph_diff import load_snapshot

        return load_snapshot(memory_dir or self._dir, row["commit_sha"])

    def persist_edge_changes(self, cycle_no: int, edge_changes: list) -> None:
        if self.readonly or not edge_changes:
            return
        with self._connect() as connection:
            for change in edge_changes:
                connection.execute(
                    """
                    INSERT INTO edges (
                        src_symbol, dst_symbol, edge_type, first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(src_symbol, dst_symbol, edge_type)
                    DO UPDATE SET last_seen=excluded.last_seen
                    """,
                    (
                        change.src,
                        change.dst,
                        change.edge_type,
                        cycle_no,
                        cycle_no,
                    ),
                )

    def symbol_history(self, symbol_id: str) -> dict:
        if self.readonly or not os.path.exists(self._db_path):
            return {}
        with self._connect() as connection:
            row = connection.execute(
                "SELECT first_seen_cycle, last_seen_cycle FROM symbols "
                "WHERE symbol_id=?",
                (symbol_id,),
            ).fetchone()
        return dict(row) if row else {}

    def edge_drift(self, since_cycle: int) -> List[dict]:
        if self.readonly or not os.path.exists(self._db_path):
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM edges WHERE first_seen >= ?", (since_cycle,)
            ).fetchall()
        return [dict(row) for row in rows]

    def assert_cross_cycle(self) -> None:
        assert self.cycle_count() >= 2, "expected at least two persisted cycles"
        assert self.recall(k=1), "no hypotheses readable across cycles"


def format_findings_for_prompt(findings: List[dict], max_findings: int = 5) -> str:
    """Compatibility formatter for callers not yet using the recall tool."""
    if not findings:
        return "(no prior findings in memory)"
    lines = []
    for item in findings[:max_findings]:
        commit = str(item.get("commit") or item.get("commit_sha") or "?")[:8]
        lines.append(
            f"[{commit}] ({item.get('grade', 'finding')}) "
            f"{item.get('claim') or item.get('title', '')}"
        )
        if item.get("remedy"):
            lines.append(f"  remedy: {str(item['remedy'])[:160]}")
    if len(findings) > max_findings:
        lines.append(f"  … ({len(findings) - max_findings} more not shown)")
    return "\n".join(lines)
