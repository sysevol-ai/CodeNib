# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Edit-run audit artifacts for the agent harness.

This module does not edit files itself. It wraps an edit attempt with:

* before/after git status and diffs;
* preimage snapshots for files that were already dirty at the start;
* a revert script that restores the worktree to the start-of-run state;
* an optional smallest-local verification command result.

The goal is to make edit-capable harness runs reviewable without assuming the
worktree started clean.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

_MAX_CAPTURE_CHARS = 8000


@dataclass
class CommandRecord:
    command: List[str]
    cwd: str
    exit_code: int
    duration_ms: float
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    timed_out: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output_truncated": self.output_truncated,
            "timed_out": self.timed_out,
        }


class VerificationRecord(CommandRecord):
    pass


class EditCommandRecord(CommandRecord):
    pass


@dataclass
class EditRunAudit:
    repo_path: str
    artifact_dir: str
    before_status: Dict[str, Dict[str, Any]]
    after_status: Dict[str, Dict[str, Any]]
    changed_paths: List[str]
    dirty_at_start: bool
    before_diff_path: str
    after_diff_path: str
    revert_script_path: str
    revert_manifest_path: str
    audit_json_path: str
    edit_command: Optional[EditCommandRecord] = None
    verification: Optional[VerificationRecord] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "artifact_dir": self.artifact_dir,
            "before_status": self.before_status,
            "after_status": self.after_status,
            "changed_paths": self.changed_paths,
            "dirty_at_start": self.dirty_at_start,
            "before_diff_path": self.before_diff_path,
            "after_diff_path": self.after_diff_path,
            "revert_script_path": self.revert_script_path,
            "revert_manifest_path": self.revert_manifest_path,
            "audit_json_path": self.audit_json_path,
            "edit_command": (
                self.edit_command.to_dict() if self.edit_command else None
            ),
            "verification": self.verification.to_dict() if self.verification else None,
            "metadata": self.metadata,
        }


@dataclass
class AgentEditRunAuditResult:
    """Result of running ``AgentRunner.run`` under edit audit."""

    result: Any
    audit: EditRunAudit
    result_json_path: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_result_json_path": self.result_json_path,
            "answer": str(getattr(self.result, "answer", "") or ""),
            "total_turns": getattr(self.result, "total_turns", None),
            "tool_call_count": len(getattr(self.result, "tool_calls", []) or []),
            "audit": self.audit.to_dict(),
        }


class AgentEditRunAuditError(RuntimeError):
    """Raised when an audited ``AgentRunner.run`` fails after finalizing audit."""

    def __init__(
        self,
        message: str,
        *,
        audit: EditRunAudit,
        error_json_path: str,
        original_error: Exception,
    ) -> None:
        super().__init__(message)
        self.audit = audit
        self.error_json_path = error_json_path
        self.original_error = original_error


