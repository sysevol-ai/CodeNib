#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Check each listed Wiki writing model with one small request.

The request goes through the same provider code a Wiki run uses: streaming,
JSON-schema structured output with ``require_parameters``, reasoning off where
the model accepts it, and the reported charge. Each model is capped at
$0.05; a full pass normally costs a few cents in total.

The key comes from OPENROUTER_API_KEY or ``codenib auth login``.

    python scripts/probe_openrouter_wiki_models.py [--only MODEL ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from codenib.openrouter_auth import OpenRouterAuthError, require_key
from codenib.wiki.visitor_models import WIKI_MODEL_CHOICES
from codenib.wiki.visitor_provider import VisitorProvider, WikiRunStopped

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "probe",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"language": {"type": "string"}},
            "required": ["language"],
            "additionalProperties": False,
        },
    },
}
MESSAGES = [
    {"role": "system", "content": "Answer with JSON that matches the schema."},
    {"role": "user", "content": "Which language is `def main(): pass` written in?"},
]


def probe(model_id: str, key: str) -> dict:
    provider = VisitorProvider(key, 0.05, lambda: None, lambda _: None, model=model_id)
    started = time.monotonic()
    try:
        text = provider.complete(MESSAGES, response_format=SCHEMA, max_tokens=200)
        answer = json.loads(text)
        ok = isinstance(answer, dict) and isinstance(answer.get("language"), str)
        error = "" if ok else "response did not match the schema"
    except (WikiRunStopped, ValueError) as exc:
        ok, error = False, str(exc)
    finally:
        provider.close()
    return {
        "model": model_id,
        "ok": ok,
        "cost_usd": round(provider.cost, 6),
        "seconds": round(time.monotonic() - started, 1),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="*", help="model ids to probe")
    args = parser.parse_args()
    try:
        key = require_key()
    except OpenRouterAuthError as exc:
        print(f"{exc}. Set OPENROUTER_API_KEY or run `codenib auth login`.")
        return 2
    models = [m.id for m in WIKI_MODEL_CHOICES if not args.only or m.id in args.only]
    results = [probe(model_id, key) for model_id in models]
    for row in results:
        mark = "ok  " if row["ok"] else "FAIL"
        cost, seconds = f"${row['cost_usd']}", f"{row['seconds']}s"
        print(f"{mark} {row['model']:<34} {cost:<10} {seconds:>6} {row['error']}")
    total = sum(row["cost_usd"] for row in results)
    passed = sum(row["ok"] for row in results)
    print(f"{passed}/{len(results)} models passed; ${total:.4f} reported")
    return 0 if all(row["ok"] for row in results) else 1


if __name__ == "__main__":
    sys.exit(main())
