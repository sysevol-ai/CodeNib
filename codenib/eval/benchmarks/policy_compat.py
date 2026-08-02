# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared contracts for paired external-policy compatibility experiments.

The experiment keeps an upstream policy fixed and swaps only its repository
provider between the upstream implementation and CodeNib.  This module owns
the parts that must not drift between scripts: case identity, preflight
eligibility, revision provenance, result naming, and denominator accounting.
It deliberately does not import either upstream agent runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from codenib.compiler.manifest import RepoManifest
from codenib.languages import extension_to_language_map
from codenib.repository_filters import walk_repository_files

POLICY_RESULT_SCHEMA_VERSION = 1
POLICY_PROVIDERS = ("native", "codenib")

_REQUIRED_CASE_FIELDS = {
    "instance_id",
    "problem_statement",
    "repo_path",
    "base_commit",
    "manifest_path",
}
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_SOURCE_SUFFIXES = frozenset(extension_to_language_map("chunker"))


def _resolved_path(value: object, base_dir: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PolicyBenchmarkCase:
    """One fixed repository-context request and its repository identity."""

    instance_id: str
    problem_statement: str
    repo_path: Path
    base_commit: str
    manifest_path: Path
    repo: str | None = None
    ground_truth: Mapping[str, Any] = field(default_factory=dict)
    native_repo_paths: Mapping[str, Path] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        base_dir: str | Path = ".",
    ) -> "PolicyBenchmarkCase":
        """Validate and normalize one case without touching its checkout."""

        missing = _REQUIRED_CASE_FIELDS - set(value)
        if missing:
            identifier = value.get("instance_id", "<unknown>")
            raise ValueError(f"case {identifier} is missing {sorted(missing)}")

        instance_id = str(value["instance_id"]).strip()
        problem_statement = str(value["problem_statement"]).strip()
        base_commit = str(value["base_commit"]).strip().lower()
        if not instance_id:
            raise ValueError("policy benchmark instance_id must not be empty")
        if not problem_statement:
            raise ValueError(f"case {instance_id} has an empty problem_statement")
        if not _COMMIT_RE.fullmatch(base_commit):
            raise ValueError(
                f"case {instance_id} base_commit must be a full Git object id"
            )

        root = Path(base_dir).expanduser().resolve()
        native_repo_paths = {
            key[: -len("_native_repo_path")]: _resolved_path(raw, root)
            for key, raw in value.items()
            if key.endswith("_native_repo_path") and raw
        }
        ground_truth = value.get("ground_truth") or {}
        if not isinstance(ground_truth, Mapping):
            raise ValueError(f"case {instance_id} ground_truth must be a mapping")

        reserved = (
            _REQUIRED_CASE_FIELDS
            | {"repo", "ground_truth"}
            | {f"{name}_native_repo_path" for name in native_repo_paths}
        )
        return cls(
            instance_id=instance_id,
            problem_statement=problem_statement,
            repo_path=_resolved_path(value["repo_path"], root),
            base_commit=base_commit,
            manifest_path=_resolved_path(value["manifest_path"], root),
            repo=str(value.get("repo") or "").strip() or None,
            ground_truth=dict(ground_truth),
            native_repo_paths=native_repo_paths,
            extra={key: raw for key, raw in value.items() if key not in reserved},
        )

    def checkout_for(self, *, agent: str, provider: str) -> Path:
        """Return the checkout used by one policy/provider cell."""

        agent_name = validate_policy_name(agent, field_name="agent")
        provider_name = validate_provider(provider)
        if provider_name == "native":
            return self.native_repo_paths.get(agent_name, self.repo_path)
        return self.repo_path

    def semantic_record(self) -> dict[str, Any]:
        """Return the path-independent fields that identify this request."""

        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "problem_statement_sha256": hashlib.sha256(
                self.problem_statement.encode("utf-8")
            ).hexdigest(),
            "ground_truth": dict(self.ground_truth),
        }