class EditRunAuditSession:
    """Capture edit-run before/after artifacts around an existing edit flow."""

    def __init__(
        self,
        repo_path: str | Path,
        artifact_dir: Optional[str | Path] = None,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        if artifact_dir is None:
            artifact_dir = tempfile.mkdtemp(prefix="codeminer-edit-audit-")
        self.artifact_dir = Path(artifact_dir).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.preimages_dir = self.artifact_dir / "preimages"
        self.preimages_dir.mkdir(exist_ok=True)
        self.metadata = dict(metadata or {})

        self.before_status = _git_status(self.repo_path)
        self.before_diff_path = self.artifact_dir / "before.diff"
        self.after_diff_path = self.artifact_dir / "after.diff"
        _write_git_diff(self.repo_path, self.before_diff_path)
        self._preimages = _snapshot_preimages(
            self.repo_path, self.preimages_dir, self.before_status
        )

    def __enter__(self) -> "EditRunAuditSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def finalize(
        self,
        *,
        edit_command: Optional[EditCommandRecord] = None,
        verification_command: Optional[Sequence[str]] = None,
        verification_cwd: Optional[str | Path] = None,
        verification_timeout_s: Optional[float] = None,
    ) -> EditRunAudit:
        after_status = _git_status(self.repo_path)
        _write_git_diff(self.repo_path, self.after_diff_path)
        changed_paths = sorted(set(self.before_status) | set(after_status))
        revert_manifest_path = self.artifact_dir / "revert_manifest.json"
        revert_script_path = self.artifact_dir / "revert.py"
        actions = _revert_actions(
            self.repo_path,
            changed_paths,
            self.before_status,
            self._preimages,
        )
        _write_json(
            revert_manifest_path,
            {
                "repo_path": str(self.repo_path),
                "actions": actions,
            },
        )
        _write_revert_script(revert_script_path)

        verification = None
        if verification_command:
            verification = _run_verification(
                list(verification_command),
                cwd=(
                    Path(verification_cwd).resolve()
                    if verification_cwd
                    else self.repo_path
                ),
                timeout_s=verification_timeout_s,
            )

        audit_json_path = self.artifact_dir / "edit_audit.json"
        audit = EditRunAudit(
            repo_path=str(self.repo_path),
            artifact_dir=str(self.artifact_dir),
            before_status=self.before_status,
            after_status=after_status,
            changed_paths=changed_paths,
            dirty_at_start=bool(self.before_status),
            before_diff_path=str(self.before_diff_path),
            after_diff_path=str(self.after_diff_path),
            revert_script_path=str(revert_script_path),
            revert_manifest_path=str(revert_manifest_path),
            audit_json_path=str(audit_json_path),
            edit_command=edit_command,
            verification=verification,
            metadata=self.metadata,
        )
        _write_json(audit_json_path, audit.to_dict())
        return audit


def begin_edit_run_audit(
    repo_path: str | Path,
    artifact_dir: Optional[str | Path] = None,
    *,
    metadata: Optional[Mapping[str, Any]] = None,
) -> EditRunAuditSession:
    return EditRunAuditSession(repo_path, artifact_dir, metadata=metadata)


