#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render or check the benchmark and context-method tables in the docs.

The tables are generated from ``scripts/support_matrix.yaml``. Each page embeds
a block between ``<!-- BEGIN CODENIB_SUPPORT_MATRIX:<kind> -->`` and the
matching END marker, where ``<kind>`` is ``methods``, ``benchmarks``,
``grid:<layers>`` (for example ``grid:L1`` or ``grid:L2+L3``), or
``card:<benchmark id>``.

The script imports nothing from ``codenib`` so that it runs in the docs-only CI
environment. It validates the data statically: entry points, links, anchors,
and that every benchmark adapter module is claimed by exactly one entry.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_PATH = _PROJECT_ROOT / "scripts" / "support_matrix.yaml"
_DOCS_ROOT = _PROJECT_ROOT / "docs"
_BENCHMARK_MODULES = _PROJECT_ROOT / "codenib" / "eval" / "benchmarks"

_BLOCK_RE = re.compile(
    r"<!-- BEGIN CODENIB_SUPPORT_MATRIX:(?P<kind>[\w:+-]+) -->\n"
    r".*?"
    r"<!-- END CODENIB_SUPPORT_MATRIX:(?P=kind) -->",
    re.DOTALL,
)
_PARADIGMS = {"A": "one-shot retrieval", "B": "fixed LLM workflow", "C": "agentic"}
_ROLES = {"CodeNib", "Reproduced", "Client", "Control"}
_ORIGINS = {"CodeNib", "External"}
_NO_HARNESS = "—"


@dataclass(frozen=True)
class Cell:
    level: str
    note: str = ""
    link: str = ""
    label: str = ""


