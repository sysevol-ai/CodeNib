# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Vector Store implementation using FAISS and sentence-transformers for code embeddings.
This module provides functionality to create, store, and query vector embeddings
of code chunks for semantic similarity search.
"""

import hashlib
import inspect
import json
import os
import pickle
import sys
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Literal, Optional, Set, Tuple

import faiss
import numpy as np

from ... import compat_pickle
from ..._atomic_directory import PublicationDirectoryReader, _annotate_secondary_error
from ..._bounded_json import canonical_json_array_chunks, iter_bounded_json_array
from ..._captured_directory import AuthenticatedSnapshotReader
from ...log_utils import get_logger
from ...native_index_authorization import (
    NativeIndexAuthorization,
    require_native_index_authorization,
    require_native_index_authorization_preflight,
)
from ...profiler import Profiler
from ...provider_routes import normalize_provider
from ...types import NodeInfo
from .artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    VECTOR_ROW_MAPPING_CONTRACT,
    VECTOR_VIEW_UPDATE_MARKER,
    AuthenticatedVectorView,
    _attach_vector_view_cleanup_owner,
    capture_authenticated_vector_view,
    validate_schema_8_vector_document_row,
    vector_level_artifact_records,
)
from .model_policy import (
    EmbeddingLoadPolicy,
    resolve_embedding_load_policy_from_options,
)
from .text_policy import (
    REMOTE_EMBEDDING_DOCUMENT_MAX_CHARS,
    bounded_remote_embedding_document,
)

logger = get_logger(__name__)

Level = Literal["l0", "l2"]

_MAX_PORTABLE_DOCUMENTS_JSON_BYTES = 256 * 1024 * 1024
_MAX_VECTOR_CONFIG_BYTES = 16 * 1024 * 1024
_MAX_FAISS_INDEX_BYTES = 8 * 1024 * 1024 * 1024

_UNSET = object()
_MODEL_IDENTITY_KEYS = frozenset({"code_revision", "revision", "trust_remote_code"})
_HUGGINGFACE_ONLY_OPTIONS = frozenset(
    {
        "config_kwargs",
        "default_batch_size",
        "encode_kwargs",
        "max_seq_length",
        "model_kwargs",
        "revision",
        "tokenizer_kwargs",
        "trust_remote_code",
    }
)


@contextmanager
def _authenticated_binary_file(
    view: AuthenticatedVectorView,
    relative: str,
):
    """Yield a file object backed only by an immutable authenticated snapshot."""

    with view.authenticated_snapshot(relative) as (snapshot, _record):
        yield AuthenticatedSnapshotReader(snapshot)


def _read_authenticated_faiss(
    view: AuthenticatedVectorView,
    relative: str,
) -> Any:
    """Give FAISS only a fixed-chunk reader over an authenticated snapshot."""

    with view.authenticated_snapshot(relative) as (snapshot, _record):
        source = AuthenticatedSnapshotReader(snapshot)
        return faiss.read_index(faiss.PyCallbackIOReader(source.read))


def _pop_compatible_model_option(
    kwargs: Dict[str, Any],
    model_kwargs: Dict[str, Any],
    name: str,
) -> Any:
    """Resolve an option accepted in either legacy wrapper location."""

    direct = kwargs.pop(name, _UNSET)
    nested = model_kwargs.pop(name, _UNSET)
    if direct is not _UNSET and nested is not _UNSET and direct != nested:
        raise ValueError(f"conflicting {name} values in embedding model options")
    if direct is not _UNSET:
        return direct
    if nested is not _UNSET:
        return nested
    return None


def _reject_nested_identity_options(value: Any, *, path: str) -> None:
    """Prevent nested kwargs from overriding the validated model identity."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            location = f"{path}.{key}" if path else str(key)
            if key in _MODEL_IDENTITY_KEYS:
                raise ValueError(
                    "embedding model identity options must be declared at the "
                    f"top level, not {location}"
                )
            _reject_nested_identity_options(item, path=location)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nested_identity_options(item, path=f"{path}[{index}]")


def _validate_provider_options(provider: str, options: Mapping[str, Any]) -> str:
    """Reject local-model controls before they reach a remote SDK client."""

    canonical = normalize_provider(provider)
    if canonical == "huggingface":
        return canonical
    unsupported = sorted(_HUGGINGFACE_ONLY_OPTIONS.intersection(options))
    if unsupported:
        raise ValueError(
            "Hugging Face embedding options require provider='huggingface': "
            + ", ".join(unsupported)
        )
    return canonical


def _atomic_replace(target: Path, writer: Callable[[Path], None]) -> None:
    """Write a sibling temporary file and atomically publish it."""

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        writer(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json_dump(target: Path, value: object) -> None:
    def _write(path: Path) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")

    _atomic_replace(target, _write)


def _atomic_canonical_document_dump(
    target: Path,
    documents: List["_Document"],
) -> None:
    """Atomically stream one bounded canonical document array."""

    def values():
        for document in documents:
            yield {
                "page_content": document.page_content,
                "metadata": dict(document.metadata),
            }

    def _write(path: Path) -> None:
        size = 0
        with path.open("wb") as handle:
            for chunk in canonical_json_array_chunks(values()):
                size += len(chunk)
                if size > _MAX_PORTABLE_DOCUMENTS_JSON_BYTES:
                    raise ValueError(
                        "canonical vector documents exceed their "
                        f"{_MAX_PORTABLE_DOCUMENTS_JSON_BYTES}-byte limit"
                    )
                handle.write(chunk)

    _atomic_replace(target, _write)


def _atomic_pickle_dump(target: Path, value: object) -> None:
    def _write(path: Path) -> None:
        with path.open("wb") as handle:
            pickle.dump(value, handle)

    _atomic_replace(target, _write)


def _sentence_transformer_load_kwargs(
    sentence_transformer: object,
    load_policy: EmbeddingLoadPolicy,
) -> Dict[str, Any]:
    """Return only model-identity options supported by the installed API."""

    try:
        parameters = inspect.signature(sentence_transformer).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_extra_kwargs = not parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )

    def accepts(name: str) -> bool:
        return accepts_extra_kwargs or name in parameters

    kwargs: Dict[str, Any] = {}
    trust_remote_code = load_policy.trust_remote_code
    revision = load_policy.revision
    if accepts("trust_remote_code"):
        kwargs["trust_remote_code"] = trust_remote_code
    elif trust_remote_code:
        raise RuntimeError(
            "The installed sentence-transformers cannot enforce trusted remote "
            "model loading; upgrade sentence-transformers"
        )
    if revision is not None:
        if not accepts("revision"):
            raise RuntimeError(
                "The installed sentence-transformers cannot honor the requested "
                "embedding revision; upgrade sentence-transformers"
            )
        kwargs["revision"] = revision
    return kwargs


class _Document:
    """Lightweight document container replacing LangChain Document.

    Provides the same ``page_content`` / ``metadata`` interface so that
    callers accessing ``store.l0_documents`` or ``store.l2_documents``
    continue to work without changes.
    """

    __slots__ = ("page_content", "metadata")

    def __init__(self, page_content: str = "", metadata: Optional[Dict] = None):
        self.page_content = page_content
        self.metadata = metadata if metadata is not None else {}

    def __repr__(self) -> str:
        name = self.metadata.get("name", "")
        return f"_Document(name={name!r}, len={len(self.page_content)})"


@dataclass(slots=True)
class _LoadedVectorState:
    """Temporary replacement state committed only after final authentication."""

    l0_index: Any
    l0_documents: List[_Document]
    l2_index: Any
    l2_documents: List[_Document]
    artifact_metadata: Dict[str, Any]
    store_path: Path | None


class _VectorIndexCleanupOwner:
    """Retryable aggregate owner for unpublished or superseded FAISS indices."""

    __slots__ = ("indices",)

    def __init__(self, indices: tuple[Any, ...]) -> None:
        seen: set[int] = set()
        self.indices: list[Any] = []
        for index in indices:
            if index is None or id(index) in seen:
                continue
            seen.add(id(index))
            self.indices.append(index)

    @property
    def closed(self) -> bool:
        return not self.indices

    def close(self) -> None:
        pending: list[Any] = []
        failure: BaseException | None = None
        for index in self.indices:
            reset = getattr(index, "reset", None)
            if not callable(reset):
                continue
            try:
                reset()
            except BaseException as exc:  # noqa: B036 - visit every index
                pending.append(index)
                if failure is None:
                    failure = exc
                else:
                    _annotate_secondary_error(
                        failure,
                        "additional FAISS index cleanup also failed",
                        exc,
                    )
        self.indices = pending
        if failure is not None:
            try:
                failure.vector_index_cleanup_owner = self  # type: ignore[attr-defined]
            except BaseException:  # noqa: B036 - traceback still retains owner
                pass
            raise failure


def _attach_vector_index_cleanup_owner(
    primary: BaseException,
    cleanup_error: BaseException,
) -> None:
    owner = getattr(cleanup_error, "vector_index_cleanup_owner", None)
    if owner is None:
        return
    try:
        primary.vector_index_cleanup_owner = owner  # type: ignore[attr-defined]
    except BaseException:  # noqa: B036 - traceback still retains cleanup error
        pass


def _validate_faiss_row_ids(
    index: faiss.Index,
    *,
    relative: str,
    document_count: int,
) -> None:
    """Require FAISS labels to be the canonical document-array positions."""

    if isinstance(index, faiss.IndexFlat):
        # A bare IndexFlat has no user-supplied label table. Its labels are
        # necessarily the insertion positions 0..ntotal-1.
        return
    if not isinstance(index, faiss.IndexIVFFlat):
        raise ValueError(f"FAISS index type cannot bind row IDs at {relative}")

    nlist = int(index.nlist)
    if nlist <= 0 or (document_count > 0 and nlist > document_count):
        raise ValueError(f"FAISS IVF list count is invalid at {relative}")
    if document_count == 0:
        return
    seen = bytearray(document_count)
    observed = 0
    for list_number in range(nlist):
        list_size = int(index.invlists.list_size(list_number))
        if list_size < 0 or list_size > document_count - observed:
            raise ValueError(f"FAISS IVF row count is invalid at {relative}")
        if list_size == 0:
            continue
        ids = None
        try:
            ids = index.invlists.get_ids(list_number)
            for raw_row_id in faiss.rev_swig_ptr(ids, list_size):
                row_id = int(raw_row_id)
                if row_id < 0 or row_id >= document_count or seen[row_id]:
                    raise ValueError(f"FAISS row IDs are not canonical at {relative}")
                seen[row_id] = 1
                observed += 1
        finally:
            if ids is not None:
                index.invlists.release_ids(list_number, ids)
    if observed != document_count or any(value == 0 for value in seen):
        raise ValueError(f"FAISS row IDs are not canonical at {relative}")


def _validate_faiss_index_contract(
    index: faiss.Index,
    *,
    relative: str,
    document_count: int,
    expected_dimension: int,
    expected_metric: str,
    expected_index_type: str,
) -> None:
    if int(index.d) != expected_dimension:
        raise ValueError(
            f"FAISS dimension mismatch at {relative}: expected "
            f"{expected_dimension}, found {int(index.d)}"
        )
    if int(index.ntotal) != document_count:
        raise ValueError(
            f"FAISS document count mismatch at {relative}: expected "
            f"{document_count}, found {int(index.ntotal)}"
        )
    expected_metric_type = {
        "ip": faiss.METRIC_INNER_PRODUCT,
        "l2": faiss.METRIC_L2,
    }.get(expected_metric)
    if expected_metric_type is None:
        raise ValueError(f"Unsupported index_metric: {expected_metric!r}")
    if int(index.metric_type) != int(expected_metric_type):
        raise ValueError(
            f"FAISS metric mismatch at {relative}: expected "
            f"{expected_metric}, found {int(index.metric_type)}"
        )
    if expected_index_type == "flat":
        valid_type = isinstance(index, faiss.IndexFlat)
    elif expected_index_type == "ivf":
        valid_type = isinstance(index, faiss.IndexIVFFlat)
    else:
        raise ValueError(f"Unsupported index_type: {expected_index_type!r}")
    if not valid_type:
        raise ValueError(
            f"FAISS index type mismatch at {relative}: expected {expected_index_type}"
        )
    if document_count > 0 and not bool(index.is_trained):
        raise ValueError(f"FAISS index is not trained at {relative}")
    _validate_faiss_row_ids(
        index,
        relative=relative,
        document_count=document_count,
    )


