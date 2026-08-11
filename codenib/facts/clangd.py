# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Strict clangd compatibility identity and per-file ``FactBatch`` adapter.

The adapter reads the same normalized records used by ``ClangdGraphDecoder``
but leaves cross-file target SymbolIDs unresolved.  The legacy graph builder is
not changed or bypassed; callers may dual-write facts beside that graph until
the public query parity program is complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from ..ls_index.clangd_contract import normalize_clangd_position_encoding
from ..storage.models import canonical_json
from ..types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)
from .model import (
    CAPABILITY_DEFINITIONS,
    CAPABILITY_DIAGNOSTICS,
    CAPABILITY_EDGES,
    CAPABILITY_OCCURRENCES,
    COMPLETENESS_PARTIAL,
    FACT_BATCH_SCHEMA_VERSION,
    PROVENANCE_CLANGD,
    DiagnosticFact,
    EdgeFact,
    FactBatch,
    OccurrenceFact,
    SourceRange,
    SymbolFact,
    sha256_digest,
)

CLANGD_FACT_ADAPTER_SCHEMA = "codenib.clangd-fact-adapter.v1"
CLANGD_FACT_PROFILE_CONTRACT = "codenib.clangd-fact-profile.v1"
CLANGD_FACT_PROVIDER = "clangd"


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    if value != value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be canonical text")
    return value


