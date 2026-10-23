#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
SCIP indexer for Go projects using scip-go.

scip-go indexes one module at a time, and ``./...`` at the repository root
stops at nested ``go.mod`` files. Repositories split into nested modules are
indexed module by module and merged into one index. The merge prefixes each
document with its module's directory and renames the module's symbols into
the root module's namespace, so ``go.etcd.io/etcd/server/v3/auth`` becomes
``go.etcd.io/etcd/v3/server/auth``. The decoders then name every package by
its directory and resolve references across modules.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple, Union

from ..log_utils import get_logger
from ..profiler import Profiler
from .scip_indexer_base import SCIPIndexerBase

logger = get_logger("scip_go_indexer")

# Directories the go command ignores, plus vendored and VCS trees.
_SKIPPED_DIRS = {"vendor", "testdata", "node_modules"}
_MODULE_LINE = re.compile(r"^[ \t]*module[ \t]+(\S+)", re.MULTILINE)
_PACKAGE = re.compile(r"`([^`]+)`")


def go_module_path(go_mod: Path) -> Optional[str]:
    """The module path declared in ``go_mod``, if any."""
    try:
        match = _MODULE_LINE.search(go_mod.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return match.group(1).strip('"') if match else None


def nested_go_modules(project_root: Path) -> List[Tuple[str, str]]:
    """``(module path, directory)`` of modules below the root, in path order."""
    modules = []
    for directory, subdirs, files in os.walk(project_root):
        subdirs[:] = sorted(
            name
            for name in subdirs
            if name not in _SKIPPED_DIRS and not name.startswith((".", "_"))
        )
        relative = Path(directory).relative_to(project_root)
        if relative != Path(".") and "go.mod" in files:
            module = go_module_path(Path(directory) / "go.mod")
            if module:
                modules.append((module, relative.as_posix()))
    return modules


_REPLACE = re.compile(r"^(?:replace\s+)?(\S+)(?:\s+\S+)?\s+=>\s+(\S+)")


def local_replacements(go_mod: Path, project_root: Path) -> List[Tuple[str, str]]:
    """``(module path, directory)`` for ``replace`` directives in ``go_mod``
    that point at a directory inside ``project_root``."""
    try:
        text = go_mod.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    found, in_block = [], False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if re.fullmatch(r"replace\s*\(", line):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if not (in_block or line.startswith("replace ")):
            continue
        match = _REPLACE.match(line)
        if not match or not match.group(2).startswith(("./", "../")):
            continue
        target = (go_mod.parent / match.group(2)).resolve()
        try:
            directory = target.relative_to(project_root.resolve()).as_posix()
        except ValueError:
            continue
        found.append((match.group(1).strip('"'), "" if directory == "." else directory))
    return found


def rename_module_symbol(
    symbol: str, root_module: str, directories: dict, version: Optional[str] = None
) -> str:
    """Rename a nested module's scip-go symbol into the root module's namespace.

    ``directories`` maps module paths to directories; ``""`` means the root
    module itself, whose symbols keep their packages. ``version``, when
    given, replaces the symbol's version, so a reference to a required
    release meets the checked-out definition.
    """
    parts = symbol.split(" ", 4)
    if len(parts) < 5 or parts[:2] != ["scip-go", "gomod"]:
        return symbol
    module = parts[2]
    directory = directories.get(module)
    if directory is None:
        return symbol

    def package(match: re.Match) -> str:
        path = match.group(1)
        if path == module or path.startswith(module + "/"):
            prefix = f"{root_module}/{directory}" if directory else root_module
            path = prefix + path[len(module) :]
        return f"`{path}`"

    parts[2] = root_module
    if version:
        parts[3] = version
    parts[4] = _PACKAGE.sub(package, parts[4])
    return " ".join(parts)


def merge_module_indexes(root, nested, root_module: str, modules=(), aliases=None):
    """Merge per-module SCIP indexes into ``root`` (``scip_pb2.Index`` objects).

    ``nested`` holds ``(module path, directory, index)`` for each module
    indexed on its own; ``modules`` lists ``(module path, directory)`` for
    nested modules the root index already covers. ``aliases`` maps the
    directory of each ``go.mod`` (``""`` for the root) to its local
    ``replace`` paths and their module directories; a module's replacements
    apply only to its own index, as in Go.
    Symbols of a path that names one directory are renamed everywhere, the
    root index included, so references across modules meet the definitions.
    A path declared in several directories is renamed only inside its own
    index; references to it from elsewhere stay unresolved rather than pick a
    directory. Root-module symbols keep their packages and take the canonical
    version.
    """
    aliases = aliases or {}
    pairs = {(module, directory) for module, directory, _ in nested}
    pairs |= set(modules)
    places: dict = {}
    for module, directory in pairs:
        if module != root_module:
            places.setdefault(module, set()).add(directory)
    unique = {
        module: next(iter(directories))
        for module, directories in places.items()
        if len(directories) == 1
    }
    unique[root_module] = ""

    # The root module's own version, else the first nested module's, so the
    # choice is deterministic even when the root holds no Go sources.
    version = _module_version(root, root_module)
    for module, _directory, index in nested:
        version = version or _module_version(index, module)

    def renamer(directories: dict):
        def rename(value: str) -> str:
            return rename_module_symbol(value, root_module, directories, version)

        return rename

    def rename_information(information, rename) -> None:
        information.symbol = rename(information.symbol)
        information.enclosing_symbol = rename(information.enclosing_symbol)
        for relationship in information.relationships:
            relationship.symbol = rename(relationship.symbol)

    def rename_document(document, rename) -> None:
        for occurrence in document.occurrences:
            occurrence.symbol = rename(occurrence.symbol)
        for information in document.symbols:
            rename_information(information, rename)

    shared = renamer({**unique, **dict(aliases.get("", ()))})
    for document in root.documents:
        rename_document(document, shared)
    for information in root.external_symbols:
        rename_information(information, shared)
    known = {information.symbol for information in root.external_symbols}
    for module, directory, index in nested:
        own = renamer({**unique, **dict(aliases.get(directory, ())), module: directory})
        for document in index.documents:
            rename_document(document, own)
            document.relative_path = f"{directory}/{document.relative_path}"
            root.documents.append(document)
        for information in index.external_symbols:
            rename_information(information, own)
            if information.symbol not in known:
                known.add(information.symbol)
                root.external_symbols.append(information)
    return root


def _module_version(index, module: str) -> Optional[str]:
    """The version scip-go stamped on ``module``'s own symbols in ``index``."""
    for document in index.documents:
        for occurrence in document.occurrences:
            parts = occurrence.symbol.split(" ", 4)
            if len(parts) == 5 and parts[:3] == ["scip-go", "gomod", module]:
                return parts[3]
    return None


def _without_workspace() -> dict:
    """The environment with any enclosing ``go.work`` disabled.

    A nested module is indexed on its own, so a workspace file that lists
    only some modules must not apply to it.
    """
    return {**os.environ, "GOWORK": "off"}


class SCIPGoIndexer(SCIPIndexerBase):
    """
    SCIP indexer for Go projects.

    Uses the scip-go tool to generate SCIP indices for Go codebases.
    Install: make scip-go-tool
    """

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
            language="go",
            decoder_backend=decoder_backend,
        )

    def _check_indexer_available(self) -> bool:
        """Check if scip-go is available."""
        try:
            result = subprocess.run(
                ["scip-go", "--help"],
                capture_output=True,
                text=True,
            )
            # scip-go --help returns 0 on success
            if result.returncode == 0:
                logger.info("scip-go is available")
                return True
            # Some versions print help to stderr but still work
            logger.info("scip-go is available (non-zero exit on --help)")
            return True
        except FileNotFoundError:
            logger.error(
                "scip-go not found. Run make scip-go-tool and use "
                "make active-scip-env for PATH."
            )
            return False

    def _build_index_command(self, **kwargs) -> List[str]:
        """Build the command to generate the SCIP index for Go."""
        return ["scip-go", "--output", str(self.index_file)]

    def _get_decoder_class(self):
        """Get the decoder class for Go."""
        from .scip_decode_go import SCIPGoGraphDecoder

        return SCIPGoGraphDecoder

    def generate_index(self, **kwargs) -> bool:
        """Generate SCIP index for the Go project."""
        # Check if go.mod exists
        go_mod = self.project_root / "go.mod"
        if not go_mod.exists():
            logger.error(
                f"go.mod not found at {go_mod}. "
                "This doesn't appear to be a Go project."
            )
            return False

        if not self._check_indexer_available():
            return False

        cmd = self._build_index_command(**kwargs)
        self.index_file.unlink(missing_ok=True)
        nested = nested_go_modules(self.project_root)

        logger.debug(f"Running command: {' '.join(cmd)}")

        with self.profiler.section("generate_index") as section:
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                )
                success = True
            except subprocess.CalledProcessError as e:
                logger.error(f"Error generating SCIP index: {e}")
                if e.stdout:
                    logger.error(f"stdout: {e.stdout}")
                if e.stderr:
                    logger.error(f"stderr: {e.stderr}")
                success = False
            except BaseException:
                self.index_file.unlink(missing_ok=True)
                raise

            # Nested modules count toward the same generation time.
            indexed = self.index_file.is_file() and not self.index_file.is_symlink()
            if success and indexed and nested:
                try:
                    success = self._merge_nested_modules(go_mod, nested)
                except BaseException:
                    self.index_file.unlink(missing_ok=True)
                    raise

        duration = section.duration

        if success and not self.index_file.is_symlink() and self.index_file.is_file():
            logger.info(f"Successfully generated SCIP index at {self.index_file}")
            logger.info(f"Index generation took: {duration:.2f} seconds")
            return True
        self.index_file.unlink(missing_ok=True)
        logger.error(f"Index generation failed after {duration:.2f} seconds")
        return False

    def _merge_nested_modules(
        self, go_mod: Path, nested: List[Tuple[str, str]]
    ) -> bool:
        """Index each nested module and merge it into ``self.index_file``.

        A module that fails to index is skipped and logged; the root index
        and the other modules are kept.
        """
        from .scip_pb2 import Index

        root_module = go_module_path(go_mod)
        if not root_module:
            logger.warning(f"No module path in {go_mod}; nested modules skipped")
            return True
        root = Index()
        root.ParseFromString(self.index_file.read_bytes())
        # A root go.work can already pull nested modules into the root run.
        # Each document belongs to the deepest module directory holding it.
        owners = set()
        for document in root.documents:
            holders = [
                directory
                for _module, directory in nested
                if document.relative_path.startswith(directory + "/")
            ]
            if holders:
                owners.add(max(holders, key=len))
        covered = [entry for entry in nested if entry[1] in owners]
        pending = [entry for entry in nested if entry not in covered]
        indexes = []
        with tempfile.TemporaryDirectory(dir=self.output_dir) as scratch:
            for number, (module, directory) in enumerate(pending):
                output = Path(scratch) / f"module-{number}.scip"
                try:
                    subprocess.run(
                        ["scip-go", "--output", str(output)],
                        check=True,
                        cwd=self.project_root / directory,
                        env=_without_workspace(),
                        capture_output=True,
                        text=True,
                    )
                    index = Index()
                    index.ParseFromString(output.read_bytes())
                except (subprocess.CalledProcessError, OSError) as error:
                    logger.warning(
                        f"Skipping Go module {module} at {directory}: {error}"
                    )
                    continue
                indexes.append((module, directory, index))
        directories = {""} | {directory for _module, directory in nested}
        aliases = {
            source: [
                (alias, target)
                for alias, target in local_replacements(
                    self.project_root / source / "go.mod", self.project_root
                )
                if target in directories
            ]
            for source in directories
        }
        merge_module_indexes(root, indexes, root_module, covered, aliases)
        staging = self.index_file.with_name(self.index_file.name + ".merging")
        staging.write_bytes(root.SerializeToString())
        staging.replace(self.index_file)
        logger.info(
            f"Merged {len(indexes)} of {len(pending)} nested Go modules into the "
            f"index; the root run covered {len(covered)}"
        )
        return True

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
        **kwargs,
    ):
        """Run Go pipeline while ignoring other-language kwargs."""
        kwargs.pop("project_name", None)
        kwargs.pop("target_dir", None)
        kwargs.pop("cwd", None)
        kwargs.pop("config_path", None)
        kwargs.pop("exclude_vendored_libraries", None)
        kwargs.pop("infer_tsconfig", None)
        kwargs.pop("yarn_workspaces", None)
        kwargs.pop("pnpm_workspaces", None)
        kwargs.pop("npm_workspaces", None)
        kwargs.pop("compdb_path", None)
        kwargs.pop("show_compiler_diagnostics", None)

        return super().run_pipeline(
            output_file=output_file,
            skip_level=skip_level,
            reset_profiler=reset_profiler,
            report_profile=report_profile,
            **kwargs,
        )
