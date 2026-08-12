# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import hashlib
import inspect
import io
import json
import os
import shutil
import stat
import struct
import sys
import tracemalloc
import warnings
import zipfile
from dataclasses import fields, replace
from pathlib import Path
from typing import Callable, Iterable

import pytest

import codenib._atomic_directory as atomic_module
import codenib.storage.view_bundle as bundle_module
from codenib.storage import (
    VIEW_BUNDLE_MANIFEST,
    VIEW_BUNDLE_MEDIA_TYPE,
    VIEW_BUNDLE_SCHEMA,
    LocalCAS,
    StorageIntegrityError,
    StorageValidationError,
    build_view_bundle,
    materialize_view_bundle,
    validate_view_bundle_physical_size,
    verify_view_bundle,
)
from codenib.storage.models import canonical_json
from codenib.storage.view_bundle import (
    consume_planned_view_bundle,
    plan_view_bundle_reader,
)


def _exception_notes(error: BaseException) -> tuple[str, ...]:
    return (
        *tuple(getattr(error, "__notes__", ())),
        *tuple(getattr(error, "_codenib_cleanup_notes", ())),
    )


def _source(root: Path) -> Path:
    source = root / "view"
    source.mkdir(parents=True)
    (source / "documents.json").write_text("[]\n", encoding="utf-8")
    nested = source / "index"
    nested.mkdir()
    executable = nested / "shard"
    executable.write_bytes(b"immutable shard")
    executable.chmod(0o751)
    return source


def test_public_physical_size_gate_rejects_before_archive_access() -> None:
    validate_view_bundle_physical_size(
        1,
        max_files=1,
        max_bytes=1,
        max_metadata_bytes=1,
    )
    with pytest.raises(StorageValidationError, match="physical limit"):
        validate_view_bundle_physical_size(
            2 * 1024 * 1024,
            max_files=1,
            max_bytes=1,
            max_metadata_bytes=1,
        )


@pytest.mark.parametrize("byte_size", [None, True, -1, 1.5, "1"])
def test_public_physical_size_gate_rejects_malformed_size(byte_size: object) -> None:
    with pytest.raises(StorageValidationError, match="nonnegative integer"):
        validate_view_bundle_physical_size(
            byte_size,  # type: ignore[arg-type]
            max_files=1,
            max_bytes=1,
            max_metadata_bytes=1,
        )


def test_public_physical_size_gate_rejects_integer_subclass_before_comparison() -> None:
    class ForgedSize(int):
        def __lt__(self, other: object) -> bool:
            raise AssertionError("integer subclass comparison must not run")

        def __gt__(self, other: object) -> bool:
            raise AssertionError("integer subclass comparison must not run")

    with pytest.raises(StorageValidationError, match="nonnegative integer"):
        validate_view_bundle_physical_size(
            ForgedSize(2 * 1024 * 1024),
            max_files=1,
            max_bytes=1,
            max_metadata_bytes=1,
        )


def _moved_publication_root(destination: Path) -> Path:
    matches = tuple(
        path
        for path in destination.parent.iterdir()
        if path.name.startswith(f".{destination.name}.previous-")
    )
    assert len(matches) == 1
    return matches[0]


def _canonical_info(
    name: str,
    mode: int = 0o644,
    *,
    guarded: bool = False,
) -> zipfile.ZipInfo:
    info = bundle_module._canonical_zip_info(name, mode)
    if guarded:
        info.extra = bundle_module._ZIP_GUARD_EXTRA
    return info


def _manifest(
    files: list[dict[str, object]],
    *,
    view_type: str = "bm25",
) -> bytes:
    return canonical_json(
        {
            "schema": VIEW_BUNDLE_SCHEMA,
            "view_type": view_type,
            "files": files,
        }
    ).encode("utf-8")


def _record(path: str, payload: bytes, *, mode: int = 0o644) -> dict[str, object]:
    return {
        "path": path,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mode": mode,
    }


def _write_custom_bundle(
    archive_path: Path,
    files: list[tuple[str, bytes, int]],
    *,
    manifest_files: list[dict[str, object]] | None = None,
    compression: int = zipfile.ZIP_STORED,
    extra_members: tuple[tuple[zipfile.ZipInfo, bytes], ...] = (),
) -> None:
    records = manifest_files or [
        _record(name.removeprefix("payload/"), payload, mode=mode)
        for name, payload, mode in files
    ]
    with zipfile.ZipFile(archive_path, "w", compression=compression) as archive:
        archive.writestr(_canonical_info(VIEW_BUNDLE_MANIFEST), _manifest(records))
        for index, (name, payload, mode) in enumerate(files):
            info = _canonical_info(
                name,
                mode,
                guarded=index == len(files) - 1 and not extra_members,
            )
            info.compress_type = compression
            archive.writestr(info, payload)
        for index, (info, payload) in enumerate(extra_members):
            if index == len(extra_members) - 1 and not info.extra.endswith(
                bundle_module._ZIP_GUARD_EXTRA
            ):
                info.extra += bundle_module._ZIP_GUARD_EXTRA
            archive.writestr(info, payload)


def _write_raw_manifest_bundle(
    archive_path: Path,
    manifest_bytes: bytes,
    *,
    payload: bytes = b"x",
) -> None:
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(_canonical_info("bundle.json"), manifest_bytes)
        archive.writestr(_canonical_info("payload/a", guarded=True), payload)


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _local_extra(archive_path: Path, info: zipfile.ZipInfo) -> bytes:
    with archive_path.open("rb") as handle:
        handle.seek(info.header_offset)
        header = handle.read(30)
        name_size, extra_size = struct.unpack_from("<HH", header, 26)
        handle.seek(name_size, os.SEEK_CUR)
        return handle.read(extra_size)


def _source_file_descriptor_count(path: Path) -> int:
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        return 0
    expected = str(path.resolve())
    count = 0
    for descriptor in descriptor_root.iterdir():
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        if target == expected:
            count += 1
    return count


def _consume_until_payload(chunks: object, payload: bytes) -> None:
    iterator = iter(chunks)  # type: ignore[arg-type]
    for chunk in iterator:
        if chunk == payload:
            return
    raise AssertionError("planned bundle stream did not yield its payload")


def _call_with_interrupt_at_ordered_action_call(
    callback: Callable[[], object],
    *,
    label: str,
    error: BaseException,
) -> None:
    function = atomic_module._attempt_ordered_action
    code = function.__code__
    source, first_line = inspect.getsourcelines(function)
    action_lines = {
        first_line + offset
        for offset, line in enumerate(source)
        if "ordered.action()" in line
    }
    assert len(action_lines) == 1
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            ordered = frame.f_locals.get("ordered")
            if getattr(ordered, "label", None) == label:
                frame.f_trace_lines = True
                return trace
        if (
            not injected
            and frame.f_code is code
            and event == "line"
            and frame.f_lineno in action_lines
            and getattr(frame.f_locals.get("ordered"), "label", None) == label
        ):
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, "failed to inject at the ordered bundle action call"


def test_reader_plan_matches_canonical_builder_without_path_or_temp_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    unicode_file = source / "café.json"
    unicode_file.write_bytes(b'{"ok":true}\n')
    expected = build_view_bundle(
        source,
        tmp_path / "expected.bundle",
        view_type="bm25",
    )
    expected_bytes = expected.path.read_bytes()
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)

    def reject_path_or_temp(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("reader-native bundle planning used a path/temp helper")

    monkeypatch.setattr(bundle_module, "_open_stable_regular_file", reject_path_or_temp)
    monkeypatch.setattr(bundle_module.tempfile, "mkstemp", reject_path_or_temp)
    monkeypatch.setattr(bundle_module.tempfile, "mkdtemp", reject_path_or_temp)
    observed_chunks: list[bytes] = []

    def consume(reader: atomic_module.PublicationDirectoryReader) -> object:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )
        consumed = consume_planned_view_bundle(
            reader,
            plan,
            lambda chunks: observed_chunks.extend(chunks),
        )
        assert consumed is None
        return plan

    plan = atomic_module.reopen_authenticated_directory(
        artifact_root,
        outer_ownership,
        consume,
    )
    observed = b"".join(observed_chunks)
    assert plan.source_ownership == source_ownership
    assert plan.members == expected.members
    assert plan.byte_size == expected.byte_size == len(observed)
    assert plan.digest == expected.digest == hashlib.sha256(observed).hexdigest()
    assert (
        plan.digest
        == "ab54fd4c3a4f191b93c60cb1035758b88680228551975833a9dcee18333283e4"
    )
    assert plan.byte_size == 965
    assert observed == expected_bytes


