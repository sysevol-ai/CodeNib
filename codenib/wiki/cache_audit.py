# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only coverage and quality audit for persisted AgentWiki pages."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Callable, Iterable, Optional

from .._bounded_json import canonical_json_bytes
from .agent_wiki import AgentWiki
from .store import WikiStore, WikiStoredEntry


def _summary(values: Iterable[float]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "max": None}

    def percentile(fraction: float) -> float:
        index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
        return round(ordered[index], 1)

    return {
        "count": len(ordered),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "max": round(ordered[-1], 1),
    }


def _coverage(cached: int, total: int) -> dict[str, Any]:
    return {
        "cached": cached,
        "total": total,
        "missing": max(0, total - cached),
        "percent": round((cached / total * 100) if total else 100.0, 1),
    }


def _stored_entry_bytes(entry: WikiStoredEntry) -> int:
    """Return the domain-canonical byte size of one stored envelope."""

    return len(canonical_json_bytes(dict(entry.envelope)))


def audit_wiki_cache(
    registry: Any,
    *,
    model: str,
    repo_ids: Optional[Iterable[str]] = None,
    wiki_factory: Callable[..., AgentWiki] = AgentWiki,
    store: Optional[WikiStore] = None,
) -> dict[str, Any]:
    """Inspect only cache entries reachable from each current Wiki outline.

    The function never calls ``outline()`` or ``page()`` and therefore never
    invokes a model. Missing outlines are reported instead of generated.
    """

    selected = {str(repo_id) for repo_id in repo_ids or ()}
    totals = Counter()
    modes = Counter()
    missing_overviews: list[str] = []
    missing_outlines: list[str] = []
    degraded_pages: list[dict[str, Any]] = []
    quality_invalid_pages: list[dict[str, Any]] = []
    fallback_pages: list[dict[str, Any]] = []
    retry_scheduled_pages: list[dict[str, Any]] = []
    retry_exhausted_pages: list[dict[str, Any]] = []
    current_entry_ids: set[str] = set()
    metric_samples: dict[str, list[float]] = {
        "total_ms": [],
        "retrieval_ms": [],
        "planning_ms": [],
        "model_call_ms": [],
        "model_calls": [],
        "repair_attempts": [],
    }
    repo_reports: list[dict[str, Any]] = []

    infos = list(registry.list_infos())
    available_repo_ids = {str(info.id) for info in infos}
    unknown_repo_ids = sorted(selected - available_repo_ids)
    if unknown_repo_ids:
        raise ValueError(
            "unknown repository selector(s): " + ", ".join(unknown_repo_ids)
        )

    for info in infos:
        repo_id = str(info.id)
        if selected and repo_id not in selected:
            continue
        bundle = registry.get(repo_id)
        if bundle is None:
            continue
        wiki_kwargs: dict[str, Any] = {"model": model}
        if store is not None:
            wiki_kwargs["store"] = store
        wiki = wiki_factory(bundle, **wiki_kwargs)
        current_entry_ids.add(wiki._store_entry_id("outline"))
        outline = wiki._read_cache("outline")
        if not isinstance(outline, dict) or not outline.get("pages"):
            missing_outlines.append(repo_id)
            repo_reports.append(
                {
                    "repo": repo_id,
                    "pages": _coverage(0, 0),
                    "root_pages": _coverage(0, 0),
                    "child_pages": _coverage(0, 0),
                    "overview_cached": False,
                    "outline_cached": False,
                }
            )
            continue

        wiki._outline = outline
        repo_counts = Counter()

        def inspect(
            raw_meta: dict[str, Any],
            depth: int,
            *,
            repo_counts: Counter = repo_counts,
            repo_id: str = repo_id,
            wiki: AgentWiki = wiki,
            outline: dict[str, Any] = outline,
        ) -> None:
            totals["pages"] += 1
            repo_counts["pages"] += 1
            scope = "root" if depth == 0 else "child"
            totals[f"{scope}_pages"] += 1
            repo_counts[f"{scope}_pages"] += 1
            is_overview = raw_meta.get("id") == "overview"
            if is_overview:
                totals["overviews"] += 1
                meta = wiki._overview_page_meta(
                    raw_meta,
                    (outline.get("pages") or [])[1:],
                )
            else:
                meta = raw_meta

            suffix = wiki._page_cache_suffix(meta)
            current_entry_ids.add(wiki._store_entry_id(suffix))
            current_entry_ids.add(wiki._store_entry_id(f"evidence_{suffix}"))
            page = wiki._read_cache(suffix)
            if isinstance(page, dict):
                totals["cached_pages"] += 1
                repo_counts["cached_pages"] += 1
                totals[f"cached_{scope}_pages"] += 1
                repo_counts[f"cached_{scope}_pages"] += 1
                if is_overview:
                    totals["cached_overviews"] += 1

                generation = page.get("generation") or {}
                mode = str(generation.get("mode") or "unknown")
                modes[mode] += 1
                record = {
                    "repo": repo_id,
                    "id": str(page.get("id") or meta.get("id") or ""),
                    "title": str(page.get("title") or meta.get("title") or ""),
                    "mode": mode,
                    "reason": generation.get("reason"),
                    "fallback": generation.get("fallback"),
                    "quality_valid": (page.get("quality") or {}).get("valid"),
                    "retry": generation.get("retry"),
                }
                if mode == "degraded":
                    degraded_pages.append(record)
                if generation.get("fallback"):
                    fallback_pages.append(record)
                if (page.get("quality") or {}).get("valid") is False:
                    quality_invalid_pages.append(record)
                retry_state = (generation.get("retry") or {}).get("state")
                if retry_state == "scheduled":
                    retry_scheduled_pages.append(record)
                elif retry_state == "exhausted":
                    retry_exhausted_pages.append(record)

                metrics = generation.get("metrics") or {}
                for name in metric_samples:
                    value = metrics.get(name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        metric_samples[name].append(float(value))
            elif is_overview:
                missing_overviews.append(repo_id)

            for child in raw_meta.get("children") or []:
                if isinstance(child, dict):
                    inspect(child, depth + 1)

        for root in outline.get("pages") or []:
            if isinstance(root, dict):
                inspect(root, 0)

        repo_reports.append(
            {
                "repo": repo_id,
                "pages": _coverage(
                    repo_counts["cached_pages"],
                    repo_counts["pages"],
                ),
                "root_pages": _coverage(
                    repo_counts["cached_root_pages"],
                    repo_counts["root_pages"],
                ),
                "child_pages": _coverage(
                    repo_counts["cached_child_pages"],
                    repo_counts["child_pages"],
                ),
                "overview_cached": repo_id not in missing_overviews,
                "outline_cached": True,
            }
        )

    if store is not None:
        entries = store.scan(repository_ids=selected or None)
        orphan_entries = tuple(
            entry for entry in entries if entry.entry_id not in current_entry_ids
        )
        database_payload_bytes = sum(_stored_entry_bytes(entry) for entry in entries)
        orphan_database_payload_bytes = sum(
            _stored_entry_bytes(entry) for entry in orphan_entries
        )
        storage_report = {
            "backend": type(store).__name__,
            "entries": len(entries),
            "database_payload_bytes": database_payload_bytes,
            "orphan_entries": len(orphan_entries),
            "orphan_database_payload_bytes": orphan_database_payload_bytes,
        }
    else:
        storage_report = {
            "backend": None,
            "entries": 0,
            "database_payload_bytes": 0,
            "orphan_entries": 0,
            "orphan_database_payload_bytes": 0,
        }
    return {
        "schema_version": 2,
        "repositories": len(repo_reports),
        "coverage": {
            "all": _coverage(totals["cached_pages"], totals["pages"]),
            "overview": _coverage(
                totals["cached_overviews"],
                totals["overviews"],
            ),
            "root": _coverage(
                totals["cached_root_pages"],
                totals["root_pages"],
            ),
            "child": _coverage(
                totals["cached_child_pages"],
                totals["child_pages"],
            ),
        },
        "generation_modes": dict(sorted(modes.items())),
        "generation_metrics": {
            name: _summary(values) for name, values in metric_samples.items()
        },
        "missing_outlines": sorted(missing_outlines),
        "missing_overviews": sorted(missing_overviews),
        "degraded_pages": sorted(
            degraded_pages,
            key=lambda item: (item["repo"], item["id"]),
        ),
        "quality_invalid_pages": sorted(
            quality_invalid_pages,
            key=lambda item: (item["repo"], item["id"]),
        ),
        "fallback_pages": sorted(
            fallback_pages,
            key=lambda item: (item["repo"], item["id"]),
        ),
        "retry_scheduled_pages": sorted(
            retry_scheduled_pages,
            key=lambda item: (item["repo"], item["id"]),
        ),
        "retry_exhausted_pages": sorted(
            retry_exhausted_pages,
            key=lambda item: (item["repo"], item["id"]),
        ),
        "storage": storage_report,
        "repos": sorted(repo_reports, key=lambda item: item["repo"]),
    }


__all__ = ["audit_wiki_cache"]
