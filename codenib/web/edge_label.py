# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""On-demand, cached LLM labels for graph dependency edges.

When a user clicks an edge in the interactive graph view, the frontend asks
"how/why does the source symbol use the target?" and gets back a short phrase
(e.g. ``"validates user input"``). Generation is:

* **On-demand** — one small LLM call the first time an edge is seen.
* **Cached** — the phrase is stored on disk (``edge_labels_<repo>.json``) keyed
  by the *content hash* of both symbols' code, so it survives renames/moves and
  auto-invalidates when either symbol's code changes.
* **Fail-soft** — any missing code, LLM error, or timeout yields ``""`` and the
  UI falls back to the existing ref-count display.

Symbols are addressed by ``(file, line-span)`` (what the frontend actually
has), and their source is read with the repo's existing path-safe
``source(file, start, end)`` helper — no dependency on graph identity names or
``project_root``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..log_utils import get_logger

logger = get_logger(__name__)

# A dependency phrase is deliberately tiny, but thinking models (qwen3, …) spend
# hundreds of hidden reasoning tokens before the answer, so the budget must be
# generous enough for them to *finish* thinking (a truncated think yields empty
# content). For snappy labels, point ``edge_label_model`` at a non-thinking model
# (e.g. openai/gpt-4o-mini). _sanitize also strips any inline <think> block.
_MAX_WORDS = 6
# High ceiling, not high cost: a thinking model stops as soon as it finishes
# reasoning (~300-450 tokens observed for qwen3), so a bigger budget doesn't slow
# the call — it only prevents a truncated <think> (which yields empty content,
# and hit field/property targets whose reasoning ran longer than a 512 budget).
_MAX_TOKENS = 1024
_LLM_TIMEOUT_S = 30
_MAX_CODE_CHARS = 1500  # per symbol body fed to the LLM
MAX_EDGE_LABEL_ANCHORS = 3
_UNIT_SEP = "\x1f"
_PROMPT_VERSION = "2"

# source_fn(file, start_1based, end_1based) -> {"content": str, ...} | None
SourceFn = Callable[[str, Optional[int], Optional[int]], Optional[dict]]

_PROMPT = """\
You are labeling one dependency edge in a code graph.

In AT MOST {max_words} words, describe HOW or WHY the SOURCE symbol uses the \
TARGET symbol (for example: "validates user input", "loads DB config", \
"dispatches request to handler"). Reply with ONLY the phrase — lowercase, no \
quotes, no trailing punctuation, no explanation.

SOURCE `{src_name}` ({src_file}):
```
{src_code}
```

TARGET `{tgt_name}` ({tgt_file}):
```
{tgt_code}
```
{anchor_block}"""


