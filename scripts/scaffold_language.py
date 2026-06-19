#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Scaffold the first files for a new CodeMiner language.

The scaffold is intentionally conservative: it creates TODO stubs and prints
the LanguageSpec snippet and manual wiring checklist, but it does not register
the language automatically. A generated file should never make a language look
supported before the implementation and tests are filled in.
"""

from __future__ import annotations

import argparse
import ast
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ("none", "scip", "lsp", "clangd")
INCREMENTAL_BACKENDS = ("none", "lsp", "clangd")


@dataclass(frozen=True)
class PlannedFile:
    """A file that the scaffold can create."""

    path: Path
    content: str


@dataclass(frozen=True)
class ScaffoldConfig:
    """Normalized scaffold settings."""

    key: str
    display_name: str
    extensions: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    chunker: bool = True
    graph_backend: str = "none"
    incremental_backend: str = "none"
    core_decoder: bool = False
    root: Path = PROJECT_ROOT


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _split_repeatable(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    parts: list[str] = []
    for value in values:
        parts.extend(piece.strip() for piece in value.split(","))
    return _unique(part for part in parts if part)


def _normalize_extension(value: str) -> str:
    value = value.strip().lower()
    if not value:
        raise ValueError("empty extension")
    return value if value.startswith(".") else f".{value}"


def _validate_key(value: str) -> str:
    value = value.strip().lower().replace("-", "_")
    if not value:
        raise ValueError("language key cannot be empty")
    if not value[0].isalpha():
        raise ValueError("language key must start with a letter")
    bad = [ch for ch in value if not (ch.isalnum() or ch == "_")]
    if bad:
        raise ValueError("language key may only contain letters, numbers, and '_'")
    return value


def _class_prefix(key: str) -> str:
    return "".join(part.capitalize() for part in key.split("_") if part)


def _default_display_name(key: str) -> str:
    return " ".join(part.capitalize() for part in key.split("_") if part)


def _tuple_literal(values: Sequence[str]) -> str:
    if not values:
        return "()"
    if len(values) == 1:
        return f"({values[0]!r},)"
    return "(" + ", ".join(repr(value) for value in values) + ")"


def _pair_tuple_literal(values: Sequence[tuple[str, str]]) -> str:
    if not values:
        return "()"
    rendered = ", ".join(f"({left!r}, {right!r})" for left, right in values)
    if len(values) == 1:
        return f"({rendered},)"
    return f"({rendered})"


def render_language_spec(config: ScaffoldConfig) -> str:
    """Return the LanguageSpec block to paste into codeminer/languages.py."""

    aliases = _unique((config.key, *config.aliases))
    agent_aliases = tuple((alias, config.key) for alias in aliases)
    graph_language = config.key if config.graph_backend != "none" else None
    cold_start_backend = (
        config.graph_backend if config.graph_backend != "none" else None
    )
    incremental_backend = (
        config.incremental_backend if config.incremental_backend != "none" else None
    )

    lines = [
        "LanguageSpec(",
        f"    key={config.key!r},",
        f"    display_name={config.display_name!r},",
        f"    aliases={_tuple_literal(config.aliases)},",
    ]
    if config.chunker:
        lines.extend(
            [
                f"    chunker_language={config.key!r},",
                f"    chunker_aliases={_tuple_literal(aliases)},",
                f"    chunk_extensions={_tuple_literal(config.extensions)},",
                f"    gt_language={config.key!r},",
                f"    gt_extensions={_tuple_literal(config.extensions)},",
            ]
        )
    if graph_language:
        lines.extend(
            [
                f"    graph_language={graph_language!r},",
                f"    graph_aliases={_tuple_literal(aliases)},",
                f"    graph_extensions={_tuple_literal(config.extensions)},",
            ]
        )
    lines.extend(
        [
            f"    agent_languages={_tuple_literal((config.key,))},",
            f"    agent_aliases={_pair_tuple_literal(agent_aliases)},",
            f"    cold_start_backend={cold_start_backend!r},",
            f"    incremental_backend={incremental_backend!r},",
            f"    core_decoder={config.core_decoder!r},",
            "),",
        ]
    )
    return "\n".join(lines)


def _chunker_template(config: ScaffoldConfig) -> str:
    cls = _class_prefix(config.key)
    return textwrap.dedent(
        f'''\
        #!/usr/bin/env python3

        # SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
        #
        # SPDX-License-Identifier: Apache-2.0

        """{config.display_name} code chunker scaffold."""

        from __future__ import annotations

        from typing import List, Optional, Tuple

        from .base import BaseCodeChunker


        class {cls}CodeChunker(BaseCodeChunker):
            """TODO: implement tree-sitter chunking for {config.display_name}."""

            def __init__(
                self,
                max_lines_per_chunk: Optional[int] = None,
                chunk_depth: int = 2,
                l2_level_exclusive: bool = True,
                **kwargs,
            ):
                super().__init__(
                    {config.key!r},
                    max_lines_per_chunk=max_lines_per_chunk,
                    chunk_depth=chunk_depth,
                    l2_level_exclusive=l2_level_exclusive,
                    **kwargs,
                )

            def _find_top_level_definitions(
                self, root_node, include_l2_in_file_skeleton: bool = False
            ) -> List[Tuple]:
                raise NotImplementedError(
                    "Implement {config.key} tree-sitter symbol extraction before "
                    "registering this chunker."
                )
        '''
    )


def _chunker_test_template(config: ScaffoldConfig) -> str:
    cls = _class_prefix(config.key)
    ext = config.extensions[0]
    return textwrap.dedent(
        f'''\
        # SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
        #
        # SPDX-License-Identifier: Apache-2.0

        """TODO tests for the {config.display_name} chunker scaffold."""

        import pytest

        from codeminer.code_chunking.{config.key}_chunker import {cls}CodeChunker

        pytestmark = pytest.mark.skip(
            reason="Scaffold stub: implement {config.key} chunker assertions."
        )


        def test_{config.key}_chunker_extracts_top_level_symbols():
            chunker = {cls}CodeChunker()
            chunks = chunker.chunk_code("TODO sample source\\n", file_path="sample{ext}")
            assert chunks
        '''
    )


def _scip_decoder_template(config: ScaffoldConfig) -> str:
    cls = _class_prefix(config.key)
    return textwrap.dedent(
        f'''\
        #!/usr/bin/env python3

        # SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
        #
        # SPDX-License-Identifier: Apache-2.0

        """SCIP decoder scaffold for {config.display_name} projects."""

        from __future__ import annotations

        from ..graph.code_graph import CodeGraph


        class SCIP{cls}GraphDecoder:
            """TODO: decode {config.display_name} SCIP data into CodeGraph."""

            def __init__(self, index_file_path, project_root=None):
                self.index_file_path = index_file_path
                self.project_root = project_root
                self.code_graph = CodeGraph(project_root)

            def decode(self) -> CodeGraph:
                raise NotImplementedError(
                    "Implement SCIP {config.key} decode and keep serial/core "
                    "parity tests green before enabling this backend."
                )
        '''
    )


def _scip_indexer_template(config: ScaffoldConfig) -> str:
    cls = _class_prefix(config.key)
    return textwrap.dedent(
        f'''\
        #!/usr/bin/env python3

        # SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
        #
        # SPDX-License-Identifier: Apache-2.0

        """SCIP indexer scaffold for {config.display_name} projects."""

        from __future__ import annotations

        from pathlib import Path
        from typing import List, Optional, Union

        from ..profiler import Profiler
        from .scip_indexer_base import SCIPIndexerBase


        class SCIP{cls}Indexer(SCIPIndexerBase):
            """TODO: invoke the {config.display_name} SCIP indexer."""

            def __init__(
                self,
                project_root: Union[str, Path],
                output_dir: Optional[Union[str, Path]] = None,
                exclude_patterns: Optional[List] = None,
                profiler: Optional[Profiler] = None,
                decoder_backend: Optional[str] = None,
            ):
                super().__init__(
                    project_root=project_root,
                    output_dir=output_dir,
                    exclude_patterns=exclude_patterns,
                    profiler=profiler,
                    language={config.key!r},
                    decoder_backend=decoder_backend,
                )

            def _check_indexer_available(self) -> bool:
                return False

            def _build_index_command(self, **kwargs) -> List[str]:
                raise NotImplementedError(
                    "Add the {config.display_name} SCIP index command."
                )

            def _get_decoder_class(self):
                from .scip_decode_{config.key} import SCIP{cls}GraphDecoder

                return SCIP{cls}GraphDecoder
        '''
    )


def _ls_indexer_template(config: ScaffoldConfig) -> str:
    cls = _class_prefix(config.key)
    backend = "clangd-style" if config.graph_backend == "clangd" else "generic LSP"
    return textwrap.dedent(
        f'''\
        #!/usr/bin/env python3

        # SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
        #
        # SPDX-License-Identifier: Apache-2.0

        """{backend} indexer scaffold for {config.display_name} projects."""

        from __future__ import annotations

        from pathlib import Path
        from typing import Optional, Union


        class {cls}LSPIndexer:
            """TODO: drive the {config.display_name} language server."""

            def __init__(
                self,
                project_root: Union[str, Path],
                output_dir: Optional[Union[str, Path]] = None,
            ):
                self.project_root = Path(project_root).absolute()
                self.output_dir = (
                    Path(output_dir).absolute()
                    if output_dir is not None
                    else Path("/tmp") / self.project_root.name
                )

            def generate_index(self) -> bool:
                raise NotImplementedError(
                    "Implement {config.display_name} LSP lifecycle, document "
                    "symbol collection, and reference collection."
                )

            def process_index(self):
                from .{config.key}_decode import {cls}LSPGraphDecoder

                return {cls}LSPGraphDecoder(self.project_root).decode()
        '''
    )


def _ls_decoder_template(config: ScaffoldConfig) -> str:
    cls = _class_prefix(config.key)
    return textwrap.dedent(
        f'''\
        #!/usr/bin/env python3

        # SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
        #
        # SPDX-License-Identifier: Apache-2.0

        """LSP graph decoder scaffold for {config.display_name} projects."""

        from __future__ import annotations

        from pathlib import Path

        from ..graph.code_graph import CodeGraph


        class {cls}LSPGraphDecoder:
            """TODO: normalize {config.display_name} LSP responses into CodeGraph."""

            def __init__(self, project_root: str | Path):
                self.project_root = Path(project_root).absolute()
                self.code_graph = CodeGraph(self.project_root)

            def decode(self) -> CodeGraph:
                raise NotImplementedError(
                    "Implement LSP-to-CodeGraph normalization and backend "
                    "alignment tests before enabling this backend."
                )
        '''
    )


def _incremental_patcher_template(config: ScaffoldConfig) -> str:
    cls = _class_prefix(config.key)
    command = (
        '["TODO-language-server", "--stdio"]'
        if config.incremental_backend == "lsp"
        else '["TODO-indexer"]'
    )
    return textwrap.dedent(
        f'''\
        # SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
        #
        # SPDX-License-Identifier: Apache-2.0

        """{config.display_name}-specific incremental patcher scaffold."""

        from __future__ import annotations

        from ...types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
        from .patcher_base import PatcherBase


        class Patcher{cls}(PatcherBase):
            """TODO: align with the {config.display_name} cold-start graph decoder."""

            def get_lsp_command(self) -> list[str]:
                return {command}

            def _language_id(self) -> str:
                return {config.key!r}

            def _get_crossfile_token_types(self) -> set[str]:
                return {{"class", "function", "method", "namespace", "property"}}

            def _build_unified_name(self, file_path, name, parent_unified_part, kind):
                node_type = self._classify_symbol_type(kind)
                display = f"{{parent_unified_part}}.{{name}}" if parent_unified_part else name
                if node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
                    if not display.endswith("()"):
                        display = f"{{display}}()"
                return f"{{file_path}}:{{display}}"

            def flatten_symbols(self, file_path, lsp_symbols):
                return self._flatten_symbols_default(file_path, lsp_symbols)
        '''
    )


def _incremental_test_template(config: ScaffoldConfig) -> str:
    cls = _class_prefix(config.key)
    return textwrap.dedent(
        f'''\
        # SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
        #
        # SPDX-License-Identifier: Apache-2.0

        """TODO tests for the {config.display_name} incremental patcher scaffold."""

        import pytest

        from codeminer.graph.incremental.patcher_{config.key} import Patcher{cls}

        pytestmark = pytest.mark.skip(
            reason="Scaffold stub: implement {config.key} patcher assertions."
        )


        def test_{config.key}_patcher_language_id():
            patcher = Patcher{cls}(".", None)
            assert patcher._language_id() == {config.key!r}
        '''
    )


def _graph_test_template(config: ScaffoldConfig) -> str:
    area = "scip" if config.graph_backend == "scip" else "ls_index"
    return textwrap.dedent(
        f'''\
        # SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
        #
        # SPDX-License-Identifier: Apache-2.0

        """TODO {config.display_name} {config.graph_backend} graph tests."""

        import pytest

        pytestmark = pytest.mark.skip(
            reason="Scaffold stub: implement {config.key} {config.graph_backend} graph tests."
        )


        def test_{config.key}_{area}_builds_codegraph():
            raise AssertionError("replace scaffold with a real graph fixture")
        '''
    )


def _core_header_template(config: ScaffoldConfig) -> str:
    cls = _class_prefix(config.key)
    guard = f"CODEMINER_CORE_SCIP_DECODE_{config.key.upper()}_H"
    return textwrap.dedent(
        f"""\
        /*
         * SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
         *
         * SPDX-License-Identifier: Apache-2.0
         */

        #ifndef {guard}
        #define {guard}

        #include "scip_decode_base.h"

        namespace codeminer::core {{

        class SCIP{cls}Decoder : public SCIPDecoderBase {{
        public:
          using SCIPDecoderBase::SCIPDecoderBase;

        protected:
          Subgraph process_document(const std::string &document_block) const override;
        }};

        }} // namespace codeminer::core

        #endif // {guard}
        """
    )


def _core_cpp_template(config: ScaffoldConfig) -> str:
    cls = _class_prefix(config.key)
    return textwrap.dedent(
        f"""\
        /*
         * SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
         *
         * SPDX-License-Identifier: Apache-2.0
         */

        #include "scip_decode_{config.key}.h"

        #include <stdexcept>

        namespace codeminer::core {{

        Subgraph SCIP{cls}Decoder::process_document(
            const std::string &document_block) const {{
          (void)document_block;
          throw std::runtime_error(
              "TODO: implement SCIP {config.display_name} core decode and keep "
              "serial/core parity bit-for-bit.");
        }}

        }} // namespace codeminer::core
        """
    )


def _core_test_template(config: ScaffoldConfig) -> str:
    return textwrap.dedent(
        f'''\
        # SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
        #
        # SPDX-License-Identifier: Apache-2.0

        """TODO serial/core parity test for {config.display_name}."""

        import pytest

        pytestmark = pytest.mark.skip(
            reason="Scaffold stub: implement {config.key} serial/core parity."
        )


        def test_core_{config.key}_parity():
            raise AssertionError("replace scaffold with real serial/core parity fixture")
        '''
    )


def build_plan(config: ScaffoldConfig) -> list[PlannedFile]:
    """Build the list of files for a scaffold run."""

    files: list[PlannedFile] = []
    key = config.key

    if config.chunker:
        files.extend(
            [
                PlannedFile(
                    Path("codeminer") / "code_chunking" / f"{key}_chunker.py",
                    _chunker_template(config),
                ),
                PlannedFile(
                    Path("test") / "chunker" / f"test_{key}_chunker.py",
                    _chunker_test_template(config),
                ),
            ]
        )

    if config.graph_backend == "scip":
        files.extend(
            [
                PlannedFile(
                    Path("codeminer") / "scip_interface" / f"scip_indexer_{key}.py",
                    _scip_indexer_template(config),
                ),
                PlannedFile(
                    Path("codeminer") / "scip_interface" / f"scip_decode_{key}.py",
                    _scip_decoder_template(config),
                ),
                PlannedFile(
                    Path("test") / "scip" / f"test_{key}_scip.py",
                    _graph_test_template(config),
                ),
            ]
        )
    elif config.graph_backend in {"lsp", "clangd"}:
        files.extend(
            [
                PlannedFile(
                    Path("codeminer") / "ls_index" / f"{key}_indexer.py",
                    _ls_indexer_template(config),
                ),
                PlannedFile(
                    Path("codeminer") / "ls_index" / f"{key}_decode.py",
                    _ls_decoder_template(config),
                ),
                PlannedFile(
                    Path("test") / "ls_index" / f"test_{key}_indexer.py",
                    _graph_test_template(config),
                ),
            ]
        )

    if config.incremental_backend != "none":
        files.extend(
            [
                PlannedFile(
                    Path("codeminer") / "graph" / "incremental" / f"patcher_{key}.py",
                    _incremental_patcher_template(config),
                ),
                PlannedFile(
                    Path("test") / "graph" / "incremental" / f"test_patcher_{key}.py",
                    _incremental_test_template(config),
                ),
            ]
        )

    if config.core_decoder:
        files.extend(
            [
                PlannedFile(
                    Path("core") / f"scip_decode_{key}.h",
                    _core_header_template(config),
                ),
                PlannedFile(
                    Path("core") / f"scip_decode_{key}.cpp",
                    _core_cpp_template(config),
                ),
                PlannedFile(
                    Path("test") / "scip" / f"test_core_{key}.py",
                    _core_test_template(config),
                ),
            ]
        )

    return files


def _manual_wiring(config: ScaffoldConfig) -> list[str]:
    items = [
        "Add the printed LanguageSpec to codeminer/languages.py.",
        "Add alias/extension tests to test/test_languages.py.",
    ]
    if config.chunker:
        items.append(
            "Export and route the chunker in codeminer/code_chunking/__init__.py."
        )
    if config.graph_backend != "none":
        items.append("Route the graph backend through codeminer/ls_router.py.")
    if config.incremental_backend != "none":
        items.append(
            "Route the patcher through codeminer/graph/incremental/graph_patcher.py."
        )
    if config.core_decoder:
        items.append(
            "Register the core decoder in core/CMakeLists.txt and "
            "core/bindings/pybind_module.cpp."
        )
    return items


def render_plan(config: ScaffoldConfig, files: Sequence[PlannedFile]) -> str:
    """Render the dry-run plan and LanguageSpec snippet."""

    lines = [
        "LanguageSpec snippet:",
        "",
        render_language_spec(config),
        "",
        "Planned files:",
    ]
    if files:
        lines.extend(f"  - {file.path}" for file in files)
    else:
        lines.append("  - none")
    lines.extend(["", "Manual wiring after scaffold:"])
    lines.extend(f"  - {item}" for item in _manual_wiring(config))
    return "\n".join(lines)


def write_plan(files: Sequence[PlannedFile], root: Path, force: bool = False) -> None:
    """Write planned files, refusing to overwrite unless force=True."""

    root = root.resolve()
    existing = [file.path for file in files if (root / file.path).exists()]
    if existing and not force:
        rendered = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"refusing to overwrite existing file(s): {rendered}")

    for file in files:
        target = root / file.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file.content, encoding="utf-8")


def _language_exists(key: str, root: Path = PROJECT_ROOT) -> bool:
    languages_py = root / "codeminer" / "languages.py"
    if not languages_py.exists():
        languages_py = PROJECT_ROOT / "codeminer" / "languages.py"
    try:
        tree = ast.parse(languages_py.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "LanguageSpec":
            continue
        for keyword in node.keywords:
            if keyword.arg != "key":
                continue
            if isinstance(keyword.value, ast.Constant) and keyword.value.value == key:
                return True
    return False


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("language", help="canonical registry key, e.g. java")
    parser.add_argument("--display-name", help="human-readable language name")
    parser.add_argument(
        "--extension",
        action="append",
        help="source extension, repeatable or comma-separated, e.g. .java",
    )
    parser.add_argument(
        "--alias",
        action="append",
        help="language alias, repeatable or comma-separated",
    )
    parser.add_argument(
        "--graph-backend",
        choices=BACKENDS,
        default="none",
        help="cold-start graph backend to scaffold",
    )
    parser.add_argument(
        "--incremental-backend",
        choices=INCREMENTAL_BACKENDS,
        default="none",
        help="incremental graph backend to scaffold",
    )
    parser.add_argument(
        "--no-chunker",
        action="store_true",
        help="skip tree-sitter chunker and chunker test stubs",
    )
    parser.add_argument(
        "--core-decoder",
        action="store_true",
        help="scaffold current SCIP core decoder files and parity test stub",
    )
    parser.add_argument(
        "--allow-existing",
        action="store_true",
        help="allow scaffolding a key that already exists in LanguageSpec",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="create files; without this flag the command only prints a plan",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow --write to overwrite generated paths",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    try:
        args.language = _validate_key(args.language)
    except ValueError as exc:
        parser.error(str(exc))

    extensions = _split_repeatable(args.extension)
    if not extensions:
        parser.error("at least one --extension is required")
    try:
        args.extensions = tuple(_normalize_extension(ext) for ext in extensions)
    except ValueError as exc:
        parser.error(str(exc))

    aliases = _split_repeatable(args.alias)
    try:
        args.aliases = tuple(_validate_key(alias) for alias in aliases)
    except ValueError as exc:
        parser.error(str(exc))

    if args.core_decoder and args.graph_backend != "scip":
        parser.error("--core-decoder currently requires --graph-backend scip")
    if args.incremental_backend != "none" and args.graph_backend == "none":
        parser.error("--incremental-backend requires a non-none --graph-backend")
    if _language_exists(args.language, args.root) and not args.allow_existing:
        parser.error(
            f"{args.language!r} is already registered; pass --allow-existing "
            "only when intentionally adding missing scaffold files"
        )
    return args


def config_from_args(args: argparse.Namespace) -> ScaffoldConfig:
    """Convert argparse output to a normalized config."""

    return ScaffoldConfig(
        key=args.language,
        display_name=args.display_name or _default_display_name(args.language),
        extensions=_unique(args.extensions),
        aliases=_unique(args.aliases),
        chunker=not args.no_chunker,
        graph_backend=args.graph_backend,
        incremental_backend=args.incremental_backend,
        core_decoder=args.core_decoder,
        root=args.root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    config = config_from_args(args)
    files = build_plan(config)
    print(render_plan(config, files))
    if args.write:
        try:
            write_plan(files, config.root, force=args.force)
        except FileExistsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"\nCreated {len(files)} file(s) under {config.root.resolve()}")
    else:
        print("\nDry run only. Re-run with --write to create these files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
