# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free OrcaLoca localization schemas and published metrics.

OrcaLoca counts an instance as a file or function match when every location
derived from the golden patch appears in the predicted set. Extra predictions
do not change the binary match, so precision is reported separately.
"""

from __future__ import annotations

import ast
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .policy_compat import source_files


def _normalize_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _normalize_function_id(value: object) -> str:
    identifier = str(value or "").strip()
    if ":" not in identifier:
        return identifier
    file_path, symbol = identifier.split(":", 1)
    normalized_path = _normalize_path(file_path)
    return f"{normalized_path}:{symbol.strip()}" if normalized_path else ""


@dataclass(frozen=True, slots=True)
class OrcaLocaLocation:
    """One location in OrcaLoca's file/class/method output contract."""

    file_path: str
    class_name: str = ""
    method_name: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OrcaLocaLocation":
        """Normalize one upstream or CodeNib location record."""

        return cls(
            file_path=_normalize_path(value.get("file_path", value.get("path", ""))),
            class_name=str(value.get("class_name", "") or "").strip(),
            method_name=str(value.get("method_name", "") or "").strip(),
        )

    def function_ids(self) -> tuple[str, ...]:
        """Return the function identifiers emitted by OrcaLoca's artifact scorer."""

        if not self.file_path:
            return ()
        identifiers: list[str] = []
        if self.class_name:
            identifiers.append(f"{self.file_path}:{self.class_name}")
        if self.method_name:
            qualified = (
                f"{self.class_name}.{self.method_name}"
                if self.class_name
                else self.method_name
            )
            identifiers.append(f"{self.file_path}:{qualified}")
        return tuple(identifiers)

    def to_record(self) -> dict[str, str]:
        """Return the upstream JSON-compatible location representation."""

        return {
            "file_path": self.file_path,
            "class_name": self.class_name,
            "method_name": self.method_name,
        }


@dataclass(frozen=True, slots=True)
class OrcaLocaGroundTruth:
    """Golden-patch file and function identifiers for one benchmark instance."""

    files: tuple[str, ...]
    functions: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OrcaLocaGroundTruth":
        """Load the explicit labels used by the paired compatibility harness."""

        ground_truth = value.get("ground_truth", value)
        if not isinstance(ground_truth, Mapping):
            raise ValueError("OrcaLoca ground_truth must be a mapping")
        missing = {
            key for key in ("gold_files", "gold_functions") if key not in ground_truth
        }
        if missing:
            raise ValueError(
                "OrcaLoca ground truth is missing " + ", ".join(sorted(missing))
            )

        raw_files = _label_sequence(ground_truth["gold_files"], "gold_files")
        raw_functions = _label_sequence(
            ground_truth["gold_functions"], "gold_functions"
        )
        files = {path for path in (_normalize_path(item) for item in raw_files) if path}
        functions = {
            identifier
            for identifier in (_normalize_function_id(item) for item in raw_functions)
            if identifier
        }
        return cls(files=tuple(sorted(files)), functions=tuple(sorted(functions)))


@dataclass(frozen=True, slots=True)
class OrcaLocaScore:
    """Per-instance OrcaLoca localization metrics."""

    gold_files: tuple[str, ...]
    predicted_files: tuple[str, ...]
    file_match: bool
    file_precision: float
    gold_functions: tuple[str, ...]
    predicted_functions: tuple[str, ...]
    function_match: bool
    function_precision: float

    def to_record(self) -> dict[str, Any]:
        """Return stable JSON-ready metric fields."""

        return {
            "gold_files": list(self.gold_files),
            "predicted_files": list(self.predicted_files),
            "file_match": self.file_match,
            "file_precision": self.file_precision,
            "gold_functions": list(self.gold_functions),
            "predicted_functions": list(self.predicted_functions),
            "function_match": self.function_match,
            "function_precision": self.function_precision,
        }


@dataclass(frozen=True, slots=True)
class OrcaLocaSummary:
    """Aggregate OrcaLoca metrics over a fixed instance set."""

    n: int
    file_match_count: int
    file_match_rate: float | None
    function_match_count: int
    function_match_rate: float | None
    file_precision_mean: float | None
    function_precision_mean: float | None

    def to_record(self) -> dict[str, Any]:
        """Return stable JSON-ready aggregate fields."""

        return {
            "n": self.n,
            "file_match_count": self.file_match_count,
            "file_match_rate": self.file_match_rate,
            "function_match_count": self.function_match_count,
            "function_match_rate": self.function_match_rate,
            "file_precision_mean": self.file_precision_mean,
            "function_precision_mean": self.function_precision_mean,
        }


