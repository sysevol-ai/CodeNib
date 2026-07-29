#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Verify that internal CodeNib documents are absent from the public site."""

from __future__ import annotations

import argparse
import html
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

import yaml

REQUIRED_EXCLUSIONS = {
    "experiments/",
    "product_roadmap.md",
    "agent_runner_architecture_goal.md",
    "agent_compile_design.md",
    "scip_multilanguage_roadmap.md",
    "upload_dataset_to_huggingface.md",
    "diagnose_query_leak.md",
    "codenib_namespace_migration.md",
}

FORBIDDEN_PUBLIC_PREFIXES = {
    "experiments/",
    "product_roadmap/",
    "agent_runner_architecture_goal/",
    "agent_compile_design/",
    "scip_multilanguage_roadmap/",
    "upload_dataset_to_huggingface/",
    "diagnose_query_leak/",
    "codenib_namespace_migration/",
}

ALLOWED_PUBLIC_STATIC_FILES = {
    "assets/fonts/LICENSE-inter.txt",
    "assets/fonts/LICENSE-newsreader.txt",
    "assets/fonts/inter-latin-wght-normal.woff2",
    "assets/fonts/newsreader-latin-wght-italic.woff2",
    "assets/fonts/newsreader-latin-wght-normal.woff2",
    "assets/images/codenib-icon.svg",
    "assets/stylesheets/extra.css",
    "incremental_graph/incremental_interactive.html",
}

_INLINE_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_REFERENCE_LINK_RE = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
_MARKDOWN_AUTOLINK_RE = re.compile(r"<([^<>\s]+)>")
_HTML_LINK_RE = re.compile(
    r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.IGNORECASE,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that internal docs are absent from a MkDocs build."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("mkdocs.yml"),
        help="MkDocs configuration to inspect (default: mkdocs.yml)",
    )
    parser.add_argument(
        "--site-dir",
        type=Path,
        default=Path("site"),
        help="Generated site directory to inspect (default: site)",
    )
    return parser.parse_args()


def _load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return config


