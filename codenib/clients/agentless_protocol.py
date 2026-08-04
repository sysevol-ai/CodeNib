# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Frozen prompt contract for classic Agentless localization.

The prompt wording follows Agentless v1.5.0 (MIT), pinned by
``AGENTLESS_PROTOCOL_REVISION``. Keeping it in a dependency-free module lets
CodeNib validate drift against an optional upstream checkout without importing
Agentless at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass

from codenib.integrations.agentless import AGENTLESS_REVISION

AGENTLESS_PROTOCOL_REVISION = AGENTLESS_REVISION

_FILE_PROMPT = """
Please look through the following GitHub problem description and Repository structure and provide a list of files that one would need to edit to fix the problem.

### GitHub Problem Description ###
{problem_statement}

###

### Repository Structure ###
{structure}

###

Please only provide the full path and return at most 5 files.
The returned files should be separated by new lines ordered by most to least important and wrapped with ```
For example:
```
file1.py
file2.py
```
"""  # noqa: B950 - exact text from the pinned upstream policy

_SYMBOL_PROMPT = """
Please look through the following GitHub Problem Description and the Skeleton of Relevant Files.
Identify all locations that need inspection or editing to fix the problem, including directly related areas as well as any potentially related global variables, functions, and classes.
For each location you provide, either give the name of the class, the name of a method in a class, the name of a function, or the name of a global variable.

### GitHub Problem Description ###
{problem_statement}

### Skeleton of Relevant Files ###
{file_contents}

###

Please provide the complete set of locations as either a class name, a function name, or a variable name.
Note that if you include a class, you do not need to list its specific methods.
You can include either the entire class or don't include the class name and instead include specific methods in the class.
### Examples:
```
full_path1/file1.py
function: my_function_1
class: MyClass1
function: MyClass2.my_method

full_path2/file2.py
variable: my_var
function: MyClass3.my_method

full_path3/file3.py
function: my_function_2
function: my_function_3
function: MyClass4.my_method_1
class: MyClass5
```

Return just the locations.
"""  # noqa: B950 - exact text from the pinned upstream policy

_LINE_PROMPT = """
Please review the following GitHub problem description and relevant files, and provide a set of locations that need to be edited to fix the issue.
The locations can be specified as class names, function or method names, or exact line numbers that require modification.

### GitHub Problem Description ###
{problem_statement}

###
{file_contents}

###

Please provide the class name, function or method name, or the exact line numbers that need to be edited.
### Examples:
```
full_path1/file1.py
line: 10
class: MyClass1
line: 51

full_path2/file2.py
function: MyClass2.my_method
line: 12

full_path3/file3.py
function: my_function
line: 24
line: 156
```

Return just the location(s)
"""  # noqa: B950 - exact text from the pinned upstream policy


@dataclass(frozen=True, slots=True)
class AgentlessProtocol:
    """Three prompt stages used by classic Agentless localization."""

    problem_statement: str

    def file_prompt(self, structure: str) -> str:
        return _FILE_PROMPT.format(
            problem_statement=self.problem_statement,
            structure=structure,
        ).strip()

    def symbol_prompt(self, file_contents: str) -> str:
        return _SYMBOL_PROMPT.format(
            problem_statement=self.problem_statement,
            file_contents=file_contents,
        ).strip()

    def line_prompt(self, file_contents: str) -> str:
        return _LINE_PROMPT.format(
            problem_statement=self.problem_statement,
            file_contents=file_contents,
        ).strip()


def build_agentless_protocol(problem_statement: str) -> AgentlessProtocol:
    """Build the pinned protocol for one non-empty issue statement."""

    problem = str(problem_statement or "").strip()
    if not problem:
        raise ValueError("Agentless requires a non-empty problem statement")
    return AgentlessProtocol(problem_statement=problem)


__all__ = [
    "AGENTLESS_PROTOCOL_REVISION",
    "AgentlessProtocol",
    "build_agentless_protocol",
]
