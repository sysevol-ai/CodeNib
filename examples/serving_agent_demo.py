# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A minimal code agent that uses CodeNib for context and CodeNib serving to generate.

This is the concrete shape of the serving stack from the outside — the two calls
a code agent makes:

1. **Retrieve context** from CodeNib (in-process index lookup) to ground the prompt.
2. **Generate** by calling LiteLLM (which routes to CodeNib serving) over the OpenAI
   chat-completions API — exactly as it would call OpenAI.

CodeNib is *not* an agent; it is the retrieval library this agent queries. The
LLM call is a plain OpenAI request, so nothing here knows speculation is happening.

The pieces (``retrieve_context`` / ``generate``) take injectable ``ctx``/``client``
so the flow is unit-testable without a running CodeNib index or LLM. ``main``
wires the real ones.

Run::

    python -m examples.agent_demo \\
        --manifest /path/to/repo/.codenib_cache/repo_manifest.json \\
        --base-url http://localhost:4000/v1 \\
        --model codenib-qwen-coder \\
        --task "Add a docstring to the parse_config function"
"""

from __future__ import annotations

import argparse
from typing import Any, List, Optional, Protocol


class _Completions(Protocol):
    """``client.chat.completions`` — the one call this demo makes."""

    def create(
        self, model: str, messages: List[dict], stream: bool = ...
    ) -> Any: ...  # a response, or an iterable of chunks when stream=True


class _Chat(Protocol):
    @property
    def completions(self) -> _Completions: ...


class OpenAIClient(Protocol):
    """The slice of an OpenAI-style client this demo depends on.

    Declared structurally so the real ``openai.OpenAI`` and the test fake are
    both accepted, without the example having to import ``openai`` to be
    type-checked.
    """

    @property
    def chat(self) -> _Chat: ...


_SYSTEM_PROMPT = (
    "You are a coding assistant. Use the repository context below to answer "
    "accurately; prefer the project's own conventions."
)


def retrieve_context(
    query: str,
    *,
    ctx: object = None,
    manifest_path: Optional[str] = None,
    top_k: int = 5,
) -> str:
    """Fetch relevant code snippets from CodeNib and join them into a context block.

    ``ctx`` is a CodeNib ``ServerContext`` (injected for tests); if omitted it is
    loaded from ``manifest_path``.
    """
    if ctx is None:
        from codenib.mcp.context import ServerContext

        ctx = ServerContext.load(manifest_path)

    bm25 = getattr(ctx, "bm25", None)
    if bm25 is None:
        return ""
    hits = bm25.search(query, top_k=top_k, return_code_content=True)
    snippets = []
    for hit in hits:
        content = getattr(hit, "content", None)
        if content:
            where = getattr(hit, "file", None) or getattr(hit, "node_name", "")
            snippets.append(f"# {where}\n{content}" if where else content)
    return "\n\n".join(snippets)


def build_messages(task: str, context: str) -> List[dict]:
    """Assemble the chat messages from the task and retrieved context."""
    user = f"Repository context:\n{context}\n\nTask:\n{task}" if context else task
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def generate(
    messages: List[dict],
    *,
    base_url: str,
    model: str,
    api_key: str = "none",
    stream: bool = False,
    client: Optional[OpenAIClient] = None,
    echo: bool = False,
) -> str:
    """Call LiteLLM/CodeNib serving over the OpenAI API and return the completion text.

    ``client`` is an OpenAI-style client (injected for tests); if omitted a real
    ``openai.OpenAI`` pointed at ``base_url`` is created.
    """
    if client is None:
        from openai import OpenAI

        client = OpenAI(base_url=base_url, api_key=api_key)

    if stream:
        parts: List[str] = []
        for chunk in client.chat.completions.create(
            model=model, messages=messages, stream=True
        ):
            delta = chunk.choices[0].delta.content
            if delta:
                parts.append(delta)
                if echo:
                    print(delta, end="", flush=True)
        if echo:
            print()
        return "".join(parts)

    resp = client.chat.completions.create(model=model, messages=messages)
    text = resp.choices[0].message.content or ""
    if echo:
        print(text)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="CodeNib serving agent demo")
    parser.add_argument(
        "--manifest",
        required=True,
        help="path to <repo>/.codenib_cache/repo_manifest.json",
    )
    parser.add_argument("--task", required=True, help="the coding task / question")
    parser.add_argument("--base-url", default="http://localhost:4000/v1")
    parser.add_argument("--model", default="codenib-qwen-coder")
    parser.add_argument("--api-key", default="none")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()

    context = retrieve_context(args.task, manifest_path=args.manifest, top_k=args.top_k)
    messages = build_messages(args.task, context)
    generate(
        messages,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        stream=not args.no_stream,
        echo=True,
    )


if __name__ == "__main__":
    main()