def _configured_exclusions(config: dict[str, Any]) -> set[str]:
    raw = config.get("exclude_docs", "")
    if not isinstance(raw, str):
        raise ValueError("mkdocs.yml exclude_docs must be a block string")
    return {
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _nav_targets(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _nav_targets(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _nav_targets(item)


def _source_to_prefix(source: str) -> str:
    if source.endswith("/"):
        return source
    if source.endswith(".md"):
        return f"{source.removesuffix('.md')}/"
    return source


def _normalize_public_location(location: str) -> str:
    parsed = urlparse(html.unescape(location))
    path = unquote(parsed.path).replace("\\", "/")
    return posixpath.normpath(f"/{path}").lstrip("/")


def _location_candidates(location: str) -> Iterable[str]:
    """Yield public paths represented by a local or repository URL."""

    normalized = _normalize_public_location(location)
    if normalized:
        yield normalized

    parsed = urlparse(html.unescape(location))
    if parsed.netloc.lower() not in {
        "github.com",
        "raw.githubusercontent.com",
    }:
        return
    marker = "/docs/"
    padded = f"/{normalized}"
    if marker in padded:
        yield padded.split(marker, 1)[1]


def _matches_exclusion(path: str, exclusion: str) -> bool:
    normalized_exclusion = exclusion.rstrip("/")
    return path == normalized_exclusion or (
        exclusion.endswith("/") and path.startswith(exclusion)
    )


def _path_is_forbidden(path: str) -> bool:
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in FORBIDDEN_PUBLIC_PREFIXES
    ) or any(_matches_exclusion(path, exclusion) for exclusion in REQUIRED_EXCLUSIONS)


def _is_forbidden(location: str) -> bool:
    return any(_path_is_forbidden(path) for path in _location_candidates(location))


def _link_targets(text: str) -> list[str]:
    targets = [
        *_INLINE_MARKDOWN_LINK_RE.findall(text),
        *_REFERENCE_LINK_RE.findall(text),
        *_MARKDOWN_AUTOLINK_RE.findall(text),
    ]
    for match in _HTML_LINK_RE.findall(text):
        value = next((value for value in match if value), "")
        if value:
            targets.append(value)
    return targets


def _check_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    configured = _configured_exclusions(config)
    missing = sorted(REQUIRED_EXCLUSIONS - configured)
    if missing:
        errors.append("mkdocs.yml exclude_docs is missing: " + ", ".join(missing))

    forbidden_nav = sorted(
        target
        for target in _nav_targets(config.get("nav", []))
        if _source_to_prefix(target) in FORBIDDEN_PUBLIC_PREFIXES
    )
    if forbidden_nav:
        errors.append(
            "mkdocs.yml nav exposes internal docs: " + ", ".join(forbidden_nav)
        )
    return errors


def _source_is_excluded(source: str, exclusions: set[str]) -> bool:
    normalized = source.lstrip("/")
    return any(
        (
            normalized.startswith(exclusion)
            if exclusion.endswith("/")
            else normalized == exclusion
        )
        for exclusion in exclusions
    )


def _check_source_links(
    docs_dir: Path,
    config: dict[str, Any],
) -> list[str]:
    """Reject public Markdown links that lead to excluded source documents."""

    exclusions = _configured_exclusions(config)
    exposed_links: list[str] = []
    docs_root = docs_dir.resolve()
    for source_path in sorted(docs_root.rglob("*.md")):
        source = source_path.relative_to(docs_root).as_posix()
        if _source_is_excluded(source, exclusions):
            continue
        text = source_path.read_text(encoding="utf-8")
        for raw_target in _link_targets(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            parsed = urlparse(target)
            if not target or target.startswith("#"):
                continue
            if _is_forbidden(target):
                exposed_links.append(f"{source} -> {target}")
                continue
            if parsed.scheme or parsed.netloc:
                continue
            target_path = unquote(parsed.path)
            if not target_path:
                continue
            resolved = (source_path.parent / target_path).resolve()
            try:
                relative = resolved.relative_to(docs_root).as_posix()
            except ValueError:
                continue
            if _source_is_excluded(relative, exclusions):
                exposed_links.append(f"{source} -> {relative}")

    if not exposed_links:
        return []
    return [
        "public source documents link to excluded content: "
        + ", ".join(sorted(set(exposed_links)))
    ]


def _check_source_inventory(
    docs_dir: Path,
    config: dict[str, Any],
) -> list[str]:
    """Keep the navigation as the allowlist for public Markdown and data."""

    exclusions = _configured_exclusions(config)
    nav_markdown = {
        target
        for target in _nav_targets(config.get("nav", []))
        if target.endswith(".md")
    }
    public_markdown: set[str] = set()
    unexpected_static: list[str] = []
    docs_root = docs_dir.resolve()

    for source_path in docs_root.rglob("*"):
        if not source_path.is_file():
            continue
        source = source_path.relative_to(docs_root).as_posix()
        if _source_is_excluded(source, exclusions):
            continue
        if source.endswith(".md"):
            public_markdown.add(source)
            continue
        if source in ALLOWED_PUBLIC_STATIC_FILES:
            continue
        unexpected_static.append(source)

    errors: list[str] = []
    unlisted = sorted(public_markdown - nav_markdown)
    missing = sorted(nav_markdown - public_markdown)
    if unlisted:
        errors.append(
            "public Markdown is not explicitly listed in mkdocs.yml nav: "
            + ", ".join(unlisted)
        )
    if missing:
        errors.append(
            "mkdocs.yml nav targets missing or excluded Markdown: " + ", ".join(missing)
        )
    if unexpected_static:
        errors.append(
            "unexpected public static source outside assets/: "
            + ", ".join(sorted(unexpected_static))
        )
    return errors


def _check_site_files(site_dir: Path) -> list[str]:
    exposed = sorted(
        str(path.relative_to(site_dir))
        for path in site_dir.rglob("*")
        if path.is_file() and _is_forbidden(str(path.relative_to(site_dir)))
    )
    if not exposed:
        return []
    return ["generated site contains internal files: " + ", ".join(exposed)]


def _check_site_links(site_dir: Path) -> list[str]:
    exposed_links: list[str] = []
    site_root = site_dir.resolve()
    for html_path in sorted(site_root.rglob("*.html")):
        source = html_path.relative_to(site_root).as_posix()
        text = html_path.read_text(encoding="utf-8")
        for raw_target in _link_targets(text):
            target = raw_target.strip()
            if not target or target.startswith("#"):
                continue
            if _is_forbidden(target):
                exposed_links.append(f"{source} -> {target}")
                continue
            parsed = urlparse(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            resolved = (html_path.parent / unquote(parsed.path)).resolve()
            try:
                relative = resolved.relative_to(site_root).as_posix()
            except ValueError:
                continue
            if _is_forbidden(relative):
                exposed_links.append(f"{source} -> {relative}")

    if not exposed_links:
        return []
    return [
        "generated site links to internal locations: "
        + ", ".join(sorted(set(exposed_links)))
    ]


def _check_search(site_dir: Path) -> list[str]:
    path = site_dir / "search" / "search_index.json"
    if not path.is_file():
        return [f"missing MkDocs search index: {path}"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    exposed = sorted(
        {
            str(document.get("location", ""))
            for document in payload.get("docs", [])
            if _is_forbidden(str(document.get("location", "")))
        }
    )
    if not exposed:
        return []
    return ["search index exposes internal locations: " + ", ".join(exposed)]


def _check_sitemap(site_dir: Path) -> list[str]:
    path = site_dir / "sitemap.xml"
    if not path.is_file():
        return [f"missing MkDocs sitemap: {path}"]
    root = ET.parse(path).getroot()
    exposed = sorted(
        {
            element.text or ""
            for element in root.iter()
            if element.tag.endswith("loc")
            and element.text
            and _is_forbidden(element.text)
        }
    )
    if not exposed:
        return []
    return ["sitemap exposes internal locations: " + ", ".join(exposed)]


def main() -> int:
    args = _parse_args()
    config = _load_config(args.config)
    docs_dir = args.config.parent / str(config.get("docs_dir", "docs"))
    if not args.site_dir.is_dir():
        raise FileNotFoundError(
            f"{args.site_dir} does not exist; run `mkdocs build --strict` first"
        )

    errors = [
        *_check_config(config),
        *_check_source_inventory(docs_dir, config),
        *_check_source_links(docs_dir, config),
        *_check_site_files(args.site_dir),
        *_check_site_links(args.site_dir),
        *_check_search(args.site_dir),
        *_check_sitemap(args.site_dir),
    ]
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print(
        "Public docs boundary OK: internal plans, experiments, and publisher "
        "procedures are absent from site files, search, and sitemap."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