def _decode_generation_config(payload: bytes, *, relative: str) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        decoded: dict[str, Any] = {}
        for key, value in pairs:
            if key in decoded:
                raise ValueError(f"duplicate JSON object key: {key}")
            decoded[key] = value
        return decoded

    def reject_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        decoded = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_nonfinite,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid vector generation config: {relative}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"vector generation config must be an object: {relative}")
    return decoded


def _require_committed_publication_record(
    reader: PublicationDirectoryReader,
    relative: str,
    expected: object,
) -> None:
    if not isinstance(expected, Mapping) or set(expected) != {
        "file",
        "size",
        "sha256",
    }:
        raise ValueError(f"invalid committed vector artifact record: {relative}")
    record = next(
        (
            candidate
            for candidate in reader.file_records()
            if candidate.path == relative
        ),
        None,
    )
    if (
        record is None
        or expected.get("file") != PurePosixPath(relative).name
        or (
            expected.get("size") != record.size
            or expected.get("sha256") != record.sha256
        )
    ):
        raise ValueError(f"committed vector artifact does not match: {relative}")


def _validate_schema_8_native_generation_reader(
    reader: PublicationDirectoryReader,
    *,
    model_suffix: str,
    artifact_identity: Mapping[str, Any],
    expected_dimension: int,
    expected_metric: str,
    expected_index_type: str,
    expected_counts: Mapping[str, int],
) -> None:
    """Validate the persisted schema-8 receipt inside its publication sandwich."""

    if type(reader) is not PublicationDirectoryReader:
        raise TypeError("vector producer validation requires its publication reader")
    config_relative = f"config_{model_suffix}.json"
    config = _decode_generation_config(
        reader.read_bytes(config_relative, max_bytes=_MAX_VECTOR_CONFIG_BYTES),
        relative=config_relative,
    )
    expected_root_fields = {
        "embedding_model",
        "embedding_provider",
        "embedding_revision",
        "dimension",
        "index_type",
        "index_metric",
        "l0_documents",
        "l2_documents",
        "persistence_schema",
        "row_mapping",
        "level_artifacts",
        "artifact",
    }
    if set(config) != expected_root_fields:
        raise ValueError("schema-8 vector generation root config shape is invalid")
    persisted_identity = config.get("artifact")
    if (
        not isinstance(persisted_identity, dict)
        or type(persisted_identity.get("builder_schema")) is not int
        or persisted_identity.get("builder_schema") != 8
        or persisted_identity != dict(artifact_identity)
    ):
        raise ValueError("schema-8 vector generation artifact identity is invalid")
    if config.get("row_mapping") != VECTOR_ROW_MAPPING_CONTRACT:
        raise ValueError("schema-8 vector generation has an invalid row mapping")
    if config.get("persistence_schema") != VECTOR_PERSISTENCE_SCHEMA:
        raise ValueError("schema-8 vector generation persistence schema is invalid")
    if config.get("dimension") != expected_dimension:
        raise ValueError("schema-8 vector generation dimension is invalid")
    if config.get("index_metric") != expected_metric:
        raise ValueError("schema-8 vector generation metric is invalid")
    if config.get("index_type") != expected_index_type:
        raise ValueError("schema-8 vector generation index type is invalid")
    from ...artifacts.portable_views import (
        validate_portable_vector_persistence_semantics,
    )

    validate_portable_vector_persistence_semantics(config, artifact_identity)
    if set(expected_counts) != {"l0", "l2"}:
        raise ValueError("schema-8 vector producer counts are invalid")
    if any(
        type(expected_counts[level]) is not int or expected_counts[level] < 0
        for level in ("l0", "l2")
    ):
        raise ValueError("schema-8 vector producer counts are invalid")
    committed_levels = config.get("level_artifacts")
    if not isinstance(committed_levels, dict):
        raise ValueError("schema-8 vector generation committed levels are invalid")
    expected_nonempty = {level for level in ("l0", "l2") if expected_counts[level] > 0}
    if set(committed_levels) != expected_nonempty:
        raise ValueError("schema-8 vector generation committed levels are invalid")

    for level in ("l0", "l2"):
        count = expected_counts[level]
        if (
            type(count) is not int
            or count < 0
            or config.get(f"{level}_documents") != count
        ):
            raise ValueError(f"schema-8 vector generation {level} count is invalid")
        if count == 0:
            continue
        artifacts = committed_levels[level]
        if not isinstance(artifacts, dict) or set(artifacts) != {
            "index",
            "documents",
        }:
            raise ValueError(
                f"schema-8 vector generation {level} artifacts are invalid"
            )
        documents_relative = f"{level}/documents_{model_suffix}.json"
        index_relative = f"{level}/index_{model_suffix}.faiss"
        _require_committed_publication_record(
            reader,
            documents_relative,
            artifacts["documents"],
        )
        _require_committed_publication_record(
            reader,
            index_relative,
            artifacts["index"],
        )

        document_size = 0
        document_digest = hashlib.sha256()
        document_count = 0
        with reader.open_authenticated_file(
            documents_relative,
            max_bytes=_MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        ) as source:
            documents = iter_bounded_json_array(
                source,
                label=f"schema-8 vector documents {documents_relative}",
                max_element_bytes=_MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
            )

            def counted_documents(
                documents=documents,
                level=level,
            ):
                nonlocal document_count
                for document in documents:
                    validate_schema_8_vector_document_row(
                        document,
                        row_index=document_count,
                        level=level,
                    )
                    document_count += 1
                    yield document

            for chunk in canonical_json_array_chunks(counted_documents()):
                document_size += len(chunk)
                if document_size > _MAX_PORTABLE_DOCUMENTS_JSON_BYTES:
                    raise ValueError(
                        "schema-8 vector documents exceed their byte limit: "
                        f"{documents_relative}"
                    )
                document_digest.update(chunk)
        documents_record = artifacts["documents"]
        if (
            document_count != count
            or documents_record.get("size") != document_size
            or documents_record.get("sha256") != document_digest.hexdigest()
        ):
            raise ValueError(
                f"schema-8 vector {level} documents do not bind their rows"
            )

        level_config_relative = f"{level}/config_{model_suffix}.json"
        level_config = _decode_generation_config(
            reader.read_bytes(
                level_config_relative,
                max_bytes=_MAX_VECTOR_CONFIG_BYTES,
            ),
            relative=level_config_relative,
        )
        expected_level_config = {
            "embedding_model": config["embedding_model"],
            "embedding_provider": config["embedding_provider"],
            "embedding_revision": config["embedding_revision"],
            "dimension": expected_dimension,
            "index_type": expected_index_type,
            "index_metric": expected_metric,
            "level": level,
            "num_documents": count,
        }
        if level_config != expected_level_config:
            raise ValueError(
                f"schema-8 vector generation {level} config differs from root"
            )

        index = None
        try:
            with reader.open_authenticated_file(
                index_relative,
                max_bytes=_MAX_FAISS_INDEX_BYTES,
            ) as source:
                index = faiss.read_index(faiss.PyCallbackIOReader(source.read))
            _validate_faiss_index_contract(
                index,
                relative=index_relative,
                document_count=count,
                expected_dimension=expected_dimension,
                expected_metric=expected_metric,
                expected_index_type=expected_index_type,
            )
        except BaseException as primary_error:
            if index is not None:
                try:
                    _VectorIndexCleanupOwner((index,)).close()
                except BaseException as cleanup_error:  # noqa: B036
                    _attach_vector_index_cleanup_owner(primary_error, cleanup_error)
                    _annotate_secondary_error(
                        primary_error,
                        "producer FAISS validation cleanup also failed",
                        cleanup_error,
                    )
            raise
        else:
            _VectorIndexCleanupOwner((index,)).close()


