#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Materialize or execute an external-agent rerun spec.

The default mode is a dry run: read ``external_agent_rerun_specs.json``, write a
prompt file plus an execution artifact, and stop. Passing ``--execute`` and a
``--command-template`` runs an external wrapper without a shell and captures
stdout/stderr to the spec's JSONL/stderr paths.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PROMOTE_EXIT = 0
BLOCKED_EXIT = 5
_PROVIDER_PROFILES: Dict[str, Dict[str, Any]] = {
    "codex_readonly": {
        "provider": "codex",
        "template_status": "observed_repo_contract",
        "command_template": (
            "codex --ask-for-approval never exec --json --sandbox read-only "
            "{prompt_file}"
        ),
        "requirements": [],
        "notes": (
            "Read-only/no-approval Codex runs are useful for shell/file baselines "
            "but have been observed to cancel CodeMiner MCP calls."
        ),
    },
    "codex_mcp_bypass": {
        "provider": "codex",
        "template_status": "observed_repo_contract",
        "command_template": (
            "codex --dangerously-bypass-approvals-and-sandbox exec --json "
            "{prompt_file}"
        ),
        "requirements": ["allow_codeminer_mcp_calls"],
        "notes": (
            "Policy-isolated CodeMiner MCP comparison profile; use only when the "
            "external-run label explicitly records the bypass policy."
        ),
    },
    "claude_code_ide": {
        "provider": "claude",
        "template_status": "template_requires_environment_review",
        "command_template": "claude --ide --print --output-format stream-json {prompt_file}",
        "requirements": ["attach_native_ide_lsp_surface"],
        "notes": (
            "Native IDE/LSP baseline profile; verify the init/tool trace exposes "
            "definition/reference tools before promoting the run."
        ),
    },
    "claude_code_bare": {
        "provider": "claude",
        "template_status": "template_requires_local_review",
        "command_template": "claude --print --output-format stream-json {prompt_file}",
        "requirements": [],
        "notes": "Bare Claude Code profile; do not treat it as a native-LSP baseline.",
    },
    "opencode": {
        "provider": "opencode",
        "template_status": "template_requires_local_wrapper",
        "command_template": "opencode run --json {prompt_file}",
        "requirements": ["install_or_configure_opencode"],
        "notes": (
            "Placeholder OpenCode wrapper profile. Keep results unpromoted until "
            "the local binary/wrapper contract is verified."
        ),
    },
}


def _load_json_object(path: Path, *, label: str) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a JSON object: {path}")
    return payload


def _select_spec(payload: Dict[str, Any], spec_index: int) -> Dict[str, Any]:
    specs = [spec for spec in payload.get("specs") or [] if isinstance(spec, dict)]
    for spec in specs:
        if int(spec.get("index") or -1) == spec_index:
            return spec
    if 1 <= spec_index <= len(specs):
        return specs[spec_index - 1]
    raise RuntimeError(f"rerun spec index is not available: {spec_index}")


def _default_artifact_dir(spec_file: Path) -> Path:
    return spec_file.parent


def _default_execution_file(artifact_dir: Path, spec_index: int) -> Path:
    return artifact_dir / f"external_agent_rerun_execution.{spec_index}.json"


def _default_prompt_file(artifact_dir: Path, spec_index: int) -> Path:
    return artifact_dir / f"external_agent_rerun_prompt.{spec_index}.md"


def _default_provider_file(artifact_dir: Path, spec_index: int) -> Path:
    return artifact_dir / f"external_agent_provider_profile.{spec_index}.json"


def _output_path(spec: Dict[str, Any], key: str, fallback: Path) -> Path:
    output_paths = spec.get("output_paths") or {}
    value = output_paths.get(key) if isinstance(output_paths, dict) else None
    return Path(str(value)) if value else fallback