class EdgeLabeler:
    """Per-repo, cached generator of short dependency-edge phrases."""

    def __init__(
        self,
        source_fn: SourceFn,
        model: str,
        cache_dir: Optional[str],
        cache_namespace: str,
        *,
        llm: Any = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        """
        Args:
            source_fn: repo's path-safe ``source(file, start, end)`` reader
                (1-based, inclusive) — e.g. ``WikiBuilder.source``.
            model: litellm model string for the (short) label call.
            cache_dir: directory for ``edge_labels_<ns>.json`` (created if given).
            cache_namespace: per-repo key (e.g. ``instance_id@commit``) so repos
                don't share a cache file.
        """
        self._source = source_fn
        self._model = model
        self._llm = llm
        self._llm_identity = getattr(llm, "cache_identity", "")
        self._api_base = api_base
        self._api_key = api_key
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        self._cache: Optional[Dict[str, str]] = None  # loaded lazily
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            h = hashlib.sha1(cache_namespace.encode("utf-8")).hexdigest()[:16]
            self._cache_path: Optional[str] = os.path.join(
                cache_dir, f"edge_labels_{h}.json"
            )
        else:
            self._cache_path = None

    # -- public ------------------------------------------------------------

    def label(
        self,
        src_file: str,
        src_line: Optional[int],
        src_end: Optional[int],
        src_name: str,
        tgt_file: str,
        tgt_line: Optional[int],
        tgt_end: Optional[int],
        tgt_name: str,
        anchors: Optional[List[dict]] = None,
    ) -> Tuple[str, bool]:
        """Return ``(phrase, cached)``. Empty phrase on any failure.

        Works for symbol->symbol edges (both bodies available) *and* file-level
        aggregate edges (no symbol body, but call-site anchors) — the latter are
        the ones visible by default in the graph. Evidence is any of: source
        body, target body, or the call-site snippets.
        """
        src_code = self._read_symbol(src_file, src_line, src_end)
        tgt_code = self._read_symbol(tgt_file, tgt_line, tgt_end)
        anchor_block = self._anchor_block(anchors or [])
        if not (src_code or tgt_code or anchor_block):
            return "", False

        # Key on all evidence: file-level edges have empty bodies, so the anchors
        # are what distinguish one file->file edge from another.
        key = self._key(src_code, tgt_code, anchor_block)
        cached = self._cache_get(key)
        if cached is not None:
            return cached, True

        phrase = self._generate(
            src_name, src_file, src_code, tgt_name, tgt_file, tgt_code, anchor_block
        )
        # Don't cache empty results — leave misses open so a transient LLM failure
        # retries on the next hover/click. Only persist real phrases.
        if phrase:
            self._cache_put(key, phrase)
        return phrase, False

    # -- code fetch --------------------------------------------------------

    def _read_symbol(self, file: str, start: Optional[int], end: Optional[int]) -> str:
        if not file or start is None:
            return ""
        try:
            res = self._source(file, start, end or start)
        except Exception:  # noqa: BLE001 - fail soft
            return ""
        content = (res or {}).get("content", "") if res else ""
        return content[:_MAX_CODE_CHARS] if content else ""

    def _anchor_block(self, anchors: List[dict]) -> str:
        snippets: List[str] = []
        for a in anchors[:MAX_EDGE_LABEL_ANCHORS]:
            file = a.get("file") or ""
            line = a.get("line")
            if not file or not isinstance(line, int):
                continue
            res = None
            try:
                res = self._source(file, max(1, line - 1), line + 1)
            except Exception:  # noqa: BLE001
                res = None
            snippet = (res or {}).get("content", "").strip() if res else ""
            if snippet:
                snippets.append(f"{file}:{line}\n{snippet}")
        if not snippets:
            return ""
        return "\nCall site(s) where SOURCE references TARGET:\n" + "\n".join(snippets)

    # -- LLM ---------------------------------------------------------------

    def _client(self):
        if self._llm is None:
            from ..llm.litellm_chat import LiteLLMChat

            self._llm = LiteLLMChat(
                model=self._model,
                temperature=0.0,
                max_tokens=_MAX_TOKENS,
                api_base=self._api_base,
                api_key=self._api_key,
            )
        return self._llm

    def _generate(
        self,
        src_name: str,
        src_file: str,
        src_code: str,
        tgt_name: str,
        tgt_file: str,
        tgt_code: str,
        anchor_block: str,
    ) -> str:
        prompt = _PROMPT.format(
            max_words=_MAX_WORDS,
            src_name=src_name or "(source)",
            src_file=src_file,
            src_code=src_code or "(source unavailable)",
            tgt_name=tgt_name or "(target)",
            tgt_file=tgt_file,
            tgt_code=tgt_code or "(target is external / unavailable)",
            anchor_block=anchor_block,
        )
        try:
            text = self._client().complete(
                [{"role": "user", "content": prompt}],
                max_tokens=_MAX_TOKENS,
                temperature=0.0,
                timeout=_LLM_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - transient/model errors degrade to ""
            logger.warning("edge-label generation failed (%s): %s", self._model, exc)
            return ""
        return _sanitize(text)

    # -- cache -------------------------------------------------------------

    def _key(self, src_code: str, tgt_code: str, anchor_block: str = "") -> str:
        raw = _UNIT_SEP.join(
            (
                _PROMPT_VERSION,
                self._model,
                self._api_base or "",
                self._llm_identity,
                src_code,
                tgt_code,
                anchor_block,
            )
        ).encode(
            "utf-8",
        )
        return hashlib.sha1(raw).hexdigest()

    def _ensure_loaded(self) -> Dict[str, str]:
        if self._cache is None:
            data: Dict[str, str] = {}
            if self._cache_path and os.path.isfile(self._cache_path):
                try:
                    with open(self._cache_path, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                    if isinstance(raw, dict):
                        data = {
                            k: v.get("label", "") if isinstance(v, dict) else str(v)
                            for k, v in raw.items()
                        }
                except Exception:  # noqa: BLE001 - corrupt cache -> start fresh
                    data = {}
            self._cache = data
        return self._cache

    def _cache_get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._ensure_loaded().get(key)

    def _cache_put(self, key: str, phrase: str) -> None:
        with self._lock:
            cache = self._ensure_loaded()
            cache[key] = phrase
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Best-effort atomic write. Caller holds ``self._lock``."""
        if not self._cache_path or self._cache is None:
            return
        payload = {
            k: {"label": v, "model": self._model} for k, v in self._cache.items()
        }
        tmp = f"{self._cache_path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self._cache_path)
        except Exception as exc:  # noqa: BLE001 - never fail the request on cache IO
            logger.debug("edge-label cache write failed: %s", exc)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def _sanitize(text: str) -> str:
    """Coerce raw model output into a lowercase, ≤6-word phrase (or "")."""
    if not text:
        return ""
    s = text
    # drop a thinking model's reasoning block(s), then anything before a stray
    # closing tag (a truncated think that never opened cleanly).
    s = re.sub(r"(?is)<think>.*?</think>", " ", s)
    s = re.sub(r"(?is)^.*</think>", " ", s)
    s = re.sub(r"(?is)<think>.*$", " ", s)  # unclosed think -> all reasoning
    s = s.strip()
    # strip code fences / stray backticks
    s = re.sub(r"```[a-zA-Z]*", "", s).replace("```", "").strip()
    # first non-empty line
    for line in s.splitlines():
        if line.strip():
            s = line.strip()
            break
    # strip surrounding quotes/backticks
    s = s.strip("`\"'“”‘’ ").strip()
    # drop a leading "the source " style filler is left to the model; just clamp
    words = s.split()
    if not words:
        return ""
    s = " ".join(words[:_MAX_WORDS])
    # trailing punctuation off
    s = s.rstrip(".,;:!?").strip()
    return s.lower()