def test_reader_plan_rejects_wrong_exact_subtree_ownership(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    other = artifact_root / "other"
    shutil.copytree(source, other)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    wrong_ownership = atomic_module.capture_directory_ownership(other)

    with pytest.raises(RuntimeError, match="subtree root"):
        atomic_module.reopen_authenticated_directory(
            artifact_root,
            outer_ownership,
            lambda reader: plan_view_bundle_reader(
                reader,
                "view",
                wrong_ownership,
                view_type="bm25",
            ),
        )


def test_reader_plan_rejects_subclassed_subtree_ownership(tmp_path: Path) -> None:
    class ForgedOwnership(atomic_module._TreeOwnership):
        pass

    artifact_root = tmp_path / "artifacts"
    _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    forged = object.__new__(ForgedOwnership)

    with pytest.raises(TypeError, match="exact token"):
        atomic_module.reopen_authenticated_directory(
            artifact_root,
            outer_ownership,
            lambda reader: plan_view_bundle_reader(
                reader,
                "view",
                forged,
                view_type="bm25",
            ),
        )


def test_reader_plan_cannot_cross_publication_callbacks(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    plan = atomic_module.reopen_authenticated_directory(
        artifact_root,
        outer_ownership,
        lambda reader: plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        ),
    )

    with pytest.raises(RuntimeError, match="planning reader callback"):
        atomic_module.reopen_authenticated_directory(
            artifact_root,
            outer_ownership,
            lambda reader: consume_planned_view_bundle(
                reader,
                plan,
                lambda chunks: tuple(chunks),
            ),
        )


def test_planned_chunk_iterable_is_drained_then_revoked(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    escaped: list[object] = []

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )
        result = consume_planned_view_bundle(
            reader,
            plan,
            lambda chunks: escaped.append(chunks) or "deduplicated",
        )
        assert result == "deduplicated"

    atomic_module.reopen_authenticated_directory(
        artifact_root,
        outer_ownership,
        consume,
    )
    assert len(escaped) == 1
    with pytest.raises(RuntimeError, match="no longer active"):
        iter(escaped[0])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "label",
    [
        "planned view bundle stream close also failed",
        "planned view bundle stream revocation also failed",
    ],
)
def test_planned_stream_cleanup_retries_real_pre_call_cancellation(
    tmp_path: Path,
    label: str,
) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    escaped: list[object] = []
    interruption = KeyboardInterrupt(f"{label} cancellation")

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )
        consume_planned_view_bundle(
            reader,
            plan,
            lambda chunks: escaped.append(chunks),
        )

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_at_ordered_action_call(
            lambda: atomic_module.reopen_authenticated_directory(
                artifact_root,
                outer_ownership,
                consume,
            ),
            label=label,
            error=interruption,
        )
    assert caught.value is interruption
    assert len(escaped) == 1
    with pytest.raises(RuntimeError, match="no longer active"):
        iter(escaped[0])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "label",
    [
        "planned view bundle hashing stream close also failed",
        "planned view bundle hashing stream revocation also failed",
    ],
)
def test_planning_stream_cleanup_retries_real_pre_call_cancellation(
    tmp_path: Path,
    label: str,
) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    interruption = KeyboardInterrupt(f"{label} cancellation")

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_at_ordered_action_call(
            lambda: atomic_module.reopen_authenticated_directory(
                artifact_root,
                outer_ownership,
                lambda reader: plan_view_bundle_reader(
                    reader,
                    "view",
                    source_ownership,
                    view_type="bm25",
                ),
            ),
            label=label,
            error=interruption,
        )
    assert caught.value is interruption
    assert _source_file_descriptor_count(source / "documents.json") == 0


@pytest.mark.parametrize(
    ("error_type", "after_real_revoke"),
    [
        (KeyboardInterrupt, False),
        (KeyboardInterrupt, True),
        (SystemExit, False),
        (SystemExit, True),
    ],
)
def test_planned_stream_revocation_reconciles_before_and_after_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
    after_real_revoke: bool,
) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    real_revoke = bundle_module._revoke_bundle_stream
    escaped: list[object] = []
    attempts = 0

    def interrupted_revoke(stream: object) -> None:
        nonlocal attempts
        if stream._expected_digest is None:  # type: ignore[attr-defined]
            real_revoke(stream)  # type: ignore[arg-type]
            return
        attempts += 1
        if attempts == 1:
            if after_real_revoke:
                real_revoke(stream)  # type: ignore[arg-type]
            raise error_type("injected stream revocation interruption")
        real_revoke(stream)  # type: ignore[arg-type]

    monkeypatch.setattr(bundle_module, "_revoke_bundle_stream", interrupted_revoke)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )
        consume_planned_view_bundle(
            reader,
            plan,
            lambda chunks: escaped.append(chunks),
        )

    with pytest.raises(error_type, match="injected stream revocation interruption"):
        atomic_module.reopen_authenticated_directory(
            artifact_root,
            outer_ownership,
            consume,
        )
    assert attempts >= 1
    assert len(escaped) == 1
    with pytest.raises(RuntimeError, match="no longer active"):
        iter(escaped[0])  # type: ignore[arg-type]


def test_planned_consumer_drains_after_normal_partial_read(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    first_chunks: list[bytes] = []

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )

        def read_one(chunks: object) -> str:
            first_chunks.append(next(iter(chunks)))  # type: ignore[arg-type]
            return "partial"

        assert consume_planned_view_bundle(reader, plan, read_one) == "partial"

    atomic_module.reopen_authenticated_directory(
        artifact_root,
        outer_ownership,
        consume,
    )
    assert len(first_chunks) == 1


def test_planned_consumer_composes_with_cas_new_write_and_dedup(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    store = LocalCAS.provision(tmp_path / "cas")

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )

        def put(chunks: object) -> object:
            return store.put_chunks(  # type: ignore[arg-type]
                chunks,
                plan.digest,
                plan.byte_size,
            )

        first = consume_planned_view_bundle(reader, plan, put)
        second = consume_planned_view_bundle(reader, plan, put)
        assert first == second
        assert store.verify_receipt(first) == first

    atomic_module.reopen_authenticated_directory(
        artifact_root,
        outer_ownership,
        consume,
    )


def test_planned_consumer_preserves_primary_and_runs_namespace_sandwich(
    tmp_path: Path,
) -> None:
    class ConsumerFailure(RuntimeError):
        pass

    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    documents = source / "documents.json"
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )

        def fail_after_open(chunks: object) -> None:
            _consume_until_payload(chunks, b"[]\n")
            documents.write_bytes(b"changed")
            raise ConsumerFailure("consumer primary")

        consume_planned_view_bundle(reader, plan, fail_after_open)

    with pytest.raises(ConsumerFailure, match="consumer primary") as captured:
        atomic_module.reopen_authenticated_directory(
            artifact_root,
            outer_ownership,
            consume,
        )
    notes = "\n".join(_exception_notes(captured.value))
    assert "completeness validation also failed" in notes
    assert "namespace validation also failed" in notes


@pytest.mark.parametrize(
    "primary_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_planned_consumer_failure_aborts_without_another_source_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[BaseException],
) -> None:
    artifact_root = tmp_path / "artifacts"
    source = artifact_root / "view"
    source.mkdir(parents=True)
    payload = b"x" * (2 * bundle_module._COPY_CHUNK_BYTES + 7)
    large_file = source / "large.bin"
    large_file.write_bytes(payload)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    real_init = atomic_module.PublicationAuthenticatedFile.__init__
    count_authenticated_reads = False
    consumer_read_requests: list[int] = []

    def counting_init(
        authenticated: atomic_module.PublicationAuthenticatedFile,
        *,
        path: str,
        mode: int,
        size: int,
        read_callback: Callable[[int], bytes],
        verify_callback: Callable[[], None],
    ) -> None:
        if count_authenticated_reads:
            backend_read = read_callback

            def counted_read(requested: int) -> bytes:
                consumer_read_requests.append(requested)
                return backend_read(requested)

            read_callback = counted_read
        real_init(
            authenticated,
            path=path,
            mode=mode,
            size=size,
            read_callback=read_callback,
            verify_callback=verify_callback,
        )

    monkeypatch.setattr(
        atomic_module.PublicationAuthenticatedFile,
        "__init__",
        counting_init,
    )

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        nonlocal count_authenticated_reads
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )
        count_authenticated_reads = True

        def fail_after_first_payload_chunk(chunks: Iterable[bytes]) -> None:
            for chunk in chunks:
                if chunk == payload[: bundle_module._COPY_CHUNK_BYTES]:
                    assert consumer_read_requests == [bundle_module._COPY_CHUNK_BYTES]
                    raise primary_type("consumer primary")
            raise AssertionError("large source payload was not consumed")

        consume_planned_view_bundle(
            reader,
            plan,
            fail_after_first_payload_chunk,
        )

    with pytest.raises(primary_type, match="consumer primary") as captured:
        atomic_module.reopen_authenticated_directory(
            artifact_root,
            outer_ownership,
            consume,
        )
    assert consumer_read_requests == [bundle_module._COPY_CHUNK_BYTES]
    assert _source_file_descriptor_count(large_file) == 0
    assert "reader validity validation also failed" in "\n".join(
        _exception_notes(captured.value)
    )


@pytest.mark.parametrize(
    ("injected", "after_real_close"),
    [
        (KeyboardInterrupt("before close"), False),
        (SystemExit("after close"), True),
        (OSError(errno.EIO, "before close"), False),
    ],
)
def test_planned_partial_stream_close_reconciles_interruption_without_fd_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injected: BaseException,
    after_real_close: bool,
) -> None:
    class ConsumerFailure(RuntimeError):
        pass

    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    documents = source / "documents.json"
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    real_close = bundle_module._close_bundle_iterator
    attempts = 0

    def interrupted_close(iterator: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if after_real_close:
                real_close(iterator)  # type: ignore[arg-type]
            raise injected
        real_close(iterator)  # type: ignore[arg-type]

    monkeypatch.setattr(bundle_module, "_close_bundle_iterator", interrupted_close)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )

        def fail_partial(chunks: object) -> None:
            _consume_until_payload(chunks, b"[]\n")
            assert _source_file_descriptor_count(documents) == 1
            raise ConsumerFailure("consumer primary")

        consume_planned_view_bundle(reader, plan, fail_partial)

    with pytest.raises(ConsumerFailure, match="consumer primary") as captured:
        atomic_module.reopen_authenticated_directory(
            artifact_root,
            outer_ownership,
            consume,
        )
    assert attempts >= 1
    assert _source_file_descriptor_count(documents) == 0
    assert injected.__class__.__name__ in "\n".join(_exception_notes(captured.value))


