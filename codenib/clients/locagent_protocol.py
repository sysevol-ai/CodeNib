# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free LocAgent policy assets pinned by CodeNib."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from codenib.integrations.locagent import LOCAGENT_REVISION, get_locagent_tool_schemas

LOCAGENT_PROTOCOL_REVISION = LOCAGENT_REVISION

_READ_ONLY_WARNING = (
    "IMPORTANT: You should only use the provided read-only tools, never ask "
    "for human help, and never modify files.\n"
)

_SYSTEM_PROMPT = """You are a helpful assistant that can interact with a computer to solve tasks.
<IMPORTANT>
* If user provides a path, you should NOT assume it's relative to the current working directory. Instead, you should explore the file system to find the file before working on it.
</IMPORTANT>
"""  # noqa: B950 - exact text from the pinned upstream policy

_TASK_INSTRUCTION_TEMPLATE = """
Given the following GitHub problem description, your objective is to localize the specific files, classes or functions, and lines of code that need modification or contain key information to resolve the issue.

Follow these steps to localize the issue:
## Step 1: Categorize and Extract Key Problem Information
 - Classify the problem statement into the following categories:
    Problem description, error trace, code to reproduce the bug, and additional context.
 - Identify modules in the '{package_name}' package mentioned in each category.
 - Use extracted keywords and line numbers to search for relevant code references for additional context.

## Step 2: Locate Referenced Modules
- Accurately determine specific modules
    - Explore the repo to familiarize yourself with its structure.
    - Analyze the described execution flow to identify specific modules or components being referenced.
- Pay special attention to distinguishing between modules with similar names using context and described execution flow.
- Output Format for collected relevant modules:
    - Use the format: 'file_path:QualifiedName'
    - E.g., for a function `calculate_sum` in the `MathUtils` class located in `src/helpers/math_helpers.py`, represent it as: 'src/helpers/math_helpers.py:MathUtils.calculate_sum'.

## Step 3: Analyze and Reproducing the Problem
- Clarify the Purpose of the Issue
    - If expanding capabilities: Identify where and how to incorporate new behavior, fields, or modules.
    - If addressing unexpected behavior: Focus on localizing modules containing potential bugs.
- Reconstruct the execution flow
    - Identify main entry points triggering the issue.
    - Trace function calls, class interactions, and sequences of events.
    - Identify potential breakpoints causing the issue.
    Important: Keep the reconstructed flow focused on the problem, avoiding irrelevant details.

## Step 4: Locate Areas for Modification
- Locate specific files, functions, or lines of code requiring changes or containing critical information for resolving the issue.
- Consider upstream and downstream dependencies that may affect or be affected by the issue.
- If applicable, identify where to introduce new fields, functions, or variables.
- Think Thoroughly: List multiple potential solutions and consider edge cases that could impact the resolution.

## Output Format for Final Results:
Your final output should list the locations requiring modification, wrapped with triple backticks ```
Each location should include the file path, class name (if applicable), function name, or line numbers, ordered by importance.
Your answer would better include about 5 files.

### Examples:
```
full_path1/file1.py
line: 10
class: MyClass1
function: my_function1

full_path2/file2.py
line: 76
function: MyClass2.my_function2

full_path3/file3.py
line: 24
line: 156
function: my_function3
```

Return just the location(s)

Note: Your thinking should be thorough and so it's fine if it's very long.
"""  # noqa: B950 - exact text from the pinned upstream policy

_PROBLEM_TEMPLATE = """
--- BEGIN PROBLEM STATEMENT ---
Title: {title}

{description}
--- END PROBLEM STATEMENT ---

"""

_FOLLOWUP = (
    "Verify if the found locations contain all the necessary information to "
    "address the issue, and check for any relevant references in other parts "
    "of the codebase that may not have appeared in the search results. If not, "
    "continue searching for additional locations related to the issue.\n"
    "Verify that you have carefully analyzed the impact of the found locations "
    "on the repository, especially their dependencies. If you think you have "
    "solved the task, please send your final answer (including the former "
    "answer and reranking) to user through message and then call `finish` to "
    "finish.\n"
    "IMPORTANT: YOU SHOULD NEVER ASK FOR HUMAN HELP.\n"
)

_FINISH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "Finish the interaction when the task is complete OR if the "
            "assistant cannot proceed further with the task."
        ),
    },
}


def _protocol_asset_sha256() -> str:
    payload = {
        "revision": LOCAGENT_PROTOCOL_REVISION,
        "system_prompt": _SYSTEM_PROMPT,
        "task_template": _TASK_INSTRUCTION_TEMPLATE,
        "problem_template": _PROBLEM_TEMPLATE,
        "followup": _FOLLOWUP,
        "tools": [_FINISH_TOOL, *get_locagent_tool_schemas()],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


LOCAGENT_PROTOCOL_SHA256 = _protocol_asset_sha256()


@dataclass(frozen=True, slots=True)
class LocAgentProtocol:
    """Pinned LocAgent prompts and OpenAI-compatible function schemas."""

    system_prompt: str
    instruction: str
    followup: str
    tools: tuple[Mapping[str, Any], ...]


def build_locagent_protocol(
    *,
    problem_statement: str,
    package_name: str,
) -> LocAgentProtocol:
    """Build the pinned LocAgent protocol without an upstream runtime."""

    lines = str(problem_statement or "").strip().splitlines()
    instruction = _TASK_INSTRUCTION_TEMPLATE.format(package_name=package_name)
    instruction += _PROBLEM_TEMPLATE.format(
        title=lines[0] if lines else "",
        description="\n".join(lines[1:]).strip(),
    )
    instruction += _READ_ONLY_WARNING
    tools = [copy.deepcopy(_FINISH_TOOL), *get_locagent_tool_schemas()]
    return LocAgentProtocol(
        system_prompt=_SYSTEM_PROMPT,
        instruction=instruction,
        followup=_FOLLOWUP,
        tools=tuple(tools),
    )


__all__ = [
    "LOCAGENT_PROTOCOL_REVISION",
    "LOCAGENT_PROTOCOL_SHA256",
    "LocAgentProtocol",
    "build_locagent_protocol",
]