@dataclass(frozen=True, slots=True)
class PolicyCaseSet:
    """A validated, ordered collection of policy benchmark cases."""

    cases: tuple[PolicyBenchmarkCase, ...]
    metadata: Mapping[str, Any]
    source_sha256: str
    source_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "PolicyCaseSet":
        """Load the legacy list form or the metadata-plus-cases object form."""

        source = Path(path).expanduser().resolve()
        raw = source.read_bytes()
        payload = json.loads(raw)
        if isinstance(payload, list):
            records = payload
            metadata: Mapping[str, Any] = {}
        elif isinstance(payload, Mapping):
            records = payload.get("cases")
            metadata = {key: value for key, value in payload.items() if key != "cases"}
        else:
            raise ValueError(f"{source} must contain a case list or object")
        if not isinstance(records, list) or not records:
            raise ValueError(f"{source} does not contain a non-empty case list")

        cases = []
        seen = set()
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError(f"{source} contains a non-object case")
            case = PolicyBenchmarkCase.from_mapping(record, base_dir=source.parent)
            if case.instance_id in seen:
                raise ValueError(f"duplicate case instance_id: {case.instance_id}")
            seen.add(case.instance_id)
            cases.append(case)
        return cls(
            cases=tuple(cases),
            metadata=dict(metadata),
            source_sha256=hashlib.sha256(raw).hexdigest(),
            source_path=source,
        )

    def select(
        self, instance_ids: Sequence[str] | None
    ) -> tuple[PolicyBenchmarkCase, ...]:
        """Select requested IDs while retaining the case file's stable order."""

        if not instance_ids:
            return self.cases
        requested = {str(value) for value in instance_ids}
        selected = tuple(case for case in self.cases if case.instance_id in requested)
        missing = requested - {case.instance_id for case in selected}
        if missing:
            raise ValueError(f"unknown instance ids: {sorted(missing)}")
        return selected

    def selection_sha256(self, cases: Sequence[PolicyBenchmarkCase]) -> str:
        """Hash the ordered semantic request set, excluding machine-local paths."""

        return _canonical_sha256([case.semantic_record() for case in cases])


@dataclass(frozen=True, slots=True)
class GitRevision:
    """Git identity recorded without embedding a machine-local checkout path."""

    commit: str | None
    dirty: bool | None

    def to_record(self) -> dict[str, Any]:
        return {"commit": self.commit, "dirty": self.dirty}


def inspect_git_revision(checkout: str | Path) -> GitRevision:
    """Return HEAD and non-ignored working-tree dirtiness for a checkout."""

    root = Path(checkout).expanduser().resolve()
    try:
        commit = _git_output(root, "rev-parse", "HEAD")
        status = _git_output(root, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return GitRevision(commit=None, dirty=None)
    return GitRevision(commit=commit or None, dirty=bool(status))


def _git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root}",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


@dataclass(frozen=True, slots=True)
class RevisionSource:
    """Expected and observed revision for one implementation dependency."""

    name: str
    repository: str
    expected_commit: str
    checkout: Path | None = None

    def __post_init__(self) -> None:
        commit = str(self.expected_commit).strip().lower()
        if not _COMMIT_RE.fullmatch(commit):
            raise ValueError(
                f"{self.name} expected_commit must be a full Git object id"
            )
        object.__setattr__(self, "expected_commit", commit)
        if self.checkout is not None:
            object.__setattr__(
                self, "checkout", Path(self.checkout).expanduser().resolve()
            )

    def to_record(self) -> dict[str, Any]:
        actual = (
            inspect_git_revision(self.checkout)
            if self.checkout is not None
            else GitRevision(commit=None, dirty=None)
        )
        contains_expected: bool | None = None
        if self.checkout is not None and actual.commit:
            contains_expected = _git_contains_revision(
                self.checkout,
                expected_commit=self.expected_commit,
                actual_commit=actual.commit,
            )
        return {
            "name": self.name,
            "repository": self.repository,
            "expected_commit": self.expected_commit,
            "actual_commit": actual.commit,
            "dirty": actual.dirty,
            "matches_expected": (
                actual.commit == self.expected_commit if actual.commit else None
            ),
            "contains_expected": contains_expected,
        }


