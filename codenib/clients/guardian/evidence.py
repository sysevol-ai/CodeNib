# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic validation for model-produced Guardian evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .types import (
    CandidateSpecification,
    ContextFidelity,
    Evidence,
    EvidenceAuthority,
    EvidenceSourceType,
    GuardianRequest,
    TaskContextSource,
)

_FILE_SOURCES = {
    EvidenceSourceType.DOCUMENTATION,
    EvidenceSourceType.INTERFACE,
    EvidenceSourceType.CALLER,
    EvidenceSourceType.TEST,
    EvidenceSourceType.EXISTING_BEHAVIOR,
}

_FILE_LOCATORS = (
    re.compile(r"^(?P<path>.+?)#L(?P<start>\d+)(?:-L?(?P<end>\d+))?$"),
    re.compile(r"^(?P<path>.+?):L?(?P<start>\d+)(?:[-:](?:L)?(?P<end>\d+))?$"),
    re.compile(
        r"^(?P<path>.+?)\s*\((?:lines?\s+)?(?P<start>\d+)"
        r"(?:\s*[-:]\s*(?P<end>\d+))?\)$",
        re.IGNORECASE,
    ),
)


def _strip_locator_markup(value: str) -> str:
    return value.strip().strip("`'\"").strip()


def _normalize_file_locator(workspace: Path, evidence: Evidence) -> Evidence:
    """Repair common model-produced file locator notation before validation."""

    raw_path = _strip_locator_markup(evidence.path).replace("\\", "/")
    relative = PurePosixPath(raw_path)
    safe_direct = (
        not relative.is_absolute()
        and bool(relative.parts)
        and ".." not in relative.parts
    )
    direct = workspace.joinpath(*relative.parts)
    if safe_direct and direct.is_file():
        return replace(evidence, path=PurePosixPath(raw_path).as_posix())
    for pattern in _FILE_LOCATORS:
        match = pattern.fullmatch(raw_path)
        if match is None:
            continue
        path = _strip_locator_markup(match.group("path"))
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if start < 1 or end < start:
            return replace(evidence, path=raw_path)
        return replace(evidence, path=path, line_start=start, line_end=end)
    return replace(evidence, path=raw_path)


def _normalize_context_locator(request: GuardianRequest, locator: str) -> str:
    raw = _strip_locator_markup(locator)
    contexts = {item.context_id: item for item in request.task_context}
    if raw in contexts:
        return raw
    lowered = {context_id.casefold(): context_id for context_id in contexts}
    if raw.casefold() in lowered:
        return lowered[raw.casefold()]
    matches = [
        context_id
        for context_id in contexts
        if any(
            raw.casefold().startswith(f"{context_id.casefold()}{separator}")
            for separator in ("#", ":", "[", "/")
        )
    ]
    return matches[0] if len(matches) == 1 else raw


def _exact_source_quote(source: str, quote: str) -> str | None:
    """Return the exact source slice for an exact or whitespace-varied quote."""

    stripped = quote.strip()
    if not stripped:
        return None
    if stripped in source:
        return stripped
    tokens = re.findall(r"\S+", stripped)
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    match = re.search(pattern, source)
    return match.group(0) if match is not None else None


def _repair_quote_location(
    lines: list[str], quote: str, *, preferred_line: int
) -> tuple[str, int, int] | None:
    """Re-anchor a misplaced quote to the closest occurrence in the file."""

    stripped = quote.strip()
    if not stripped:
        return None
    source = "\n".join(lines)
    patterns = [re.escape(stripped)]
    tokens = re.findall(r"\S+", stripped)
    if tokens:
        patterns.append(r"\s+".join(re.escape(token) for token in tokens))
    matches = []
    for pattern in dict.fromkeys(patterns):
        matches.extend(re.finditer(pattern, source))
        if matches:
            break
    if not matches:
        return None

    def location(match: re.Match[str]) -> tuple[int, int]:
        start = source.count("\n", 0, match.start()) + 1
        end = source.count("\n", 0, match.end()) + 1
        return start, max(start, end)

    match = min(matches, key=lambda item: abs(location(item)[0] - preferred_line))
    start, end = location(match)
    return match.group(0), start, end


def _validate_file(workspace: Path, evidence: Evidence) -> tuple[Evidence | None, str]:
    evidence = _normalize_file_locator(workspace, evidence)
    raw_path = evidence.path.strip()
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None, "path is not repository-relative"
    target = workspace.joinpath(*relative.parts)
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "path does not resolve to a repository file"
    if not resolved.is_relative_to(workspace) or not resolved.is_file():
        return None, "path does not resolve to a repository file"
    try:
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, "repository file could not be read"
    selected = (
        "\n".join(lines[evidence.line_start - 1 : evidence.line_end])
        if evidence.line_end <= len(lines)
        else ""
    )
    quote = evidence.quote
    if quote:
        exact_quote = _exact_source_quote(selected, quote)
        if exact_quote is None:
            repaired = _repair_quote_location(
                lines, quote, preferred_line=evidence.line_start
            )
            if repaired is None:
                return None, "quoted content does not occur in the repository file"
            exact_quote, line_start, line_end = repaired
            evidence = replace(evidence, line_start=line_start, line_end=line_end)
        quote = exact_quote
    elif evidence.line_end > len(lines):
        return None, "line range exceeds the repository file"
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    fresh = not evidence.blob_sha256 or evidence.blob_sha256 == digest
    authority = evidence.authority
    if evidence.source_type is EvidenceSourceType.EXISTING_BEHAVIOR:
        authority = EvidenceAuthority.BEHAVIORAL
    return (
        replace(
            evidence,
            path=relative.as_posix(),
            quote=quote or "",
            authority=authority,
            blob_sha256=evidence.blob_sha256 or digest,
            fresh=fresh,
        ),
        "",
    )