def load_data(path: Path = _DATA_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _cell(raw: dict[str, Any]) -> Cell:
    return Cell(
        level=str(raw["level"]),
        note=str(raw.get("note", "")).strip(),
        link=str(raw.get("link", "")),
        label=str(raw.get("label", "")),
    )


def _rank(data: dict[str, Any], level: str) -> int:
    return list(data["evidence_levels"]).index(level)


def _relative(link: str, page: str) -> str:
    """Rewrite a docs-root-relative ``link`` for a page at docs/``page``."""

    if not link or "://" in link or link.startswith("#"):
        return link
    target, _, anchor = link.partition("#")
    if target == page:
        return f"#{anchor}" if anchor else posixpath.basename(page)
    relative = posixpath.relpath(target, posixpath.dirname(page) or ".")
    return f"{relative}#{anchor}" if anchor else relative


def _linked(text: str, link: str, page: str) -> str:
    return f"[{text}]({_relative(link, page)})" if link else text


def _entry(entry: dict[str, str]) -> str:
    if "cli" in entry:
        return f"`{entry['cli']}`"
    if "python" in entry:
        return f"`{entry['python'].rpartition(':')[2]}`"
    if "module" in entry:
        prefix = "python -m " if entry.get("run") else ""
        return f"`{prefix}{entry['module']}`"
    return f"`{entry['path']}`"


def _grid_benchmarks(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in data["benchmarks"] if item.get("in_grid", True)]


def _method_evidence(data: dict[str, Any], method: dict[str, Any], page: str) -> str:
    """Summarize a method's strongest claim across benchmarks and contracts."""

    benchmarks = {item["id"]: item for item in data["benchmarks"]}
    # (level, layer, earlier first, text): ties prefer the higher layer.
    candidates: list[tuple[int, int, int, str]] = []
    cells = method.get("cells", {})
    for order, (benchmark, raw) in enumerate(cells.items()):
        cell = _cell(raw)
        where = benchmarks[benchmark]["column"]
        if cell.level == "Measured":
            where += f" ({benchmarks[benchmark]['layer']})"
        candidates.append(
            (
                _rank(data, cell.level),
                int(benchmarks[benchmark]["layer"].removeprefix("L")),
                -order,
                f"{_linked(cell.level, cell.link, page)} · {where}",
            )
        )
    if "contract" in method:
        cell = _cell(method["contract"])
        label = cell.label or "upstream contract"
        candidates.append(
            (
                _rank(data, cell.level),
                0,
                -len(cells) - 1,
                f"{_linked(cell.level, cell.link, page)} · {label}",
            )
        )
    if "baseline" in method:
        level = str(method["baseline"])
        candidates.append(
            (_rank(data, level), 0, -len(cells) - 2, f"{level} · test suite")
        )
    if not candidates:
        raise ValueError(f"method {method['id']} has no evidence")
    best = max(candidates)
    if best[0] == _rank(data, "Runnable"):
        runnable = sum(1 for raw in cells.values() if _cell(raw).level == "Runnable")
        if runnable > 1:
            return f"Runnable · {runnable} benchmarks"
    return best[3]


def render_methods(data: dict[str, Any], page: str) -> str:
    lines = [
        "| Method | Type | Views read | Model at query time | Exposed through | Evidence |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for method in data["methods"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    method["name"],
                    f"{method['paradigm']} · {method['role']}",
                    method["views"],
                    method["model"],
                    method["surfaces"],
                    _method_evidence(data, method, page),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _benchmark_level(item: dict[str, Any], page: str) -> str:
    level = _linked(item["level"], item.get("results", ""), page)
    note = item.get("level_note")
    return f"{level}; {note}" if note else level


def render_benchmarks(data: dict[str, Any], page: str) -> str:
    lines = [
        "| Benchmark | Layer | Task | Languages | Entry point | Evidence |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in data["benchmarks"]:
        layer = data["layers"][item["layer"]]["name"]
        lines.append(
            "| "
            + " | ".join(
                [
                    _linked(item["name"], item["page"], page),
                    f"{item['layer']} {layer}",
                    item["task"],
                    item["languages"],
                    ", ".join(_entry(entry) for entry in item["entry"]),
                    _benchmark_level(item, page),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def render_grid(data: dict[str, Any], page: str, layers: set[str]) -> str:
    benchmarks = [item for item in _grid_benchmarks(data) if item["layer"] in layers]
    if not benchmarks:
        raise ValueError(f"no grid benchmarks in layers {sorted(layers)}")
    header = ["Method"] + [
        (
            item["column"]
            if item["layer"] == "L1"
            else f"{item['column']} ({item['layer']})"
        )
        for item in benchmarks
    ]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    notes: list[str] = []
    for method in data["methods"]:
        cells = method.get("cells", {})
        if not any(item["id"] in cells for item in benchmarks):
            continue
        row = [f"{method['paradigm']} · {method['name']}"]
        for item in benchmarks:
            raw = cells.get(item["id"])
            if raw is None:
                row.append(_NO_HARNESS)
                continue
            cell = _cell(raw)
            text = _linked(cell.level, cell.link, page)
            if cell.note:
                notes.append(f"**{method['name']} × {item['column']}.** {cell.note}")
                text += f"<sup>{len(notes)}</sup>"
            row.append(text)
        lines.append("| " + " | ".join(row) + " |")
    if notes:
        lines.append("")
        lines.extend(f"{index}. {note}" for index, note in enumerate(notes, 1))
    return "\n".join(lines)


def render_card(data: dict[str, Any], benchmark_id: str, page: str) -> str:
    items = {item["id"]: item for item in data["benchmarks"]}
    if benchmark_id not in items:
        raise ValueError(f"unknown benchmark card: {benchmark_id}")
    item = items[benchmark_id]
    layer = data["layers"][item["layer"]]
    scored = layer["scored"][0].lower() + layer["scored"][1:]
    rows = [
        ("Layer", f"{item['layer']} {layer['name']}: {scored}"),
        ("Origin", item["origin"]),
        ("Task", item["task"]),
        ("Ground truth", item["ground_truth"]),
        ("Granularity", item["granularity"]),
        ("Languages", item["languages"]),
        ("Size", item["size"]),
        ("Pinned revisions", item["pins"]),
        ("Scorer", item["scorer"]),
        ("Setup", item["setup"]),
        ("CodeNib entry point", ", ".join(_entry(entry) for entry in item["entry"])),
        ("Evidence", _benchmark_level(item, page)),
    ]
    if "url" in item:
        rows.append(("Upstream", f"<{item['url']}>"))
    lines = ["| Benchmark card | |", "| --- | --- |"]
    lines.extend(f"| {name} | {value} |" for name, value in rows)
    return "\n".join(lines)


def render_kind(data: dict[str, Any], kind: str, page: str) -> str:
    if kind == "methods":
        return render_methods(data, page)
    if kind == "benchmarks":
        return render_benchmarks(data, page)
    if kind.startswith("grid:"):
        return render_grid(data, page, set(kind.removeprefix("grid:").split("+")))
    if kind.startswith("card:"):
        return render_card(data, kind.removeprefix("card:"), page)
    raise ValueError(f"unknown support-matrix block: {kind}")


def render_page(text: str, page: str, data: dict[str, Any]) -> str:
    """Return ``text`` with every generated block regenerated for ``page``."""

    def replace(match: re.Match[str]) -> str:
        kind = match.group("kind")
        body = render_kind(data, kind, page)
        return (
            f"<!-- BEGIN CODENIB_SUPPORT_MATRIX:{kind} -->\n"
            f"{body}\n"
            f"<!-- END CODENIB_SUPPORT_MATRIX:{kind} -->"
        )

    return _BLOCK_RE.sub(replace, text)


def pages_with_blocks(docs_root: Path = _DOCS_ROOT) -> list[Path]:
    return sorted(
        path
        for path in docs_root.rglob("*.md")
        if "CODENIB_SUPPORT_MATRIX:" in path.read_text(encoding="utf-8")
    )


def _slugify(value: str) -> str:
    """Match Python-Markdown's default ``toc`` slugify."""

    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if in_fence or not match:
            continue
        heading = match.group(1)
        custom = re.search(r"\{[^}]*#([\w-]+)[^}]*\}\s*$", heading)
        if custom:
            anchors.add(custom.group(1))
            continue
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
        text = re.sub(r"<[^>]+>", "", text).replace("`", "")
        slug = _slugify(text)
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}_{count}")
    return anchors


def _iter_links(data: dict[str, Any]):
    for item in data["benchmarks"]:
        yield f"benchmark {item['id']} page", item["page"]
        if item.get("results"):
            yield f"benchmark {item['id']} results", item["results"]
    for method in data["methods"]:
        for key, raw in method.get("cells", {}).items():
            if raw.get("link"):
                yield f"method {method['id']} × {key}", raw["link"]
        if method.get("contract", {}).get("link"):
            yield f"method {method['id']} contract", method["contract"]["link"]


def _console_scripts(root: Path) -> set[str]:
    with (root / "pyproject.toml").open("rb") as stream:
        return set(tomllib.load(stream)["project"]["scripts"])


def _claimed_modules(data: dict[str, Any]) -> list[str]:
    claimed: list[str] = list(data.get("shared_modules", []))
    for section in ("benchmarks", "methods"):
        for item in data[section]:
            claimed.extend(item.get("modules", []))
    return claimed


def validate(data: dict[str, Any], root: Path = _PROJECT_ROOT) -> list[str]:
    """Return every inconsistency between the data, the code, and the docs."""

    errors: list[str] = []
    levels = set(data["evidence_levels"])
    benchmark_ids = [item["id"] for item in data["benchmarks"]]
    grid_ids = {item["id"] for item in _grid_benchmarks(data)}
    method_ids = [item["id"] for item in data["methods"]]
    for kind, ids in (("benchmark", benchmark_ids), ("method", method_ids)):
        duplicates = {value for value in ids if ids.count(value) > 1}
        if duplicates:
            errors.append(f"duplicate {kind} ids: {sorted(duplicates)}")

    scripts = _console_scripts(root)
    for item in data["benchmarks"]:
        where = f"benchmark {item['id']}"
        if item["layer"] not in data["layers"]:
            errors.append(f"{where}: unknown layer {item['layer']}")
        if item["level"] not in levels:
            errors.append(f"{where}: unknown level {item['level']}")
        if item["origin"] not in _ORIGINS:
            errors.append(f"{where}: unknown origin {item['origin']}")
        for entry in item["entry"]:
            if "cli" in entry and entry["cli"] not in scripts:
                errors.append(
                    f"{where}: console script {entry['cli']} not in pyproject"
                )
            if "python" in entry:
                relative, _, symbol = entry["python"].partition(":")
                source = root / relative
                pattern = rf"^(?:class|def)\s+{re.escape(symbol)}\b"
                if not source.is_file():
                    errors.append(f"{where}: {relative} does not exist")
                elif not re.search(pattern, source.read_text(encoding="utf-8"), re.M):
                    errors.append(f"{where}: {symbol} not defined in {relative}")
            if "path" in entry and not (root / entry["path"]).exists():
                errors.append(f"{where}: {entry['path']} does not exist")
            if "module" in entry:
                base = root / entry["module"].replace(".", "/")
                if not (
                    base.with_suffix(".py").is_file()
                    or (base / "__init__.py").is_file()
                ):
                    errors.append(f"{where}: module {entry['module']} does not exist")

    for method in data["methods"]:
        where = f"method {method['id']}"
        if method["paradigm"] not in _PARADIGMS:
            errors.append(f"{where}: unknown paradigm {method['paradigm']}")
        if method["role"] not in _ROLES:
            errors.append(f"{where}: unknown role {method['role']}")
        raw_cells = list(method.get("cells", {}).items())
        if "contract" in method:
            raw_cells.append(("contract", method["contract"]))
        for key, raw in raw_cells:
            if key != "contract" and key not in grid_ids:
                errors.append(f"{where}: cell for unknown benchmark {key}")
            if raw.get("level") not in levels:
                errors.append(f"{where} × {key}: unknown level {raw.get('level')}")
        if "baseline" in method and method["baseline"] not in levels:
            errors.append(f"{where}: unknown baseline {method['baseline']}")

    docs = root / "docs"
    anchor_cache: dict[Path, set[str]] = {}
    for where, link in _iter_links(data):
        target, _, anchor = link.partition("#")
        path = docs / target
        if not path.is_file():
            errors.append(f"{where}: docs/{target} does not exist")
            continue
        if anchor:
            anchors = anchor_cache.setdefault(path, _anchors(path))
            if anchor not in anchors:
                errors.append(f"{where}: no #{anchor} heading in docs/{target}")

    claimed = _claimed_modules(data)
    duplicates = sorted({value for value in claimed if claimed.count(value) > 1})
    if duplicates:
        errors.append(f"modules claimed more than once: {duplicates}")
    present = {
        path.relative_to(root).as_posix()
        for path in (root / _BENCHMARK_MODULES.relative_to(_PROJECT_ROOT)).rglob("*.py")
        if "__pycache__" not in path.parts
    }
    for module in sorted(present - set(claimed)):
        errors.append(
            f"{module} is not claimed by any benchmark or method in "
            "scripts/support_matrix.yaml"
        )
    for module in sorted(set(claimed) - present):
        errors.append(f"claimed module {module} does not exist")

    cards: dict[str, list[str]] = {}
    for path in pages_with_blocks(docs):
        page = path.relative_to(docs).as_posix()
        for match in _BLOCK_RE.finditer(path.read_text(encoding="utf-8")):
            kind = match.group("kind")
            if kind.startswith("card:"):
                cards.setdefault(kind.removeprefix("card:"), []).append(page)
    for item in data["benchmarks"]:
        expected = item["page"].partition("#")[0]
        found = cards.get(item["id"], [])
        if found != [expected]:
            errors.append(
                f"benchmark {item['id']}: card must appear once, in docs/{expected}; "
                f"found in {found or 'no page'}"
            )
    return errors


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render/check the support tables generated from support_matrix.yaml."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Update every page")
    mode.add_argument(
        "--check", action="store_true", help="Fail if data or pages are out of date"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    data = load_data()
    errors = validate(data)
    stale: list[str] = []
    for path in pages_with_blocks():
        page = path.relative_to(_DOCS_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        rendered = render_page(text, page, data)
        if rendered == text:
            continue
        if args.write:
            path.write_text(rendered, encoding="utf-8")
        else:
            stale.append(page)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    for page in stale:
        print(
            f"error: docs/{page} is out of date; run "
            "python scripts/support_matrix.py --write",
            file=sys.stderr,
        )
    return 1 if errors or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
