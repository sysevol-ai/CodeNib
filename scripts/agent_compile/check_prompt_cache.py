#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Smoke test: is Anthropic prompt caching active on the agent's LLM path?

Makes a few real LLM calls (slow — needs vertex creds). Reports the
cache token counts for two paths so we can answer two questions:

  PATH A — the path the agent uses today (``LiteLLMChat._call_raw`` with plain
           string messages). If ``cache_read_input_tokens`` stays 0 on the
           repeat call, the sweep is paying full price for the static prefix
           (system prompt + tool schemas) on every turn.
  PATH B — a manual ``cache_control: ephemeral`` call, repeated. If the repeat
           shows ``cache_read_input_tokens`` > 0, the provider honors caching,
           so enabling it on PATH A is worthwhile (cache reads are ~10% of the
           base input price).

Usage:
    python scripts/agent_compile/check_prompt_cache.py \
        [--model vertex_ai/claude-haiku-4-5] [--vertex-location us-east5]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# A static prefix large enough to clear Anthropic's cache minimum (~2k tokens
# for Haiku). Repeating a sentence is fine — only token count matters here.
_PREFIX = "You are a code-localization assistant. " + (
    "This is shared, static reference context that should be cached. " * 400
)


def _usage_dict(resp) -> dict:
    """Best-effort flatten of a litellm response's usage into a plain dict."""
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    for attr in ("model_dump", "dict"):
        if hasattr(u, attr):
            try:
                return getattr(u, attr)()
            except Exception:  # noqa: BLE001
                pass
    return {k: getattr(u, k) for k in dir(u) if not k.startswith("_")}


def _cache_cols(resp) -> dict:
    u = _usage_dict(resp)
    # Anthropic-style keys; some litellm versions nest cached reads under
    # prompt_tokens_details.cached_tokens (OpenAI style) — surface both.
    details = u.get("prompt_tokens_details") or {}
    if hasattr(details, "model_dump"):
        details = details.model_dump()
    return {
        "prompt_tokens": u.get("prompt_tokens"),
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": u.get("cache_read_input_tokens"),
        "cached_tokens(openai-style)": (details or {}).get("cached_tokens"),
    }


def _read(v) -> int:
    return int(v or 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="vertex_ai/claude-haiku-4-5")
    ap.add_argument("--vertex-location", default="us-east5")
    args = ap.parse_args(argv)

    import litellm

    from codeminer.llm.litellm_chat import LiteLLMChat

    extra = {"vertex_location": args.vertex_location}
    llm = LiteLLMChat(
        model=args.model, temperature=0.0, max_tokens=32, extra_kwargs=extra
    )

    msgs = [
        {"role": "system", "content": _PREFIX},
        {"role": "user", "content": "Reply with the single word OK."},
    ]
    print("=== PATH A — agent path today (LiteLLMChat._call_raw, no cache_control) ===")
    a1 = llm._call_raw([dict(m) for m in msgs])
    a2 = llm._call_raw([dict(m) for m in msgs])
    print("  call 1:", _cache_cols(a1))
    print("  call 2:", _cache_cols(a2))

    print("\n=== PATH B — manual cache_control: ephemeral on the system prefix ===")
    blocks = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": _PREFIX,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {"role": "user", "content": "Reply with the single word OK."},
    ]
    kw = dict(model=args.model, temperature=0.0, max_tokens=32, **extra)
    b1 = litellm.completion(messages=[dict(m) for m in blocks], **kw)
    b2 = litellm.completion(messages=[dict(m) for m in blocks], **kw)
    print("  call 1:", _cache_cols(b1))
    print("  call 2:", _cache_cols(b2))

    a_on = _read(_cache_cols(a2)["cache_read_input_tokens"]) > 0
    b_on = _read(_cache_cols(b2)["cache_read_input_tokens"]) > 0
    print("\n=== VERDICT ===")
    print(f"  agent path caching: {'ON' if a_on else 'OFF'}")
    print(
        f"  provider honors manual cache_control: "
        f"{'YES' if b_on else 'NO (or below cache minimum / unsupported)'}"
    )
    if not a_on and b_on:
        print(
            "  => Caching is OFF on the agent path but the provider supports it: "
            "enabling cache_control on the system prompt + tool schemas would cut "
            "the per-turn input cost substantially."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
