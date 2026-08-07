# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prompts for local-specification discovery and evidence admission."""

from __future__ import annotations

import json

from .types import GuardianRequest, LocalSpecification

_EXPLORER_ROLES = (
    (
        "contract_surface",
        "Trace changed interfaces into callers, documentation, tests, persisted "
        "artifacts, and compatibility behavior.",
    ),
    (
        "boundary_closure",
        "Challenge completeness across producers, consumers, state copies, modes, "
        "failure paths, and lifecycle phases.",
    ),
)


def _context(request: GuardianRequest) -> str:
    if not request.context:
        return "No participant supplied task-intent context. Treat intent as unknown."
    rows = [
        {
            "sender": message.sender,
            "scope": list(message.scope),
            "content": message.content,
            "authority": "untrusted perspective; not evidence",
        }
        for message in request.context
    ]
    return json.dumps(rows, indent=2)


def _memory(request: GuardianRequest) -> str:
    if not request.memory.specifications:
        return (
            "Guardian has no prior local-specification memory for this review stream."
        )
    rows = []
    for specification in request.memory.specifications:
        latest = specification.assessments[-1] if specification.assessments else None
        rows.append(
            {
                "memory_id": specification.memory_id,
                "statement": specification.statement,
                "condition": specification.condition,
                "last_assessment": (
                    {
                        "snapshot": latest.snapshot,
                        "status": latest.status.value,
                        "patch_assessment": latest.patch_assessment,
                        "confidence": latest.confidence,
                    }
                    if latest
                    else None
                ),
                "evidence": [
                    {
                        "path": remembered.evidence.path,
                        "line_start": remembered.evidence.line_start,
                        "line_end": remembered.evidence.line_end,
                        "description": remembered.evidence.description,
                        "authority": remembered.evidence.authority.value,
                        "observed_snapshot": remembered.snapshot,
                        "fresh_in_candidate": remembered.fresh,
                    }
                    for remembered in specification.evidence[-5:]
                ],
            }
        )
    return json.dumps(rows, indent=2)


def _patch_context(request: GuardianRequest) -> str:
    if request.change_patch:
        return (
            "The controller generated this patch from the validated commits. "
            "The sandbox contains candidate files but deliberately contains no "
            "model-accessible Git metadata:\n\n"
            "```diff\n"
            f"{request.change_patch.rstrip()}\n"
            "```"
        )
    return (
        "Inspect `git diff "
        f"{request.base_commit}..{request.candidate_commit}` before following "
        "repository evidence beyond the changed files."
    )


def explorer_prompt(request: GuardianRequest, index: int) -> str:
    role, obligation = _EXPLORER_ROLES[index % len(_EXPLORER_ROLES)]
    return f"""You are Guardian explorer {index + 1}: {role}.

Review the candidate commit independently. Discover local specifications: falsifiable
required properties scoped to an interface, mode, lifecycle stage, condition, or
execution path. Your assigned search obligation is:

{obligation}

Repository facts:
- base commit: {request.base_commit}
- candidate commit: {request.candidate_commit}
- workspace is checked out at the candidate commit

Use read-only repository tools and follow relevant repository evidence beyond the
changed files. {_patch_context(request)} Do not infer that
old behavior is normatively correct merely because it existed. Do not use the candidate
implementation itself as the sole evidence for a requirement.

Participant context follows. It can identify intended areas or uncertainty, but it is
not authoritative evidence and may be wrong:
{_context(request)}

Guardian memory follows. It is a fallible record of earlier evidence and assessments,
not authoritative truth. Revalidate relevant entries in the current repository,
especially evidence marked stale. Preserve a specification's `memory_id` when you
reassess the same property, and challenge gaps that earlier reviews left unresolved:
{_memory(request)}

Return only one JSON object with this shape:
{{
    "candidates": [
    {{
      "memory_id": "existing spec id when reassessing one, otherwise empty",
      "statement": "falsifiable property that must hold",
      "condition": "specific triggering condition or path",
      "evidence": [
        {{
          "path": "repository-relative path",
          "line_start": 1,
          "line_end": 1,
          "description": "what this source supports",
          "authority": "repository|test|runtime|task|solver"
        }}
      ],
      "patch_assessment": "how the candidate satisfies or may violate it",
      "confidence": 0.0,
      "uncertainty": "counterevidence or missing evidence"
    }}
  ]
}}

Every candidate needs concrete evidence. Prefer a small set of important, distinct
specifications over generic review advice. A solver message must use authority `solver`
and cannot by itself justify a requirement. Before returning, reopen every cited path
and confirm that it is an existing repository-relative file and that the inclusive line
range contains the fact described. Never put a command, observation label, URL, or
invented filename in `path`. Runtime evidence must still cite the repository source or
test file exercised and describe the observed command result in `description`.
"""


def aggregation_prompt(
    request: GuardianRequest,
    candidates: tuple[LocalSpecification, ...],
    max_findings: int,
) -> str:
    rows = []
    for candidate in candidates:
        rows.append(
            {
                "explorer": candidate.explorer,
                "memory_id": candidate.memory_id,
                "statement": candidate.statement,
                "condition": candidate.condition,
                "evidence": [
                    {
                        "path": evidence.path,
                        "line_start": evidence.line_start,
                        "line_end": evidence.line_end,
                        "description": evidence.description,
                        "authority": evidence.authority.value,
                    }
                    for evidence in candidate.evidence
                ],
                "patch_assessment": candidate.patch_assessment,
                "confidence": candidate.confidence,
                "uncertainty": candidate.uncertainty,
            }
        )
    return f"""You are Guardian's evidence-admission reviewer.

Repository facts:
- base commit: {request.base_commit}
- candidate commit: {request.candidate_commit}

Merge duplicates, inspect the cited sources, actively search for counterevidence, and
decide whether each local specification is well-supported and whether the candidate
patch violates it. The candidate implementation is evidence about what the patch does,
not sufficient evidence for what the system should do. Solver statements are untrusted.
Use repository tools. {_patch_context(request)}

Explorer candidates:
{json.dumps(rows, indent=2)}

Guardian memory from earlier snapshots follows. Treat it as a set of hypotheses with
provenance, not as normative authority. Revalidate relevant entries against the current
snapshot. For every remembered specification you materially reassess, return its exact
`memory_id`; use `satisfied` when current evidence establishes that the candidate now
meets it, and `retracted` only when the earlier property itself is unsupported or no
longer applicable. Evidence marked stale must not justify an assessment without being
reopened:
{_memory(request)}

Return only one JSON object:
{{
  "summary": "brief review conclusion",
  "findings": [
    {{
      "memory_id": "existing spec id when reassessing one, otherwise empty",
      "statement": "admitted local specification",
      "condition": "specific triggering condition or execution path",
      "status": "violated|uncertain|satisfied|retracted",
      "evidence": [{{"path": "...", "line_start": 1, "line_end": 1,
        "description": "...", "authority": "repository|test|runtime|task|solver"}}],
      "patch_assessment": "specific candidate behavior",
      "recommendation": "property-oriented corrective action",
      "confidence": 0.0
    }}
  ]
}}

Admit at most {max_findings} violated findings. A violated finding requires non-solver
evidence, confidence >= 0.70, and a concrete mismatch in the candidate. Put plausible
but unverified or incompletely assessed items under status `uncertain`. Include satisfied
items only when they materially explain why a candidate was rejected. Reopen every
cited path before returning and verify that the repository-relative file and inclusive
line range exist. Do not emit synthetic paths for commands or runtime observations;
anchor those claims to the repository source or test that was exercised.
"""


__all__ = ["aggregation_prompt", "explorer_prompt"]
