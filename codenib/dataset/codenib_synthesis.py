# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dataset wrapper and quality checks for ``sysevol-ai/codeminer-synthesis``."""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

import datasets
from datasets import Features, Value

from ..log_utils import get_logger
from .base import DatasetBase

logger = get_logger(__name__)


DEFAULT_DATASET = "sysevol-ai/codeminer-synthesis"
DEFAULT_SPLIT = "test"
ALL_CONFIGS = ("C++_C", "Go", "Python", "Rust", "TypeScript_JavaScript")

CONFIG_LANGUAGE_GROUP = {
    "C++_C": "C++/C",
    "Go": "Go",
    "Python": "Python",
    "Rust": "Rust",
    "TypeScript_JavaScript": "TypeScript/JavaScript",
}

LANGUAGE_EXTENSIONS = {
    "C++/C": {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"},
    "Go": {".go"},
    "Python": {".py", ".pyi", ".pyx"},
    "Rust": {".rs"},
    "TypeScript/JavaScript": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
}

EXPECTED_BASE_CATEGORY_COUNTS = {
    "behavioral": 20,
    "module_hint": 10,
    "file_hint": 10,
    "symbol_hint": 5,
    "reasoning": 5,
}

EXPECTED_TRAVERSAL_COUNT = 10
EXPECTED_CATEGORY_COUNTS_WITH_TRAVERSAL = {
    **EXPECTED_BASE_CATEGORY_COUNTS,
    "traversal": EXPECTED_TRAVERSAL_COUNT,
}


_SYMBOL_NODE_FEATURE = [
    {
        "content": Value("string"),
        "end_line": Value("int64"),
        "file": Value("string"),
        "node_name": Value("string"),
        "start_line": Value("int64"),
        "type": Value("string"),
    }
]

_CHAIN_METADATA_FEATURE = {
    "anchor_indices": [Value("int64")],
    "anchor_symbols": [Value("string")],
    "anti_grep_leaks_at_last_gen": [Value("string")],
    "chain_length": Value("int64"),
    "direction": Value("string"),
    "hidden_indices": [Value("int64")],
    "hidden_symbols": [Value("string")],
    "hop_count": Value("int64"),
    "spans_files": [Value("string")],
    "spans_n_files": Value("int64"),
    "stance": Value("string"),
}

SYNTHESIS_FEATURES = Features(
    {
        "repo": Value("string"),
        "instance_id": Value("string"),
        "base_commit": Value("string"),
        "query": Value("string"),
        "problem_statement": Value("string"),
        "category": Value("string"),
        "gt_symbols": [Value("string")],
        "gt_symbol_nodes": _SYMBOL_NODE_FEATURE,
        "gt_files": [Value("string")],
        "query_id": Value("string"),
        "length_variant": Value("string"),
        "judge_verdict": Value("string"),
        "judge_reason": Value("string"),
        "chain_metadata": _CHAIN_METADATA_FEATURE,
        "language_group": Value("string"),
        "source_config": Value("string"),
        "gt_code_blocks_count": Value("int64"),
    }
)


def parse_config_list(configs: Optional[Union[str, Sequence[str]]]) -> List[str]:
    """Normalize a config selector into concrete HF config names."""

    if configs is None:
        return list(ALL_CONFIGS)
    if isinstance(configs, str):
        raw = [part.strip() for part in configs.split(",") if part.strip()]
    else:
        raw = [str(part).strip() for part in configs if str(part).strip()]
    if not raw or any(part.lower() == "all" for part in raw):
        return list(ALL_CONFIGS)
    unknown = [name for name in raw if name not in ALL_CONFIGS]
    if unknown:
        raise ValueError(
            f"Unknown CodeNib synthesis config(s): {unknown}. "
            f"Valid configs: {', '.join(ALL_CONFIGS)}"
        )
    return raw


def language_group_for_config(config_name: str) -> str:
    """Return the canonical ``language_group`` label for a HF config."""

    return CONFIG_LANGUAGE_GROUP.get(config_name, config_name.replace("_", "/"))


def expected_extensions_for_language(language_group: str) -> set[str]:
    """Return accepted source extensions for a ``language_group`` label."""

    if language_group in LANGUAGE_EXTENSIONS:
        return set(LANGUAGE_EXTENSIONS[language_group])
    text = (language_group or "").lower()
    for label, extensions in LANGUAGE_EXTENSIONS.items():
        label_text = label.lower()
        if label_text == text or any(part in text for part in label_text.split("/")):
            return set(extensions)
    return set()


def normalize_synthesis_record(
    row: Mapping[str, Any], config_name: str
) -> Dict[str, Any]:
    """Project a HF synthesis row into the common benchmark shape."""

    gt_symbol_nodes = []
    for node in row.get("gt_symbol_nodes") or []:
        gt_symbol_nodes.append(
            {
                "content": node.get("content") or "",
                "end_line": int(node.get("end_line") or 0),
                "file": node.get("file") or "",
                "node_name": node.get("node_name") or "",
                "start_line": int(node.get("start_line") or 0),
                "type": node.get("type") or "",
            }
        )

    chain_metadata = row.get("chain_metadata")
    if chain_metadata is not None:
        chain_metadata = _normalize_chain_metadata(chain_metadata)

    query = row.get("query") or row.get("question") or ""
    return {
        "repo": row.get("repo") or "",
        "instance_id": row.get("instance_id") or "",
        "base_commit": row.get("base_commit") or "",
        "query": query,
        "problem_statement": row.get("problem_statement") or query,
        "category": row.get("category")
        or row.get("query_type")
        or row.get("difficulty")
        or "",
        "gt_symbols": list(row.get("gt_symbols") or row.get("target_symbols") or []),
        "gt_symbol_nodes": gt_symbol_nodes,
        "gt_files": list(row.get("gt_files") or row.get("target_files") or []),
        "query_id": row.get("query_id") or row.get("instance_id") or "",
        "length_variant": row.get("length_variant") or "",
        "judge_verdict": row.get("judge_verdict") or "",
        "judge_reason": row.get("judge_reason") or "",
        "chain_metadata": chain_metadata,
        "language_group": row.get("language_group")
        or language_group_for_config(config_name),
        "source_config": config_name,
        "gt_code_blocks_count": len(gt_symbol_nodes),
    }


def _normalize_chain_metadata(raw: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "anchor_indices": [int(v) for v in raw.get("anchor_indices") or []],
        "anchor_symbols": list(raw.get("anchor_symbols") or []),
        "anti_grep_leaks_at_last_gen": list(
            raw.get("anti_grep_leaks_at_last_gen") or []
        ),
        "chain_length": int(raw.get("chain_length") or 0),
        "direction": raw.get("direction") or "",
        "hidden_indices": [int(v) for v in raw.get("hidden_indices") or []],
        "hidden_symbols": list(raw.get("hidden_symbols") or []),
        "hop_count": int(raw.get("hop_count") or 0),
        "spans_files": list(raw.get("spans_files") or []),
        "spans_n_files": int(raw.get("spans_n_files") or 0),
        "stance": raw.get("stance") or "",
    }


def gt_file_language_mismatches(row: Mapping[str, Any]) -> List[str]:
    """Return GT files whose extensions do not match the row language group."""

    expected = expected_extensions_for_language(str(row.get("language_group") or ""))
    if not expected:
        return []
    files = set(row.get("gt_files") or [])
    for node in row.get("gt_symbol_nodes") or []:
        file_path = node.get("file") if isinstance(node, Mapping) else None
        if file_path:
            files.add(str(file_path))
    bad = []
    for file_path in sorted(files):
        suffix = Path(file_path).suffix.lower()
        if suffix and suffix not in expected:
            bad.append(file_path)
    return bad


def validate_synthesis_records(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_category_counts: Optional[Mapping[str, int]] = None,
    compare_category_distribution: bool = True,
) -> Dict[str, Any]:
    """Validate normalized synthesis records and return a structured report."""

    records = list(rows)
    issues: List[Dict[str, Any]] = []
    by_config_category: Dict[str, Counter[str]] = {}
    by_config: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    seen_query_ids: set[str] = set()

    for row in records:
        source_config = str(row.get("source_config") or "")
        query_id = str(row.get("query_id") or "")
        category = str(row.get("category") or "")
        by_config[source_config] += 1
        by_category[category] += 1
        by_config_category.setdefault(source_config, Counter())[category] += 1

        if not query_id:
            issues.append(_issue("error", "missing_query_id", row))
        elif query_id in seen_query_ids:
            issues.append(_issue("error", "duplicate_query_id", row))
        seen_query_ids.add(query_id)

        for key in ("repo", "instance_id", "base_commit", "query", "category"):
            if not row.get(key):
                issues.append(_issue("error", f"missing_{key}", row))

        if not row.get("gt_files") and not row.get("gt_symbols"):
            issues.append(_issue("error", "empty_ground_truth", row))

        if row.get("judge_verdict") and row.get("judge_verdict") != "valid":
            issues.append(_issue("warning", "non_valid_judge_verdict", row))

        bad_files = gt_file_language_mismatches(row)
        if bad_files:
            item = _issue("error", "gt_file_language_mismatch", row)
            item["files"] = bad_files
            item["language_group"] = row.get("language_group")
            issues.append(item)

    expected_counts = dict(expected_category_counts or {})
    if expected_counts:
        for config_name, counter in by_config_category.items():
            actual = {k: int(counter.get(k, 0)) for k in expected_counts}
            extras = {k: int(v) for k, v in counter.items() if k not in expected_counts}
            if actual != expected_counts or extras:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "category_counts_differ_from_expected",
                        "source_config": config_name,
                        "expected": expected_counts,
                        "actual": dict(sorted(counter.items())),
                    }
                )

    if compare_category_distribution and len(by_config_category) > 1:
        reference_config, reference_counts = sorted(by_config_category.items())[0]
        for config_name, counter in sorted(by_config_category.items())[1:]:
            if dict(counter) != dict(reference_counts):
                issues.append(
                    {
                        "severity": "warning",
                        "code": "category_distribution_mismatch",
                        "source_config": config_name,
                        "reference_config": reference_config,
                        "reference": dict(sorted(reference_counts.items())),
                        "actual": dict(sorted(counter.items())),
                    }
                )

    return {
        "row_count": len(records),
        "by_config": dict(sorted(by_config.items())),
        "by_category": dict(sorted(by_category.items())),
        "by_config_category": {
            cfg: dict(sorted(counter.items()))
            for cfg, counter in sorted(by_config_category.items())
        },
        "issues": issues,
        "error_count": sum(1 for issue in issues if issue["severity"] == "error"),
        "warning_count": sum(1 for issue in issues if issue["severity"] == "warning"),
    }