def test_persistent_stream_close_keeps_retry_owner_on_actual_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConsumerFailure(RuntimeError):
        pass

    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    documents = source / "documents.json"
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    real_close = bundle_module._close_bundle_iterator

    def persistent_failure(_iterator: object) -> None:
        raise OSError(errno.EIO, "persistent close failure")

    monkeypatch.setattr(bundle_module, "_close_bundle_iterator", persistent_failure)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )

        def fail_partial(chunks: object) -> None:
            _consume_until_payload(chunks, b"[]\n")
            raise ConsumerFailure("consumer primary")

        try:
            consume_planned_view_bundle(reader, plan, fail_partial)
        except ConsumerFailure as error:
            owners = getattr(
                error,
                "_codenib_bundle_stream_cleanup_owners",
                (),
            )
            assert len(owners) == 1
            assert _source_file_descriptor_count(documents) == 1
            monkeypatch.setattr(bundle_module, "_close_bundle_iterator", real_close)
            owners[0].retry_cleanup()
            assert _source_file_descriptor_count(documents) == 0
            raise

    with pytest.raises(ConsumerFailure, match="consumer primary") as captured:
        atomic_module.reopen_authenticated_directory(
            artifact_root,
            outer_ownership,
            consume,
        )
    owners = getattr(
        captured.value,
        "_codenib_bundle_stream_cleanup_owners",
        (),
    )
    assert len(owners) == 1
    assert _source_file_descriptor_count(documents) == 0


@pytest.mark.parametrize(
    ("injected", "after_real_close"),
    [
        (KeyboardInterrupt("planning close before real"), False),
        (SystemExit("planning close after real"), True),
        (OSError(errno.EIO, "planning close before real"), False),
    ],
)
def test_planning_hash_stream_preserves_iteration_primary_and_reconciles_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injected: BaseException,
    after_real_close: bool,
) -> None:
    class PlanningIterationFailure(RuntimeError):
        pass

    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    documents = source / "documents.json"
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    real_next = bundle_module._CallbackScopedBundleChunks.__next__
    real_close = bundle_module._close_bundle_iterator
    interrupted_iteration = False
    close_attempts = 0

    def interrupt_during_payload(stream: object) -> bytes:
        nonlocal interrupted_iteration
        chunk = real_next(stream)  # type: ignore[arg-type]
        if not interrupted_iteration and chunk == b"[]\n":
            interrupted_iteration = True
            assert _source_file_descriptor_count(documents) == 1
            raise PlanningIterationFailure("planning iteration primary")
        return chunk

    def interrupted_close(iterator: object) -> None:
        nonlocal close_attempts
        close_attempts += 1
        if close_attempts == 1:
            if after_real_close:
                real_close(iterator)  # type: ignore[arg-type]
            raise injected
        real_close(iterator)  # type: ignore[arg-type]

    monkeypatch.setattr(
        bundle_module._CallbackScopedBundleChunks,
        "__next__",
        interrupt_during_payload,
    )
    monkeypatch.setattr(bundle_module, "_close_bundle_iterator", interrupted_close)

    with pytest.raises(
        PlanningIterationFailure,
        match="planning iteration primary",
    ) as captured:
        atomic_module.reopen_authenticated_directory(
            artifact_root,
            outer_ownership,
            lambda reader: plan_view_bundle_reader(
                reader,
                "view",
                source_ownership,
                view_type="bm25",
            ),
        )
    assert interrupted_iteration
    assert close_attempts >= 1
    assert _source_file_descriptor_count(documents) == 0
    assert injected.__class__.__name__ in "\n".join(_exception_notes(captured.value))


def test_planned_bundle_rejects_forged_public_identity(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )
        forged = replace(plan, digest="0" * 64)
        with pytest.raises(StorageIntegrityError, match="authority receipt"):
            consume_planned_view_bundle(
                reader,
                forged,
                lambda chunks: tuple(chunks),
            )

    atomic_module.reopen_authenticated_directory(
        artifact_root,
        outer_ownership,
        consume,
    )


def test_planned_bundle_rejects_subclass_and_hostile_scalar_forgery(
    tmp_path: Path,
) -> None:
    class ForgedPlan(bundle_module.PlannedViewBundle):
        pass

    class ForgedText(str):
        def __eq__(self, _other: object) -> bool:
            return True

    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )
        forged = object.__new__(ForgedPlan)
        for field in fields(bundle_module.PlannedViewBundle):
            object.__setattr__(forged, field.name, getattr(plan, field.name))
        with pytest.raises(TypeError, match="exact plan type"):
            consume_planned_view_bundle(reader, forged, lambda chunks: tuple(chunks))

        hostile = replace(plan, digest=ForgedText(plan.digest))
        with pytest.raises(StorageValidationError, match="text fields"):
            consume_planned_view_bundle(reader, hostile, lambda chunks: tuple(chunks))

    atomic_module.reopen_authenticated_directory(
        artifact_root,
        outer_ownership,
        consume,
    )


def test_reader_bundle_rejects_reader_subclass_and_private_layout_forgery(
    tmp_path: Path,
) -> None:
    class ForgedReader(atomic_module.PublicationDirectoryReader):
        pass

    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    forged_reader = object.__new__(ForgedReader)
    with pytest.raises(TypeError, match="publication reader"):
        plan_view_bundle_reader(
            forged_reader,
            "view",
            source_ownership,
            view_type="bm25",
        )

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )
        first = plan._receipt.zip_members[0]
        forged_member = replace(first, encoded_name=b"forged.json")
        forged_receipt = replace(
            plan._receipt,
            zip_members=(forged_member, *plan._receipt.zip_members[1:]),
        )
        forged_plan = replace(plan, _receipt=forged_receipt)
        with pytest.raises(StorageIntegrityError, match="canonical layout"):
            consume_planned_view_bundle(
                reader,
                forged_plan,
                lambda chunks: tuple(chunks),
            )

    atomic_module.reopen_authenticated_directory(
        artifact_root,
        outer_ownership,
        consume,
    )


def test_build_verify_and_materialize_round_trip(tmp_path: Path) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "objects" / "bm25.bundle"

    built = build_view_bundle(source, archive, view_type="bm25")
    verified = verify_view_bundle(
        archive,
        expected_view_type="bm25",
        expected_digest=built.digest,
        expected_size=built.byte_size,
    )
    materialized = materialize_view_bundle(
        archive,
        tmp_path / "runtime" / "bm25",
        expected_digest=built.digest,
    )

    assert built == verified
    assert VIEW_BUNDLE_MEDIA_TYPE == "application/vnd.codenib.view-bundle.v1+zip"
    assert [member.path for member in built.members] == [
        "documents.json",
        "index/shard",
    ]
    assert [member.mode for member in built.members] == [0o644, 0o755]
    assert materialized.payload_dir == materialized.output_dir / "payload"
    assert (
        atomic_module.capture_directory_ownership(materialized.output_dir)
        == materialized.ownership
    )
    assert _tree(materialized.payload_dir) == {
        "documents.json": (b"[]\n", 0o644),
        "index/shard": (b"immutable shard", 0o755),
    }
    metadata = json.loads(
        (materialized.output_dir / VIEW_BUNDLE_MANIFEST).read_text(encoding="utf-8")
    )
    assert metadata["schema"] == VIEW_BUNDLE_SCHEMA
    assert (
        stat.S_IMODE((materialized.output_dir / VIEW_BUNDLE_MANIFEST).stat().st_mode)
        == 0o644
    )


def test_bundle_bytes_are_deterministic_across_metadata_and_creation_order(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "b").write_bytes(b"b")
    (first / "a").write_bytes(b"a")
    (second / "a").write_bytes(b"a")
    (second / "b").write_bytes(b"b")
    (first / "a").chmod(0o600)
    (second / "a").chmod(0o644)
    os.utime(first / "a", (1, 1))
    os.utime(second / "a", (2, 2))

    left = build_view_bundle(first, tmp_path / "left.zip", view_type="vector")
    right = build_view_bundle(second, tmp_path / "right.zip", view_type="vector")

    assert left.digest == right.digest
    assert (tmp_path / "left.zip").read_bytes() == (tmp_path / "right.zip").read_bytes()
    with zipfile.ZipFile(tmp_path / "left.zip") as archive:
        infos = archive.infolist()
        assert [info.filename for info in infos] == [
            "bundle.json",
            "payload/a",
            "payload/b",
        ]
        assert all(info.compress_type == zipfile.ZIP_STORED for info in infos)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
        assert [info.extra for info in infos] == [
            b"",
            b"",
            bundle_module._ZIP_GUARD_EXTRA,
        ]
        assert _local_extra(tmp_path / "left.zip", infos[-1]) == (
            bundle_module._ZIP_GUARD_EXTRA
        )
        assert archive.read("bundle.json") == canonical_json(
            json.loads(archive.read("bundle.json"))
        ).encode("utf-8")


def test_canonical_zip64_records_are_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "view"
    source.mkdir()
    (source / "a").write_bytes(b"payload")
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1)
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
    monkeypatch.setattr(bundle_module, "_ZIP64_LIMIT", 1)
    monkeypatch.setattr(bundle_module, "_ZIP_FILECOUNT_LIMIT", 1)

    archive = tmp_path / "zip64.bundle"
    built = build_view_bundle(source, archive, view_type="bm25")

    assert verify_view_bundle(archive) == built
    with zipfile.ZipFile(archive) as opened:
        infos = opened.infolist()
        assert infos[0].create_version == 45
        assert infos[-1].extra[:2] == b"\x01\x00"
        assert infos[-1].extra.endswith(bundle_module._ZIP_GUARD_EXTRA)
        local_extra = _local_extra(archive, infos[-1])
        assert local_extra[:2] == b"\x01\x00"
        assert local_extra.endswith(bundle_module._ZIP_GUARD_EXTRA)


