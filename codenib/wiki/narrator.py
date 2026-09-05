# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
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
from typing import Any, List, Optional

from ..llm.options import validate_model_options

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
_PROMPT_VERSION = "2"


class Narrator:
    """Cached, fail-soft LLM prose generator."""

    def __init__(
        self,
        model: Optional[str] = None,
        cache_dir: Optional[str] = None,
        enabled: Optional[bool] = None,
        llm: Optional[Any] = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model_options: Optional[dict[str, Any]] = None,
    ) -> None:
        self.model = (
            model
            or os.environ.get("CODENIB_WIKI_MODEL")
            or os.environ.get("CODENIB_DEMO_MODEL")
            or "openai/gpt-4o-mini"
        )
        self._llm = llm
        self.api_base = api_base
        self.api_key = api_key
        self.model_options = validate_model_options(
            model_options,
            source="Narrator.model_options",
        )
        self.cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        auto_enabled = enabled is None
        self.enabled = self._usable() if auto_enabled else enabled
        if not self.enabled:
            reason = "no usable credentials" if auto_enabled else "configuration"
            logger.debug("Wiki narrator disabled by %s for %s", reason, self.model)

    def _usable(self) -> bool:
        if self._llm is not None:
            return True
        if self.api_key or self.api_base:
            return True
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
        # Model-independent by design (same rationale as AgentWiki): the
        # prose is keyed by prompt version + call key, so pointing the narrator
        # at a different backend reuses what was already generated. The
        # producing model is recorded in the entry by ``_write_cache``.
        identity = f"{_PROMPT_VERSION}\0{key}"
        h = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20]
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
                # ``model``/``api_base`` are provenance, not key material.
                json.dump(
                    {
                        "model": self.model,
                        "api_base": self.api_base or "",
                        "key": key,
                        "text": text,
                    },
                    fh,
                )
        except Exception as exc:  # noqa: BLE001 - cache write is best-effort
            # Fail soft: a missing/unwritable cache only costs a recompute.
            logger.debug("wiki narrator cache write failed for %r: %s", key, exc)

    # -- core call ---------------------------------------------------------

    def _client(self):
        if self._llm is None:
            from ..llm.litellm_chat import LiteLLMChat

            self._llm = LiteLLMChat(
                model=self.model,
                temperature=0.2,
                max_tokens=500,
                api_base=self.api_base,
                api_key=self.api_key,
                extra_kwargs=self.model_options,
            )
        return self._llm

    def _complete(self, key: str, prompt: str, max_tokens: int = 400) -> Optional[str]:
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        if not self.enabled:
            return None
        try:
            text = self._client().complete(
                [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.2,
                timeout=40,
            )
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