def _digest(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if (
        not text.startswith("sha256:")
        or len(text) != 71
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise ValueError(f"{field} must use canonical sha256:<64 lowercase hex>")
    return text


@dataclass(frozen=True, slots=True)
class ClangdFactProfile:
    """Fail-closed compatibility axes for reusable clangd file facts."""

    analyzer_version: str
    target_triple: str
    toolchain_digest: str
    compile_commands_digest: str
    build_context_digest: str
    position_encoding: str
    native_format: str
    normalization_profile: str
    native_contract_digest: str
    supported_riff_versions: tuple[int, ...]

    def __post_init__(self) -> None:
        for field in (
            "analyzer_version",
            "target_triple",
            "native_format",
            "normalization_profile",
        ):
            _required_text(getattr(self, field), field)
        for field in (
            "toolchain_digest",
            "compile_commands_digest",
            "build_context_digest",
            "native_contract_digest",
        ):
            object.__setattr__(self, field, _digest(getattr(self, field), field))
        object.__setattr__(
            self,
            "position_encoding",
            normalize_clangd_position_encoding(self.position_encoding),
        )
        versions = tuple(self.supported_riff_versions)
        if (
            not versions
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in versions
            )
            or versions != tuple(sorted(set(versions)))
        ):
            raise ValueError("supported_riff_versions must be sorted unique integers")
        object.__setattr__(self, "supported_riff_versions", versions)

    @classmethod
    def from_runtime(
        cls,
        *,
        analyzer_version: str,
        target_triple: str,
        toolchain_digest: str,
        compile_commands_digest: str,
        build_context_digest: str,
        position_encoding: str,
        native_contract: Mapping[str, Any] | None = None,
    ) -> "ClangdFactProfile":
        """Bind caller build context to the installed native RIFF contract."""

        if native_contract is None:
            try:
                import codenib_core
            except ImportError as exc:
                raise RuntimeError(
                    "compiled core is required to build a clangd fact profile"
                ) from exc
            contract_reader = getattr(codenib_core, "clangd_fact_query_contract", None)
            if not callable(contract_reader):
                raise RuntimeError(
                    "compiled core does not expose the clangd fact contract"
                )
            native_contract = contract_reader()
        if not isinstance(native_contract, Mapping):
            raise ValueError("native clangd contract must be an object")
        required = {
            "format",
            "normalization_profile",
            "supported_versions",
            "supported_position_encodings",
        }
        missing = sorted(required - set(native_contract))
        if missing:
            raise ValueError(f"native clangd contract is incomplete: {missing}")
        normalized_encoding = normalize_clangd_position_encoding(position_encoding)
        encodings = tuple(native_contract["supported_position_encodings"])
        if normalized_encoding not in encodings:
            raise ValueError(
                f"position encoding {normalized_encoding!r} is not supported by "
                "the native clangd contract"
            )
        contract_json = canonical_json(dict(native_contract))
        return cls(
            analyzer_version=analyzer_version,
            target_triple=target_triple,
            toolchain_digest=toolchain_digest,
            compile_commands_digest=compile_commands_digest,
            build_context_digest=build_context_digest,
            position_encoding=normalized_encoding,
            native_format=native_contract["format"],
            normalization_profile=native_contract["normalization_profile"],
            native_contract_digest=sha256_digest(contract_json),
            supported_riff_versions=tuple(native_contract["supported_versions"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": CLANGD_FACT_PROFILE_CONTRACT,
            "adapter_schema": CLANGD_FACT_ADAPTER_SCHEMA,
            "fact_batch_schema_version": FACT_BATCH_SCHEMA_VERSION,
            "provider": CLANGD_FACT_PROVIDER,
            "analyzer_version": self.analyzer_version,
            "target_triple": self.target_triple,
            "toolchain_digest": self.toolchain_digest,
            "compile_commands_digest": self.compile_commands_digest,
            "build_context_digest": self.build_context_digest,
            "position_encoding": self.position_encoding,
            "native_format": self.native_format,
            "normalization_profile": self.normalization_profile,
            "native_contract_digest": self.native_contract_digest,
            "supported_riff_versions": list(self.supported_riff_versions),
        }

    @property
    def digest(self) -> str:
        return sha256_digest(canonical_json(self.to_dict()))

    def catalog_config(self) -> dict[str, Any]:
        return {
            "compatibility": self.to_dict(),
            "fact_profile_digest": self.digest,
        }


def _canonical_path(value: Any) -> str:
    text = _required_text(str(value).replace("\\", "/"), "file path")
    if text.startswith("./"):
        text = text[2:]
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("file path must be a canonical repository-relative path")
    return text


def _source_range(location: Mapping[str, Any]) -> SourceRange:
    start = location.get("start")
    end = location.get("end")
    if (
        not isinstance(start, (tuple, list))
        or len(start) != 2
        or not isinstance(end, (tuple, list))
        or len(end) != 2
    ):
        raise ValueError("clangd location must contain start and end positions")
    return SourceRange(start[0], start[1], end[0], end[1])


def _relative_location(decoder: Any, location: Mapping[str, Any] | None):
    if not isinstance(location, Mapping) or not location.get("file"):
        return None
    raw = decoder._file_uri_to_relative(location["file"])
    try:
        path = _canonical_path(raw)
    except ValueError:
        return None
    if "third_party/" in path:
        return None
    return path, _source_range(location)


def _definition_location(decoder: Any, symbol_id: str):
    from ..ls_index.clangd_decode import REF_KIND_DEFINITION, REF_KIND_SPELLED

    references = decoder._refs.get(symbol_id, ())
    spelled = [
        ref
        for ref in references
        if ref.get("kind", 0) & REF_KIND_DEFINITION
        and ref.get("kind", 0) & REF_KIND_SPELLED
    ]
    expanded = [
        ref
        for ref in references
        if ref.get("kind", 0) & REF_KIND_DEFINITION
        and not ref.get("kind", 0) & REF_KIND_SPELLED
    ]
    if spelled:
        return spelled[0].get("location")
    if expanded:
        return expanded[0].get("location")
    symbol = decoder._symbols.get(symbol_id) or {}
    return symbol.get("definition")


def _content_digest(project_root: Path, file_path: str) -> str:
    source = project_root / PurePosixPath(file_path)
    try:
        resolved = source.resolve(strict=True)
        resolved.relative_to(project_root)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"source file is unavailable for {file_path!r}") from exc
    if not resolved.is_file():
        raise ValueError(f"source path is not a regular file: {file_path!r}")
    return sha256_digest(resolved.read_bytes())


def fact_batches_from_clangd_records(
    decoder: Any,
    *,
    profile: ClangdFactProfile,
    content_digests: Mapping[str, str] | None = None,
    file_paths: Sequence[str] | None = None,
    diagnostics: Mapping[str, Sequence[DiagnosticFact]] | None = None,
    require_native_validation: bool = True,
) -> tuple[FactBatch, ...]:
    """Project collected clangd records into canonical per-file batches.

    Supplying ``content_digests`` or ``file_paths`` is recommended because it
    retains empty source files.  Without either, the adapter discovers files
    from clangd definitions and occurrences and hashes their current bytes.
    """

    if not isinstance(profile, ClangdFactProfile):
        raise TypeError("profile must be a ClangdFactProfile")
    native_validator = getattr(decoder, "_decode_native_query_index", None)
    if require_native_validation:
        if not callable(native_validator):
            raise RuntimeError(
                "bounded native clangd validation is required for durable facts"
            )
        query_index = getattr(decoder, "query_index", None)
        if query_index is None:
            decoded = native_validator()
            if isinstance(decoded, tuple):
                if len(decoded) != 2 or not isinstance(decoded[1], Mapping):
                    raise RuntimeError(
                        "native clangd validation returned an invalid index/profile pair"
                    )
                query_index, query_profile = decoded
                decoder.query_profile = dict(query_profile)
            else:
                query_index = decoded
            decoder.query_index = query_index
            decoder.query_backend = "native-clangd-fact-query-v1"
            decoder.query_profile["fact_query_native"] = 1
        if not getattr(query_index, "fact_query_index", False):
            raise RuntimeError(
                "durable clangd facts require a validated native query index"
            )
        bound_snapshot_id = getattr(decoder, "query_snapshot_id", None)
        index_snapshot_id = getattr(query_index, "snapshot_id", None)
        if (
            not bound_snapshot_id
            or not index_snapshot_id
            or index_snapshot_id != bound_snapshot_id
        ):
            raise RuntimeError(
                "native clangd query index is not bound to the decoder snapshot"
            )
    snapshot_reader = getattr(decoder, "_current_native_snapshot", None)
    snapshot_before = snapshot_reader() if callable(snapshot_reader) else None
    decoder.collect_records(strict=True)
    snapshot_after = snapshot_reader() if callable(snapshot_reader) else None
    if snapshot_before is not None or snapshot_after is not None:
        before_id = (
            snapshot_before.get("snapshot_id")
            if isinstance(snapshot_before, Mapping)
            else None
        )
        after_id = (
            snapshot_after.get("snapshot_id")
            if isinstance(snapshot_after, Mapping)
            else None
        )
        if not before_id or before_id != after_id:
            raise RuntimeError("clangd index snapshot changed while facts were built")
    decoder_encoding = normalize_clangd_position_encoding(
        str(getattr(decoder, "position_encoding", ""))
    )
    if decoder_encoding != profile.position_encoding:
        raise ValueError("clangd decoder and FactBatch profile encodings differ")

    from ..ls_index.clangd_decode import (
        KIND_MACRO,
        REF_KIND_DECLARATION,
        REF_KIND_DEFINITION,
        REF_KIND_REFERENCE,
        ZERO_SYMBOL_ID,
    )

    definition_locations: dict[str, tuple[str, SourceRange]] = {}
    for symbol_id in decoder._symbols:
        located = _relative_location(decoder, _definition_location(decoder, symbol_id))
        if located is not None:
            definition_locations[symbol_id] = located

    observed_paths = {path for path, _ in definition_locations.values()}
    for references in decoder._refs.values():
        for reference in references:
            located = _relative_location(decoder, reference.get("location"))
            if located is not None:
                observed_paths.add(located[0])

    normalized_digests: dict[str, str] = {}
    if content_digests is not None:
        for raw_path, digest in content_digests.items():
            path = _canonical_path(raw_path)
            if path in normalized_digests:
                raise ValueError(f"duplicate clangd FactBatch path {path!r}")
            normalized_digests[path] = _digest(digest, "content digest")
    if file_paths is not None:
        normalized_paths = tuple(_canonical_path(path) for path in file_paths)
        if len(normalized_paths) != len(set(normalized_paths)):
            raise ValueError("duplicate clangd FactBatch file paths")
        selected_paths = set(normalized_paths)
    else:
        selected_paths = set(normalized_digests) or observed_paths
    if normalized_digests and selected_paths - set(normalized_digests):
        missing = sorted(selected_paths - set(normalized_digests))
        raise ValueError(f"content digests are missing for files: {missing}")
    project_root = Path(decoder.project_root).resolve()
    for path in selected_paths:
        observed_digest = _content_digest(project_root, path)
        expected_digest = normalized_digests.setdefault(path, observed_digest)
        if expected_digest != observed_digest:
            raise ValueError(
                f"source content digest differs from current bytes for {path!r}"
            )
    normalized_diagnostics: dict[str, tuple[DiagnosticFact, ...]] = {}
    if diagnostics is not None:
        for raw_path, values in diagnostics.items():
            path = _canonical_path(raw_path)
            if path not in selected_paths:
                raise ValueError(f"clangd diagnostics name an unselected file {path!r}")
            if path in normalized_diagnostics:
                raise ValueError(f"duplicate clangd diagnostics path {path!r}")
            normalized = tuple(values)
            if any(not isinstance(item, DiagnosticFact) for item in normalized):
                raise TypeError("clangd diagnostics must contain DiagnosticFact values")
            normalized_diagnostics[path] = normalized

    symbols_by_file: dict[str, list[SymbolFact]] = {path: [] for path in selected_paths}
    occurrences_by_file: dict[str, dict[tuple[str, SourceRange], int]] = {
        path: {} for path in selected_paths
    }
    edges_by_file: dict[str, list[EdgeFact]] = {path: [] for path in selected_paths}

    for symbol_id, symbol in decoder._symbols.items():
        located = definition_locations.get(symbol_id)
        if located is None or located[0] not in selected_paths:
            continue
        file_path, definition_range = located
        qualified = f"{symbol.get('scope', '')}{symbol.get('name', '')}"
        if not qualified or symbol.get("kind", 0) == KIND_MACRO:
            continue
        node_type = decoder._kind_to_node_type(symbol.get("kind", 0))
        if node_type in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}:
            qualified += "()"
        enclosing = None
        for reference in decoder._refs.get(symbol_id, ()):
            container = reference.get("container")
            if (
                reference.get("kind", 0) & REF_KIND_DEFINITION
                and container
                and container != ZERO_SYMBOL_ID
                and definition_locations.get(container, (None,))[0] == file_path
            ):
                enclosing = container
                break
        symbols_by_file[file_path].append(
            SymbolFact(
                symbol_id=symbol_id,
                moniker=symbol_id,
                display_name=qualified.replace("::", "."),
                kind=node_type,
                definition_range=definition_range,
                selection_range=definition_range,
                enclosing_symbol_id=enclosing,
            )
        )

    def add_occurrence(
        file_path: str, moniker: str, source_range: SourceRange, roles: int
    ) -> None:
        key = (moniker, source_range)
        occurrences_by_file[file_path][key] = (
            occurrences_by_file[file_path].get(key, 0) | roles
        )

    for symbol_id, symbol in decoder._symbols.items():
        for location, roles in (
            (symbol.get("definition"), REF_KIND_DEFINITION),
            (symbol.get("declaration"), REF_KIND_DECLARATION),
        ):
            located = _relative_location(decoder, location)
            if located is not None and located[0] in selected_paths:
                add_occurrence(located[0], symbol_id, located[1], roles)

    for target_moniker, references in decoder._refs.items():
        target_file = definition_locations.get(target_moniker, (None,))[0]
        for reference in references:
            located = _relative_location(decoder, reference.get("location"))
            if located is None or located[0] not in selected_paths:
                continue
            file_path, anchor = located
            roles = int(reference.get("kind", 0))
            add_occurrence(file_path, target_moniker, anchor, roles)
            container = reference.get("container")
            source_symbol = (
                container
                if container
                and container != ZERO_SYMBOL_ID
                and definition_locations.get(container, (None,))[0] == file_path
                else None
            )
            if roles & REF_KIND_REFERENCE and container and container != ZERO_SYMBOL_ID:
                target_kwargs = (
                    {"target_symbol_id": target_moniker}
                    if target_file == file_path
                    and any(
                        symbol.symbol_id == target_moniker
                        for symbol in symbols_by_file[file_path]
                    )
                    else {"target_moniker": target_moniker}
                )
                edges_by_file[file_path].append(
                    EdgeFact(
                        kind=EDGE_TYPE_REFERENCE,
                        provenance=PROVENANCE_CLANGD,
                        source_symbol_id=source_symbol,
                        anchor_range=anchor,
                        **target_kwargs,
                    )
                )
            if (
                roles & REF_KIND_DEFINITION
                and source_symbol is not None
                and target_file == file_path
            ):
                edges_by_file[file_path].append(
                    EdgeFact(
                        kind=EDGE_TYPE_CONTAIN,
                        provenance=PROVENANCE_CLANGD,
                        source_symbol_id=source_symbol,
                        target_symbol_id=target_moniker,
                        anchor_range=anchor,
                    )
                )

    for relation in decoder._relations:
        target_moniker = relation.get("subject")
        source_symbol = relation.get("object")
        source_file = definition_locations.get(source_symbol, (None,))[0]
        target_file = definition_locations.get(target_moniker, (None,))[0]
        if not source_symbol or not target_moniker or source_file not in selected_paths:
            continue
        target_kwargs = (
            {"target_symbol_id": target_moniker}
            if target_file == source_file
            else {"target_moniker": target_moniker}
        )
        edges_by_file[source_file].append(
            EdgeFact(
                kind=EDGE_TYPE_REFERENCE,
                provenance=PROVENANCE_CLANGD,
                source_symbol_id=source_symbol,
                **target_kwargs,
            )
        )

    batches = []
    for file_path in sorted(selected_paths):
        occurrences = tuple(
            OccurrenceFact(
                symbol_moniker=moniker,
                source_range=source_range,
                roles=roles,
            )
            for (moniker, source_range), roles in occurrences_by_file[file_path].items()
        )
        batches.append(
            FactBatch(
                file_path=file_path,
                language="cpp",
                content_digest=normalized_digests[file_path],
                profile_digest=profile.digest,
                provider=CLANGD_FACT_PROVIDER,
                completeness=COMPLETENESS_PARTIAL,
                position_encoding=profile.position_encoding,
                capabilities=(
                    CAPABILITY_DEFINITIONS,
                    CAPABILITY_OCCURRENCES,
                    CAPABILITY_EDGES,
                    *(
                        (CAPABILITY_DIAGNOSTICS,)
                        if file_path in normalized_diagnostics
                        else ()
                    ),
                ),
                symbols=tuple(symbols_by_file[file_path]),
                occurrences=occurrences,
                edges=tuple(edges_by_file[file_path]),
                diagnostics=normalized_diagnostics.get(file_path, ()),
            )
        )
    for batch in batches:
        if _content_digest(project_root, batch.file_path) != batch.content_digest:
            raise RuntimeError(
                f"source file changed while clangd facts were built: "
                f"{batch.file_path}"
            )
    return tuple(batches)


__all__ = [
    "CLANGD_FACT_ADAPTER_SCHEMA",
    "CLANGD_FACT_PROFILE_CONTRACT",
    "CLANGD_FACT_PROVIDER",
    "ClangdFactProfile",
    "fact_batches_from_clangd_records",
]