def test_reader_native_zip64_layout_matches_canonical_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    source = _source(artifact_root)
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1)
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
    monkeypatch.setattr(bundle_module, "_ZIP64_LIMIT", 1)
    monkeypatch.setattr(bundle_module, "_ZIP_FILECOUNT_LIMIT", 1)
    expected = build_view_bundle(
        source,
        tmp_path / "expected-zip64.bundle",
        view_type="bm25",
    )
    outer_ownership = atomic_module.capture_directory_ownership(artifact_root)
    source_ownership = atomic_module.capture_directory_ownership(source)
    observed_chunks: list[bytes] = []

    def consume(reader: atomic_module.PublicationDirectoryReader) -> object:
        plan = plan_view_bundle_reader(
            reader,
            "view",
            source_ownership,
            view_type="bm25",
        )
        consume_planned_view_bundle(
            reader,
            plan,
            lambda chunks: observed_chunks.extend(chunks),
        )
        return plan

    plan = atomic_module.reopen_authenticated_directory(
        artifact_root,
        outer_ownership,
        consume,
    )
    observed = b"".join(observed_chunks)
    assert observed == expected.path.read_bytes()
    assert plan.digest == expected.digest == hashlib.sha256(observed).hexdigest()
    assert plan.byte_size == expected.byte_size == len(observed)
    envelope = bundle_module._read_zip_envelope(
        io.BytesIO(observed),
        max_files=bundle_module.DEFAULT_MAX_BUNDLE_FILES,
        max_metadata_bytes=bundle_module.DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    )
    assert envelope.zip64
    with zipfile.ZipFile(io.BytesIO(observed)) as opened:
        infos = opened.infolist()
        assert all(info.create_version == 45 for info in infos)
        assert infos[-1].extra[:2] == b"\x01\x00"
        assert infos[-1].extra.endswith(bundle_module._ZIP_GUARD_EXTRA)


def test_zip64_central_budget_accepts_maximal_portable_member_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative = "/".join(["a" * 200] * 20)
    payload = b"xx"
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1)
    monkeypatch.setattr(bundle_module, "_ZIP64_LIMIT", 1)
    archive = tmp_path / "long-zip64.bundle"

    _write_custom_bundle(
        archive,
        [(f"payload/{relative}", payload, 0o644)],
    )

    verified = verify_view_bundle(archive)
    assert verified.members[0].path == relative


def test_false_zip64_locator_signature_is_rejected_by_guard_before_zipfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 16-byte final name puts the four-byte local-header offset exactly 20
    # bytes before the EOCD.  Its legitimate value may spell PK\x06\x07.
    archive_path = tmp_path / "classic.zip"
    payload_name = "payload/aaaaaaaa"
    payload = b"x"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            _canonical_info(VIEW_BUNDLE_MANIFEST),
            _manifest([_record("aaaaaaaa", payload)]),
        )
        archive.writestr(_canonical_info(payload_name), payload)
    raw = bytearray(archive_path.read_bytes())
    assert raw[-42:-38] != b"PK\x06\x07"
    raw[-42:-38] = b"PK\x06\x07"
    assert struct.unpack_from("<L", raw, len(raw) - 42)[0] == 0x07064B50
    archive_path.write_bytes(raw)

    def should_not_parse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ZipFile parsed an archive without the terminal guard")

    monkeypatch.setattr(bundle_module.zipfile, "ZipFile", should_not_parse)
    with pytest.raises(StorageValidationError, match="terminal guard"):
        verify_view_bundle(archive_path)


def test_central_directory_budget_is_checked_before_zipfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "oversized-central.zip"
    _write_custom_bundle(archive_path, [("payload/a", b"x", 0o644)])
    raw = bytearray(archive_path.read_bytes())
    old_central_size = struct.unpack_from("<L", raw, len(raw) - 10)[0]
    padding = b"x" * 1024
    raw = raw[:-22] + padding + raw[-22:]
    struct.pack_into(
        "<L",
        raw,
        len(raw) - 10,
        old_central_size + len(padding),
    )
    archive_path.write_bytes(raw)

    def should_not_parse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ZipFile parsed an over-budget central directory")

    monkeypatch.setattr(bundle_module.zipfile, "ZipFile", should_not_parse)
    with pytest.raises(StorageValidationError, match="directory exceeds"):
        verify_view_bundle(archive_path, max_metadata_bytes=1)


@pytest.mark.parametrize(
    "relative",
    [
        "AUX.txt",
        "CONIN$.txt",
        "conout$",
        "COM¹.log",
        "lpt¹.bin",
        "com²",
        "lpt²",
        "COM³",
        "LPT³.txt",
        "name. ",
        "bad:name",
        "control\nname",
        "decomposed-e\u0301",
    ],
)
def test_build_rejects_nonportable_member_names(
    tmp_path: Path,
    relative: str,
) -> None:
    source = tmp_path / "view"
    source.mkdir()
    (source / relative).write_bytes(b"x")

    with pytest.raises(StorageValidationError, match="not portable|NFC"):
        build_view_bundle(source, tmp_path / "bundle.zip", view_type="bm25")

    malicious = tmp_path / "malicious.zip"
    _write_custom_bundle(
        malicious,
        [(f"payload/{relative}", b"x", 0o644)],
    )
    with pytest.raises(StorageValidationError, match="not portable|NFC"):
        verify_view_bundle(malicious)


def test_build_rejects_casefold_collision(tmp_path: Path) -> None:
    source = tmp_path / "view"
    source.mkdir()
    (source / "Name").write_bytes(b"one")
    (source / "name").write_bytes(b"two")

    with pytest.raises(StorageValidationError, match="collision"):
        build_view_bundle(source, tmp_path / "bundle.zip", view_type="bm25")

    (source / "Name").unlink()
    (source / "name").unlink()
    (source / "Dir").mkdir()
    (source / "dir").mkdir()
    (source / "Dir" / "x").write_bytes(b"one")
    (source / "dir" / "y").write_bytes(b"two")
    with pytest.raises(StorageValidationError, match="case-folding collision"):
        build_view_bundle(source, tmp_path / "directories.zip", view_type="bm25")


def test_build_rejects_empty_link_special_and_overlapping_sources(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(StorageValidationError, match="at least one"):
        build_view_bundle(empty, tmp_path / "empty.zip", view_type="bm25")

    source = _source(tmp_path)
    (source / "link").symlink_to(source / "documents.json")
    with pytest.raises(StorageValidationError, match="symbolic link"):
        build_view_bundle(source, tmp_path / "linked.zip", view_type="bm25")
    (source / "link").unlink()

    if hasattr(os, "mkfifo"):
        os.mkfifo(source / "fifo")
        with pytest.raises(StorageValidationError, match="special file"):
            build_view_bundle(source, tmp_path / "fifo.zip", view_type="bm25")
        (source / "fifo").unlink()

    with pytest.raises(StorageValidationError, match="overlap"):
        build_view_bundle(source, source / "self.zip", view_type="bm25")
    with pytest.raises(StorageValidationError, match="forbidden root"):
        build_view_bundle(
            source,
            tmp_path / "publish" / "bundle.zip",
            view_type="bm25",
            forbidden_roots=[tmp_path / "publish"],
        )


def test_build_bounds_empty_directory_fanout_and_path_depth(tmp_path: Path) -> None:
    fanout = tmp_path / "fanout"
    fanout.mkdir()
    for number in range(4):
        (fanout / str(number)).mkdir()
    with pytest.raises(StorageValidationError, match="directories"):
        build_view_bundle(
            fanout,
            tmp_path / "fanout.zip",
            view_type="bm25",
            max_files=2,
        )

    deep = tmp_path / "deep"
    deep.mkdir()
    current = deep
    for _number in range(bundle_module._MAX_MEMBER_DEPTH + 1):
        current = current / "d"
        current.mkdir()
    (current / "value").write_bytes(b"x")
    with pytest.raises(StorageValidationError, match="components"):
        build_view_bundle(deep, tmp_path / "deep.zip", view_type="bm25")


def test_build_bounds_directory_listing_before_sorting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "view"
    source.mkdir()
    yielded = 0

    class FakeEntry:
        __slots__ = ("name",)

        def __init__(self, name: str) -> None:
            self.name = name

    class FakeScandir:
        def __enter__(self) -> FakeScandir:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self):
            nonlocal yielded
            for number in range(1_000_000):
                yielded += 1
                yield FakeEntry(str(number))

    monkeypatch.setattr(bundle_module.os, "scandir", lambda _path: FakeScandir())
    with pytest.raises(StorageValidationError, match="entry budget"):
        build_view_bundle(
            source,
            tmp_path / "fanout.zip",
            view_type="bm25",
            max_files=1,
        )
    assert yielded == 4


@pytest.mark.skipif(
    not hasattr(os, "mkfifo")
    or not hasattr(os, "O_NONBLOCK")
    or not bundle_module._SAFE_DIRECTORY_FDS,
    reason="requires descriptor-relative FIFO race coverage",
)
def test_build_regular_open_does_not_block_after_fifo_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "view"
    source.mkdir()
    candidate = source / "value"
    candidate.write_bytes(b"x")
    original_hash = bundle_module._hash_regular_at

    def swap_then_hash(*args: object, **kwargs: object) -> str:
        candidate.unlink()
        os.mkfifo(candidate)
        return original_hash(*args, **kwargs)

    monkeypatch.setattr(bundle_module, "_hash_regular_at", swap_then_hash)
    with pytest.raises(StorageIntegrityError, match="changed while opening"):
        build_view_bundle(source, tmp_path / "fifo.zip", view_type="bm25")
    candidate.unlink()