def run_edit_command_with_audit(
    repo_path: str | Path,
    edit_command: Sequence[str],
    *,
    artifact_dir: Optional[str | Path] = None,
    edit_cwd: Optional[str | Path] = None,
    edit_timeout_s: Optional[float] = None,
    verification_command: Optional[Sequence[str]] = None,
    verification_cwd: Optional[str | Path] = None,
    verification_timeout_s: Optional[float] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> EditRunAudit:
    """Run an edit command under audit and persist diff/revert artifacts."""
    repo = Path(repo_path).resolve()
    edit_record = None
    with begin_edit_run_audit(repo, artifact_dir, metadata=metadata) as session:
        edit_record = EditCommandRecord(
            **_run_command(
                edit_command,
                cwd=Path(edit_cwd).resolve() if edit_cwd else repo,
                timeout_s=edit_timeout_s,
            ).to_dict()
        )
        return session.finalize(
            edit_command=edit_record,
            verification_command=verification_command,
            verification_cwd=verification_cwd,
            verification_timeout_s=verification_timeout_s,
        )


def run_agent_with_edit_audit(
    runner: Any,
    query: str,
    repo_path: str | Path,
    *,
    artifact_dir: Optional[str | Path] = None,
    max_turns: Optional[int] = None,
    chat_history: Optional[List[Dict[str, str]]] = None,
    preload_candidates: Optional[Sequence[Mapping[str, Any]]] = None,
    verification_command: Optional[Sequence[str]] = None,
    verification_cwd: Optional[str | Path] = None,
    verification_timeout_s: Optional[float] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> AgentEditRunAuditResult:
    """Run an LLM ``AgentRunner`` edit attempt under diff/revert audit."""
    repo = Path(repo_path).resolve()
    run_metadata = dict(metadata or {})
    run_metadata.setdefault("surface", "agent_runner")
    with begin_edit_run_audit(repo, artifact_dir, metadata=run_metadata) as session:
        run_kwargs: Dict[str, Any] = {}
        if max_turns is not None:
            run_kwargs["max_turns"] = max_turns
        if chat_history is not None:
            run_kwargs["chat_history"] = chat_history
        if preload_candidates is not None:
            run_kwargs["preload_candidates"] = preload_candidates

        try:
            result = runner.run(query, **run_kwargs)
        except Exception as exc:
            error_json_path = session.artifact_dir / "agent_error.json"
            _write_json(
                error_json_path,
                {
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            session.metadata.update(
                {
                    "agent_failed": True,
                    "agent_error_type": type(exc).__name__,
                    "agent_error_json_path": str(error_json_path),
                }
            )
            audit = session.finalize(
                verification_command=verification_command,
                verification_cwd=verification_cwd,
                verification_timeout_s=verification_timeout_s,
            )
            raise AgentEditRunAuditError(
                f"AgentRunner edit run failed: {exc}",
                audit=audit,
                error_json_path=str(error_json_path),
                original_error=exc,
            ) from exc
        result_json_path = session.artifact_dir / "agent_result.json"
        _write_json(result_json_path, _agent_result_to_dict(result))
        session.metadata.update(
            {
                "agent_failed": False,
                "agent_result_json_path": str(result_json_path),
                "agent_answer_chars": len(str(getattr(result, "answer", "") or "")),
                "agent_total_turns": getattr(result, "total_turns", None),
                "agent_tool_call_count": len(getattr(result, "tool_calls", []) or []),
            }
        )
        audit = session.finalize(
            verification_command=verification_command,
            verification_cwd=verification_cwd,
            verification_timeout_s=verification_timeout_s,
        )
    return AgentEditRunAuditResult(
        result=result,
        audit=audit,
        result_json_path=str(result_json_path),
    )


def _agent_result_to_dict(result: Any) -> Dict[str, Any]:
    usage = getattr(result, "usage", None)
    trace = getattr(result, "trace", None)
    messages = getattr(result, "messages", []) or []
    return {
        "answer": str(getattr(result, "answer", "") or ""),
        "total_turns": getattr(result, "total_turns", None),
        "total_duration_ms": getattr(result, "total_duration_ms", None),
        "usage": _json_safe(usage.to_dict() if hasattr(usage, "to_dict") else usage),
        "usage_records": [
            _json_safe(record.to_dict() if hasattr(record, "to_dict") else record)
            for record in (getattr(result, "usage_records", []) or [])
        ],
        "message_count": len(messages),
        "messages": _json_safe(messages),
        "tool_calls": [
            _tool_call_to_dict(record)
            for record in (getattr(result, "tool_calls", []) or [])
        ],
        "trace": _json_safe(trace.to_dict() if hasattr(trace, "to_dict") else trace),
    }


def _tool_call_to_dict(record: Any) -> Dict[str, Any]:
    envelope = getattr(record, "envelope", None)
    return {
        "tool_call_id": getattr(record, "tool_call_id", None),
        "skill_id": getattr(record, "skill_id", None),
        "arguments": _json_safe(getattr(record, "arguments", None) or {}),
        "duration_ms": getattr(record, "duration_ms", None),
        "error": getattr(record, "error", None),
        "result": _json_safe(getattr(record, "result", None)),
        "envelope": _json_safe(
            envelope.to_dict() if hasattr(envelope, "to_dict") else envelope
        ),
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate(value)[0]
    if isinstance(value, bytes):
        return _truncate(value.decode("utf-8", errors="replace"))[0]
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def _run_git(repo_path: Path, args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _git_status(repo_path: Path) -> Dict[str, Dict[str, Any]]:
    proc = _run_git(
        repo_path,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git status failed: {proc.stderr.strip()}")
    parts = proc.stdout.split("\0")
    out: Dict[str, Dict[str, Any]] = {}
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        xy = entry[:2]
        path = entry[3:]
        record: Dict[str, Any] = {"xy": xy}
        if "R" in xy or "C" in xy:
            if i < len(parts):
                record["orig_path"] = parts[i]
                i += 1
        out[path] = record
    return out


def _write_git_diff(repo_path: Path, path: Path) -> None:
    proc = _run_git(repo_path, ["diff", "--binary", "HEAD"])
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed: {proc.stderr.strip()}")
    path.write_text(proc.stdout, encoding="utf-8")


def _snapshot_preimages(
    repo_path: Path,
    preimages_dir: Path,
    before_status: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    snapshots: Dict[str, Dict[str, Any]] = {}
    for rel in sorted(before_status):
        src = repo_path / rel
        record: Dict[str, Any] = {
            "exists": src.exists(),
            "is_file": src.is_file() or src.is_symlink(),
        }
        if record["is_file"]:
            dest = preimages_dir / _safe_preimage_name(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            record["preimage_path"] = str(dest)
        snapshots[rel] = record
    return snapshots


def _safe_preimage_name(path: str) -> str:
    return path.replace("\\", "__").replace("/", "__").replace(":", "_")


def _path_in_head(repo_path: Path, rel: str) -> bool:
    proc = _run_git(repo_path, ["cat-file", "-e", f"HEAD:{rel}"])
    return proc.returncode == 0


def _revert_actions(
    repo_path: Path,
    changed_paths: Sequence[str],
    before_status: Mapping[str, Mapping[str, Any]],
    preimages: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for rel in changed_paths:
        snapshot = preimages.get(rel)
        if rel in before_status:
            if snapshot and snapshot.get("preimage_path"):
                actions.append(
                    {
                        "kind": "restore_preimage",
                        "path": rel,
                        "preimage_path": snapshot["preimage_path"],
                    }
                )
            else:
                actions.append({"kind": "delete", "path": rel})
        elif _path_in_head(repo_path, rel):
            actions.append({"kind": "restore_head", "path": rel})
        else:
            actions.append({"kind": "delete", "path": rel})
    return actions


def _run_verification(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_s: Optional[float],
) -> VerificationRecord:
    return VerificationRecord(
        **_run_command(command, cwd=cwd, timeout_s=timeout_s).to_dict()
    )


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_s: Optional[float],
) -> CommandRecord:
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\nTimed out after {timeout_s}s"
        timed_out = True
    duration_ms = (time.monotonic() - start) * 1000
    stdout, out_truncated = _truncate(stdout)
    stderr, err_truncated = _truncate(stderr)
    return CommandRecord(
        command=list(command),
        cwd=str(cwd),
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
        output_truncated=out_truncated or err_truncated,
        timed_out=timed_out,
    )


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= _MAX_CAPTURE_CHARS:
        return text, False
    return text[:_MAX_CAPTURE_CHARS] + "\n... (truncated)", True


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_revert_script(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import shutil
import subprocess
from pathlib import Path

manifest = Path(__file__).with_name("revert_manifest.json")
data = json.loads(manifest.read_text())
repo = Path(data["repo_path"])

for action in data["actions"]:
    rel = action["path"]
    target = repo / rel
    kind = action["kind"]
    if kind == "restore_preimage":
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(action["preimage_path"], target)
    elif kind == "restore_head":
        subprocess.run(["git", "checkout", "--", rel], cwd=repo, check=True)
    elif kind == "delete":
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
    else:
        raise SystemExit(f"unknown revert action: {kind}")
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


__all__ = [
    "AgentEditRunAuditError",
    "AgentEditRunAuditResult",
    "CommandRecord",
    "EditCommandRecord",
    "EditRunAudit",
    "EditRunAuditSession",
    "VerificationRecord",
    "begin_edit_run_audit",
    "run_agent_with_edit_audit",
    "run_edit_command_with_audit",
]