def _issue(severity: str, code: str, row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "query_id": row.get("query_id"),
        "instance_id": row.get("instance_id"),
        "source_config": row.get("source_config"),
        "category": row.get("category"),
    }


class CodeNibSynthesisDataset(DatasetBase):
    """Dataset wrapper for CodeNib Synthesis benchmark rows.

    The public HF dataset is organized by language config. CodeNib consumers
    expect each row to carry its own ``language_group`` and
    ``problem_statement`` fields, so this loader injects them while preserving
    the source config and synthesized GT columns.
    """

    simplified_symbols: bool = False

    def __init__(
        self,
        dataset: str = DEFAULT_DATASET,
        split: str = DEFAULT_SPLIT,
        configs: Optional[Union[str, Sequence[str]]] = None,
        filter_instance: str = ".*",
        root: str | None = None,
        repo_root: str | None = None,
        log: bool = True,
        force_reload: bool = False,
    ) -> None:
        super().__init__(root=root, log=log, force_reload=force_reload)
        self.dataset = dataset
        self.split = split
        self.configs = parse_config_list(configs)
        self.filter_instance = filter_instance
        self.repo_root = (
            self._resolve_root(repo_root) if repo_root else self.processed_dir
        )
        self._data: Optional[datasets.arrow_dataset.Dataset] = None

    def load(
        self,
        idx_list: Optional[Sequence[int]] = None,
        idx_range: Optional[Sequence[int]] = None,
    ) -> datasets.arrow_dataset.Dataset:
        data = self._load_dataset()
        if idx_list is not None and idx_range is not None:
            raise ValueError("Cannot have both idx_list and idx_range")
        if idx_list is not None:
            return data.select(list(idx_list))
        if idx_range is not None:
            start_idx, end_idx = idx_range[0], idx_range[1]
            if start_idx >= end_idx:
                raise ValueError("start_idx should be smaller than end_idx")
            return data.select(range(start_idx, end_idx))
        return data

    def _load_dataset(self) -> datasets.arrow_dataset.Dataset:
        if self._data is not None and not self.force_reload:
            return self._data

        records: List[Dict[str, Any]] = []
        for config_name in self.configs:
            try:
                ds = datasets.load_dataset(
                    self.dataset,
                    config_name,
                    split=self.split,
                    cache_dir=os.path.abspath(os.path.expanduser(self.processed_dir)),
                )
            except ValueError as exc:
                raise ValueError(
                    f"Failed to load {self.dataset!r} config {config_name!r} "
                    f"split {self.split!r}. CodeNib synthesis is expected to "
                    f"publish benchmark data under split {DEFAULT_SPLIT!r}."
                ) from exc
            logger.info(
                "Loaded %d rows from %s/%s split %s",
                len(ds),
                self.dataset,
                config_name,
                self.split,
            )
            records.extend(normalize_synthesis_record(row, config_name) for row in ds)

        data = datasets.Dataset.from_list(records, features=SYNTHESIS_FEATURES)
        data = data.filter(
            input_columns=["query_id", "instance_id"],
            function=lambda query_id, instance_id: bool(
                re.match(self.filter_instance, query_id)
                or re.match(self.filter_instance, instance_id)
            ),
        )
        self._data = data
        return data

    def get_repo_path(
        self, dataset_row: Dict[str, Any], repo_root: Union[Path, str, None] = None
    ) -> str:
        repo_name = dataset_row["repo"]
        root = self.repo_root
        if repo_root is not None:
            root = os.path.abspath(os.path.expanduser(str(repo_root)))
        repo_dir_name = repo_name.replace("/", "_")
        return os.path.join(root, repo_dir_name)

    def process_instance(
        self, dataset_row: Dict[str, Any], repo_root: Union[Path, str, None] = None
    ) -> None:
        """Clone the repository if needed and check out the row's base commit."""

        repo_name = dataset_row["repo"]
        base_commit = dataset_row["base_commit"]
        repo_path = self.get_repo_path(dataset_row, repo_root=repo_root)
        os.makedirs(os.path.dirname(repo_path), exist_ok=True)

        if not os.path.exists(repo_path):
            logger.info("Cloning repository %s to %s", repo_name, repo_path)
            subprocess.run(
                ["git", "clone", f"https://github.com/{repo_name}.git", repo_path],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            logger.info("Repository %s already exists at %s", repo_name, repo_path)

        original_dir = os.getcwd()
        os.chdir(repo_path)
        try:
            subprocess.run(
                [
                    "git",
                    "config",
                    "remote.origin.fetch",
                    "+refs/heads/*:refs/remotes/origin/*",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "fetch", "--all", "--tags"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "reset", "--hard"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "clean", "-fd"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "checkout", "-f", base_commit],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        finally:
            os.chdir(original_dir)

    def process(self) -> Any:
        self._data = self._load_dataset()
        return self._data

    def get_summary(self) -> Dict[str, Any]:
        data = self._data or self._load_dataset()
        return {
            "dataset": self.dataset,
            "split": self.split,
            "configs": list(self.configs),
            "instances": len(data),
            "by_language": dict(Counter(data["language_group"])),
            "by_category": dict(Counter(data["category"])),
            "by_source_config": dict(Counter(data["source_config"])),
        }

    def load_eval_metadata(self, path: str = "") -> Dict[str, Any]:
        if path:
            resolved = Path(os.path.expanduser(path)).resolve()
            if resolved.exists():
                return super().load_eval_metadata(str(resolved))
            logger.info(
                "No eval metadata file at %s; projecting GT columns from %s",
                resolved,
                self.dataset,
            )

        data = self._data or self._load_dataset()
        metadata: Dict[str, Any] = {}
        instance_counts = Counter(row["instance_id"] for row in data)
        for row in data:
            key = row.get("query_id") or row["instance_id"]
            entry = {
                "instance_id": row["instance_id"],
                "query_id": row.get("query_id"),
                "repo": row.get("repo"),
                "base_commit": row.get("base_commit"),
                "target_files": list(row.get("gt_files") or []),
                "symbols_modified": list(row.get("gt_symbols") or []),
                "symbols_deleted": [],
                "symbols_added": [],
                "code_blocks": list(row.get("gt_symbol_nodes") or []),
            }
            metadata[key] = entry
            if instance_counts[row["instance_id"]] == 1:
                metadata[row["instance_id"]] = entry
        return metadata