class _HuggingFaceEmbeddingWrapper:
    """Wraps ``SentenceTransformer`` to expose ``embed_query`` / ``embed_documents``.

    The interface is intentionally compatible with the LangChain ``Embeddings``
    protocol so that external callers accessing ``store.embedding.embed_query``
    or ``store.embedding.embed_documents`` keep working.
    """

    def __init__(self, model_name: str, max_seq_length: Optional[int] = None, **kwargs):
        from sentence_transformers import SentenceTransformer

        from .prompt_registry import resolve_prompts

        model_kwargs = dict(kwargs.pop("model_kwargs", {}) or {})
        self._encode_kwargs = kwargs.pop("encode_kwargs", {})
        self._default_batch_size: Optional[int] = kwargs.pop("default_batch_size", None)

        revision = _pop_compatible_model_option(kwargs, model_kwargs, "revision")
        trust_remote_code = _pop_compatible_model_option(
            kwargs, model_kwargs, "trust_remote_code"
        )
        load_policy = resolve_embedding_load_policy_from_options(
            model_name,
            {
                "revision": revision,
                "trust_remote_code": trust_remote_code,
            },
        )
        _reject_nested_identity_options(model_kwargs, path="model_kwargs")
        _reject_nested_identity_options(kwargs, path="")

        # Pop prompt-related kwargs so they aren't forwarded to
        # SentenceTransformer's __init__. Anything left as None falls back to
        # the per-model registry; anything set explicitly (including "")
        # overrides the registry.
        explicit = {
            "query_prompt_name": kwargs.pop("query_prompt_name", None),
            "query_prompt": kwargs.pop("query_prompt", None),
            "document_prompt_name": kwargs.pop("document_prompt_name", None),
            "document_prompt": kwargs.pop("document_prompt", None),
        }
        defaults = resolve_prompts(model_name)
        merged = {
            k: (v if v is not None else defaults.get(k)) for k, v in explicit.items()
        }
        self._query_prompt_name = merged["query_prompt_name"]
        self._query_prompt = merged["query_prompt"]
        self._document_prompt_name = merged["document_prompt_name"]
        self._document_prompt = merged["document_prompt"]

        # Build SentenceTransformer init kwargs
        st_kwargs = _sentence_transformer_load_kwargs(
            SentenceTransformer,
            load_policy,
        )
        # Forward remaining kwargs (e.g. device, cache_folder)
        st_kwargs.update(kwargs)
        st_kwargs.update(model_kwargs)

        self._model = SentenceTransformer(model_name, **st_kwargs)

        # Cap the effective sequence length to avoid CUDA OOM.
        self._apply_max_seq_length(model_name, max_seq_length)

        logger.info(
            "Embedding wrapper for %s: query_prompt_name=%r query_prompt=%r "
            "document_prompt_name=%r document_prompt=%r",
            model_name,
            self._query_prompt_name,
            (
                (self._query_prompt[:60] + "…")
                if isinstance(self._query_prompt, str) and len(self._query_prompt) > 60
                else self._query_prompt
            ),
            self._document_prompt_name,
            self._document_prompt,
        )

    # Expose the underlying SentenceTransformer so that callers that
    # previously reached through ``store.embedding._client`` (langchain-
    # huggingface >=0.1) or ``store.embedding.client`` (older) keep working.
    @property
    def _client(self):
        return self._model

    @property
    def client(self):
        return self._model

    def _apply_max_seq_length(
        self, model_name: str, max_seq_length: Optional[int]
    ) -> None:
        """Cap tokenizer input to the model's usable position capacity."""
        if max_seq_length is not None and (
            isinstance(max_seq_length, bool)
            or not isinstance(max_seq_length, int)
            or max_seq_length <= 0
        ):
            raise ValueError("max_seq_length must be a positive integer")

        effective_max = max_seq_length
        try:
            auto_model = self._model[0].auto_model
            max_pos = getattr(
                getattr(auto_model, "config", None),
                "max_position_embeddings",
                None,
            )

            # RoBERTa-derived models number non-padding positions after their
            # padding index. Their embedding table can therefore hold fewer
            # input tokens than ``max_position_embeddings`` suggests. For
            # example, UniXcoder advertises 1026 positions but accepts 1024
            # tokens (including special tokens).
            position_embeddings = getattr(
                getattr(auto_model, "embeddings", None),
                "position_embeddings",
                None,
            )
            table_size = getattr(position_embeddings, "num_embeddings", None)
            capacities = [
                value
                for value in (max_pos, table_size)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0
            ]
            model_capacity = min(capacities) if capacities else None
            padding_idx = getattr(position_embeddings, "padding_idx", None)
            if (
                isinstance(model_capacity, int)
                and isinstance(padding_idx, int)
                and not isinstance(padding_idx, bool)
                and padding_idx >= 0
            ):
                model_capacity -= padding_idx + 1

            if isinstance(model_capacity, int) and model_capacity > 0:
                effective_max = (
                    min(max_seq_length, model_capacity)
                    if max_seq_length is not None
                    else model_capacity
                )
        except Exception as e:
            if max_seq_length is not None:
                logger.warning(
                    "Could not inspect position capacity for model %s: %s. "
                    "Applying requested --max-seq-length %s as a best-effort cap.",
                    model_name,
                    e,
                    max_seq_length,
                )
            else:
                logger.debug("Could not inspect tokenizer position capacity: %s", e)

        if effective_max is None:
            return

        failures = []
        try:
            tok = self._model.tokenizer
            if tok.model_max_length > effective_max:
                tok.model_max_length = effective_max
        except Exception as e:
            failures.append(f"tokenizer: {e}")

        try:
            if self._model.max_seq_length > effective_max:
                logger.info(
                    "Capping max_seq_length from %s to %s for model %s",
                    self._model.max_seq_length,
                    effective_max,
                    model_name,
                )
                self._model.max_seq_length = effective_max
        except Exception as e:
            failures.append(f"sentence transformer: {e}")

        if failures:
            logger.warning(
                "Sequence-length cap %s could not be fully applied to model %s "
                "(%s). CUDA OOM or position-index errors may occur.",
                effective_max,
                model_name,
                "; ".join(failures),
            )

    def _build_encode_kwargs(
        self,
        prompt: Optional[str],
        prompt_name: Optional[str],
    ) -> Dict[str, Any]:
        """Merge per-call prompt args on top of self._encode_kwargs.

        ``prompt`` (raw string) wins over ``prompt_name``; either being a
        non-None value disables the other. Empty string is a valid prompt
        meaning "encode with empty prefix" — same as no-prefix.
        """
        kwargs = dict(self._encode_kwargs)
        if prompt is not None:
            kwargs["prompt"] = prompt
            kwargs.pop("prompt_name", None)
        elif prompt_name is not None:
            kwargs["prompt_name"] = prompt_name
            kwargs.pop("prompt", None)
        return kwargs

    def embed_query(self, text: str) -> List[float]:
        kwargs = self._build_encode_kwargs(self._query_prompt, self._query_prompt_name)
        vec = self._model.encode([text], **kwargs)
        return vec[0].tolist()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        kwargs = self._build_encode_kwargs(
            self._document_prompt, self._document_prompt_name
        )
        # Apply default batch size if configured (e.g., to avoid CUDA OOM)
        if self._default_batch_size is not None:
            kwargs.setdefault("batch_size", self._default_batch_size)
        vecs = self._model.encode(texts, **kwargs)
        return vecs.tolist()


class _OpenAIEmbeddingWrapper:
    """Wraps the OpenAI SDK to expose ``embed_query`` / ``embed_documents``."""

    def __init__(
        self,
        model: str,
        request_options: Optional[Dict] = None,
        batch_size: int = 64,
        max_input_chars: int = REMOTE_EMBEDDING_DOCUMENT_MAX_CHARS,
        **kwargs,
    ):
        from openai import OpenAI

        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise ValueError("embedding batch_size must be an integer")
        if batch_size <= 0:
            raise ValueError("embedding batch_size must be positive")
        self._model = model
        self._request_options = dict(request_options or {})
        self._batch_size = batch_size
        self._max_input_chars = max_input_chars
        # Validate the bound before constructing the first request.
        bounded_remote_embedding_document("", max_chars=max_input_chars)
        self._client = OpenAI(**kwargs)

    def embed_query(self, text: str) -> List[float]:
        resp = self._client.embeddings.create(
            input=[text],
            model=self._model,
            **self._request_options,
        )
        return resp.data[0].embedding

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        texts = [
            bounded_remote_embedding_document(
                text,
                max_chars=self._max_input_chars,
            )
            for text in texts
        ]
        embeddings: List[List[float]] = []
        for start in range(0, len(texts), self._batch_size):
            embeddings.extend(
                self._embed_document_batch(texts[start : start + self._batch_size])
            )
        return embeddings

    def _embed_document_batch(self, texts: List[str]) -> List[List[float]]:
        try:
            resp = self._client.embeddings.create(
                input=texts,
                model=self._model,
                **self._request_options,
            )
        except Exception as exc:
            if len(texts) <= 1 or not self._request_exceeded_context(exc):
                raise
            midpoint = len(texts) // 2
            logger.warning(
                "Embedding request exceeded the provider context; retrying "
                "as batches of %d and %d documents",
                midpoint,
                len(texts) - midpoint,
            )
            return [
                *self._embed_document_batch(texts[:midpoint]),
                *self._embed_document_batch(texts[midpoint:]),
            ]
        return [d.embedding for d in sorted(resp.data, key=lambda x: x.index)]

    @staticmethod
    def _request_exceeded_context(exc: Exception) -> bool:
        if getattr(exc, "status_code", None) != 400:
            return False
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "maximum context length",
                "input_tokens",
                "too many tokens",
                "context_length_exceeded",
            )
        )


def _to_document(obj: Any) -> _Document:
    """Convert any document-like object to ``_Document`` (duck-typed)."""
    if isinstance(obj, _Document):
        return obj
    return _Document(
        page_content=getattr(obj, "page_content", ""),
        metadata=getattr(obj, "metadata", {}),
    )


def _result_source_file(metadata: Dict[str, Any]) -> str:
    """Prefer the repository-relative file identity encoded in ``node_id``."""
    raw_file = str(metadata.get("file") or "").replace("\\", "/")
    node_id = str(metadata.get("node_id") or "").replace("\\", "/")
    if not raw_file or not node_id:
        return raw_file

    # ``chunk_file`` can be used outside a repository and then carries the
    # same absolute path in both fields. Preserve that direct-file contract.
    if node_id == raw_file or node_id.startswith(f"{raw_file}:"):
        return raw_file

    node_file = node_id.split(":", 1)[0].removeprefix("./")
    comparable_file = raw_file.removeprefix("./")
    if node_file and (
        comparable_file == node_file or comparable_file.endswith(f"/{node_file}")
    ):
        return node_file
    return raw_file


