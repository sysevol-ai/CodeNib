# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LLM-authored narrative layer for the wiki (DeepWiki-style prose).

The deterministic `WikiBuilder` already extracts real symbols, citations, and
source spans. The narrator adds *prose only* on top — explanations of what a
project/module/component does — grounded in those facts. It NEVER emits file
paths, line numbers, or code (those stay index-derived so highlights remain
real). Output is cached on disk keyed by (repo, commit, page, facts-hash);
calls degrade gracefully to ``None`` (caller falls back to templated text) when
no usable model/credentials are available.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

# Keep prose terse and on-brand; the structural facts come from the builder.
_SYSTEM = (
    "You are a senior engineer writing concise technical documentation for a "
    "code wiki (DeepWiki style). Write clear, factual prose that explains what "
    "the software does and how its pieces fit together. Ground every statement "
    "in the facts provided. Do NOT invent APIs, file paths, line numbers, or "
    "code. Do NOT include code blocks, headings, file paths, or line numbers — "
    "prose only. Be specific and avoid filler; no marketing language."
)


def _no_thinking_kwargs(model: str) -> dict:
    """litellm kwargs that disable hidden reasoning for thinking models.

    Gemini 2.5 models spend the ``max_tokens`` budget on hidden reasoning
    tokens first; with the small prose budgets here that leaves ``content``
    empty (``finish_reason="length"``). LiteLLM maps Anthropic-style
    ``thinking`` to Gemini's ``thinkingConfig``; a zero budget turns thinking off
    so the budget goes to the answer. No-op for other providers.
    """
    m = model.lower()
    if "gemini-2.5" in m or "gemini-2-5" in m:
        return {"thinking": {"type": "disabled", "budget_tokens": 0}}
    return {}


class Narrator:
    """Cached, fail-soft LLM prose generator."""

    def __init__(
        self,
        model: Optional[str] = None,
        cache_dir: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.model = (
            model
            or os.environ.get("CODEMINER_WIKI_MODEL")
            or os.environ.get("CODEMINER_DEMO_MODEL")
            or "openai/gpt-4o-mini"
        )
        self.cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        self.enabled = self._usable() if enabled is None else enabled
        if not self.enabled:
            logger.info("Wiki narrator disabled (no usable creds for %s)", self.model)

    def _usable(self) -> bool:
        m = self.model.lower()
        if m.startswith("openai/") or m.startswith("gpt-"):
            return bool(os.environ.get("OPENAI_API_KEY"))
        if m.startswith("anthropic/") or m.startswith("claude"):
            return bool(
                os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_BASE_URL")
            )
        # vertex / gemini / others: assume ambient creds may exist; let it try.
        return True

    # -- cache -------------------------------------------------------------

    def _cache_file(self, key: str) -> Optional[str]:
        if not self.cache_dir:
            return None
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return os.path.join(self.cache_dir, f"{h}.json")

    def _read_cache(self, key: str) -> Optional[str]:
        path = self._cache_file(key)
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh).get("text")
            except Exception:
                return None
        return None

    def _write_cache(self, key: str, text: str) -> None:
        path = self._cache_file(key)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"model": self.model, "key": key, "text": text}, fh)
        except Exception as exc:  # noqa: BLE001 - cache write is best-effort
            # Fail soft: a missing/unwritable cache only costs a recompute.
            logger.debug("wiki narrator cache write failed for %r: %s", key, exc)

    # -- core call ---------------------------------------------------------

    def _complete(self, key: str, prompt: str, max_tokens: int = 400) -> Optional[str]:
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        if not self.enabled:
            return None
        try:
            import litellm

            extra = _no_thinking_kwargs(self.model)
            resp = litellm.completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.2,
                timeout=40,
                **extra,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                self._write_cache(key, text)
                return text
        except Exception as e:  # network / auth / quota — fall back to templated
            logger.warning("Wiki narrator call failed (%s): %s", self.model, e)
        return None

    # -- public prose builders --------------------------------------------

    def overview(
        self,
        repo: str,
        language: str,
        modules: List[tuple],  # (name, symbol_count)
        highlights: List[str],  # short "Class — docstring-first-line" facts
        key: str,
    ) -> Optional[str]:
        mod_lines = "\n".join(f"- {n} ({c} symbols)" for n, c in modules)
        hl = "\n".join(f"- {h}" for h in highlights[:12]) or "(none extracted)"
        prompt = (
            f"Repository: {repo} (language: {language}).\n"
            f"Top-level modules by indexed symbol count:\n{mod_lines}\n\n"
            f"Notable components and their docstrings:\n{hl}\n\n"
            "Write a 2-3 paragraph overview of this repository for its wiki "
            "landing page. Start by stating what the project is and what it is "
            "for (infer from the name and components). Then describe how the "
            "major modules relate and what the codebase is organized around. "
            "Refer to modules by their names exactly as given. Prose only."
        )
        return self._complete(key, prompt, max_tokens=420)

    def module_intro(
        self,
        repo: str,
        module: str,
        files: List[str],
        components: List[str],  # "Class — docstring-first-line"
        key: str,
    ) -> Optional[str]:
        comp = "\n".join(f"- {c}" for c in components[:12]) or "(none)"
        fl = ", ".join(os.path.basename(f) for f in files[:10])
        prompt = (
            f"Repository: {repo}. Module: {module}.\n"
            f"Files: {fl}\n"
            f"Key components and docstrings:\n{comp}\n\n"
            "Write a 1-2 paragraph introduction explaining this module's purpose "
            "and responsibility within the repository, and what kinds of problems "
            "it handles. Refer to components by name. Prose only."
        )
        return self._complete(key, prompt, max_tokens=260)

    def components(
        self,
        repo: str,
        module: str,
        items: List[tuple],  # (class_name, docstring_first_lines)
        key: str,
    ) -> Optional[dict]:
        """Return {class_name: one-to-two sentence description}. Batched call."""
        listing = "\n".join(
            f"{i + 1}. {name}: {doc[:300] if doc else '(no docstring)'}"
            for i, (name, doc) in enumerate(items)
        )
        prompt = (
            f"Repository: {repo}. Module: {module}.\n"
            f"Components:\n{listing}\n\n"
            "For EACH numbered component, write one or two sentences describing "
            "its responsibility — what it represents or does in this module. "
            "Return a JSON object mapping the exact component name to its "
            "description string. Prose only inside the values; no code."
        )
        raw = self._complete("comp::" + key, prompt, max_tokens=500)
        if not raw:
            return None
        # Tolerate ```json fences.
        s = raw.strip()
        if s.startswith("```"):
            s = s.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except (ValueError, TypeError) as exc:
            # Malformed / non-JSON model output is non-fatal: fall back to None
            # so the caller uses templated prose instead.
            logger.debug(
                "wiki narrator components parse failed (repo=%s module=%s): %s; raw=%r",
                repo,
                module,
                exc,
                s[:200],
            )
        return None