def test_build_copy_rejects_growth_after_snapshot_without_reading_to_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "view"
    source.mkdir()
    candidate = source / "value"
    candidate.write_bytes(b"x")
    original_consume = bundle_module._consume_exact_file
    source_reads = 0

    def append_during_copy(
        handle,
        expected_size: int,
        *,
        label: str,
        target=None,
    ):
        nonlocal source_reads
        if label.startswith("view bundle source file"):
            source_reads += 1
            if source_reads == 2:
                with candidate.open("ab") as growing:
                    growing.write(b"y" * (8 * 1024 * 1024))
        return original_consume(
            handle,
            expected_size,
            label=label,
            target=target,
        )

    monkeypatch.setattr(bundle_module, "_consume_exact_file", append_during_copy)
    with pytest.raises(StorageIntegrityError, match="grew while reading"):
        build_view_bundle(source, tmp_path / "growing.zip", view_type="bm25")
    assert source_reads == 2


def test_build_rechecks_source_bytes_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    original = bundle_module._copy_source_file
    mutated = False

    def mutate_once(snapshot, root_descriptor, scanned, target):
        nonlocal mutated
        if not mutated:
            mutated = True
            (source / scanned.path).write_bytes(b"changed after inventory")
        return original(snapshot, root_descriptor, scanned, target)

    monkeypatch.setattr(bundle_module, "_copy_source_file", mutate_once)

    with pytest.raises(StorageIntegrityError, match="changed"):
        build_view_bundle(source, archive, view_type="bm25")

    assert not archive.exists()
    assert not list(tmp_path.glob(".bundle.zip.*.tmp"))


def test_build_publish_failure_preserves_existing_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"previous bundle")
    real_link = os.link
    failed = False

    def fail_final_link(source_path, destination_path, **kwargs: object):
        nonlocal failed
        if (
            not failed
            and Path(destination_path) == archive
            and Path(source_path).name.startswith(".bundle.zip.")
        ):
            failed = True
            raise OSError("injected bundle publish failure")
        return real_link(source_path, destination_path, **kwargs)

    monkeypatch.setattr(bundle_module.os, "link", fail_final_link)

    with pytest.raises(OSError, match="injected bundle publish failure"):
        build_view_bundle(source, archive, view_type="bm25")

    assert archive.read_bytes() == b"previous bundle"
    assert not list(tmp_path.glob(".bundle.zip.*.tmp"))