def _write_prompt(*, spec: Dict[str, Any], prompt_file: Path) -> None:
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# External Agent Rerun",
        "",
        f"Run label: {spec.get('label') or 'n/a'}",
        f"Gap category: {spec.get('gap_category') or 'n/a'}",
        f"Repair action: {spec.get('repair_action') or 'n/a'}",
        f"Repair surface: {spec.get('repair_surface') or 'n/a'}",
        "",
        "## Required Delta",
        "",
        str(spec.get("prompt_delta") or "Inspect the rerun spec before running."),
        "",
        "## Final Answer Contract",
        "",
        "- Emit single-line `Files:`, `Symbols:`, and `Locations:` fields.",
        "- Use repo-relative paths.",
        "- Put at most five `Locations` entries in strict priority order.",
        (
            "- Prefer endpoint/caller, bridge/factory, provider/value/type anchors "
            "before helper ranges."
        ),
    ]
    prompt_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _provider_profile(name: Optional[str]) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    profile = _PROVIDER_PROFILES.get(name)
    if profile is None:
        available = ", ".join(sorted(_PROVIDER_PROFILES))
        raise RuntimeError(f"unknown provider profile: {name}; available: {available}")
    return {"name": name, **profile}


def _write_provider_profile(
    *,
    provider_file: Path,
    provider_profile: Optional[Dict[str, Any]],
) -> None:
    if provider_profile is None:
        return
    provider_file.parent.mkdir(parents=True, exist_ok=True)
    provider_file.write_text(
        json.dumps(provider_profile, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _materialize_command(
    *,
    command_template: str,
    spec: Dict[str, Any],
    prompt_file: Path,
    jsonl_file: Path,
    stderr_file: Path,
) -> List[str]:
    replacements = {
        "{prompt_file}": str(prompt_file),
        "{jsonl_file}": str(jsonl_file),
        "{stderr_file}": str(stderr_file),
        "{label}": str(spec.get("label") or ""),
        "{gap_category}": str(spec.get("gap_category") or ""),
        "{repair_action}": str(spec.get("repair_action") or ""),
    }
    command = command_template
    for placeholder, value in replacements.items():
        command = command.replace(placeholder, shlex.quote(value))
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise RuntimeError(f"command template is not shell-parseable: {exc}") from exc
    if not argv:
        raise RuntimeError("command template produced an empty command")
    return argv


def _required_allowances(
    *,
    spec: Dict[str, Any],
    provider_profile: Optional[Dict[str, Any]],
) -> List[str]:
    required: List[str] = []
    status = str(spec.get("launcher_status") or "")
    if status not in {"ready", "ready_with_stderr_capture"}:
        requirement = str(spec.get("requirement") or "")
        if requirement:
            required.append(requirement)
    if provider_profile is not None:
        for requirement in provider_profile.get("requirements") or []:
            if requirement:
                required.append(str(requirement))
    return sorted(set(required))


def _requirements_allowed(
    *,
    required: Sequence[str],
    allowed_requirements: Sequence[str],
) -> bool:
    allowed = set(allowed_requirements)
    return all(requirement in allowed for requirement in required)


def _write_execution(
    *,
    execution_file: Path,
    payload: Dict[str, Any],
) -> None:
    execution_file.parent.mkdir(parents=True, exist_ok=True)
    execution_file.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _run_spec(
    *,
    spec_file: Path,
    spec_index: int,
    artifact_dir: Optional[Path],
    execution_file: Optional[Path],
    command_template: Optional[str],
    provider_profile_name: Optional[str],
    execute: bool,
    allowed_requirements: Sequence[str],
    timeout_s: Optional[float],
) -> int:
    payload = _load_json_object(spec_file, label="external agent rerun specs")
    spec = _select_spec(payload, spec_index)
    provider_profile = _provider_profile(provider_profile_name)
    resolved_artifact_dir = artifact_dir or _default_artifact_dir(spec_file)
    resolved_execution_file = execution_file or _default_execution_file(
        resolved_artifact_dir, spec_index
    )
    prompt_file = _default_prompt_file(resolved_artifact_dir, spec_index)
    provider_file = _default_provider_file(resolved_artifact_dir, spec_index)
    jsonl_file = _output_path(
        spec,
        "jsonl",
        resolved_artifact_dir / f"external_agent_rerun.{spec_index}.jsonl",
    )
    stderr_file = _output_path(
        spec,
        "stderr",
        resolved_artifact_dir / f"external_agent_rerun.{spec_index}.stderr",
    )
    _write_prompt(spec=spec, prompt_file=prompt_file)
    _write_provider_profile(
        provider_file=provider_file,
        provider_profile=provider_profile,
    )
    selected_command_template = command_template
    if selected_command_template is None and provider_profile is not None:
        selected_command_template = str(provider_profile.get("command_template") or "")
    required_allowances = _required_allowances(
        spec=spec,
        provider_profile=provider_profile,
    )
    requirements_allowed = _requirements_allowed(
        required=required_allowances,
        allowed_requirements=allowed_requirements,
    )

    base = {
        "execution_version": 1,
        "spec_file": str(spec_file),
        "spec_index": spec_index,
        "spec": spec,
        "prompt_file": str(prompt_file),
        "provider_profile_file": str(provider_file) if provider_profile else None,
        "provider_profile": provider_profile,
        "jsonl_file": str(jsonl_file),
        "stderr_file": str(stderr_file),
        "validation_command": spec.get("validation_command"),
        "execute": execute,
        "required_allowances": required_allowances,
        "requirements_allowed": requirements_allowed,
    }
    argv = None
    if selected_command_template:
        argv = _materialize_command(
            command_template=selected_command_template,
            spec=spec,
            prompt_file=prompt_file,
            jsonl_file=jsonl_file,
            stderr_file=stderr_file,
        )
    if not execute:
        _write_execution(
            execution_file=resolved_execution_file,
            payload={
                **base,
                "status": "dry_run",
                "returncode": PROMOTE_EXIT,
                "command_template": selected_command_template,
                "argv": argv,
                "ready_to_execute": bool(argv) and requirements_allowed,
            },
        )
        return PROMOTE_EXIT
    if not requirements_allowed:
        _write_execution(
            execution_file=resolved_execution_file,
            payload={
                **base,
                "status": "blocked",
                "returncode": BLOCKED_EXIT,
                "reason": "rerun spec requires an explicit policy or environment allowance",
                "required": required_allowances,
            },
        )
        return BLOCKED_EXIT
    if not argv:
        _write_execution(
            execution_file=resolved_execution_file,
            payload={
                **base,
                "status": "blocked",
                "returncode": BLOCKED_EXIT,
                "reason": "--execute requires --command-template or --provider-profile",
            },
        )
        return BLOCKED_EXIT

    jsonl_file.parent.mkdir(parents=True, exist_ok=True)
    stderr_file.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    if completed.stdout:
        jsonl_file.write_text(completed.stdout, encoding="utf-8")
    elif not jsonl_file.exists():
        jsonl_file.write_text("", encoding="utf-8")
    stderr_file.write_text(completed.stderr, encoding="utf-8")
    _write_execution(
        execution_file=resolved_execution_file,
        payload={
            **base,
            "status": "pass" if completed.returncode == 0 else "fail",
            "returncode": completed.returncode,
            "command_template": selected_command_template,
            "argv": argv,
            "duration_ms": duration_ms,
        },
    )
    return int(completed.returncode)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-file", required=True, type=Path)
    parser.add_argument("--spec-index", type=int, default=1)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--execution-file", type=Path)
    parser.add_argument("--command-template")
    parser.add_argument("--provider-profile", choices=sorted(_PROVIDER_PROFILES))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-requirement", action="append", default=[])
    parser.add_argument("--timeout-s", type=float)
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args(argv)

    try:
        rc = _run_spec(
            spec_file=args.spec_file,
            spec_index=args.spec_index,
            artifact_dir=args.artifact_dir,
            execution_file=args.execution_file,
            command_template=args.command_template,
            provider_profile_name=args.provider_profile,
            execute=args.execute,
            allowed_requirements=args.allow_requirement,
            timeout_s=args.timeout_s,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return BLOCKED_EXIT
    if args.print_json:
        execution_file = args.execution_file or _default_execution_file(
            args.artifact_dir or _default_artifact_dir(args.spec_file),
            args.spec_index,
        )
        print(execution_file.read_text(encoding="utf-8"), end="")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