def _git_contains_revision(
    checkout: str | Path,
    *,
    expected_commit: str,
    actual_commit: str,
) -> bool | None:
    """Return whether ``actual_commit`` descends from the pinned revision."""

    if actual_commit == expected_commit:
        return True
    root = Path(checkout).expanduser().resolve()
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={root}",
                "-C",
                str(root),
                "merge-base",
                "--is-ancestor",
                expected_commit,
                actual_commit,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def build_policy_provenance(
    *,
    agent: str,
    provider: str,
    model: str,
    code_root: str | Path,
    case_set: PolicyCaseSet,
    selected_cases: Sequence[PolicyBenchmarkCase],
    revision_sources: Sequence[RevisionSource] = (),
    model_revision: str | None = None,
    run_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build additive, secret-free provenance for one result cell."""

    agent_name = validate_policy_name(agent, field_name="agent")
    provider_name = validate_provider(provider)
    if not str(model).strip():
        raise ValueError("model must not be empty")
    code = inspect_git_revision(code_root)
    return {
        "schema_version": POLICY_RESULT_SCHEMA_VERSION,
        "policy": {"agent": agent_name, "provider": provider_name},
        "model": {"name": str(model), "revision": model_revision},
        "codenib": code.to_record(),
        "upstream": [source.to_record() for source in revision_sources],
        "cases": {
            "source_sha256": case_set.source_sha256,
            "selection_sha256": case_set.selection_sha256(selected_cases),
            "instance_ids": [case.instance_id for case in selected_cases],
        },
        "run_options": dict(run_options or {}),
    }


@dataclass(frozen=True, slots=True)
class CaseEligibility:
    """Preflight result for one case/provider cell."""

    instance_id: str
    provider: str
    eligible: bool
    repo_commit: str | None
    manifest_commit: str | None
    reasons: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "provider": self.provider,
            "eligible": self.eligible,
            "repo_commit": self.repo_commit,
            "manifest_commit": self.manifest_commit,
            "reasons": list(self.reasons),
        }


def inspect_case_eligibility(
    case: PolicyBenchmarkCase,
    *,
    agent: str,
    provider: str,
    required_capabilities: Sequence[str] = (),
) -> CaseEligibility:
    """Check source identity and CodeNib manifest readiness before model calls."""

    agent_name = validate_policy_name(agent, field_name="agent")
    provider_name = validate_provider(provider)
    reasons: list[str] = []
    checkout = case.checkout_for(agent=agent_name, provider=provider_name)
    revision = inspect_git_revision(checkout)
    if not checkout.is_dir():
        reasons.append(f"repository checkout does not exist: {checkout}")
    elif revision.commit is None:
        reasons.append("repository checkout has no readable Git HEAD")
    elif revision.commit != case.base_commit:
        reasons.append(
            f"repository commit mismatch: {revision.commit} != {case.base_commit}"
        )
    if revision.dirty:
        reasons.append("repository has tracked working-tree changes")

    manifest_commit: str | None = None
    if provider_name == "codenib":
        if not case.manifest_path.is_file():
            reasons.append(f"CodeNib manifest does not exist: {case.manifest_path}")
        else:
            try:
                manifest = RepoManifest.load(case.manifest_path)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                reasons.append(f"CodeNib manifest cannot be loaded: {exc}")
            else:
                manifest_commit = manifest.commit or None
                if manifest_commit != case.base_commit:
                    reasons.append(
                        "manifest commit mismatch: "
                        f"{manifest_commit or '<missing>'} != {case.base_commit}"
                    )
                if manifest.repo_path:
                    manifest_repo = Path(manifest.repo_path).expanduser().resolve()
                    if manifest_repo != case.repo_path:
                        reasons.append(
                            f"manifest repository mismatch: {manifest_repo} "
                            f"!= {case.repo_path}"
                        )
                for capability in required_capabilities:
                    if not manifest.capabilities.get(capability, False):
                        reasons.append(f"manifest lacks capability: {capability}")

    return CaseEligibility(
        instance_id=case.instance_id,
        provider=provider_name,
        eligible=not reasons,
        repo_commit=revision.commit,
        manifest_commit=manifest_commit,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class PolicyRunPreflight:
    """Eligibility audit performed before any policy model call."""

    agent: str
    providers: tuple[str, ...]
    revisions: tuple[Mapping[str, Any], ...]
    cells: tuple[CaseEligibility, ...]
    revision_errors: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.revision_errors and all(cell.eligible for cell in self.cells)

    @property
    def errors(self) -> tuple[str, ...]:
        cell_errors = tuple(
            f"{cell.instance_id}/{cell.provider}: {reason}"
            for cell in self.cells
            for reason in cell.reasons
        )
        return self.revision_errors + cell_errors

    def require_eligible(self) -> None:
        if self.eligible:
            return
        details = "\n".join(f"- {error}" for error in self.errors)
        raise RuntimeError(f"policy benchmark preflight failed:\n{details}")

    def to_record(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "agent": self.agent,
            "providers": list(self.providers),
            "revisions": [dict(record) for record in self.revisions],
            "cells": [cell.to_record() for cell in self.cells],
            "errors": list(self.errors),
        }


def inspect_policy_run_preflight(
    cases: Sequence[PolicyBenchmarkCase],
    *,
    agent: str,
    providers: Sequence[str],
    revision_sources: Sequence[RevisionSource] = (),
    required_capabilities: Mapping[str, Sequence[str]] | None = None,
) -> PolicyRunPreflight:
    """Audit the complete requested matrix without dropping ineligible cells."""

    agent_name = validate_policy_name(agent, field_name="agent")
    provider_names = tuple(validate_provider(provider) for provider in providers)
    if not provider_names or len(set(provider_names)) != len(provider_names):
        raise ValueError("providers must be a non-empty unique sequence")

    revisions = tuple(source.to_record() for source in revision_sources)
    revision_errors: list[str] = []
    for source, record in zip(revision_sources, revisions, strict=True):
        if source.checkout is None:
            continue
        prefix = f"upstream {source.name}"
        if record["actual_commit"] is None:
            revision_errors.append(f"{prefix}: checkout has no readable Git HEAD")
            continue
        if record["dirty"]:
            revision_errors.append(f"{prefix}: checkout has tracked changes")
        if record["contains_expected"] is not True:
            revision_errors.append(
                f"{prefix}: checkout does not contain pinned commit "
                f"{source.expected_commit}"
            )

    capability_map = required_capabilities or {}
    unknown_capability_providers = set(capability_map) - set(provider_names)
    if unknown_capability_providers:
        raise ValueError(
            "capabilities provided for unrequested providers: "
            f"{sorted(unknown_capability_providers)}"
        )
    cells = tuple(
        inspect_case_eligibility(
            case,
            agent=agent_name,
            provider=provider,
            required_capabilities=capability_map.get(provider, ()),
        )
        for case in cases
        for provider in provider_names
    )
    return PolicyRunPreflight(
        agent=agent_name,
        providers=provider_names,
        revisions=revisions,
        cells=cells,
        revision_errors=tuple(revision_errors),
    )


@dataclass(frozen=True, slots=True, order=True)
class PolicyCellKey:
    instance_id: str
    provider: str

    def to_record(self) -> dict[str, str]:
        return {"instance_id": self.instance_id, "provider": self.provider}


@dataclass(frozen=True, slots=True)
class PolicyCoverage:
    """Expected, observed, failed, and paired cells for a fixed case set."""

    requested_instances: tuple[str, ...]
    providers: tuple[str, ...]
    observed: tuple[PolicyCellKey, ...]
    missing: tuple[PolicyCellKey, ...]
    failed: tuple[PolicyCellKey, ...]

    @property
    def expected_cell_count(self) -> int:
        return len(self.requested_instances) * len(self.providers)

    @property
    def paired_present_count(self) -> int:
        observed = set(self.observed)
        return sum(
            all(
                PolicyCellKey(instance_id, provider) in observed
                for provider in self.providers
            )
            for instance_id in self.requested_instances
        )

    @property
    def paired_successful_count(self) -> int:
        observed = set(self.observed)
        failed = set(self.failed)
        return sum(
            all(
                PolicyCellKey(instance_id, provider) in observed
                and PolicyCellKey(instance_id, provider) not in failed
                for provider in self.providers
            )
            for instance_id in self.requested_instances
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "requested_instance_count": len(self.requested_instances),
            "providers": list(self.providers),
            "expected_cell_count": self.expected_cell_count,
            "observed_cell_count": len(self.observed),
            "missing_cells": [key.to_record() for key in self.missing],
            "failed_cells": [key.to_record() for key in self.failed],
            "paired_present_count": self.paired_present_count,
            "paired_successful_count": self.paired_successful_count,
        }


def assess_policy_coverage(
    cases: Sequence[PolicyBenchmarkCase],
    results: Sequence[Mapping[str, Any]],
    *,
    providers: Sequence[str] = POLICY_PROVIDERS,
) -> PolicyCoverage:
    """Account for every requested cell without dropping failed runs."""

    normalized_providers = tuple(str(provider) for provider in providers)
    if not normalized_providers or len(set(normalized_providers)) != len(
        normalized_providers
    ):
        raise ValueError("providers must be a non-empty unique sequence")
    for provider in normalized_providers:
        validate_provider(provider)

    instance_ids = tuple(case.instance_id for case in cases)
    expected = {
        PolicyCellKey(instance_id, provider)
        for instance_id in instance_ids
        for provider in normalized_providers
    }
    by_key: dict[PolicyCellKey, Mapping[str, Any]] = {}
    for result in results:
        key = PolicyCellKey(
            str(result.get("instance_id") or ""),
            str(result.get("provider") or ""),
        )
        if key not in expected:
            raise ValueError(
                f"unexpected result cell: {key.instance_id}/{key.provider}"
            )
        if key in by_key:
            raise ValueError(f"duplicate result cell: {key.instance_id}/{key.provider}")
        by_key[key] = result

    observed = set(by_key)
    failed = {
        key
        for key, result in by_key.items()
        if result.get("error") or result.get("success") is False
    }
    return PolicyCoverage(
        requested_instances=instance_ids,
        providers=normalized_providers,
        observed=tuple(sorted(observed)),
        missing=tuple(sorted(expected - observed)),
        failed=tuple(sorted(failed)),
    )


@dataclass(frozen=True, slots=True)
class PolicyProvenanceIssue:
    cell: PolicyCellKey
    reason: str

    def to_record(self) -> dict[str, str]:
        return {**self.cell.to_record(), "reason": self.reason}


@dataclass(frozen=True, slots=True)
class PolicyProvenanceAudit:
    """Presence and structural validity of provenance in observed cells."""

    valid: tuple[PolicyCellKey, ...]
    unrecorded: tuple[PolicyCellKey, ...]
    invalid: tuple[PolicyProvenanceIssue, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "observed_cell_count": (
                len(self.valid) + len(self.unrecorded) + len(self.invalid)
            ),
            "valid_cell_count": len(self.valid),
            "unrecorded_cells": [cell.to_record() for cell in self.unrecorded],
            "invalid_cells": [issue.to_record() for issue in self.invalid],
        }


def audit_policy_provenance(
    results: Sequence[Mapping[str, Any]],
    *,
    source_sha256: str | None = None,
    selection_sha256: str | None = None,
    instance_ids: Sequence[str] | None = None,
) -> PolicyProvenanceAudit:
    """Audit provenance and, when supplied, its active case-set identity."""

    valid: list[PolicyCellKey] = []
    unrecorded: list[PolicyCellKey] = []
    invalid: list[PolicyProvenanceIssue] = []
    seen: set[PolicyCellKey] = set()
    for result in results:
        key = PolicyCellKey(
            str(result.get("instance_id") or ""),
            str(result.get("provider") or ""),
        )
        if key in seen:
            raise ValueError(f"duplicate result cell: {key.instance_id}/{key.provider}")
        seen.add(key)
        provenance = result.get("provenance")
        if provenance is None:
            unrecorded.append(key)
            continue
        if not isinstance(provenance, Mapping):
            invalid.append(PolicyProvenanceIssue(key, "provenance is not an object"))
            continue
        policy = provenance.get("policy")
        if provenance.get("schema_version") != POLICY_RESULT_SCHEMA_VERSION:
            reason = "unsupported provenance schema_version"
        elif not isinstance(policy, Mapping):
            reason = "provenance policy is not an object"
        elif str(policy.get("provider") or "") != key.provider:
            reason = "provenance provider does not match result"
        elif str(policy.get("agent") or "") != str(result.get("agent") or ""):
            reason = "provenance agent does not match result"
        elif any(
            expected is not None
            for expected in (source_sha256, selection_sha256, instance_ids)
        ):
            cases = provenance.get("cases")
            if not isinstance(cases, Mapping):
                reason = "provenance cases is not an object"
            elif (
                source_sha256 is not None
                and str(cases.get("source_sha256") or "") != source_sha256
            ):
                reason = "provenance case source digest does not match active cases"
            elif (
                selection_sha256 is not None
                and str(cases.get("selection_sha256") or "") != selection_sha256
            ):
                reason = "provenance case selection digest does not match active cases"
            elif instance_ids is not None and (
                not isinstance(cases.get("instance_ids"), Sequence)
                or isinstance(cases.get("instance_ids"), (str, bytes))
                or list(cases["instance_ids"])
                != [str(instance_id) for instance_id in instance_ids]
            ):
                reason = "provenance instance ids do not match active cases"
            else:
                valid.append(key)
                continue
        else:
            valid.append(key)
            continue
        invalid.append(PolicyProvenanceIssue(key, reason))
    return PolicyProvenanceAudit(
        valid=tuple(sorted(valid)),
        unrecorded=tuple(sorted(unrecorded)),
        invalid=tuple(sorted(invalid, key=lambda issue: issue.cell)),
    )


def validate_policy_name(value: str, *, field_name: str) -> str:
    normalized = str(value).strip().lower()
    if not _NAME_RE.fullmatch(normalized):
        raise ValueError(f"invalid {field_name}: {value!r}")
    return normalized


def validate_provider(provider: str) -> str:
    normalized = str(provider).strip().lower()
    if normalized not in POLICY_PROVIDERS:
        raise ValueError(f"unsupported policy provider: {provider!r}")
    return normalized


def policy_result_path(
    output_dir: str | Path,
    *,
    instance_id: str,
    agent: str,
    provider: str,
) -> Path:
    """Return a stable result path after validating filename components."""

    agent_name = validate_policy_name(agent, field_name="agent")
    provider_name = validate_provider(provider)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", instance_id):
        raise ValueError(f"invalid instance_id for result path: {instance_id!r}")
    return Path(output_dir) / f"{instance_id}.{agent_name}.{provider_name}.json"


def load_policy_results(
    results_dir: str | Path,
    *,
    agent: str,
    cases: Sequence[PolicyBenchmarkCase],
    providers: Sequence[str] = POLICY_PROVIDERS,
) -> tuple[Mapping[str, Any], ...]:
    """Load all present expected cells and reject filename/payload drift."""

    agent_name = validate_policy_name(agent, field_name="agent")
    provider_names = tuple(validate_provider(provider) for provider in providers)
    results: list[Mapping[str, Any]] = []
    for case in cases:
        for provider in provider_names:
            path = policy_result_path(
                results_dir,
                instance_id=case.instance_id,
                agent=agent_name,
                provider=provider,
            )
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError(f"result must be a JSON object: {path}")
            identities = {
                "instance_id": case.instance_id,
                "agent": agent_name,
                "provider": provider,
            }
            for field_name, expected in identities.items():
                actual = str(payload.get(field_name) or "").lower()
                if actual != expected.lower():
                    raise ValueError(
                        f"result identity mismatch in {path}: "
                        f"{field_name}={actual!r}, expected {expected!r}"
                    )
            results.append(dict(payload))
    return tuple(results)


def source_files(repo_path: str | Path) -> tuple[str, ...]:
    """List source files deterministically for free-form output validation."""

    root = Path(repo_path).expanduser().resolve()
    files = [
        path.relative_to(root).as_posix()
        for path in walk_repository_files(root)
        if path.suffix.lower() in _SOURCE_SUFFIXES
    ]
    return tuple(sorted(files))


def write_json_atomic(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Write one JSON object without exposing a partially written result."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


__all__ = [
    "CaseEligibility",
    "GitRevision",
    "POLICY_PROVIDERS",
    "POLICY_RESULT_SCHEMA_VERSION",
    "PolicyBenchmarkCase",
    "PolicyCaseSet",
    "PolicyCellKey",
    "PolicyCoverage",
    "PolicyProvenanceAudit",
    "PolicyProvenanceIssue",
    "PolicyRunPreflight",
    "RevisionSource",
    "assess_policy_coverage",
    "audit_policy_provenance",
    "build_policy_provenance",
    "inspect_case_eligibility",
    "inspect_policy_run_preflight",
    "inspect_git_revision",
    "load_policy_results",
    "policy_result_path",
    "source_files",
    "validate_policy_name",
    "validate_provider",
    "write_json_atomic",
]
