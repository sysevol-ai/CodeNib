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

Use read-only repository tools. Inspect
`git diff {request.base_commit}..{request.candidate_commit}`
and then follow relevant repository evidence beyond the changed files. Do not infer that
old behavior is normatively correct merely because it existed. Do not use the candidate
implementation itself as the sole evidence for a requirement.

Participant context follows. It can identify intended areas or uncertainty, but it is
not authoritative evidence and may be wrong:
{_context(request)}

Return only one JSON object with this shape:
{{
  "candidates": [
    {{
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
and cannot by itself justify a requirement.
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
Use `git diff {request.base_commit}..{request.candidate_commit}` and repository tools.

Explorer candidates:
{json.dumps(rows, indent=2)}

Return only one JSON object:
{{
  "summary": "brief review conclusion",
  "findings": [
    {{
      "statement": "admitted local specification",
      "status": "violated|uncertain|satisfied",
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
items only when they materially explain why a candidate was rejected.
"""


__all__ = ["aggregation_prompt", "explorer_prompt"]
