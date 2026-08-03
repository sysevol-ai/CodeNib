# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast, deterministic retrieval gate for the local Ask experience."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:
    from codenib.types import QueriedNode


@dataclass(frozen=True)
class AskRetrievalCase:
    """One repository question and the implementation surfaces it must retrieve."""

    name: str
    query: str
    required_path_groups: tuple[tuple[str, ...], ...]
    forbidden_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class AskRetrievalResult:
    """Deterministic outcome for one retrieval case."""

    name: str
    passed: bool
    paths: tuple[str, ...]
    missing_path_groups: tuple[tuple[str, ...], ...]
    forbidden_hits: tuple[str, ...]


DEFAULT_CASES = (
    AskRetrievalCase(
        name="stale semantic view lifecycle",
        query=(
            "How does CodeNib prevent a semantic index built for stale source "
            "code from being served at build time and runtime?"
        ),
        required_path_groups=(
            ("codenib/compiler/index_compiler.py",),
            ("codenib/web/repo_registry.py", "codenib/mcp/context.py"),
        ),
        forbidden_paths=("scripts/smoke_release_services.py",),
    ),
    AskRetrievalCase(
        name="LiteLLM backend configuration",
        query=(
            "How does CodeNib configure LiteLLM models, endpoints, credentials, "
            "and provider-specific options for Ask and Wiki?"
        ),
        required_path_groups=(
            ("codenib/web/config.py",),
            ("codenib/llm/options.py", "codenib/llm/litellm_chat.py"),
            (
                "codenib/web/app.py",
                "codenib/wiki/agent_wiki.py",
                "codenib/wiki/narrator.py",
            ),
        ),
    ),
    AskRetrievalCase(
        name="Wiki grounding",
        query=(
            "How does Wiki generation keep pages source-grounded and reject "
            "unsupported claims?"
        ),
        required_path_groups=(
            ("codenib/wiki/agent_wiki.py",),
            ("codenib/wiki/quality.py",),
        ),
    ),
)


def _node_path(node: "QueriedNode") -> str:
    node_id = str(node.node_id or "").replace("\\", "/")
    if node_id and ":" in node_id:
        return node_id.split(":", 1)[0].removeprefix("./")
    return str(node.file or node_id).replace("\\", "/").removeprefix("./")


def evaluate_case(
    case: AskRetrievalCase,
    nodes: Iterable["QueriedNode"],
) -> AskRetrievalResult:
    """Check path coverage without using an LLM judge."""
    paths = tuple(dict.fromkeys(_node_path(node) for node in nodes if _node_path(node)))
    missing = tuple(
        group
        for group in case.required_path_groups
        if not any(
            any(path.endswith(candidate) for path in paths) for candidate in group
        )
    )
    forbidden_hits = tuple(
        path
        for path in paths
        if any(path.endswith(candidate) for candidate in case.forbidden_paths)
    )
    return AskRetrievalResult(
        name=case.name,
        passed=not missing and not forbidden_hits,
        paths=paths,
        missing_path_groups=missing,
        forbidden_hits=forbidden_hits,
    )


def _load_executor(manifest_path: Path):
    import codenib.agent.skills as skills_package
    from codenib.agent.skills.context import ComposerContexts
    from codenib.agent.skills.loader import SkillLoader
    from codenib.agent.skills.registry import SkillRegistry
    from codenib.agent.skills.repository_search.executor import create_executor
    from codenib.compiler.manifest import RepoManifest
    from codenib.compiler.skill_context import load_contexts_from_manifest

    manifest = RepoManifest.load(manifest_path)
    skills_dir = str(Path(skills_package.__file__).parent)
    SkillRegistry.reset()
    registry = SkillRegistry()
    SkillLoader().load_all(skills_dir, contexts={}, registry=registry)
    contexts = load_contexts_from_manifest(
        manifest,
        skill_ids=["repository_search"],
        skill_registry=registry,
    )
    return create_executor(ComposerContexts.from_mapping(contexts))


def run_gate(
    manifest_path: Path,
    *,
    cases: Sequence[AskRetrievalCase] = DEFAULT_CASES,
    top_k: int = 10,
) -> list[AskRetrievalResult]:
    """Run every case against one already-built repository manifest."""
    execute = _load_executor(manifest_path)
    return [evaluate_case(case, execute(case.query, top_k=top_k)) for case in cases]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check whether repository_search retrieves the implementation "
            "surfaces needed by representative Ask questions."
        )
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="repository root used to locate the default manifest",
    )
    parser.add_argument(
        "--manifest",
        help="explicit repo_manifest.json path",
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.manifest:
        manifest_path = Path(args.manifest).expanduser().resolve()
    else:
        from codenib.cli import resolve_manifest_path

        manifest_path = resolve_manifest_path(args.repo)

    results = run_gate(manifest_path, top_k=args.top_k)
    if args.as_json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print(f"Manifest: {manifest_path}")
        for result in results:
            state = "PASS" if result.passed else "FAIL"
            print(f"[{state}] {result.name}")
            for rank, path in enumerate(result.paths, 1):
                print(f"  {rank:2d}. {path}")
            for group in result.missing_path_groups:
                print(f"  missing one of: {', '.join(group)}")
            for path in result.forbidden_hits:
                print(f"  forbidden evidence: {path}")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
