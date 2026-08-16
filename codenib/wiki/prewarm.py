# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded Wiki cache prewarming with machine-readable results."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from time import perf_counter
from typing import Any, Callable, Iterable, Optional


def _flatten(pages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        flattened.append(page)
        flattened.extend(_flatten(page.get("children") or []))
    return flattened


def _scope_pages(
    pages: list[dict[str, Any]],
    scope: str,
) -> list[dict[str, Any]]:
    if scope == "overview":
        return [page for page in _flatten(pages) if page.get("id") == "overview"][:1]
    if scope == "root":
        return [page for page in pages if isinstance(page, dict)]
    if scope == "all":
        return _flatten(pages)
    raise ValueError(f"unsupported prewarm scope: {scope!r}")


def prewarm_wiki_cache(
    registry: Any,
    *,
    wiki_factory: Callable[[Any], Any],
    repo_ids: Optional[Iterable[str]] = None,
    scope: str = "overview",
    workers: int = 2,
    max_pages: Optional[int] = None,
    dry_run: bool = False,
    retry_degraded_now: bool = False,
    release_views: bool = False,
) -> dict[str, Any]:
    """Generate only cold or due-for-retry pages in a bounded worker pool."""

    started = perf_counter()
    selected = set(repo_ids or ())
    worker_count = max(1, min(int(workers), 8))
    limit = None if max_pages is None else max(0, int(max_pages))
    results: list[dict[str, Any]] = []
    repo_records: list[tuple[str, Any]] = []
    selected_for_generation = 0
    selection_lock = Lock()

    for info in registry.list_infos():
        repo_id = str(info.id)
        if selected and repo_id not in selected:
            continue
        bundle = registry.get(repo_id)
        if bundle is None:
            continue
        repo_records.append((repo_id, bundle))

    def reserve_generation() -> bool:
        nonlocal selected_for_generation
        with selection_lock:
            if limit is not None and selected_for_generation >= limit:
                return False
            selected_for_generation += 1
            return True

    def process_repo(repo_id: str, bundle: Any) -> list[dict[str, Any]]:
        repo_results: list[dict[str, Any]] = []
        wiki = wiki_factory(bundle)
        try:
            tree = wiki.page_tree()
        except Exception as exc:  # noqa: BLE001 - report one repo, keep the batch
            repo_results.append(
                {
                    "repo": repo_id,
                    "id": "",
                    "title": "",
                    "before": "unknown",
                    "status": "error",
                    "error": f"outline unavailable: {exc}",
                }
            )
        else:
            scoped = _scope_pages(tree, scope)
            if scope == "overview" and not scoped:
                repo_results.append(
                    {
                        "repo": repo_id,
                        "id": "overview",
                        "title": "Overview",
                        "before": "missing",
                        "status": "error",
                        "error": "outline has no Overview page",
                    }
                )
            else:
                for page_ref in scoped:
                    page_id = str(page_ref.get("id") or "")
                    title = str(page_ref.get("title") or page_id)
                    before = str(page_ref.get("cache_state") or "unknown")
                    retry_ready = False
                    needs_operator_retry = getattr(
                        wiki,
                        "page_needs_operator_retry",
                        None,
                    )
                    if (
                        before == "ready"
                        and retry_degraded_now
                        and callable(needs_operator_retry)
                    ):
                        retry_ready = bool(needs_operator_retry(page_id))
                    if before == "ready" and not retry_ready:
                        repo_results.append(
                            {
                                "repo": repo_id,
                                "id": page_id,
                                "title": title,
                                "before": before,
                                "status": "skipped_ready",
                            }
                        )
                        continue
                    if before == "degraded" and not retry_degraded_now:
                        repo_results.append(
                            {
                                "repo": repo_id,
                                "id": page_id,
                                "title": title,
                                "before": before,
                                "status": "skipped_retry_cooldown",
                            }
                        )
                        continue
                    if not reserve_generation():
                        repo_results.append(
                            {
                                "repo": repo_id,
                                "id": page_id,
                                "title": title,
                                "before": before,
                                "status": "skipped_limit",
                            }
                        )
                        continue
                    if dry_run:
                        repo_results.append(
                            {
                                "repo": repo_id,
                                "id": page_id,
                                "title": title,
                                "before": before,
                                "status": "planned",
                            }
                        )
                        continue
                    page_started = perf_counter()
                    try:
                        if retry_degraded_now:
                            page = wiki.page(page_id, retry_degraded_now=True)
                        else:
                            page = wiki.page(page_id)
                        if not isinstance(page, dict):
                            raise RuntimeError(
                                "page was not found after outline selection"
                            )
                        generation = page.get("generation") or {}
                        repo_results.append(
                            {
                                "repo": repo_id,
                                "id": page_id,
                                "title": title,
                                "before": before,
                                "status": "warmed",
                                "duration_ms": round(
                                    (perf_counter() - page_started) * 1000,
                                    1,
                                ),
                                "generation_mode": generation.get("mode"),
                                "quality_valid": (page.get("quality") or {}).get(
                                    "valid"
                                ),
                                "metrics": generation.get("metrics"),
                            }
                        )
                    except Exception as exc:  # noqa: BLE001 - retain other pages
                        repo_results.append(
                            {
                                "repo": repo_id,
                                "id": page_id,
                                "title": title,
                                "before": before,
                                "status": "error",
                                "duration_ms": round(
                                    (perf_counter() - page_started) * 1000,
                                    1,
                                ),
                                "error": str(exc),
                            }
                        )
        if release_views:
            release = getattr(bundle, "release_views", None)
            if callable(release):
                try:
                    release()
                except Exception as exc:  # noqa: BLE001 - report cleanup
                    repo_results.append(
                        {
                            "repo": repo_id,
                            "id": "",
                            "title": "",
                            "before": "loaded",
                            "status": "release_error",
                            "error": str(exc),
                        }
                    )
        return repo_results

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(process_repo, repo_id, bundle)
            for repo_id, bundle in repo_records
        ]
        for future in as_completed(futures):
            results.extend(future.result())

    results.sort(key=lambda item: (item["repo"], item["id"]))
    counts: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema_version": 1,
        "scope": scope,
        "dry_run": dry_run,
        "retry_degraded_now": retry_degraded_now,
        "release_views": release_views,
        "workers": worker_count,
        "repositories": len(repo_records),
        "selected_for_generation": selected_for_generation,
        "duration_ms": round((perf_counter() - started) * 1000, 1),
        "counts": dict(sorted(counts.items())),
        "pages": results,
    }


__all__ = ["prewarm_wiki_cache"]
