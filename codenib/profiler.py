# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import threading
import time
from contextlib import ContextDecorator
from dataclasses import dataclass, field, replace
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple

from .log_utils import get_logger

__all__ = ["Profiler", "RangeStats", "percentile_from_sorted"]


def percentile_from_sorted(sorted_values: List[float], p: float) -> float:
    """Linear-interpolated percentile from a pre-sorted list."""
    if not sorted_values:
        return 0.0
    if p <= 0:
        return float(sorted_values[0])
    if p >= 100:
        return float(sorted_values[-1])
    n = len(sorted_values)
    rank = (p / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


@dataclass
class RangeStats:
    """Aggregate timing statistics captured for a profiler section.

    Optional per-call sample lists (``samples``, ``rss_deltas``, ``gpu_peaks``)
    are populated only when the owning ``Profiler`` is configured with
    ``record_samples`` and/or ``record_memory``. When empty, the percentile and
    memory accessors return zero so consumers can ignore them safely.
    """

    total: float = 0.0
    count: int = 0
    max_duration: float = 0.0
    min_duration: float = field(default_factory=lambda: float("inf"))
    errors: int = 0
    samples: List[float] = field(default_factory=list)
    rss_deltas: List[int] = field(default_factory=list)
    gpu_peaks: List[int] = field(default_factory=list)

    def update(
        self,
        duration: float,
        had_error: bool,
        *,
        sample: bool = False,
        rss_delta: Optional[int] = None,
        gpu_peak: Optional[int] = None,
    ) -> None:
        self.total += duration
        self.count += 1
        if duration > self.max_duration:
            self.max_duration = duration
        if duration < self.min_duration:
            self.min_duration = duration
        if had_error:
            self.errors += 1
        if sample:
            self.samples.append(duration)
        if rss_delta is not None:
            self.rss_deltas.append(int(rss_delta))
        if gpu_peak is not None:
            self.gpu_peaks.append(int(gpu_peak))

    @property
    def average(self) -> float:
        if not self.count:
            return 0.0
        return self.total / self.count

    @property
    def safe_min(self) -> float:
        if self.min_duration == float("inf"):
            return 0.0
        return self.min_duration

    def percentile(self, p: float) -> float:
        """Linear-interpolated percentile over recorded duration samples."""
        if not self.samples:
            return 0.0
        return percentile_from_sorted(sorted(self.samples), p)

    def percentiles(self, *ps: float) -> Dict[float, float]:
        """Compute multiple percentiles in one sort pass."""
        if not self.samples:
            return {p: 0.0 for p in ps}
        ordered = sorted(self.samples)
        return {p: percentile_from_sorted(ordered, p) for p in ps}

    @property
    def rss_delta_max(self) -> int:
        return max(self.rss_deltas) if self.rss_deltas else 0

    @property
    def rss_delta_avg(self) -> float:
        if not self.rss_deltas:
            return 0.0
        return sum(self.rss_deltas) / len(self.rss_deltas)

    @property
    def gpu_peak_max(self) -> int:
        return max(self.gpu_peaks) if self.gpu_peaks else 0

    @property
    def gpu_peak_avg(self) -> float:
        if not self.gpu_peaks:
            return 0.0
        return sum(self.gpu_peaks) / len(self.gpu_peaks)


class Profiler:
    """
    Lightweight hierarchical profiler for logging execution time of code sections.

    The profiler is designed to replace hand-written timing calls and provide
    reusable instrumentation with per-section summaries. Sections can be used
    as context managers or decorators:

        profiler = Profiler("scip_indexer")

        with profiler.section("generate_index"):
            ...

        @profiler.time_function()
        def compute():
            ...
    """

    def __init__(
        self,
        name: str = "profiler",
        *,
        enabled: bool = True,
        logger: Optional[logging.Logger] = None,
        emit_events: bool = True,
        range_level: int = logging.DEBUG,
        summary_level: int = logging.INFO,
        indent: int = 2,
        record_samples: bool = False,
        record_memory: bool = False,
    ) -> None:
        self.name = name
        self.enabled = enabled
        self.emit_events = emit_events
        self.range_level = range_level
        self.summary_level = summary_level
        self.indent = indent
        self.record_samples = record_samples
        self.record_memory = record_memory
        self.logger = logger or get_logger(name)

        self._thread_state = threading.local()
        self._lock = threading.Lock()
        self._stats: Dict[str, RangeStats] = {}

        self._psutil_process = None
        self._torch_cuda = None
        if self.record_memory:
            try:
                import psutil

                self._psutil_process = psutil.Process()
            except Exception:
                self._psutil_process = None
            try:
                import torch

                if torch.cuda.is_available():
                    self._torch_cuda = torch.cuda
            except Exception:
                self._torch_cuda = None

    # --------------------------------------------------------------------- #
    # Memory snapshot helpers
    # --------------------------------------------------------------------- #
    def _rss_now(self) -> Optional[int]:
        if self._psutil_process is None:
            return None
        try:
            return int(self._psutil_process.memory_info().rss)
        except Exception:
            return None

    def _gpu_peak(self) -> Optional[int]:
        if self._torch_cuda is None:
            return None
        try:
            return int(self._torch_cuda.max_memory_allocated())
        except Exception:
            return None

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #
    def section(
        self,
        label: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "_ProfilerRange":
        """
        Create a profiling section context manager for the given label.

        Args:
            label: Name of the section to record.
            metadata: Optional values to include in the emitted logs.
        """
        return _ProfilerRange(self, label, metadata)

    def time_function(
        self,
        label: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """
        Decorator helper for profiling function execution time.

        Args:
            label: Override label for the recorded section. Defaults to the
                function's qualified name.
            metadata: Optional metadata to include in the emitted logs.
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            section_label = label or func.__qualname__

            @wraps(func)
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                with self.section(section_label, metadata):
                    return func(*args, **kwargs)

            return wrapped

        return decorator

    def report(
        self, *, reset: bool = False, top_n: Optional[int] = None
    ) -> List[Tuple[str, RangeStats]]:
        """
        Emit an aggregated timing report ordered by total time spent per section.

        Args:
            reset: Clear stored statistics after reporting.
            top_n: Limit output to the N most expensive sections.
        Returns:
            List of (label, RangeStats snapshot) pairs sorted by total duration.
        """
        if not self.enabled:
            return []

        with self._lock:
            items = list(self._stats.items())

        if not items:
            self.logger.log(
                self.summary_level, "[%s] profiler: no sections recorded", self.name
            )
            return []

        total_sections = len(items)
        grand_total = max((stats.total for _, stats in items), default=0.0)

        # Order by total duration (descending) to mirror flame graph style reports.
        items.sort(key=lambda item: item[1].total, reverse=True)
        if top_n is not None:
            items = items[:top_n]

        summary_items: List[Tuple[str, RangeStats]] = []

        self.logger.log(
            self.summary_level,
            "[%s] profiler summary: %.3fs total across %d section(s)",
            self.name,
            grand_total,
            total_sections,
        )

        for label, stats in items:
            snapshot = replace(stats)
            summary_items.append((label, snapshot))
            extra = f", errors={stats.errors}" if stats.errors else ""
            self.logger.log(
                self.summary_level,
                "  %-40s total=%6.3fs count=%3d avg=%6.3fs min=%6.3fs max=%6.3fs%s",
                label,
                stats.total,
                stats.count,
                stats.average,
                stats.safe_min,
                stats.max_duration,
                extra,
            )

        if reset:
            self.reset()

        return summary_items

    def reset(self) -> None:
        """Clear collected statistics and reset the section stack."""
        with self._lock:
            self._stats.clear()
        # Reset per-thread stacks.
        self._thread_state.__dict__.clear()

    # --------------------------------------------------------------------- #
    # Internal helpers
    # --------------------------------------------------------------------- #
    def _record(
        self,
        label: str,
        duration: float,
        had_error: bool,
        *,
        rss_delta: Optional[int] = None,
        gpu_peak: Optional[int] = None,
    ) -> None:
        with self._lock:
            stats = self._stats.get(label)
            if stats is None:
                stats = RangeStats()
                self._stats[label] = stats
            stats.update(
                duration,
                had_error,
                sample=self.record_samples,
                rss_delta=rss_delta,
                gpu_peak=gpu_peak,
            )

    def _get_stack(self) -> list:
        stack = getattr(self._thread_state, "stack", None)
        if stack is None:
            stack = []
            self._thread_state.stack = stack
        return stack

    def _format_metadata(self, metadata: Optional[Dict[str, Any]]) -> str:
        if not metadata:
            return ""
        if isinstance(metadata, dict):
            parts = [f"{key}={value}" for key, value in metadata.items()]
            return " (" + ", ".join(parts) + ")"
        return f" ({metadata})"

    def _indent(self, depth: int) -> str:
        return " " * max(depth * self.indent, 0)

    def _log_range_start(
        self,
        label: str,
        depth: int,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        if not (self.enabled and self.emit_events):
            return

        self.logger.log(
            self.range_level,
            "%s[start] %s%s",
            self._indent(depth),
            label,
            self._format_metadata(metadata),
        )

    def _log_range_stop(
        self,
        label: str,
        depth: int,
        duration: float,
        had_error: bool,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        if not (self.enabled and self.emit_events):
            return

        status = "error" if had_error else "stop"
        self.logger.log(
            self.range_level,
            "%s[%s] %s%s (%.3fs)",
            self._indent(depth),
            status,
            label,
            self._format_metadata(metadata),
            duration,
        )

    @staticmethod
    def _now() -> float:
        return time.perf_counter()


class _ProfilerRange(ContextDecorator):
    """Context manager used by Profiler.section."""

    __slots__ = (
        "_profiler",
        "label",
        "metadata",
        "start_time",
        "duration",
        "depth",
        "had_error",
        "_rss_start",
        "_gpu_peak_start",
    )

    def __init__(
        self,
        profiler: Profiler,
        label: str,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        self._profiler = profiler
        self.label = label
        self.metadata = metadata
        self.start_time: float = 0.0
        self.duration: float = 0.0
        self.depth: int = 0
        self.had_error: bool = False
        self._rss_start: Optional[int] = None
        self._gpu_peak_start: Optional[int] = None

    def __enter__(self) -> "_ProfilerRange":
        if not self._profiler.enabled:
            self.duration = 0.0
            self.depth = 0
            return self

        stack = self._profiler._get_stack()
        self.depth = len(stack)
        stack.append(self)

        if self._profiler.record_memory:
            self._rss_start = self._profiler._rss_now()
            # Snapshot the CUDA peak watermark on entry; the exit handler
            # reports (max_memory_allocated() - start) so the delta composes
            # under nesting. The previous approach reset the peak globally
            # at every section enter, which silently under-reported any
            # outer section that wrapped an inner memory-recording section.
            self._gpu_peak_start = self._profiler._gpu_peak()

        self.start_time = self._profiler._now()
        self._profiler._log_range_start(self.label, self.depth, self.metadata)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if not self._profiler.enabled:
            return False

        end_time = self._profiler._now()
        self.duration = end_time - self.start_time
        self.had_error = exc_type is not None

        rss_delta: Optional[int] = None
        gpu_peak: Optional[int] = None
        if self._profiler.record_memory:
            rss_end = self._profiler._rss_now()
            if self._rss_start is not None and rss_end is not None:
                rss_delta = rss_end - self._rss_start
            gpu_peak_end = self._profiler._gpu_peak()
            if self._gpu_peak_start is not None and gpu_peak_end is not None:
                # max(0, ...) guards against the rare case where another
                # caller resets the peak counter while a section is open.
                gpu_peak = max(0, gpu_peak_end - self._gpu_peak_start)

        stack = self._profiler._get_stack()
        if stack and stack[-1] is self:
            stack.pop()
        else:
            # Stack corruption should be rare, but guard against it so that
            # future profiling calls are not affected.
            try:
                stack.remove(self)
            except ValueError:
                stack.clear()

        self._profiler._record(
            self.label,
            self.duration,
            self.had_error,
            rss_delta=rss_delta,
            gpu_peak=gpu_peak,
        )
        self._profiler._log_range_stop(
            self.label,
            self.depth,
            self.duration,
            self.had_error,
            self.metadata,
        )
        # Propagate exceptions to the caller.
        return False