def parse_orcaloca_locations(
    payload: str | Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> tuple[OrcaLocaLocation, ...]:
    """Parse OrcaLoca output or an already-extracted location list.

    Malformed model output is treated as an empty prediction, matching the
    benchmark denominator behavior without making batch evaluation fail.
    """

    parsed: Any = payload
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return ()

    if isinstance(parsed, Mapping):
        raw_locations = parsed.get("bug_locations")
        if not raw_locations:
            raw_locations = parsed.get("top_k_bug_locations")
    else:
        raw_locations = parsed

    if not isinstance(raw_locations, Sequence) or isinstance(
        raw_locations, (str, bytes)
    ):
        return ()

    locations = []
    for value in raw_locations:
        if not isinstance(value, Mapping):
            continue
        location = OrcaLocaLocation.from_mapping(value)
        if location.file_path:
            locations.append(location)
    return tuple(locations)


def _match_repo_path(raw: str, valid_files: set[str]) -> str | None:
    candidate = _normalize_path(raw)
    if candidate in valid_files:
        return candidate
    matches = [file_path for file_path in valid_files if candidate.endswith(file_path)]
    return max(matches, key=len) if matches else None


def _resolve_python_entity(
    repo_path: Path,
    file_path: str,
    entity_name: str,
) -> tuple[int, int] | None:
    path = repo_path / file_path
    if path.suffix != ".py" or not path.is_file():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None

    parts = [part for part in entity_name.strip().strip("`() ").split(".") if part]
    if not parts:
        return None
    if len(parts) == 1:
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == parts[0]
            ):
                return node.lineno, node.end_lineno or node.lineno
        return None

    class_name, method_name = parts[-2:]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in ast.walk(node):
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == method_name
                ):
                    return child.lineno, child.end_lineno or child.lineno
    return None


def resolve_orcaloca_locations(
    payload: str | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    repo_path: str | Path,
) -> tuple[dict[str, Any], ...]:
    """Resolve free-form OrcaLoca output to files and Python source ranges."""

    root = Path(repo_path).expanduser().resolve()
    valid_files = set(source_files(root))
    resolved_locations: list[dict[str, Any]] = []
    for location in parse_orcaloca_locations(payload):
        file_path = _match_repo_path(location.file_path, valid_files)
        if not file_path:
            continue
        entity_name = ".".join(
            part for part in (location.class_name, location.method_name) if part
        )
        resolved = (
            _resolve_python_entity(root, file_path, entity_name)
            if entity_name
            else None
        )
        start, end = resolved if resolved else (1, -1)
        resolved_locations.append(
            {
                "path": file_path,
                "class_name": location.class_name,
                "method_name": location.method_name,
                "start": start,
                "end": end,
            }
        )
    return tuple(resolved_locations)


def orcaloca_regions(
    payload: str | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    repo_path: str | Path,
) -> tuple[dict[str, int | str], ...]:
    """Return source-region fields for the common localization scorer."""

    return tuple(
        {
            "path": str(location["path"]),
            "start": int(location["start"]),
            "end": int(location["end"]),
        }
        for location in resolve_orcaloca_locations(payload, repo_path=repo_path)
    )


def _label_sequence(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"OrcaLoca {field} must be a sequence")
    return value


def score_orcaloca_locations(
    ground_truth: OrcaLocaGroundTruth,
    locations: Sequence[OrcaLocaLocation | Mapping[str, Any]],
) -> OrcaLocaScore:
    """Score predictions with OrcaLoca's golden-patch subset semantics."""

    normalized_locations = tuple(
        (
            location
            if isinstance(location, OrcaLocaLocation)
            else OrcaLocaLocation.from_mapping(location)
        )
        for location in locations
    )
    predicted_files = {
        location.file_path for location in normalized_locations if location.file_path
    }
    predicted_functions = {
        identifier
        for location in normalized_locations
        for identifier in location.function_ids()
    }
    gold_files = set(ground_truth.files)
    gold_functions = set(ground_truth.functions)

    return OrcaLocaScore(
        gold_files=tuple(sorted(gold_files)),
        predicted_files=tuple(sorted(predicted_files)),
        file_match=gold_files.issubset(predicted_files),
        file_precision=(
            len(gold_files.intersection(predicted_files)) / len(predicted_files)
            if predicted_files
            else 1.0
        ),
        gold_functions=tuple(sorted(gold_functions)),
        predicted_functions=tuple(sorted(predicted_functions)),
        function_match=gold_functions.issubset(predicted_functions),
        function_precision=(
            len(gold_functions.intersection(predicted_functions))
            / len(predicted_functions)
            if predicted_functions
            else 1.0
        ),
    )


def summarize_orcaloca_scores(
    scores: Sequence[OrcaLocaScore],
) -> OrcaLocaSummary:
    """Aggregate per-instance scores without dropping failed predictions."""

    n = len(scores)
    if not scores:
        return OrcaLocaSummary(
            n=0,
            file_match_count=0,
            file_match_rate=None,
            function_match_count=0,
            function_match_rate=None,
            file_precision_mean=None,
            function_precision_mean=None,
        )

    file_match_count = sum(score.file_match for score in scores)
    function_match_count = sum(score.function_match for score in scores)
    return OrcaLocaSummary(
        n=n,
        file_match_count=file_match_count,
        file_match_rate=file_match_count / n,
        function_match_count=function_match_count,
        function_match_rate=function_match_count / n,
        file_precision_mean=statistics.fmean(score.file_precision for score in scores),
        function_precision_mean=statistics.fmean(
            score.function_precision for score in scores
        ),
    )


__all__ = [
    "OrcaLocaGroundTruth",
    "OrcaLocaLocation",
    "OrcaLocaScore",
    "OrcaLocaSummary",
    "orcaloca_regions",
    "parse_orcaloca_locations",
    "resolve_orcaloca_locations",
    "score_orcaloca_locations",
    "summarize_orcaloca_scores",
]