def _validate_non_file(
    request: GuardianRequest, evidence: Evidence
) -> tuple[Evidence | None, str]:
    if evidence.source_type in (
        EvidenceSourceType.TASK,
        EvidenceSourceType.SOLVER_MESSAGE,
    ):
        contexts = {item.context_id: item for item in request.task_context}
        path = _normalize_context_locator(request, evidence.path)
        context = contexts.get(path)
        if context is None:
            return None, "task-message locator does not exist in the request"
        if not evidence.quote:
            return None, "task and solver evidence must retain an exact quote"
        quote = _exact_source_quote(context.content, evidence.quote)
        if (
            context.source is TaskContextSource.SOLVER_SUMMARY
            or context.fidelity is ContextFidelity.DERIVED
            or evidence.source_type is EvidenceSourceType.SOLVER_MESSAGE
        ):
            if quote is None:
                return None, "quoted content does not occur in the task message"
            return (
                replace(
                    evidence,
                    path=path,
                    quote=quote,
                    authority=EvidenceAuthority.DERIVED,
                    fresh=True,
                ),
                "",
            )
        # The locator identifies verbatim task text, which remains authoritative even
        # when an explorer supplied a paraphrase instead of an exact substring. Cite
        # the complete source so the aggregator can test entailment against the task,
        # rather than accidentally granting authority to the paraphrase itself.
        if evidence.acquired_by == "controller":
            if evidence.quote != context.content:
                return (
                    None,
                    "controller task evidence no longer matches the task message",
                )
            quote = context.content
        return (
            replace(
                evidence,
                path=path,
                quote=quote or context.content,
                authority=EvidenceAuthority.NORMATIVE,
                fresh=True,
            ),
            "",
        )
    if evidence.source_type is EvidenceSourceType.CANDIDATE_PATCH:
        if not evidence.quote:
            return None, "candidate-patch evidence must retain an exact quote"
        if evidence.quote not in request.change_patch:
            return None, "quoted content does not occur in the candidate patch"
        return replace(evidence, authority=EvidenceAuthority.DERIVED, fresh=True), ""
    if evidence.source_type is EvidenceSourceType.RUNTIME_PROBE:
        if not evidence.command or (not evidence.output and evidence.exit_code is None):
            return (
                None,
                "runtime evidence must retain a command and an output or exit code",
            )
        return replace(evidence, authority=EvidenceAuthority.BEHAVIORAL, fresh=True), ""
    return None, "unsupported evidence locator"


def validate_evidence(
    request: GuardianRequest, evidence: Evidence
) -> tuple[Evidence | None, str]:
    if evidence.source_type in _FILE_SOURCES:
        return _validate_file(request.workspace, evidence)
    return _validate_non_file(request, evidence)


def validate_candidates(
    request: GuardianRequest,
    candidates: tuple[CandidateSpecification, ...],
) -> tuple[tuple[CandidateSpecification, ...], tuple[str, ...]]:
    """Drop only malformed evidence and candidates left with no support."""

    valid_candidates = []
    errors = []
    for candidate in candidates:
        supporting = []
        counter = []
        for kind, values, target in (
            ("support", candidate.supporting_evidence, supporting),
            ("counter", candidate.counterevidence, counter),
        ):
            for index, item in enumerate(values, 1):
                checked, error = validate_evidence(request, item)
                if checked is not None:
                    target.append(checked)
                else:
                    errors.append(
                        f"{candidate.explorer} {candidate.candidate_id} {kind} "
                        f"evidence {index} ({item.path!r}): {error}"
                    )
        if supporting:
            valid_candidates.append(
                replace(
                    candidate,
                    supporting_evidence=tuple(supporting),
                    counterevidence=tuple(counter),
                )
            )
    return tuple(valid_candidates), tuple(errors)


def revalidate_memory_evidence(
    request: GuardianRequest, evidence: tuple[Evidence, ...]
) -> tuple[tuple[Evidence, ...], tuple[str, ...]]:
    values = []
    errors = []
    for item in evidence:
        checked, error = validate_evidence(request, item)
        if checked is not None:
            values.append(checked)
        else:
            values.append(replace(item, fresh=False))
            errors.append(f"remembered evidence {item.evidence_id}: {error}")
    return tuple(values), tuple(errors)


__all__ = ["revalidate_memory_evidence", "validate_candidates", "validate_evidence"]