class CodeVectorStore:
    """
    Vector store for code embeddings using FAISS and sentence-transformers.
    Provides semantic search capabilities over code chunks.

    Supports hierarchical indexing:
    - L0: File-level skeletons
    - L2: Function/method-level chunks for fine-grained retrieval (default)
    """

    def _state_for_update(self) -> _LoadedVectorState:
        """Return the current state, lazily supporting lightweight test stores."""

        state = getattr(self, "_loaded_state", None)
        if state is None:
            state = _LoadedVectorState(None, [], None, [], {}, None)
            self._loaded_state = state
        return state

    @property
    def closed(self) -> bool:
        """Whether every resource owned by this store has been released."""

        state = getattr(self, "_loaded_state", None)
        if state is None:
            return getattr(self, "embedding", None) is None
        return bool(
            state.l0_index is None
            and state.l2_index is None
            and not state.l0_documents
            and not state.l2_documents
            and getattr(self, "embedding", None) is None
            and getattr(self, "_query_cache_depth", 0) == 0
            and getattr(self, "_cached_query_text", None) is None
            and getattr(self, "_cached_query_vector", None) is None
        )

    @property
    def l0_index(self) -> Any:
        return self._loaded_state.l0_index

    @l0_index.setter
    def l0_index(self, value: Any) -> None:
        self._state_for_update().l0_index = value

    @property
    def l0_documents(self) -> List[_Document]:
        return self._loaded_state.l0_documents

    @l0_documents.setter
    def l0_documents(self, value: List[_Document]) -> None:
        self._state_for_update().l0_documents = value

    @property
    def l2_index(self) -> Any:
        return self._loaded_state.l2_index

    @l2_index.setter
    def l2_index(self, value: Any) -> None:
        self._state_for_update().l2_index = value

    @property
    def l2_documents(self) -> List[_Document]:
        return self._loaded_state.l2_documents

    @l2_documents.setter
    def l2_documents(self, value: List[_Document]) -> None:
        self._state_for_update().l2_documents = value

    @property
    def artifact_metadata(self) -> Dict[str, Any]:
        return self._loaded_state.artifact_metadata

    @artifact_metadata.setter
    def artifact_metadata(self, value: Dict[str, Any]) -> None:
        self._state_for_update().artifact_metadata = value

    @property
    def store_path(self) -> Path | None:
        return self._loaded_state.store_path

    @store_path.setter
    def store_path(self, value: Path | None) -> None:
        self._state_for_update().store_path = value

    def __init__(
        self,
        embedding_model: str = "text-embedding-ada-002",
        embedding_provider: str = "openai",
        dimension: int = 1536,
        index_type: str = "flat",
        index_metric: str = "ip",
        ivf_nlist: int = 100,
        ivf_nprobe: int = 8,
        store_path: Optional[str] = None,
        profiler: Optional[Profiler] = None,
        embedding: Optional[Any] = None,
        artifact_metadata: Optional[Dict[str, Any]] = None,
        **embedding_kwargs,
    ):
        """
        Initialize the CodeVectorStore.

        Args:
            embedding_model: Name of the embedding model to use
            embedding_provider: Provider for embeddings ("openai" or "huggingface")
            dimension: Dimension of the embedding vectors
            index_type: FAISS index type — "flat" (exact brute force, default)
                or "ivf" (IVF inverted-file; approximate, faster at scale).
                IVF indices are trained lazily on the first batch of vectors.
            index_metric: Distance metric ("ip" for inner product, "l2" for L2 distance)
            ivf_nlist: IVF only — number of Voronoi cells (coarse centroids). On
                small corpora it is clamped down to the training-set size, since
                FAISS k-means needs at least ``nlist`` training points.
            ivf_nprobe: IVF only — cells probed per query; the recall/latency
                knob. Clamped to the effective ``nlist``.
            store_path: Path to store/load the vector store
            profiler: Optional profiler instance to capture detailed timings
            embedding: A pre-built embedding wrapper to reuse. When several
                stores share one model (e.g. one per repo), pass the same
                instance so the model is loaded onto the GPU only once.
            artifact_metadata: Optional immutable source/build identity persisted
                with the top-level configuration.
            **embedding_kwargs: Additional arguments for embedding model
        """
        self.embedding_model = embedding_model
        self.embedding_provider = _validate_provider_options(
            embedding_provider,
            embedding_kwargs,
        )
        self.embedding_load_policy = (
            resolve_embedding_load_policy_from_options(
                embedding_model,
                embedding_kwargs,
            )
            if self.embedding_provider == "huggingface"
            else None
        )
        self.embedding_revision = (
            self.embedding_load_policy.revision if self.embedding_load_policy else None
        )
        self.embedding_trust_remote_code = bool(
            self.embedding_load_policy and self.embedding_load_policy.trust_remote_code
        )
        self.dimension = dimension
        self.index_type = index_type.lower()
        if self.index_type not in ("flat", "ivf"):
            raise ValueError(
                f"Unsupported index_type: {index_type}. Must be 'flat' or 'ivf'."
            )
        self.index_metric = index_metric.lower()
        if self.index_metric not in ["ip", "l2"]:
            raise ValueError(
                f"Unsupported index_metric: {index_metric}. Must be 'ip' or 'l2'."
            )
        self.ivf_nlist = max(1, int(ivf_nlist))
        self.ivf_nprobe = max(1, int(ivf_nprobe))
        initial_store_path = Path(store_path) if store_path else None
        self.profiler = profiler
        initial_artifact_metadata = dict(artifact_metadata or {})

        # Initialize the embedding model — or reuse a shared one so the same
        # model isn't loaded onto the GPU once per store.
        self.embedding = (
            embedding
            if embedding is not None
            else self._initialize_embedding_model(**embedding_kwargs)
        )
        self._cached_query_text: Optional[str] = None
        self._cached_query_vector: Optional[np.ndarray] = None
        self._query_cache_depth = 0
        self.dimension = self._infer_embedding_dimension(dimension)

        # Initialize L0 (file-level skeletons)
        l0_index = self._build_faiss_index()

        # Initialize L2 (function/method-level) - default
        l2_index = self._build_faiss_index()
        self._loaded_state = _LoadedVectorState(
            l0_index=l0_index,
            l0_documents=[],
            l2_index=l2_index,
            l2_documents=[],
            artifact_metadata=initial_artifact_metadata,
            store_path=initial_store_path,
        )

        logger.info(
            f"Initialized CodeVectorStore with {embedding_provider}:{embedding_model}"
        )

    def _get_index_and_docs(self, level: Level) -> tuple[faiss.Index, List[_Document]]:
        """Get the FAISS index and documents list for the specified level."""
        state = self._loaded_state
        if level == "l0":
            return state.l0_index, state.l0_documents
        elif level == "l2":
            return state.l2_index, state.l2_documents
        else:
            raise ValueError(f"Invalid level: {level}. Must be 'l0' or 'l2'.")

    def _initialize_embedding_model(self, **kwargs):
        """Initialize the embedding model based on provider."""
        if self.embedding_provider.lower() == "openai":
            return _OpenAIEmbeddingWrapper(model=self.embedding_model, **kwargs)
        elif self.embedding_provider.lower() == "huggingface":
            return _HuggingFaceEmbeddingWrapper(
                model_name=self.embedding_model, **kwargs
            )
        else:
            raise ValueError(
                f"Unsupported embedding provider: {self.embedding_provider}"
            )

    def _infer_embedding_dimension(self, expected: Optional[int]) -> int:
        """Probe the embedding model to determine vector dimensionality."""
        probe_text = "codenib-dimension-probe"
        vector = self.embedding.embed_query(probe_text)
        if not vector:
            raise ValueError("Failed to infer embedding dimension from model output")

        actual_dim = len(vector)
        if expected is not None and actual_dim != expected:
            logger.warning(
                "Embedding dimension mismatch: expected %s, got %s",
                expected,
                actual_dim,
            )
        return actual_dim

    def _profile_section(self, label: str, metadata: Optional[Dict[str, Any]] = None):
        """Return an active profiler section context if profiling is enabled."""
        if self.profiler is None:
            return nullcontext()
        return self.profiler.section(label, metadata)

    def _embed_query(self, query: str) -> np.ndarray:
        """Encode a query, reusing it only inside an explicit request scope."""
        cached_text = getattr(self, "_cached_query_text", None)
        cached_vector = getattr(self, "_cached_query_vector", None)
        cache_active = getattr(self, "_query_cache_depth", 0) > 0
        if cache_active and cached_text == query and cached_vector is not None:
            return cached_vector

        vector = np.asarray(
            self.embedding.embed_query(query), dtype=np.float32
        ).reshape(-1)
        if cache_active:
            self._cached_query_text = query
            self._cached_query_vector = vector
        return vector

    @contextmanager
    def reuse_query_embedding(self):
        """Reuse one query vector within a composed request, then discard it."""

        depth = getattr(self, "_query_cache_depth", 0)
        if depth == 0:
            self.clear_query_cache()
        self._query_cache_depth = depth + 1
        try:
            yield
        finally:
            self._query_cache_depth -= 1
            if self._query_cache_depth == 0:
                self.clear_query_cache()

    def clear_query_cache(self) -> None:
        """Clear the single-query embedding reused by consecutive search stages."""

        self._cached_query_text = None
        self._cached_query_vector = None

    def _should_filter_by_threshold(self, score: float, threshold: float) -> bool:
        """
        Determine if a result should be filtered based on score threshold.

        For inner product (ip): higher scores are better (similarity),
        filter if score < threshold
        For L2 distance (l2): lower scores are better (distance),
        filter if score > threshold
        """
        if self.index_metric == "ip":
            return score < threshold
        elif self.index_metric == "l2":
            return score > threshold
        else:
            raise ValueError(
                f"Unsupported index_metric: {self.index_metric}. Must be 'ip' or 'l2'."
            )

    def _faiss_metric(self) -> int:
        """Map the configured metric string to a FAISS metric constant."""
        if self.index_metric == "ip":
            return faiss.METRIC_INNER_PRODUCT
        elif self.index_metric == "l2":
            return faiss.METRIC_L2
        raise ValueError(
            f"Unsupported index_metric: {self.index_metric}. Must be 'ip' or 'l2'."
        )

    def _build_flat_index(self) -> faiss.Index:
        """Create a flat (exact) FAISS index with the configured metric."""
        if self.index_metric == "ip":
            return faiss.IndexFlatIP(self.dimension)
        elif self.index_metric == "l2":
            return faiss.IndexFlatL2(self.dimension)
        raise ValueError(
            f"Unsupported index_metric: {self.index_metric}. Must be 'ip' or 'l2'."
        )

    def _build_faiss_index(self, nlist: Optional[int] = None) -> faiss.Index:
        """Create an empty FAISS index of the configured type and metric.

        Flat indices are ready for ``add``; IVF indices are returned
        *untrained* and must be trained on a batch of vectors (see
        :meth:`_add_to_index`) before any vectors are added.
        """
        if self.index_type == "flat":
            return self._build_flat_index()
        # IVF: a flat quantizer assigns each vector to one of ``cells`` cells.
        cells = max(1, int(nlist if nlist is not None else self.ivf_nlist))
        quantizer = self._build_flat_index()
        index = faiss.IndexIVFFlat(
            quantizer, self.dimension, cells, self._faiss_metric()
        )
        index.nprobe = min(self.ivf_nprobe, cells)
        return index

    def _add_to_index(self, level: Level, vectors: np.ndarray) -> None:
        """Add pre-computed ``vectors`` to *level*'s FAISS index.

        Flat indices take a plain ``add``. An untrained IVF index is trained
        on this first batch — clamping ``nlist`` down to the batch size on
        small corpora (FAISS k-means requires at least ``nlist`` training
        points) — then the vectors are added; later batches add to the
        already-trained index.
        """
        if vectors is None or len(vectors) == 0:
            return
        index = self.l0_index if level == "l0" else self.l2_index
        if self.index_type == "ivf" and not index.is_trained:
            n = int(vectors.shape[0])
            effective_nlist = max(1, min(self.ivf_nlist, n))
            if effective_nlist != index.nlist:
                # nlist is fixed at construction, so rebuild at the size the
                # training set can actually support, then re-bind the slot.
                index = self._build_faiss_index(nlist=effective_nlist)
                if level == "l0":
                    self.l0_index = index
                else:
                    self.l2_index = index
            with self._profile_section(
                f"faiss_index_train_{level}",
                {"num_vectors": n, "nlist": effective_nlist, "level": level},
            ):
                index.train(vectors)
        index.add(vectors)

    def _search_index(
        self,
        query: str,
        index: faiss.Index,
        documents: List[_Document],
        top_k: int,
    ) -> List[tuple[_Document, float]]:
        """Encode *query* and search a raw FAISS index.

        Returns a list of ``(document, score)`` pairs sorted by relevance.
        """
        if index is None or index.ntotal == 0:
            return []

        query_vec = self._embed_query(query).reshape(1, -1)

        # FAISS search
        k = min(top_k, index.ntotal)
        distances, indices = index.search(query_vec, k)

        results: List[tuple[_Document, float]] = []
        for dist, idx in zip(distances[0], indices[0], strict=True):
            if idx < 0:
                continue  # FAISS sentinel for empty slots
            if idx < len(documents):
                results.append((documents[idx], float(dist)))
        return results

    def swap_index(
        self,
        path: str,
        *,
        native_index_authorization: NativeIndexAuthorization | None = None,
    ) -> None:
        """Hot-swap the FAISS index without reloading the embedding model.

        The replacement is fully loaded and validated before the current
        L0/L2 state is released. The embedding model is left intact so the
        caller can reuse the same model across many instances.
        """
        self.load(
            path,
            native_index_authorization=native_index_authorization,
        )

    def close(self) -> None:
        """Release embeddings and FAISS resources to free memory."""

        state = self._loaded_state
        released: dict[int, bool] = {}
        deferred: BaseException | None = None
        for index in (state.l0_index, state.l2_index):
            if index is None or id(index) in released:
                continue
            reset = getattr(index, "reset", None)
            if not callable(reset):
                released[id(index)] = True
                continue
            try:
                reset()
                released[id(index)] = True
            except BaseException as exc:  # noqa: B036 - visit both native indices
                released[id(index)] = False
                if deferred is None:
                    deferred = exc
                else:
                    _annotate_secondary_error(
                        deferred,
                        "additional FAISS index cleanup also failed",
                        exc,
                    )

        l0_released = state.l0_index is None or released.get(id(state.l0_index), False)
        l2_released = state.l2_index is None or released.get(id(state.l2_index), False)
        self._loaded_state = _LoadedVectorState(
            l0_index=None if l0_released else state.l0_index,
            l0_documents=[] if l0_released else state.l0_documents,
            l2_index=None if l2_released else state.l2_index,
            l2_documents=[] if l2_released else state.l2_documents,
            artifact_metadata=state.artifact_metadata,
            store_path=state.store_path,
        )
        if deferred is not None:
            raise deferred

        self.embedding = None
        self._query_cache_depth = 0
        self._cached_query_text = None
        self._cached_query_vector = None

        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def add_code_chunks(
        self, code_chunks: List[Dict[str, Any]], level: Level = "l2"
    ) -> None:
        """
        Add code chunks to the vector store.

        Args:
            code_chunks: List of code chunk dictionaries with content and metadata
            level: Index level to add chunks to
                ("l0" for file skeletons, "l2" for functions/methods)
        """
        if not code_chunks:
            logger.warning("No code chunks provided")
            return

        index, documents_list = self._get_index_and_docs(level)
        logger.info(f"Adding {len(code_chunks)} code chunks to {level} vector store")

        # Convert chunks to _Document objects
        documents: List[_Document] = []
        for i, chunk in enumerate(code_chunks):
            content = chunk.get("content", "")
            content_hash = hashlib.md5(
                content.encode("utf-8", errors="replace")
            ).hexdigest()
            metadata = {
                "chunk_id": len(documents_list) + i,
                "chunk_type": chunk.get("chunk_type", "unknown"),
                "name": chunk.get("name", f"chunk_{i}"),
                "file": chunk.get("file", ""),
                "start_line": chunk.get("start_line", 0),
                "end_line": chunk.get("end_line", 0),
                "node_id": chunk.get("node_id", ""),
                "level": level,
                "content_hash": content_hash,
            }
            for key, value in chunk.items():
                if key not in ["content"] and key not in metadata:
                    metadata[key] = value

            documents.append(_Document(page_content=content, metadata=metadata))

        # Store documents
        documents_list.extend(documents)

        texts = [doc.page_content for doc in documents]

        # Phase 1: Embed texts (typically the bottleneck)
        with self._profile_section(
            f"embedding_encode_{level}",
            {"num_documents": len(documents), "level": level},
        ):
            embeddings = self.embedding.embed_documents(texts)

        # Phase 2: Add pre-computed vectors to FAISS index
        with self._profile_section(
            f"faiss_index_add_{level}",
            {"num_vectors": len(embeddings), "level": level},
        ):
            vectors = np.array(embeddings, dtype=np.float32)
            self._add_to_index(level, vectors)

        logger.info(
            f"Successfully added {len(documents)} documents to {level} vector store"
        )

    def add_nodes_with_content(
        self, nodes: List[NodeInfo], level: Level = "l2"
    ) -> None:
        """
        Add NodeInfo objects (with content) to the vector store.

        Args:
            nodes: List of NodeInfo objects
            level: Index level to add nodes to ("l0" or "l2")
        """
        chunks = []
        for node in nodes:
            chunk = {
                "content": node.content,
                "chunk_type": node.type,
                "name": node.node_name,
                "file": node.file,
                "start_line": node.start_line,
                "end_line": node.end_line,
            }
            chunks.append(chunk)

        self.add_code_chunks(chunks, level=level)

    def search(
        self,
        query: str,
        top_k: int = 10,
        score_threshold: Optional[float] = None,
        level: Level = "l2",
        mask_node_ids: Optional[Set[str]] = None,
    ) -> List[NodeInfo]:
        """
        Search for similar code chunks using semantic similarity.

        Args:
            query: Search query text
            top_k: Number of top results to return
            score_threshold: Minimum similarity score threshold
            level: Index level to search ("l0" for file skeletons, "l2" for
                functions/methods)
            mask_node_ids: Optional set of CodeChunk.node_id values to filter results.

        Returns:
            List of NodeInfo objects with scores populated
        """
        index, documents = self._get_index_and_docs(level)

        if index is None or index.ntotal == 0:
            logger.warning(f"No {level} vector store available. Add code chunks first.")
            return []

        logger.debug(f"Searching {level} for: {query[:100]}...")

        docs_with_scores = self._search_index(query, index, documents, top_k)

        results = []
        for doc, score in docs_with_scores:
            metadata = doc.metadata

            if score_threshold is not None and self._should_filter_by_threshold(
                score, score_threshold
            ):
                continue

            node_with_score = NodeInfo(
                node_name=metadata.get("name", "unknown"),
                type=metadata.get("chunk_type", "unknown"),
                file=_result_source_file(metadata),
                node_id=metadata.get("node_id", ""),
                start_line=metadata.get("start_line", 0),
                end_line=metadata.get("end_line", 0),
                score=float(score),
            )
            results.append(node_with_score)

        if mask_node_ids:
            results = [r for r in results if r.node_id in mask_node_ids]
        if top_k:
            results = results[:top_k]

        logger.debug(
            f"Found {len(results)} results in {level} (masked={bool(mask_node_ids)})"
        )
        return results

    def search_with_content(
        self,
        query: str,
        top_k: int = 10,
        score_threshold: Optional[float] = None,
        level: Level = "l2",
        mask_node_ids: Optional[Set[str]] = None,
    ) -> List[NodeInfo]:
        """
        Search and return results with content included.

        Args:
            query: Search query text
            top_k: Number of top results to return
            score_threshold: Minimum similarity score threshold
            level: Index level to search ("l0" for file skeletons, "l2" for
                functions/methods)
            mask_node_ids: Optional set of CodeChunk.node_id values to filter results.

        Returns:
            List of NodeInfo objects with content populated
        """
        index, documents = self._get_index_and_docs(level)

        if index is None or index.ntotal == 0:
            logger.warning(f"No {level} vector store available. Add code chunks first.")
            return []

        docs_with_scores = self._search_index(query, index, documents, top_k)

        results = []
        for doc, score in docs_with_scores:
            metadata = doc.metadata

            if score_threshold is not None and self._should_filter_by_threshold(
                score, score_threshold
            ):
                continue

            node_with_content = NodeInfo(
                node_name=metadata.get("name", "unknown"),
                type=metadata.get("chunk_type", "unknown"),
                file=_result_source_file(metadata),
                node_id=metadata.get("node_id", ""),
                start_line=metadata.get("start_line", 0),
                end_line=metadata.get("end_line", 0),
                score=float(score),
                content=doc.page_content,
            )
            results.append(node_with_content)

        if mask_node_ids:
            results = [r for r in results if r.node_id in mask_node_ids]
        if top_k:
            results = results[:top_k]

        logger.debug(
            f"Found {len(results)} results with content in {level} "
            f"(masked={bool(mask_node_ids)})"
        )
        return results

    def search_within_ids(
        self,
        query: str,
        mask_node_ids: Set[str],
        top_k: int = 10,
        level: Level = "l2",
    ) -> List[NodeInfo]:
        """Search only within a restricted set of node IDs.

        Instead of searching the full FAISS index globally and filtering
        afterwards, this method restricts the search space *before* computing
        similarity.  It reconstructs stored vectors for matching documents
        and computes similarity against the query embedding directly.

        Args:
            query: Search query text.
            mask_node_ids: Set of node_id / node_name values to restrict
                search to.
            top_k: Number of top results to return.
            level: Index level to search.

        Returns:
            List of NodeInfo objects sorted by similarity score.
        """
        index, documents = self._get_index_and_docs(level)
        if index is None or index.ntotal == 0:
            logger.warning(f"No {level} vector store available.")
            return []

        # Find documents whose node_id or name is in mask set
        matched: list[tuple[int, _Document]] = []
        for i, doc in enumerate(documents):
            meta = doc.metadata
            if (
                meta.get("node_id", "") in mask_node_ids
                or meta.get("name", "") in mask_node_ids
            ):
                matched.append((i, doc))

        if not matched:
            logger.debug("search_within_ids: no matching documents found")
            return []

        # Encode query
        query_vec = self._embed_query(query)

        # Reconstruct stored vectors and compute similarity
        results: list[NodeInfo] = []
        for faiss_idx, doc in matched:
            vec = index.reconstruct(faiss_idx)
            if self.index_metric == "ip":
                score = float(np.dot(query_vec, vec))
            else:  # l2 — lower is better
                score = float(np.sum((query_vec - vec) ** 2))

            metadata = doc.metadata
            results.append(
                NodeInfo(
                    node_name=metadata.get("name", "unknown"),
                    type=metadata.get("chunk_type", "unknown"),
                    file=_result_source_file(metadata),
                    node_id=metadata.get("node_id", ""),
                    start_line=metadata.get("start_line", 0),
                    end_line=metadata.get("end_line", 0),
                    score=score,
                    content=doc.page_content,
                )
            )

        best_by_node = {}
        for result in results:
            identity = result.node_id or (
                result.file,
                result.node_name,
                result.start_line,
                result.end_line,
            )
            existing = best_by_node.get(identity)
            if existing is None:
                best_by_node[identity] = result
                continue
            if self.index_metric == "ip":
                is_better = result.score > existing.score
            else:
                is_better = result.score < existing.score
            if is_better:
                best_by_node[identity] = result
        results = list(best_by_node.values())

        # Sort: ip → higher is better; l2 → lower is better
        results.sort(
            key=lambda r: r.score,
            reverse=(self.index_metric == "ip"),
        )

        logger.debug(
            "search_within_ids: %d matched, %d unique, returning top %d",
            len(matched),
            len(results),
            min(top_k, len(results)),
        )
        return results[:top_k]

    def hierarchical_search(
        self,
        query: str,
        l0_top_k: int = 5,
        l2_top_k: int = 10,
        l0_score_threshold: Optional[float] = None,
        l2_score_threshold: Optional[float] = None,
        filter_l2_by_l0: bool = True,
    ) -> Dict[str, List[NodeInfo]]:
        """
        Note: This method is implemented by Claude and is just for future reference.
        Perform hierarchical search: first L0 (files), then L2 (functions).

        This implements a coarse-to-fine retrieval strategy:
        1. Search L0 to find relevant files based on their skeletons
        2. Search L2 for specific functions/methods
        3. Optionally filter L2 results to only include those from L0 files

        Args:
            query: Search query text
            l0_top_k: Number of top L0 results (files)
            l2_top_k: Number of top L2 results (functions/methods)
            l0_score_threshold: Score threshold for L0 results
            l2_score_threshold: Score threshold for L2 results
            filter_l2_by_l0: If True, only return L2 results from files found in L0

        Returns:
            Dict with 'l0' and 'l2' keys containing search results
        """
        # Step 1: Search L0 to find relevant files
        l0_results = self.search(
            query, top_k=l0_top_k, score_threshold=l0_score_threshold, level="l0"
        )

        # Step 2: Search L2 for functions/methods (fetch more if filtering)
        l2_fetch_k = l2_top_k * 3 if filter_l2_by_l0 else l2_top_k
        l2_results = self.search(
            query, top_k=l2_fetch_k, score_threshold=l2_score_threshold, level="l2"
        )

        # Step 3: Optionally filter L2 results by L0 files
        if filter_l2_by_l0 and l0_results:
            l0_files = {result.file for result in l0_results}
            l2_results = [r for r in l2_results if r.file in l0_files]

        # Limit L2 results to top_k
        l2_results = l2_results[:l2_top_k]

        return {
            "l0": l0_results,
            "l2": l2_results,
        }

    def save(self, path: Optional[str] = None) -> None:
        """
        Save the vector store to disk.

        Args:
            path: Path to save the store (uses self.store_path if not provided)
        """
        save_path = Path(path) if path else self.store_path
        if save_path is None:
            raise ValueError("No save path provided")

        save_path = Path(save_path)
        save_path.mkdir(parents=True, exist_ok=True)

        logger.info(f"Saving vector store to {save_path}")

        model_suffix = self.embedding_model.replace("/", "__")

        level_state = {}
        for level in ("l0", "l2"):
            index, documents = self._get_index_and_docs(level)
            if documents and (index is None or int(index.ntotal) != len(documents)):
                vector_count = 0 if index is None else int(index.ntotal)
                raise ValueError(
                    f"Cannot save misaligned {level} vector store: "
                    f"{vector_count} vectors for {len(documents)} documents"
                )
            if index is not None:
                self._validate_loaded_faiss_index(
                    index,
                    relative=f"{level}/index_{model_suffix}.faiss",
                    document_count=len(documents),
                )
            level_state[level] = (index, documents)

        config_path = save_path / f"config_{model_suffix}.json"
        save_marker = save_path / f".{config_path.name}.save-in-progress"
        _atomic_json_dump(
            save_marker,
            {"persistence_schema": VECTOR_PERSISTENCE_SCHEMA},
        )
        try:
            level_artifacts: dict[str, dict[str, dict[str, Any]]] = {}
            for level, (_index, documents) in level_state.items():
                if documents:
                    level_artifacts[level] = self._save_level(
                        save_path, level, model_suffix
                    )
                else:
                    self._remove_level_files(save_path, level, model_suffix)

            config = {
                "embedding_model": self.embedding_model,
                "embedding_provider": self.embedding_provider,
                "embedding_revision": self.embedding_revision,
                "dimension": self.dimension,
                "index_type": self.index_type,
                "index_metric": self.index_metric,
                "l0_documents": len(self.l0_documents),
                "l2_documents": len(self.l2_documents),
                "persistence_schema": VECTOR_PERSISTENCE_SCHEMA,
                "level_artifacts": level_artifacts,
            }
            if self.artifact_metadata:
                config["artifact"] = self.artifact_metadata
            if (
                type(self.artifact_metadata.get("builder_schema")) is int
                and self.artifact_metadata.get("builder_schema") == 8
            ):
                config["row_mapping"] = VECTOR_ROW_MAPPING_CONTRACT
            # This config is the commit record for both levels. Publishing it
            # last makes an interrupted multi-file save detectable on load.
            _atomic_json_dump(config_path, config)
        except Exception:
            # Keep the marker so a later load cannot accept a partially
            # replaced legacy artifact. A successful retry removes it.
            raise
        else:
            save_marker.unlink()

        logger.info("Vector store saved successfully")

    @staticmethod
    def _remove_level_files(save_path: Path, level: str, model_suffix: str) -> None:
        """Remove persisted files when a vector level becomes empty."""

        level_path = save_path / level
        for name in (
            f"config_{model_suffix}.json",
            f"index_{model_suffix}.faiss",
            f"documents_{model_suffix}.pkl",
            f"documents_{model_suffix}.json",
            f"index_{model_suffix}.pkl",
        ):
            (level_path / name).unlink(missing_ok=True)
        try:
            level_path.rmdir()
        except OSError:
            pass

    def _save_level(
        self, save_path: Path, level: str, model_suffix: str
    ) -> dict[str, dict[str, Any]]:
        """Save a single level (l0 or l2) to disk."""
        index, documents = self._get_index_and_docs(level)

        level_path = save_path / level
        level_path.mkdir(parents=True, exist_ok=True)

        # Write raw FAISS index
        index_name = f"index_{model_suffix}"
        index_path = level_path / f"{index_name}.faiss"
        _atomic_replace(index_path, lambda path: faiss.write_index(index, str(path)))

        if (
            type(self.artifact_metadata.get("builder_schema")) is int
            and self.artifact_metadata.get("builder_schema") == 8
        ):
            # Commit the ordered row-to-document mapping as canonical, inert
            # JSON.  Its array position is the FAISS row receipt.
            docs_path = level_path / f"documents_{model_suffix}.json"
            _atomic_canonical_document_dump(docs_path, documents)
            (level_path / f"documents_{model_suffix}.pkl").unlink(missing_ok=True)
        else:
            # Preserve the legacy local persistence contract for unversioned
            # and schema-7 callers.  Strict compiler-cache ingress rejects it.
            docs_path = level_path / f"documents_{model_suffix}.pkl"
            _atomic_pickle_dump(docs_path, documents)
            (level_path / f"documents_{model_suffix}.json").unlink(missing_ok=True)
        artifacts = vector_level_artifact_records(
            level_path,
            model_suffix,
            documents_file=docs_path.name,
        )

        # Save level config
        config_path = level_path / f"config_{model_suffix}.json"
        config = {
            "embedding_model": self.embedding_model,
            "embedding_provider": self.embedding_provider,
            "embedding_revision": self.embedding_revision,
            "dimension": self.dimension,
            "index_type": self.index_type,
            "index_metric": self.index_metric,
            "level": level,
            "num_documents": len(documents),
        }
        _atomic_json_dump(config_path, config)

        logger.info(f"Saved {level.upper()} store with {len(documents)} documents")
        return artifacts

    def load(
        self,
        path: Optional[str] = None,
        *,
        native_index_authorization: NativeIndexAuthorization | None = None,
    ) -> None:
        """
        Load the vector store from disk.

        Args:
            path: Path to load the store from (uses self.store_path if not provided)
            native_index_authorization: Process-local authorization bound to the
                exact captured tree and semantic view contract. Artifact fields
                cannot provide this capability.
        """
        ambient_error = sys.exc_info()[1]
        # Exception subclasses can shadow ``__traceback__``. Read the base
        # descriptor so the ambient snapshot cannot run user code.
        ambient_traceback = (
            None
            if ambient_error is None
            else BaseException.__traceback__.__get__(
                ambient_error,
                type(ambient_error),
            )
        )
        load_path = Path(path) if path else self.store_path
        if load_path is None:
            raise ValueError("No load path provided")
        require_native_index_authorization_preflight(
            native_index_authorization,
            view_type="vector",
        )
        view = capture_authenticated_vector_view(load_path)
        loaded_state: _LoadedVectorState | None = None
        previous_state: _LoadedVectorState | None = None
        view_closed = False
        body_completed = False
        try:
            require_native_index_authorization(
                native_index_authorization,
                view.ownership,
                view_type="vector",
                semantic_contract=self.artifact_metadata,
            )
            loaded_state = self._load_captured(view)
            view.verify_final()
            # A close failure is still a failed replacement. Finish the
            # captured-view lifecycle before exposing any newly parsed state.
            view.close()
            view_closed = True
            previous_state = self._loaded_state
            self._replace_loaded_state(loaded_state)
            body_completed = True
        finally:
            primary_error = sys.exc_info()[1]
            if (
                body_completed
                and primary_error is ambient_error
                and ambient_error is not None
                and BaseException.__traceback__.__get__(
                    primary_error,
                    type(primary_error),
                )
                is ambient_traceback
            ):
                primary_error = None
            cleanup_error: BaseException | None = None
            if not view_closed:
                try:
                    view.close()
                    view_closed = True
                except BaseException as exc:  # noqa: B036 - preserve primary
                    cleanup_error = exc
            if loaded_state is not None:
                release_state = (
                    previous_state
                    if self._loaded_state is loaded_state
                    else loaded_state
                )
                if release_state is not None:
                    try:
                        self._release_vector_indices(
                            tuple(
                                index
                                for index in (
                                    release_state.l0_index,
                                    release_state.l2_index,
                                )
                                if index is not self.l0_index
                                and index is not self.l2_index
                            )
                        )
                    except BaseException as exc:  # noqa: B036 - visit all cleanup
                        if primary_error is not None:
                            _attach_vector_index_cleanup_owner(primary_error, exc)
                        if cleanup_error is None:
                            cleanup_error = exc
                        else:
                            _attach_vector_index_cleanup_owner(cleanup_error, exc)
                            _annotate_secondary_error(
                                cleanup_error,
                                "vector index cleanup also failed",
                                exc,
                            )
            if cleanup_error is not None:
                if primary_error is not None:
                    if not view_closed:
                        _attach_vector_view_cleanup_owner(
                            primary_error,
                            view,
                            cleanup_error,
                        )
                    _annotate_secondary_error(
                        primary_error,
                        "vector load cleanup also failed",
                        cleanup_error,
                    )
                else:
                    raise cleanup_error

    def _load_captured(self, view: AuthenticatedVectorView) -> _LoadedVectorState:
        """Load native state only through one already-authorized captured tree."""

        load_path = view.root
        logger.info("Loading authenticated vector store from %s", load_path)
        if view.has_file(VECTOR_VIEW_UPDATE_MARKER):
            raise ValueError(
                "vector view has an incomplete update marker: "
                f"{VECTOR_VIEW_UPDATE_MARKER}"
            )

        model_suffix = self.embedding_model.replace("/", "__")

        # Load top-level configuration
        config_relative = f"config_{model_suffix}.json"
        if not view.has_file(config_relative):
            config_relative = "config.json"
        save_marker = f".config_{model_suffix}.json.save-in-progress"
        if view.has_file(save_marker):
            raise ValueError(
                f"Vector store has an interrupted save marker: {save_marker}"
            )

        expected_artifact = dict(self.artifact_metadata)
        expected_config = expected_artifact.get("persistence_config_fingerprint")
        if expected_config is not None:
            config_relative = f"config_{model_suffix}.json"
            view.require_record(
                config_relative,
                expected_config,
            )
        loaded_artifact = expected_artifact
        expected_counts: Dict[str, Optional[int]] = {"l0": None, "l2": None}
        committed_levels: Optional[dict[str, object]] = None
        if view.has_file(config_relative):
            config = view.load_json_object(
                config_relative,
                label="vector generation config",
            )

            saved_model = config.get("embedding_model")
            if saved_model is not None and saved_model != self.embedding_model:
                raise ValueError(
                    f"Vector config model mismatch: expected {self.embedding_model!r}, "
                    f"found {saved_model!r}"
                )
            saved_provider = config.get("embedding_provider")
            if saved_provider is not None and normalize_provider(
                saved_provider
            ) != normalize_provider(self.embedding_provider):
                raise ValueError(
                    "Vector config provider mismatch: expected "
                    f"{self.embedding_provider!r}, found {saved_provider!r}"
                )
            saved_revision = config.get("embedding_revision")
            if saved_revision != self.embedding_revision:
                raise ValueError(
                    "Vector config embedding revision mismatch: expected "
                    f"{self.embedding_revision!r}, found {saved_revision!r}"
                )
            saved_dimension = config.get("dimension")
            if saved_dimension is not None and saved_dimension != self.dimension:
                raise ValueError(
                    f"Vector config dimension mismatch: expected {self.dimension}, "
                    f"found {saved_dimension}"
                )
            saved_index_type = config.get("index_type")
            if saved_index_type and saved_index_type != self.index_type:
                raise ValueError(
                    f"Vector config index type mismatch: expected {self.index_type!r}, "
                    f"found {saved_index_type!r}"
                )
            saved_metric = config.get("index_metric")
            if saved_metric and saved_metric != self.index_metric:
                raise ValueError(
                    f"Vector config metric mismatch: expected {self.index_metric!r}, "
                    f"found {saved_metric!r}"
                )
            saved_artifact = config.get("artifact")
            if isinstance(saved_artifact, dict):
                expected_fingerprint = expected_artifact.get("embedding_fingerprint")
                saved_fingerprint = saved_artifact.get("embedding_fingerprint")
                if (
                    expected_fingerprint is not None
                    and saved_fingerprint != expected_fingerprint
                ):
                    raise ValueError(
                        "Vector artifact embedding fingerprint does not match manifest"
                    )
                loaded_artifact = dict(saved_artifact)
            elif expected_artifact.get("embedding_fingerprint") is not None:
                raise ValueError("Vector config is missing embedding artifact identity")

            saved_builder_schema = (
                saved_artifact.get("builder_schema")
                if isinstance(saved_artifact, dict)
                else None
            )
            expected_builder_schema = expected_artifact.get("builder_schema")
            retained_schema_selected = (
                type(expected_builder_schema) is int and expected_builder_schema >= 7
            ) or (type(saved_builder_schema) is int and saved_builder_schema >= 7)
            if retained_schema_selected and not (
                type(expected_builder_schema) is int
                and type(saved_builder_schema) is int
                and saved_builder_schema == expected_builder_schema
            ):
                raise ValueError(
                    "Retained vector authorization and root artifact builder schemas "
                    "must match exactly"
                )
            schema_8_selected = (
                type(expected_builder_schema) is int
                and expected_builder_schema == 8
                and type(saved_builder_schema) is int
                and saved_builder_schema == 8
            )
            if schema_8_selected and config.get("row_mapping") != (
                VECTOR_ROW_MAPPING_CONTRACT
            ):
                raise ValueError("Schema-8 vector config has an invalid row mapping")
            if not schema_8_selected and config.get("row_mapping") is not None:
                raise ValueError("Legacy vector config has an unsupported row mapping")

            for level in ("l0", "l2"):
                value = config.get(f"{level}_documents")
                if value is None:
                    continue
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(
                        f"Vector config has invalid {level} document count: {value!r}"
                    )
                expected_counts[level] = value

            persistence_schema = config.get("persistence_schema")
            raw_levels = config.get("level_artifacts")
            if persistence_schema is not None or raw_levels is not None:
                if persistence_schema != VECTOR_PERSISTENCE_SCHEMA:
                    raise ValueError(
                        "Vector config has unsupported persistence schema: "
                        f"{persistence_schema!r}"
                    )
                if not isinstance(raw_levels, dict) or not set(raw_levels) <= {
                    "l0",
                    "l2",
                }:
                    raise ValueError("Vector config has invalid committed levels")
                committed_levels = dict(raw_levels)
                if any(expected_counts[level] is None for level in ("l0", "l2")):
                    raise ValueError(
                        "Vector config with committed artifacts requires level counts"
                    )
                if schema_8_selected:
                    for level, records in committed_levels.items():
                        documents_record = (
                            records.get("documents")
                            if isinstance(records, dict)
                            else None
                        )
                        expected_name = f"documents_{model_suffix}.json"
                        if not isinstance(documents_record, dict) or (
                            documents_record.get("file") != expected_name
                        ):
                            raise ValueError(
                                "Schema-8 vector config has an invalid canonical "
                                f"document record for {level}"
                            )
        elif expected_artifact.get("embedding_fingerprint") is not None:
            raise ValueError("Vector store is missing its top-level configuration")

        loaded_levels: dict[str, tuple[Any, List[_Document]]] = {}
        try:
            for level in ("l0", "l2"):
                expected_count = expected_counts[level]
                faiss_relative = f"{level}/index_{model_suffix}.faiss"
                committed_artifacts = (
                    committed_levels.get(level)
                    if committed_levels is not None
                    else None
                )

                # A zero count in the top-level config is authoritative. Older
                # writers could leave stale level files behind after deletions.
                if expected_count == 0:
                    if committed_artifacts is not None:
                        raise ValueError(
                            f"Vector config commits artifacts for empty {level} level"
                        )
                    loaded_levels[level] = (self._build_faiss_index(), [])
                    continue

                if (
                    committed_levels is not None
                    and expected_count is not None
                    and expected_count > 0
                    and committed_artifacts is None
                ):
                    raise ValueError(
                        f"Vector config is missing committed artifacts for {level}"
                    )

                if committed_artifacts is not None or view.has_file(faiss_relative):
                    index, documents = self._load_level(
                        view,
                        level,
                        model_suffix,
                        committed_artifacts=committed_artifacts,
                    )
                elif expected_count is not None and expected_count > 0:
                    raise FileNotFoundError(
                        f"Vector config expects {expected_count} {level} documents, "
                        f"but {faiss_relative} is missing"
                    )
                else:
                    index, documents = self._build_faiss_index(), []

                loaded_levels[level] = (index, documents)
                if expected_count is not None and len(documents) != expected_count:
                    raise ValueError(
                        f"{level} config expects {expected_count} documents, "
                        f"loaded {len(documents)}"
                    )
        except BaseException as primary_error:
            try:
                self._release_vector_indices(
                    tuple(index for index, _documents in loaded_levels.values())
                )
            except BaseException as cleanup_error:  # noqa: B036 - preserve primary
                _attach_vector_index_cleanup_owner(primary_error, cleanup_error)
                _annotate_secondary_error(
                    primary_error,
                    "partially loaded vector levels cleanup also failed",
                    cleanup_error,
                )
            raise

        for level, (_index, documents) in loaded_levels.items():
            if documents:
                logger.info(
                    "Loaded %s store with %d documents",
                    level.upper(),
                    len(documents),
                )

        total_docs = sum(len(documents) for _index, documents in loaded_levels.values())
        logger.info(
            f"Vector store loaded successfully with {total_docs} total documents "
            f"(L0: {len(loaded_levels['l0'][1])}, "
            f"L2: {len(loaded_levels['l2'][1])})"
        )
        return _LoadedVectorState(
            l0_index=loaded_levels["l0"][0],
            l0_documents=loaded_levels["l0"][1],
            l2_index=loaded_levels["l2"][0],
            l2_documents=loaded_levels["l2"][1],
            artifact_metadata=loaded_artifact,
            store_path=load_path,
        )

    @staticmethod
    def _release_vector_indices(indices: tuple[Any, ...]) -> None:
        _VectorIndexCleanupOwner(indices).close()

    def _replace_loaded_state(self, state: _LoadedVectorState) -> None:
        """Commit one fully verified replacement with a single reference store."""

        self._loaded_state = state

    def _load_level(
        self,
        view: AuthenticatedVectorView,
        level: str,
        model_suffix: str,
        *,
        committed_artifacts: object = None,
    ) -> tuple[faiss.Index, List[_Document]]:
        """Load a single level from disk.

        Handles both the new format (raw FAISS + _Document list) and the
        legacy LangChain format (FAISS + docstore pkl) transparently.
        """
        level_path = view.root / level
        index_name = f"index_{model_suffix}"
        faiss_relative = f"{level}/{index_name}.faiss"
        committed_documents_relative = None
        if committed_artifacts is not None:
            if not isinstance(committed_artifacts, Mapping) or set(
                committed_artifacts
            ) != {"index", "documents"}:
                raise ValueError(f"invalid committed vector artifacts for {level_path}")
            view.require_record(faiss_relative, committed_artifacts["index"])
            documents_record = committed_artifacts["documents"]
            if not isinstance(documents_record, Mapping):
                raise ValueError(
                    f"invalid documents vector artifact record for {level_path}"
                )
            documents_name = documents_record.get("file")
            if documents_name not in {
                f"documents_{model_suffix}.json",
                f"documents_{model_suffix}.pkl",
            }:
                raise ValueError(
                    f"invalid documents vector artifact filename for {level_path}"
                )
            committed_documents_relative = f"{level}/{documents_name}"
            view.require_record(
                committed_documents_relative,
                documents_record,
            )

        if not view.has_file(faiss_relative):
            raise FileNotFoundError(f"FAISS index not found at {faiss_relative}")

        index: faiss.Index | None = None
        try:
            try:
                index = _read_authenticated_faiss(view, faiss_relative)
            except Exception as e:
                raise ValueError(
                    f"Could not load FAISS index from {faiss_relative}: {e}"
                ) from e
            return self._finish_loaded_level(
                view,
                level,
                model_suffix,
                level_path=level_path,
                faiss_relative=faiss_relative,
                committed_documents_relative=committed_documents_relative,
                index=index,
            )
        except BaseException as primary_error:
            # FAISS has already allocated native state, but the caller cannot
            # assume ownership until this method returns successfully.
            if index is not None:
                try:
                    self._release_vector_indices((index,))
                except BaseException as cleanup_error:  # noqa: B036 - preserve primary
                    _attach_vector_index_cleanup_owner(primary_error, cleanup_error)
                    _annotate_secondary_error(
                        primary_error,
                        "partially loaded FAISS index cleanup also failed",
                        cleanup_error,
                    )
            raise

    def _finish_loaded_level(
        self,
        view: AuthenticatedVectorView,
        level: str,
        model_suffix: str,
        *,
        level_path: Path,
        faiss_relative: str,
        committed_documents_relative: str | None,
        index: faiss.Index,
    ) -> tuple[faiss.Index, List[_Document]]:
        """Validate documents and metadata for one already-acquired index."""

        index_name = f"index_{model_suffix}"
        if int(index.d) != self.dimension:
            raise ValueError(
                f"FAISS dimension mismatch at {faiss_relative}: "
                f"expected {self.dimension}, found {int(index.d)}"
            )

        # Portable artifacts use inert JSON so a downloaded document store is
        # never unpickled. Local indexes retain the pickle fallback for
        # compatibility with previously built artifacts.
        json_relative = f"{level}/documents_{model_suffix}.json"
        if committed_documents_relative is not None:
            if PurePosixPath(committed_documents_relative).suffix == ".json":
                documents = self._load_documents_json(
                    view,
                    committed_documents_relative,
                )
            else:
                try:
                    with _authenticated_binary_file(
                        view,
                        committed_documents_relative,
                    ) as handle:
                        raw_docs = compat_pickle.load(handle)
                    documents = [_to_document(document) for document in raw_docs]
                except Exception as exc:
                    raise ValueError(
                        "Could not load committed vector documents from "
                        f"{committed_documents_relative}: {exc}"
                    ) from exc
        elif view.has_file(json_relative):
            documents = self._load_documents_json(view, json_relative)
        else:
            documents = None

            # Try loading the local documents pickle (works for both new
            # _Document and legacy LangChain Document objects).
            docs_relative = f"{level}/documents_{model_suffix}.pkl"
            if view.has_file(docs_relative):
                try:
                    with _authenticated_binary_file(view, docs_relative) as source:
                        raw_docs = compat_pickle.load(source)
                    documents = [_to_document(d) for d in raw_docs]
                except Exception as exc:
                    logger.warning(
                        "Could not load documents from %s: %s", docs_relative, exc
                    )

            # Fallback: LangChain stores use index_name.pkl for their docstore.
            lc_pkl_relative = f"{level}/{index_name}.pkl"
            if documents is None and view.has_file(lc_pkl_relative):
                try:
                    documents = self._load_langchain_pkl(view, lc_pkl_relative)
                    logger.info(
                        "Loaded %d documents from legacy LangChain format",
                        len(documents),
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not load legacy LangChain pkl from %s: %s",
                        lc_pkl_relative,
                        exc,
                    )

            if documents is None:
                if int(index.ntotal) == 0:
                    documents = []
                else:
                    raise ValueError(
                        f"No readable document store found for {level_path}"
                    )

        if int(index.ntotal) != len(documents):
            raise ValueError(
                f"Misaligned vector level {level_path}: {int(index.ntotal)} vectors "
                f"for {len(documents)} documents"
            )
        self._validate_loaded_faiss_index(
            index,
            relative=faiss_relative,
            document_count=len(documents),
        )
        level_config_relative = f"{level}/config_{model_suffix}.json"
        if view.has_file(level_config_relative):
            level_config = view.load_json_object(
                level_config_relative,
                label=f"vector {level} config",
            )
            expected_level_config = {
                "embedding_model": self.embedding_model,
                "embedding_provider": self.embedding_provider,
                "embedding_revision": self.embedding_revision,
                "dimension": self.dimension,
                "index_type": self.index_type,
                "index_metric": self.index_metric,
                "level": level,
                "num_documents": len(documents),
            }
            for field, expected in expected_level_config.items():
                if level_config.get(field) != expected:
                    raise ValueError(
                        f"vector {level} config {field} mismatch: "
                        f"expected {expected!r}, found {level_config.get(field)!r}"
                    )
        return index, documents

    def _validate_loaded_faiss_index(
        self,
        index: faiss.Index,
        *,
        relative: str,
        document_count: int,
    ) -> None:
        _validate_faiss_index_contract(
            index,
            relative=relative,
            document_count=document_count,
            expected_dimension=self.dimension,
            expected_metric=self.index_metric,
            expected_index_type=self.index_type,
        )

    @staticmethod
    def _load_documents_json(
        view: AuthenticatedVectorView,
        relative: str,
    ) -> List[_Document]:
        """Load the non-executable portable vector document format."""

        documents: List[_Document] = []
        with view.open_file(relative) as source:
            for index, item in enumerate(
                iter_bounded_json_array(
                    source,
                    label=f"vector documents {relative}",
                )
            ):
                if not isinstance(item, dict) or set(item) != {
                    "page_content",
                    "metadata",
                }:
                    raise ValueError(
                        f"vector document {index} must have canonical shape: "
                        f"{relative}"
                    )
                page_content = item.get("page_content")
                metadata = item.get("metadata")
                if not isinstance(page_content, str) or not isinstance(metadata, dict):
                    raise ValueError(
                        f"vector document {index} has invalid content or metadata: "
                        f"{relative}"
                    )
                documents.append(
                    _Document(page_content=page_content, metadata=dict(metadata))
                )
        return documents

    @staticmethod
    def _load_langchain_pkl(
        view: AuthenticatedVectorView,
        relative: str,
    ) -> List[_Document]:
        """Extract documents from a LangChain FAISS pkl file.

        The pkl file contains ``(InMemoryDocstore, index_to_docstore_id)``
        where ``index_to_docstore_id`` maps integer FAISS indices to
        docstore string IDs.
        """
        with _authenticated_binary_file(view, relative) as source:
            docstore, index_to_docstore_id = compat_pickle.load(source)

        documents: List[_Document] = []
        for i in sorted(index_to_docstore_id.keys()):
            doc_id = index_to_docstore_id[i]
            doc = docstore.search(doc_id)
            if doc and hasattr(doc, "page_content"):
                documents.append(_to_document(doc))
        return documents

    def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the vector store.

        Returns:
            Dictionary with store statistics
        """
        stats = {
            "embedding_model": self.embedding_model,
            "embedding_provider": self.embedding_provider,
            "embedding_revision": self.embedding_revision,
            "dimension": self.dimension,
            "index_type": self.index_type,
            "index_metric": self.index_metric,
            "l0_documents": len(self.l0_documents),
            "l2_documents": len(self.l2_documents),
            "total_documents": len(self.l0_documents) + len(self.l2_documents),
        }

        # Analyze L0 chunk types
        if self.l0_documents:
            l0_chunk_types = {}
            for doc in self.l0_documents:
                chunk_type = doc.metadata.get("chunk_type", "unknown")
                l0_chunk_types[chunk_type] = l0_chunk_types.get(chunk_type, 0) + 1
            stats["l0_chunk_types"] = l0_chunk_types

        # Analyze L2 chunk types
        if self.l2_documents:
            l2_chunk_types = {}
            for doc in self.l2_documents:
                chunk_type = doc.metadata.get("chunk_type", "unknown")
                l2_chunk_types[chunk_type] = l2_chunk_types.get(chunk_type, 0) + 1
            stats["l2_chunk_types"] = l2_chunk_types

        return stats

    def get_embeddings_by_content_hash(
        self, level: Level = "l2"
    ) -> Dict[str, np.ndarray]:
        """
        Extract raw embedding vectors from the FAISS index, keyed by content hash.

        This is used to seed the ``EmbeddingsCache`` after a full build so that
        the first incremental update achieves ~100% cache hit rate for unchanged
        chunks.

        Each document's content is MD5-hashed to produce the key.  If the
        document metadata already contains a ``content_hash`` field it is used
        directly; otherwise the hash is computed on the fly.

        Returns:
            Dict mapping content_hash → np.ndarray (float32 vectors).
        """
        index, documents = self._get_index_and_docs(level)
        if not documents or index is None or index.ntotal == 0:
            return {}

        result: Dict[str, np.ndarray] = {}
        for i, doc in enumerate(documents):
            content_hash = doc.metadata.get("content_hash")
            if content_hash is None:
                content_hash = hashlib.md5(
                    doc.page_content.encode("utf-8", errors="replace")
                ).hexdigest()

            vec = index.reconstruct(i)
            result[content_hash] = np.asarray(vec, dtype=np.float32)

        logger.info(
            "Extracted %d embedding vectors from %s FAISS index for cache seeding.",
            len(result),
            level,
        )
        return result

    def rebuild_from_embeddings(
        self,
        documents: list,
        embeddings: List[np.ndarray],
        level: Level = "l2",
    ) -> None:
        """
        Clear *level* and rebuild its FAISS index from pre-computed embeddings.

        Used by the incremental update path: unchanged chunks contribute their
        cached vectors, so only genuinely new/modified chunks require model
        inference.  No embedding model calls are made by this method.

        Args:
            documents: Document-like objects with ``page_content`` and
                ``metadata`` attributes (``_Document`` or compatible).
            embeddings: Corresponding embedding vectors as ``np.ndarray``
                (shape ``[dim]``, dtype ``float32``).
            level: Which index level to rebuild (``"l0"`` or ``"l2"``).

        Raises:
            ValueError: If *documents* and *embeddings* have different lengths.
        """
        if len(documents) != len(embeddings):
            raise ValueError(
                f"documents ({len(documents)}) and embeddings ({len(embeddings)}) "
                "must have the same length."
            )

        # Wipe the existing index for this level
        self.clear(level)

        if not documents:
            logger.debug(
                "rebuild_from_embeddings: no documents; level %s cleared.", level
            )
            return

        # Convert to _Document if needed and add vectors to the raw FAISS index
        native_docs = [_to_document(d) for d in documents]
        vectors = np.array(
            [
                emb if isinstance(emb, np.ndarray) else np.asarray(emb)
                for emb in embeddings
            ],
            dtype=np.float32,
        )

        self._add_to_index(level, vectors)
        if level == "l0":
            self.l0_documents = native_docs
        else:
            self.l2_documents = native_docs

        logger.info(
            "rebuild_from_embeddings: %s index rebuilt with %d documents.",
            level,
            len(documents),
        )

    def delta_update(
        self,
        all_documents: list,
        all_embeddings: List[np.ndarray],
        changed_content_hashes: Set[str],
        level: Level = "l2",
        threshold: float = 0.1,
    ) -> None:
        """
        Patch a flat FAISS index in place when the change set is small.

        When the fraction of changed chunks is below *threshold*, this uses
        ``IndexFlat.remove_ids`` + ``add`` to modify only the affected rows,
        keeping unchanged vectors and their aligned documents untouched. IVF
        indexes are rebuilt because removing their implicit IDs does not
        compact the remaining labels to match the document array. If the
        change ratio exceeds the threshold (or the index is empty), this also
        falls back to :meth:`rebuild_from_embeddings`.

        Args:
            all_documents: The complete desired set of documents for *level*
                after the update.  Must carry ``content_hash`` in metadata.
            all_embeddings: Corresponding embedding vectors, aligned with
                *all_documents*.
            changed_content_hashes: Content hashes of chunks that were
                added, removed, or modified in this update cycle.  Used both
                to decide between delta/rebuild and to identify stale rows.
            level: Which index level to update.
            threshold: Maximum change ratio (changed/total) for the delta
                path; above this a full rebuild is performed.
        """
        total = len(all_documents)

        if total == 0:
            self.clear(level)
            return

        index, current_docs = self._get_index_and_docs(level)
        change_ratio = len(changed_content_hashes) / total

        # Fall back to full rebuild when the delta path can't help.
        if (
            index is None
            or index.ntotal == 0
            or not current_docs
            or self.index_type != "flat"
            or change_ratio > threshold
        ):
            logger.info(
                "delta_update: %d/%d changed (%.0f%%) → full rebuild of %s.",
                len(changed_content_hashes),
                total,
                change_ratio * 100,
                level,
            )
            self.rebuild_from_embeddings(all_documents, all_embeddings, level=level)
            return

        # --- Delta path: in-place patch -------------------------------
        # Use a list per hash so duplicate-content docs (same code in
        # different files) are all preserved.
        from collections import defaultdict

        target_by_hash: Dict[str, List[Tuple[object, np.ndarray]]] = defaultdict(list)
        for doc, emb in zip(all_documents, all_embeddings, strict=True):
            ch = doc.metadata.get("content_hash")
            if ch is None:
                # Can't align by hash → safest to rebuild.
                logger.warning(
                    "delta_update: target doc missing content_hash → full rebuild."
                )
                self.rebuild_from_embeddings(all_documents, all_embeddings, level=level)
                return
            target_by_hash[ch].append((doc, emb))

        current_hashes = [d.metadata.get("content_hash") for d in current_docs]

        # For each hash, allow at most target-count survivors (handles
        # both duplicate-content additions and removals correctly).
        target_avail: Dict[str, int] = {h: len(v) for h, v in target_by_hash.items()}
        rows_to_remove: List[int] = []
        for i, h in enumerate(current_hashes):
            if (
                h is None
                or h not in target_avail
                or h in changed_content_hashes
                or target_avail[h] <= 0
            ):
                rows_to_remove.append(i)
            else:
                target_avail[h] -= 1

        # Unclaimed target entries become additions.
        docs_to_add: List[Tuple[object, np.ndarray]] = []
        for h, entries in target_by_hash.items():
            claimed = len(entries) - target_avail.get(h, 0)
            docs_to_add.extend(entries[claimed:])

        if rows_to_remove:
            selector = faiss.IDSelectorBatch(np.array(rows_to_remove, dtype=np.int64))
            index.remove_ids(selector)

        # Survivors: prefer the fresh target doc (same content_hash) so that
        # pure metadata changes — file rename, start_line shift, name edit —
        # are reflected without requiring a full rebuild.  The vector is
        # identical because content_hash is identical, so no FAISS op needed.
        remove_set = set(rows_to_remove)
        survivor_idx: Dict[str, int] = {}
        new_docs_list: List[_Document] = []
        for i, d in enumerate(current_docs):
            if i in remove_set:
                continue
            h = current_hashes[i]
            idx = survivor_idx.get(h, 0)
            survivor_idx[h] = idx + 1
            entries = target_by_hash.get(h)
            if entries and idx < len(entries):
                new_docs_list.append(_to_document(entries[idx][0]))
            else:
                new_docs_list.append(d)

        if docs_to_add:
            add_vectors = np.array(
                [np.asarray(e, dtype=np.float32) for _, e in docs_to_add],
                dtype=np.float32,
            )
            self._add_to_index(level, add_vectors)
            new_docs_list.extend(_to_document(d) for d, _ in docs_to_add)

        if level == "l0":
            self.l0_documents = new_docs_list
        else:
            self.l2_documents = new_docs_list

        logger.info(
            "delta_update: %s patched in place — removed %d, added %d "
            "(ntotal=%d, %.0f%% changed).",
            level,
            len(rows_to_remove),
            len(docs_to_add),
            index.ntotal,
            change_ratio * 100,
        )

    def clear(self, level: Optional[Level] = None) -> None:
        """
        Clear data from the vector store.

        Args:
            level: If specified, only clear that level ("l0" or "l2").
                   If None, clear both levels.
        """
        if level is None or level == "l0":
            logger.info("Clearing L0 vector store")
            self.l0_index = self._build_faiss_index()
            self.l0_documents = []

        if level is None or level == "l2":
            logger.info("Clearing L2 vector store")
            self.l2_index = self._build_faiss_index()
            self.l2_documents = []

        logger.info("Vector store cleared")


def create_code_vector_store(
    embedding_model: str = "text-embedding-ada-002",
    embedding_provider: str = "openai",
    store_path: Optional[str] = None,
    **kwargs,
) -> CodeVectorStore:
    """
    Factory function to create a CodeVectorStore.

    Args:
        embedding_model: Name of the embedding model
        embedding_provider: Provider for embeddings
        store_path: Path to store/load the vector store
        **kwargs: Additional arguments for CodeVectorStore

    Returns:
        CodeVectorStore instance
    """
    return CodeVectorStore(
        embedding_model=embedding_model,
        embedding_provider=embedding_provider,
        store_path=store_path,
        **kwargs,
    )