@pytest.mark.parametrize("existing", [False, True])
def test_build_quarantines_replaced_temporary_and_restores_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    previous = b"previous bundle"
    if existing:
        archive.write_bytes(previous)
    stolen = tmp_path / "stolen-canonical.zip"
    real_link = os.link
    swapped = False

    def swap_temporary_then_link(
        source_path,
        destination_path,
        **kwargs: object,
    ):
        nonlocal swapped
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if (
            not swapped
            and destination_path == archive
            and source_path.name.startswith(".bundle.zip.")
        ):
            swapped = True
            source_path.rename(stolen)
            source_path.write_bytes(b"attacker bytes")
        return real_link(source_path, destination_path, **kwargs)

    monkeypatch.setattr(bundle_module.os, "link", swap_temporary_then_link)

    with pytest.raises(StorageIntegrityError, match="quarantined"):
        build_view_bundle(source, archive, view_type="bm25")

    assert swapped
    if existing:
        assert archive.read_bytes() == previous
    else:
        assert not archive.exists()
    quarantines = list(tmp_path.glob(".bundle.zip.quarantine-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b"attacker bytes"
    verify_view_bundle(stolen, expected_view_type="bm25")


@pytest.mark.parametrize("existing", [False, True])
def test_build_preserves_output_that_appears_at_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    previous = b"previous bundle"
    if existing:
        archive.write_bytes(previous)
    real_link = os.link
    injected = False

    def inject_output_then_link(
        source_path,
        destination_path,
        **kwargs: object,
    ):
        nonlocal injected
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if (
            not injected
            and destination_path == archive
            and source_path.name.startswith(".bundle.zip.")
        ):
            injected = True
            destination_path.write_bytes(b"late foreign output")
        return real_link(source_path, destination_path, **kwargs)

    monkeypatch.setattr(bundle_module.os, "link", inject_output_then_link)

    with pytest.raises(FileExistsError):
        build_view_bundle(source, archive, view_type="bm25")

    assert injected
    if existing:
        assert archive.read_bytes() == previous
    else:
        assert not archive.exists()
    quarantines = list(tmp_path.glob(".bundle.zip.quarantine-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b"late foreign output"


def test_build_never_reserves_or_unlinks_a_second_previous_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"previous bundle")
    real_link = os.link
    real_unlink = Path.unlink
    real_reserve = bundle_module._reserve_file_slot
    published = False
    injected = False
    post_publication_reserves = 0

    def mark_publication(source_path, destination_path, **kwargs: object):
        nonlocal published
        result = real_link(source_path, destination_path, **kwargs)
        if Path(destination_path) == archive:
            published = True
        return result

    def record_reservation(parent: Path, name: str, *, suffix: str) -> Path:
        nonlocal post_publication_reserves
        if published and suffix.startswith("previous"):
            post_publication_reserves += 1
        return real_reserve(parent, name, suffix=suffix)

    def inject_foreign_before_reserved_path_unlink(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal injected
        if published and path.name.startswith(".bundle.zip.previous-"):
            injected = True
            path.rename(tmp_path / "stolen-reserved-slot")
            path.write_bytes(b"foreign reserved path")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(bundle_module.os, "link", mark_publication)
    monkeypatch.setattr(bundle_module, "_reserve_file_slot", record_reservation)
    monkeypatch.setattr(Path, "unlink", inject_foreign_before_reserved_path_unlink)

    built = build_view_bundle(source, archive, view_type="bm25")

    assert not injected
    assert post_publication_reserves == 0
    assert verify_view_bundle(archive, expected_view_type="bm25") == built
    backups = list(tmp_path.glob(".bundle.zip.previous-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"previous bundle"


def test_build_never_renames_previous_backup_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"previous bundle")
    stolen = tmp_path / "stolen-previous-bundle"
    real_link = os.link
    real_replace = os.replace
    published = False
    injected = False

    def mark_publication(source_path, destination_path, **kwargs: object):
        nonlocal published
        result = real_link(source_path, destination_path, **kwargs)
        if Path(destination_path) == archive:
            published = True
        return result

    def inject_foreign_before_secondary_replace(source_path, destination_path):
        nonlocal injected
        source_path = Path(source_path)
        if (
            published
            and not injected
            and source_path.name.startswith(".bundle.zip.previous-")
        ):
            injected = True
            source_path.rename(stolen)
            source_path.write_bytes(b"foreign backup")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(bundle_module.os, "link", mark_publication)
    monkeypatch.setattr(
        bundle_module.os,
        "replace",
        inject_foreign_before_secondary_replace,
    )

    built = build_view_bundle(source, archive, view_type="bm25")

    assert not injected
    assert verify_view_bundle(archive, expected_view_type="bm25") == built
    backups = list(tmp_path.glob(".bundle.zip.previous-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"previous bundle"
    assert not stolen.exists()


def test_build_retains_verified_previous_bundle_as_discoverable_orphan(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"previous bundle")

    built = build_view_bundle(source, archive, view_type="bm25")

    assert verify_view_bundle(archive, expected_view_type="bm25") == built
    orphans = list(tmp_path.glob(".bundle.zip.previous-*"))
    assert len(orphans) == 1
    assert orphans[0].read_bytes() == b"previous bundle"


def test_build_never_unlinks_orphan_replaced_after_identity_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"previous bundle")
    stolen = tmp_path / "stolen-validated-previous-bundle"
    real_previous_validator = bundle_module._is_expected_previous_bundle_file
    swapped = False

    def swap_orphan_after_identity_validation(
        moved: os.stat_result,
        opened: os.stat_result,
        expected_identity: tuple[int, ...],
    ) -> bool:
        nonlocal swapped
        valid = real_previous_validator(moved, opened, expected_identity)
        if valid:
            orphan = next(tmp_path.glob(".bundle.zip.previous-*"))
            orphan.rename(stolen)
            orphan.write_bytes(b"foreign orphan")
            swapped = True
        return valid

    monkeypatch.setattr(
        bundle_module,
        "_is_expected_previous_bundle_file",
        swap_orphan_after_identity_validation,
    )

    built = build_view_bundle(source, archive, view_type="bm25")

    assert swapped
    assert verify_view_bundle(archive, expected_view_type="bm25") == built
    assert stolen.read_bytes() == b"previous bundle"
    orphans = list(tmp_path.glob(".bundle.zip.previous-*"))
    assert len(orphans) == 1
    assert orphans[0].read_bytes() == b"foreign orphan"
    assert not list(tmp_path.glob(".bundle.zip.quarantine-*"))


@pytest.mark.parametrize(
    "files, manifest_files, message",
    [
        (
            [("payload/../escape", b"x", 0o644)],
            [_record("safe", b"x")],
            "unsafe",
        ),
        (
            [("payload/back\\slash", b"x", 0o644)],
            [_record("safe", b"x")],
            "invalid path",
        ),
        (
            [("payload/a", b"x", 0o644), ("payload/a/b", b"y", 0o644)],
            [_record("a", b"x"), _record("a/b", b"y")],
            "ancestor",
        ),
        (
            [("payload/A", b"x", 0o644), ("payload/a", b"y", 0o644)],
            [_record("A", b"x"), _record("a", b"y")],
            "collision",
        ),
    ],
)
def test_verify_rejects_unsafe_and_colliding_paths(
    tmp_path: Path,
    files: list[tuple[str, bytes, int]],
    manifest_files: list[dict[str, object]],
    message: str,
) -> None:
    archive = tmp_path / "bad.zip"
    _write_custom_bundle(archive, files, manifest_files=manifest_files)

    with pytest.raises(StorageValidationError, match=message):
        verify_view_bundle(archive)


def test_deep_inventory_validation_retains_linear_path_state(tmp_path: Path) -> None:
    archive = tmp_path / "deep-inventory.zip"
    prefix = "/".join(["d"] * 126)
    files = [(f"payload/{prefix}/{number:04d}", b"x", 0o644) for number in range(1_000)]
    _write_custom_bundle(archive, files)

    tracemalloc.start()
    try:
        verified = verify_view_bundle(archive, max_files=1_000)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(verified.members) == 1_000
    assert peak < 96 * 1024 * 1024


def test_path_topology_budget_rejects_before_verify_or_materialize(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "wide-deep-inventory.zip"
    files = [
        (f"payload/{number:02d}/a/b/c/d/e/value", b"x", 0o644) for number in range(4)
    ]
    _write_custom_bundle(archive, files)

    with pytest.raises(StorageValidationError, match="path topology"):
        verify_view_bundle(archive, max_files=10)
    output = tmp_path / "runtime"
    with pytest.raises(StorageValidationError, match="path topology"):
        materialize_view_bundle(archive, output, max_files=10)
    assert not output.exists()


def test_verify_rejects_duplicate_extra_directory_and_nonstored_members(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.zip"
    payload = b"x"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        _write_custom_bundle(
            duplicate,
            [("payload/a", payload, 0o644), ("payload/a", payload, 0o644)],
            manifest_files=[_record("a", payload)],
        )
    with pytest.raises(StorageValidationError, match="duplicate"):
        verify_view_bundle(duplicate)

    extra = tmp_path / "extra.zip"
    _write_custom_bundle(
        extra,
        [("payload/a", payload, 0o644)],
        extra_members=((_canonical_info("surprise"), b"extra"),),
    )
    with pytest.raises(StorageValidationError, match="extra member"):
        verify_view_bundle(extra)

    directory = tmp_path / "directory.zip"
    directory_info = _canonical_info("payload/empty/")
    directory_info.external_attr = (stat.S_IFDIR | 0o755) << 16
    _write_custom_bundle(
        directory,
        [("payload/a", payload, 0o644)],
        extra_members=((directory_info, b""),),
    )
    with pytest.raises(StorageValidationError, match="directories"):
        verify_view_bundle(directory)

    compressed = tmp_path / "compressed.zip"
    _write_custom_bundle(
        compressed,
        [("payload/a", payload, 0o644)],
        compression=zipfile.ZIP_DEFLATED,
    )
    with pytest.raises(StorageValidationError, match="compressed"):
        verify_view_bundle(compressed)


def test_verify_rejects_symlink_and_special_zip_modes(tmp_path: Path) -> None:
    payload = b"target"
    for kind, label in ((stat.S_IFLNK, "symlink"), (stat.S_IFIFO, "special")):
        archive = tmp_path / f"{label}.zip"
        info = _canonical_info("payload/a", guarded=True)
        info.external_attr = (kind | 0o644) << 16
        _write_custom_bundle(
            archive,
            [],
            manifest_files=[_record("a", payload)],
            extra_members=((info, payload),),
        )
        with pytest.raises(StorageValidationError, match="non-canonical mode"):
            verify_view_bundle(archive)


def test_verify_rejects_noncanonical_low_external_attributes(tmp_path: Path) -> None:
    archive_path = tmp_path / "dos-attribute.zip"
    payload = b"x"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            _canonical_info(VIEW_BUNDLE_MANIFEST),
            _manifest([_record("a", payload)]),
        )
        info = _canonical_info("payload/a", guarded=True)
        info.external_attr |= 0x20
        archive.writestr(info, payload)

    with pytest.raises(StorageValidationError, match="non-canonical mode"):
        verify_view_bundle(archive_path)


def test_verify_rejects_encryption_flag_and_nul_filename(tmp_path: Path) -> None:
    archive = tmp_path / "patched.zip"
    _write_custom_bundle(archive, [("payload/a", b"x", 0o644)])
    raw = bytearray(archive.read_bytes())
    local = raw.find(b"PK\x03\x04", 1)
    central = raw.find(b"PK\x01\x02")
    central = raw.find(b"PK\x01\x02", central + 1)
    assert local >= 0 and central >= 0
    struct.pack_into(
        "<H", raw, local + 6, struct.unpack_from("<H", raw, local + 6)[0] | 1
    )
    struct.pack_into(
        "<H", raw, central + 8, struct.unpack_from("<H", raw, central + 8)[0] | 1
    )
    archive.write_bytes(raw)
    with pytest.raises(StorageValidationError, match="encrypted"):
        verify_view_bundle(archive)

    nul = tmp_path / "nul.zip"
    _write_custom_bundle(nul, [("payload/a", b"x", 0o644)])
    raw = bytearray(nul.read_bytes())
    raw[:] = raw.replace(b"payload/a", b"payload/\x00")
    nul.write_bytes(raw)
    with pytest.raises(StorageValidationError, match="NUL"):
        verify_view_bundle(nul)


def test_verify_enforces_metadata_file_and_streamed_byte_budgets(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    record = build_view_bundle(source, archive, view_type="bm25")

    with pytest.raises(StorageValidationError, match="metadata exceeds"):
        verify_view_bundle(archive, max_metadata_bytes=8)
    with pytest.raises(StorageValidationError, match="expanded bytes"):
        verify_view_bundle(archive, max_bytes=1)
    with pytest.raises(StorageValidationError, match="too many ZIP members"):
        verify_view_bundle(archive, max_files=1)
    with pytest.raises(StorageIntegrityError, match="size"):
        verify_view_bundle(archive, expected_size=record.byte_size + 1)
    with pytest.raises(StorageIntegrityError, match="digest"):
        verify_view_bundle(archive, expected_digest="0" * 64)


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"),
    reason="requires FIFO nonblocking-open coverage",
)
def test_verify_regular_open_does_not_block_after_fifo_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    fifo = tmp_path / "raced-archive"
    os.mkfifo(fifo)
    monkeypatch.setattr(
        bundle_module,
        "_regular_file_path",
        lambda _path, *, label: fifo,
    )

    with pytest.raises(StorageIntegrityError, match="changed while opening"):
        verify_view_bundle(archive)
    fifo.unlink()


def test_verify_rejects_archive_growth_before_zipfile_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    original_consume = bundle_module._consume_exact_file
    appended = False

    def append_before_digest(
        handle,
        expected_size: int,
        *,
        label: str,
        target=None,
    ):
        nonlocal appended
        if label == "view bundle archive" and not appended:
            appended = True
            with archive.open("ab") as growing:
                growing.write(b"x" * (8 * 1024 * 1024))
        return original_consume(
            handle,
            expected_size,
            label=label,
            target=target,
        )

    def should_not_parse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ZipFile parsed an archive before its digest was stable")

    monkeypatch.setattr(bundle_module, "_consume_exact_file", append_before_digest)
    monkeypatch.setattr(bundle_module.zipfile, "ZipFile", should_not_parse)
    with pytest.raises(StorageIntegrityError, match="grew while reading"):
        verify_view_bundle(archive)
    assert appended


def test_verify_rejects_noncanonical_inventory_and_member_set(tmp_path: Path) -> None:
    payload = b"x"
    unsorted = tmp_path / "unsorted.zip"
    _write_custom_bundle(
        unsorted,
        [("payload/b", payload, 0o644), ("payload/a", payload, 0o644)],
    )
    with pytest.raises(StorageValidationError, match="sorted"):
        verify_view_bundle(unsorted)

    mismatch = tmp_path / "mismatch.zip"
    _write_custom_bundle(
        mismatch,
        [("payload/a", payload, 0o644)],
        manifest_files=[_record("b", payload)],
    )
    with pytest.raises(StorageValidationError, match="exactly match"):
        verify_view_bundle(mismatch)

    whitespace = tmp_path / "whitespace.zip"
    record = _record("a", payload)
    with zipfile.ZipFile(whitespace, "w") as archive:
        archive.writestr(
            _canonical_info("bundle.json"),
            json.dumps(
                {
                    "schema": VIEW_BUNDLE_SCHEMA,
                    "view_type": "bm25",
                    "files": [record],
                },
                indent=2,
            ).encode(),
        )
        archive.writestr(_canonical_info("payload/a", guarded=True), payload)
    with pytest.raises(StorageValidationError, match="not canonical JSON"):
        verify_view_bundle(whitespace)


@pytest.mark.parametrize(
    "malformed, message",
    [
        (
            b'{"files":[{"mode":420,"path":"a","sha256":"'
            + b"0" * 64
            + b'","size":999999999999999999999999}],"schema":"'
            + VIEW_BUNDLE_SCHEMA.encode()
            + b'","view_type":"bm25"}',
            "64-bit",
        ),
        (
            b'{"files":[{"mode":420,"path":"a","sha256":"'
            + b"0" * 64
            + b'","size":1.0}],"schema":"'
            + VIEW_BUNDLE_SCHEMA.encode()
            + b'","view_type":"bm25"}',
            "does not allow floats",
        ),
        (
            b"[" * 2000 + b"0" + b"]" * 2000,
            "valid JSON|deeply nested|invalid shape",
        ),
    ],
)
def test_metadata_parser_is_bounded(
    tmp_path: Path,
    malformed: bytes,
    message: str,
) -> None:
    archive = tmp_path / "malformed.zip"
    _write_raw_manifest_bundle(archive, malformed)

    with pytest.raises(StorageValidationError, match=message):
        verify_view_bundle(archive)


def test_verify_rejects_trailing_bytes_and_noncanonical_zip_metadata(
    tmp_path: Path,
) -> None:
    trailing = tmp_path / "trailing.zip"
    _write_custom_bundle(trailing, [("payload/a", b"x", 0o644)])
    trailing.write_bytes(trailing.read_bytes() + b"trailing")
    with pytest.raises(StorageValidationError, match="envelope"):
        verify_view_bundle(trailing)

    extra = tmp_path / "metadata.zip"
    payload_info = _canonical_info("payload/a")
    payload_info.extra = struct.pack("<HH4s", 0xCAFE, 4, b"junk")
    _write_custom_bundle(
        extra,
        [],
        manifest_files=[_record("a", b"x")],
        extra_members=((payload_info, b"x"),),
    )
    with pytest.raises(StorageValidationError, match="canonical"):
        verify_view_bundle(extra)


def test_archive_and_materialization_targets_reject_links_overlap_and_foreign_data(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    archive_link = tmp_path / "bundle-link.zip"
    archive_link.symlink_to(archive)
    with pytest.raises(StorageValidationError, match="not a real file"):
        verify_view_bundle(archive_link)

    with pytest.raises(StorageValidationError, match="overlap"):
        materialize_view_bundle(archive, tmp_path)
    with pytest.raises(StorageValidationError, match="forbidden root"):
        materialize_view_bundle(
            archive,
            tmp_path / "runtime" / "view",
            forbidden_roots=[tmp_path / "runtime"],
        )

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("keep", encoding="utf-8")
    with pytest.raises(StorageValidationError, match="non-CodeNib"):
        materialize_view_bundle(archive, foreign)
    assert (foreign / "keep").read_text(encoding="utf-8") == "keep"

    owned_plus_extra = tmp_path / "owned-plus-extra"
    materialize_view_bundle(archive, owned_plus_extra)
    (owned_plus_extra / "external.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(StorageValidationError, match="non-CodeNib"):
        materialize_view_bundle(archive, owned_plus_extra)
    assert (owned_plus_extra / "external.txt").read_text(encoding="utf-8") == "keep"

    nested_extra = tmp_path / "nested-extra"
    materialize_view_bundle(archive, nested_extra)
    untracked = nested_extra / "payload" / "index" / "external.txt"
    untracked.write_text("keep", encoding="utf-8")
    with pytest.raises(StorageValidationError, match="non-CodeNib"):
        materialize_view_bundle(archive, nested_extra)
    assert untracked.read_text(encoding="utf-8") == "keep"

    tampered = tmp_path / "same-size-tamper"
    materialize_view_bundle(archive, tampered)
    tampered_payload = tampered / "payload" / "documents.json"
    tampered_payload.write_bytes(b"xx\n")
    with pytest.raises(StorageValidationError, match="non-CodeNib"):
        materialize_view_bundle(archive, tampered)
    assert tampered_payload.read_bytes() == b"xx\n"

    linked_payload = tmp_path / "linked-payload"
    materialize_view_bundle(archive, linked_payload)
    link = linked_payload / "payload" / "external-link"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(StorageValidationError, match="non-CodeNib"):
        materialize_view_bundle(archive, linked_payload)
    assert link.is_symlink()

    if hasattr(os, "mkfifo"):
        special_payload = tmp_path / "special-payload"
        materialize_view_bundle(archive, special_payload)
        fifo = special_payload / "payload" / "external-fifo"
        os.mkfifo(fifo)
        with pytest.raises(StorageValidationError, match="non-CodeNib"):
            materialize_view_bundle(archive, special_payload)
        assert stat.S_ISFIFO(fifo.lstat().st_mode)

    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / "payload").mkdir()
    (forged / "bundle.json").write_text(
        '{"schema":"codenib.view-bundle.v1",'
        '"schema":"codenib.view-bundle.v1","view_type":"bm25","files":[]}',
        encoding="utf-8",
    )
    with pytest.raises(StorageValidationError, match="non-CodeNib"):
        materialize_view_bundle(archive, forged)

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("keep", encoding="utf-8")
    link = tmp_path / "output-link"
    link.symlink_to(victim, target_is_directory=True)
    with pytest.raises(StorageValidationError, match="not a real directory"):
        materialize_view_bundle(archive, link)
    assert (victim / "keep").read_text(encoding="utf-8") == "keep"


def test_materialization_bounds_existing_root_listing_at_three_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    output.mkdir()
    yielded = 0

    class FakeEntry:
        __slots__ = ("name",)

        def __init__(self, name: str) -> None:
            self.name = name

    class FakeScandir:
        def __enter__(self) -> FakeScandir:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self):
            nonlocal yielded
            for number in range(1_000_000):
                yielded += 1
                yield FakeEntry(str(number))

    monkeypatch.setattr(bundle_module.os, "scandir", lambda _path: FakeScandir())
    with pytest.raises(StorageValidationError, match="non-CodeNib"):
        materialize_view_bundle(archive, output)
    assert yielded == 3


def test_materialization_failure_preserves_previous_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    first_archive = tmp_path / "first.zip"
    build_view_bundle(source, first_archive, view_type="bm25")
    output = tmp_path / "runtime"
    materialize_view_bundle(first_archive, output)
    previous = _tree(output)

    (source / "documents.json").write_bytes(b"updated")
    second_archive = tmp_path / "second.zip"
    build_view_bundle(source, second_archive, view_type="bm25")

    def fail_publish(
        _stage: Path,
        _destination: Path,
        **_kwargs: object,
    ) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr(bundle_module, "publish_staged_directory", fail_publish)
    with pytest.raises(OSError, match="injected"):
        materialize_view_bundle(second_archive, output)

    assert _tree(output) == previous
    assert not list(tmp_path.glob(".runtime.materialize-*"))
    assert not [path for path in tmp_path.iterdir() if ".cleanup-" in path.name]


def test_materialization_rejects_late_destination_mutation_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    materialize_view_bundle(archive, output)
    original_publish = bundle_module.publish_staged_directory

    def mutate_then_publish(
        stage: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        late = destination / "payload" / "index" / "late.txt"
        late.write_text("preserve", encoding="utf-8")
        original_publish(stage, destination, **kwargs)

    monkeypatch.setattr(
        bundle_module,
        "publish_staged_directory",
        mutate_then_publish,
    )
    with pytest.raises(RuntimeError, match="publication-boundary validation"):
        materialize_view_bundle(archive, output)

    assert (output / "payload" / "index" / "late.txt").read_text(
        encoding="utf-8"
    ) == "preserve"
    assert (output / "payload" / "documents.json").read_bytes() == b"[]\n"


def test_materialization_rejects_concurrent_valid_generation_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    documents = source / "documents.json"
    first_archive = tmp_path / "first.zip"
    build_view_bundle(source, first_archive, view_type="bm25")
    output = tmp_path / "runtime"
    materialize_view_bundle(first_archive, output)

    documents.write_bytes(b"[1]\n")
    second_archive = tmp_path / "second.zip"
    build_view_bundle(source, second_archive, view_type="bm25")
    documents.write_bytes(b"[2]\n")
    concurrent_archive = tmp_path / "concurrent.zip"
    build_view_bundle(source, concurrent_archive, view_type="bm25")
    concurrent = tmp_path / "concurrent"
    materialize_view_bundle(concurrent_archive, concurrent)

    original_verify = bundle_module._verify_archive_handle
    stolen = tmp_path / "stolen-original"
    swapped = False

    def verify_then_publish_concurrent(*args: object, **kwargs: object):
        nonlocal swapped
        result = original_verify(*args, **kwargs)
        os.replace(output, stolen)
        os.replace(concurrent, output)
        swapped = True
        return result

    monkeypatch.setattr(
        bundle_module,
        "_verify_archive_handle",
        verify_then_publish_concurrent,
    )
    with pytest.raises(RuntimeError, match="destination changed before"):
        materialize_view_bundle(second_archive, output)

    assert swapped
    assert (output / "payload" / "documents.json").read_bytes() == b"[2]\n"
    assert (stolen / "payload" / "documents.json").read_bytes() == b"[]\n"
    assert not concurrent.exists()
    assert not list(tmp_path.glob(".runtime.materialize-*"))
    assert not list(tmp_path.glob(".runtime.previous-*"))
    assert not [path for path in tmp_path.iterdir() if ".cleanup-" in path.name]


def test_materialization_boundary_revalidates_same_size_payload_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    materialize_view_bundle(archive, output)
    original_publish = bundle_module.publish_staged_directory

    def mutate_then_publish(
        stage: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        (destination / "payload" / "documents.json").write_bytes(b"xx\n")
        original_publish(stage, destination, **kwargs)

    monkeypatch.setattr(
        bundle_module,
        "publish_staged_directory",
        mutate_then_publish,
    )
    with pytest.raises(RuntimeError, match="publication-boundary validation"):
        materialize_view_bundle(archive, output)

    assert (output / "payload" / "documents.json").read_bytes() == b"xx\n"


@pytest.mark.parametrize(
    "relative, changed_mode",
    [
        (VIEW_BUNDLE_MANIFEST, 0o600),
        ("payload/documents.json", 0o600),
        ("payload/documents.json", 0o666),
        ("payload/documents.json", 0o4644),
        ("payload/index/shard", 0o700),
        ("payload/index/shard", 0o4755),
    ],
)
def test_materialization_rejects_noncanonical_existing_file_mode(
    tmp_path: Path,
    relative: str,
    changed_mode: int,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    materialize_view_bundle(archive, output)
    changed = output / relative
    changed.chmod(changed_mode)

    with pytest.raises(StorageValidationError, match="non-CodeNib"):
        materialize_view_bundle(archive, output)

    assert changed.stat().st_mode & 0o7777 == changed_mode


def test_materialization_rolls_back_late_payload_mode_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    materialize_view_bundle(archive, output)
    original_publish = bundle_module.publish_staged_directory

    def publish_with_late_chmod(
        stage: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        validate = kwargs["validate_moved_destination"]
        calls = 0

        def validate_then_mutate(
            moved: atomic_module.PublicationDirectoryReader,
        ) -> None:
            nonlocal calls
            assert callable(validate)
            validate(moved)
            calls += 1
            if calls == 1:
                moved_root = _moved_publication_root(destination)
                (moved_root / "payload" / "documents.json").chmod(0o600)

        kwargs["validate_moved_destination"] = validate_then_mutate
        original_publish(stage, destination, **kwargs)

    monkeypatch.setattr(
        bundle_module,
        "publish_staged_directory",
        publish_with_late_chmod,
    )

    with pytest.raises(RuntimeError, match="moved destination changed"):
        materialize_view_bundle(archive, output)

    assert stat.S_IMODE((output / "payload" / "documents.json").stat().st_mode) == 0o600
    assert (output / "payload" / "documents.json").read_bytes() == b"[]\n"


def test_materialization_rolls_back_write_after_first_boundary_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    materialize_view_bundle(archive, output)
    original_publish = bundle_module.publish_staged_directory

    def publish_with_late_write(
        stage: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        validate = kwargs["validate_moved_destination"]
        calls = 0

        def validate_then_write(
            moved: atomic_module.PublicationDirectoryReader,
        ) -> None:
            nonlocal calls
            assert callable(validate)
            validate(moved)
            calls += 1
            if calls == 1:
                moved_root = _moved_publication_root(destination)
                (moved_root / "payload" / "index" / "late.txt").write_text(
                    "preserve",
                    encoding="utf-8",
                )

        kwargs["validate_moved_destination"] = validate_then_write
        original_publish(stage, destination, **kwargs)

    monkeypatch.setattr(
        bundle_module,
        "publish_staged_directory",
        publish_with_late_write,
    )

    with pytest.raises(RuntimeError, match="moved destination changed"):
        materialize_view_bundle(archive, output)

    assert (output / "payload" / "index" / "late.txt").read_text(
        encoding="utf-8"
    ) == "preserve"


def test_materialization_stage_symlink_swap_cannot_write_victim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not bundle_module._SAFE_DIRECTORY_FDS:
        pytest.skip("requires secure directory-fd materialization")
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    stolen = tmp_path / "stolen-stage"
    original_open = bundle_module._open_extraction_file
    swapped = False

    def swap_stage_then_open(
        root_descriptor: int,
        parts: tuple[str, ...],
        mode: int,
    ):
        nonlocal swapped
        if not swapped and parts[0] == "payload":
            swapped = True
            stage = next(tmp_path.glob(".runtime.materialize-*"))
            stage.rename(stolen)
            stage.symlink_to(victim, target_is_directory=True)
        return original_open(root_descriptor, parts, mode)

    monkeypatch.setattr(bundle_module, "_open_extraction_file", swap_stage_then_open)

    with pytest.raises(StorageIntegrityError, match="stage changed"):
        materialize_view_bundle(archive, output)

    assert swapped
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (victim / VIEW_BUNDLE_MANIFEST).exists()
    assert not (victim / "payload").exists()
    assert (stolen / VIEW_BUNDLE_MANIFEST).is_file()
    assert (stolen / "payload" / "documents.json").read_bytes() == b"[]\n"


def test_materialization_rejects_real_stage_swap_at_publish_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    stolen = tmp_path / "stolen-stage"
    original_publish = bundle_module.publish_staged_directory
    substitute: Path | None = None

    def swap_stage_then_publish(
        stage: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        nonlocal substitute
        stage.rename(stolen)
        stage.mkdir()
        (stage / "foreign.txt").write_text("preserve", encoding="utf-8")
        substitute = stage
        original_publish(stage, destination, **kwargs)

    monkeypatch.setattr(
        bundle_module,
        "publish_staged_directory",
        swap_stage_then_publish,
    )

    with pytest.raises(RuntimeError, match="staged directory changed"):
        materialize_view_bundle(archive, output)

    assert not output.exists()
    assert substitute is not None
    assert (substitute / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert (stolen / VIEW_BUNDLE_MANIFEST).is_file()
    assert (stolen / "payload" / "documents.json").read_bytes() == b"[]\n"


def test_materialization_revalidates_published_stage_before_old_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    first_archive = tmp_path / "first.zip"
    build_view_bundle(source, first_archive, view_type="bm25")
    output = tmp_path / "runtime"
    materialize_view_bundle(first_archive, output)
    previous = _tree(output)

    (source / "documents.json").write_bytes(b"new")
    second_archive = tmp_path / "second.zip"
    build_view_bundle(source, second_archive, view_type="bm25")
    real_rename = atomic_module._rename_noreplace_at

    def mutate_after_stage_publish(
        source_name: str,
        destination_name: str,
        source_descriptor: int,
        destination_descriptor: int,
    ) -> None:
        real_rename(
            source_name,
            destination_name,
            source_descriptor,
            destination_descriptor,
        )
        if destination_name == output.name and source_name.startswith(
            ".runtime.materialize-"
        ):
            (output / "payload" / "documents.json").write_bytes(b"bad")

    monkeypatch.setattr(
        atomic_module,
        "_rename_noreplace_at",
        mutate_after_stage_publish,
    )

    with pytest.raises(RuntimeError, match="published directory identity"):
        materialize_view_bundle(second_archive, output)

    assert _tree(output) == previous
    quarantines = list(tmp_path.glob(".runtime.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "payload" / "documents.json").read_bytes() == b"bad"


def test_materialization_fails_closed_without_safe_directory_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    monkeypatch.setattr(bundle_module, "_SAFE_DIRECTORY_FDS", False)

    with pytest.raises(StorageValidationError, match="directory-fd"):
        materialize_view_bundle(archive, output)

    assert not output.exists()


def test_materialization_rejects_owned_payload_mount_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    materialize_view_bundle(archive, output)
    mounted = output / "payload" / "index"
    original_mount_check = atomic_module._path_is_mount_point

    def fake_mount_check(path: Path, **kwargs: object) -> bool:
        return Path(path) == mounted or original_mount_check(path, **kwargs)

    monkeypatch.setattr(bundle_module, "_path_is_mount_point", fake_mount_check)

    with pytest.raises(StorageValidationError, match="non-CodeNib"):
        materialize_view_bundle(archive, output)

    assert (mounted / "shard").read_bytes() == b"immutable shard"


def test_materialization_rolls_back_mount_detected_after_linearization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    output = tmp_path / "runtime"
    materialize_view_bundle(archive, output)
    original_publish = bundle_module.publish_staged_directory
    mounted = False
    original_mount_check = bundle_module._path_is_mount_point

    def fake_mount_check(path: Path, **kwargs: object) -> bool:
        candidate = Path(path)
        is_moved_old_tree = any(
            part.startswith(".runtime.previous-") for part in candidate.parts
        )
        return (
            mounted
            and is_moved_old_tree
            and candidate.parts[-2:] == ("payload", "index")
        ) or original_mount_check(path, **kwargs)

    def publish_with_late_mount(
        stage: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        validate = kwargs["validate_moved_destination"]
        calls = 0

        def validate_then_mount(
            moved: atomic_module.PublicationDirectoryReader,
        ) -> None:
            nonlocal calls, mounted
            assert callable(validate)
            validate(moved)
            calls += 1
            if calls == 1:
                mounted = True

        kwargs["validate_moved_destination"] = validate_then_mount
        original_publish(stage, destination, **kwargs)

    monkeypatch.setattr(atomic_module, "_path_is_mount_point", fake_mount_check)
    monkeypatch.setattr(
        bundle_module,
        "publish_staged_directory",
        publish_with_late_mount,
    )

    with pytest.raises(RuntimeError, match="moved destination changed"):
        materialize_view_bundle(archive, output)

    assert (output / "payload" / "index" / "shard").read_bytes() == b"immutable shard"


def test_materialization_cleanup_preserves_swapped_in_stage_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    archive = tmp_path / "bundle.zip"
    build_view_bundle(source, archive, view_type="bm25")
    stolen = tmp_path / "stolen-stage"
    swapped_stage: Path | None = None

    def swap_stage_then_fail(
        stage: Path,
        _destination: Path,
        **_kwargs: object,
    ) -> None:
        nonlocal swapped_stage
        stage.rename(stolen)
        stage.mkdir()
        (stage / "foreign.txt").write_text("preserve", encoding="utf-8")
        swapped_stage = stage
        raise OSError("injected stage substitution")

    monkeypatch.setattr(
        bundle_module,
        "publish_staged_directory",
        swap_stage_then_fail,
    )
    with pytest.raises(OSError, match="stage substitution"):
        materialize_view_bundle(archive, tmp_path / "runtime")

    assert swapped_stage is not None
    assert (swapped_stage / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert (stolen / VIEW_BUNDLE_MANIFEST).is_file()


def test_materialized_mode_is_applied_before_file_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    target = tmp_path / "root" / "mode-test"
    target.parent.mkdir()
    real_fchmod = bundle_module.os.fchmod

    def record_fchmod(descriptor: int, mode: int) -> None:
        events.append("chmod")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(bundle_module.os, "fchmod", record_fchmod)
    real_fsync = bundle_module.os.fsync

    def record_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    monkeypatch.setattr(bundle_module.os, "fsync", record_fsync)

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(target.parent, flags)
    try:
        bundle_module._write_extraction_bytes(
            descriptor,
            (target.name,),
            b"payload",
            0o755,
        )
    finally:
        os.close(descriptor)

    assert events[:2] == ["chmod", "fsync"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"max_files": 0}, "positive integer"),
        ({"max_bytes": True}, "positive integer"),
        ({"max_metadata_bytes": -1}, "positive integer"),
        ({"expected_size": True}, "nonnegative integer"),
    ],
)
def test_invalid_limits_fail_closed(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    archive = tmp_path / "missing.zip"
    with pytest.raises(StorageValidationError, match=message):
        verify_view_bundle(archive, **kwargs)
