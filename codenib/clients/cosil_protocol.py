# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Frozen prompt assets for the pinned CoSIL localization policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from codenib.integrations.cosil import COSIL_REVISION

COSIL_PROTOCOL_REVISION = COSIL_REVISION

_BUG_REPORT = """
The bug report is as follows:
```
### GitHub Problem Description ###
{problem_statement}

###

### Candidate Files ###
{structure}

###
```
"""

_BUG_REPORT_NO_STRUCTURE = """
The bug report is as follows:
```
### GitHub Problem Description ###
{problem_statement}

###
"""

_FILE_SYSTEM = """
You will be presented with a bug report with repository structure to access the source code of the system under test (SUT).
Your task is to locate the top-5 most likely culprit files based on the bug report.
"""  # noqa: B950 - exact text from the pinned upstream policy

_FILE_GUIDANCE = """
Let's locate the faulty file step by step using reasoning.\x20
In order to locate accurately, you can pre-select {pre_select_num} files, then check them through function calls, and finally confirm {top_n} file names.
"""  # noqa: B950 - exact text from the pinned upstream policy

_FILE_SUMMARY = """
Based on the available information, reconfirm and provide complete name of the top-5 most likely culprit files for the bug.\x20
Since your answer will be processed automatically, please give your answer in the format as follows.
The returned files should be separated by new lines ordered by most to least important and wrapped with ```.
```
file1.py
file2.py
file3.py
file4.py
file5.py
```
Replace the 'file1.py' with the actual file path.
For example,\x20
```
sklearn/linear_model/__init__.py
sklearn/base.py
```
"""  # noqa: B950 - exact text from the pinned upstream policy

_FORMAT_CORRECT = """
Here is a localization result, but it seems not in the correct format. Please correct it.
The returned files should be separated by new lines ordered by most to least important and wrapped with ```
This is an example of expected output:
```
sklearn/linear_model/__init__.py
sklearn/base.py
```

Please help me corrct the following result.
{res}
"""  # noqa: B950 - exact text from the pinned upstream policy

_FILE_REFLECTION = """
Please look through the following GitHub problem description and Repository structure and provide a list of files that one would need to edit to fix the problem.
I have already find 5 relevent files. Accrording to the import relations, construct the call graph first.
Then, Rank them again and reflect the result.

### GitHub Problem Description ###
{problem_statement}

###

### Repository Structure ###
{structure}

###

### Files To Be Ranked ###
{pre_files}

###

### Import Relations ###
{import_content}
###


Please only provide the full path and return top 5 files.
The returned files should be separated by new lines ordered by most to least important and wrapped with ```
For example:
```
file1.py
file2.py
file3.py
file4.py
file5.py
```
Note: file1.py indicates the top-1 file, file2.py indicates the top-2 file, and so on. Do not include test files.
"""  # noqa: B950 - exact text from the pinned upstream policy

_LOCATION_SYSTEM = """
You will be presented with a bug report and tools to access the source code of the system under test (SUT).
Since the modification is based on the code repository, the modified locations may include files, classes, and functions, and the modifications may be in the form of addition, deletion, or update.
Your task is to locate the top-5 most likely culprit locations based on the bug report and the information you retrieve using the provided tools.

The tools available to you are:
* get_code_of_class(file_name, class_name) -> source code of a class.
* get_code_of_class_function(file_name, class_name, func_name) -> source code of a method defined in a class.
* get_code_of_file_function(file_name, func_name) -> source code of a top-level (static) function in a file.
* exit() -> stop calling tools and proceed to your final answer.

Working rules:
- Do NOT guess the culprit from the file structure alone. You MUST first retrieve and read the actual
  source code with the tools before deciding a location is relevant.
- Inspect candidates one or a few at a time, reason over the returned code, then continue.
- Avoid repeating an identical tool call.
- You have at most {max_try} tool-call rounds. As soon as you are confident, call exit() to finish early.
- Do NOT produce the final top-5 answer while you are still calling tools; you will be explicitly asked
  for the final answer after the tool phase ends.
"""  # noqa: B950 - exact text from the pinned upstream policy

_LOCATION_GUIDANCE = """
Let's locate the faulty location (class / function) step by step using reasoning and tool calls.
I have pre-identified top-5 files that may contain bugs. Their structures are as follows:
{bug_file_list}
The tool parameter 'file_name' takes the value in "file:".
The tool parameter 'class_name' takes the value in "class:".
The tool parameter 'func_name' takes the value in "static functions:" and "class functions:".
Use the tools as follows:
* For a class, use 'get_code_of_class(file_name, class_name)'.
* For a class method (in "class functions:"), use 'get_code_of_class_function(file_name, class_name, func_name)'.
* For a static/top-level function (in "static functions:"), use 'get_code_of_file_function(file_name, func_name)'.
* When you are confident about the culprit locations, call 'exit()' to finish.
Avoid making multiple identical tool calls to save overhead.
In order to locate accurately, pre-select {pre_select_num} locations, inspect their code through tool calls, and finally confirm {top_n} locations.
"""  # noqa: B950 - exact text from the pinned upstream policy

_LOCATION_SUMMARY = """
Based on the available information, reconfirm and provide the complete names of the top-5 most likely culprit locations for the bug.
Before making the final decision, please check whether the function name is correct or not. For static (top-level) functions, do NOT add a class name.
{bug_file_list}

Since your answer will be processed automatically, return ONLY an XML document in the exact structure below,
ordered from most to least likely. Do not add any text outside the <locations> element.
For each location:
- <file> is the full file path.
- <type> is either "function" or "class".
- <name> is "ClassName.MethodName" for a class method, "FunctionName" for a static/top-level function,
  or "ClassName" for a whole class.

<locations>
  <location>
    <file>top1_file_fullpath.py</file>
    <type>function</type>
    <name>Class1.Function1</name>
  </location>
  <location>
    <file>top2_file_fullpath.py</file>
    <type>function</type>
    <name>Function2</name>
  </location>
  <location>
    <file>top3_file_fullpath.py</file>
    <type>class</type>
    <name>Class3</name>
  </location>
  <location>
    <file>top4_file_fullpath.py</file>
    <type>function</type>
    <name>Class4.Function4</name>
  </location>
  <location>
    <file>top5_file_fullpath.py</file>
    <type>function</type>
    <name>Function5</name>
  </location>
</locations>

For example:
<locations>
  <location>
    <file>sklearn/linear_model/__init__.py</file>
    <type>function</type>
    <name>LinearRegression.fit</name>
  </location>
</locations>
"""  # noqa: B950 - exact text from the pinned upstream policy

_SUMMARY_SYSTEM = (
    "You are a debugging assistant.  Based on the bug report and the code "
    "retrieved above, output ONLY the XML locations summary as instructed.  "
    "Do NOT make any tool calls; you are in the final summary phase."
)

_PRUNE_SYSTEM = (
    "You are a debugging assistant that judges code relevance to a bug report."
)

_PRUNE_DECISION = """
Based on everything you have inspected, decide whether the original retrieved code is
related to the bug and should be added into context.
Since your answer will be processed automatically, please give your answer in the format as follows.
The returned content should be wrapped with ```.
```
True
```
or
```
False
```
"""  # noqa: B950 - exact text from the pinned upstream policy


@dataclass(frozen=True, slots=True)
class CoSILProtocol:
    """Prompts and message builders for CoSIL's RQ1 localization path."""

    problem_statement: str

    def file_messages(
        self,
        *,
        structure: str,
        max_retry: int,
    ) -> list[dict[str, str]]:
        bug_report = _BUG_REPORT.format(
            problem_statement=self.problem_statement,
            structure=structure,
        )
        guidance = _FILE_GUIDANCE.format(
            pre_select_num=int(max_retry * 0.75),
            top_n=int(max_retry / 2),
        )
        return [
            {"role": "system", "content": _FILE_SYSTEM},
            {
                "role": "user",
                "content": f"\n{bug_report}\n{guidance}\n{_FILE_SUMMARY}\n",
            },
        ]

    def correction_prompt(self, result: str) -> str:
        return _FORMAT_CORRECT.format(res=result)

    def reflection_prompt(
        self,
        *,
        structure: str,
        pre_files: list[str],
        import_content: str,
    ) -> str:
        return _FILE_REFLECTION.format(
            problem_statement=self.problem_statement,
            structure=structure,
            pre_files=pre_files,
            import_content=import_content,
        )

    def location_messages(
        self,
        *,
        candidate_structure: str,
        max_rounds: int,
    ) -> list[dict[str, str]]:
        bug_report = _BUG_REPORT_NO_STRUCTURE.format(
            problem_statement=self.problem_statement
        ).strip()
        guidance = _LOCATION_GUIDANCE.format(
            bug_file_list=candidate_structure,
            pre_select_num=7,
            top_n=5,
        )
        return [
            {
                "role": "system",
                "content": _LOCATION_SYSTEM.format(max_try=max_rounds),
            },
            {"role": "user", "content": f"\n{bug_report}\n{guidance}\n"},
        ]

    def summary_messages(
        self,
        *,
        candidate_structure: str,
        collected_code: Sequence[tuple[str, Mapping[str, Any], str]],
    ) -> list[dict[str, str]]:
        bug_report = _BUG_REPORT_NO_STRUCTURE.format(
            problem_statement=self.problem_statement
        ).strip()
        retrieved = ""
        if collected_code:
            blocks = "\n\n".join(
                (
                    f"Tool: {name}\n"
                    f"Arguments: {json.dumps(dict(arguments), sort_keys=True)}\n"
                    f"<code>\n{content}\n</code>"
                )
                for name, arguments, content in collected_code
            )
            retrieved = f"Relevant code retrieved during analysis:\n\n{blocks}\n\n"
        summary = _LOCATION_SUMMARY.format(bug_file_list=candidate_structure)
        return [
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"{bug_report}\n\n{candidate_structure}\n\n" f"{retrieved}{summary}"
                ),
            },
        ]

    def prune_messages(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        tool_result: str,
    ) -> list[dict[str, str]]:
        quoted_tool_name = f"'{tool_name}'"  # noqa: B907 - pinned prompt text
        prompt = f"""
You will be presented with a bug report with repository structure to access the source code of the system under test (SUT).
Your task is to decide whether a retrieved function/class is related to the bug and should be kept in context.
<bug report>
{self.problem_statement}
</bug report>

Here is a result of a function/class code retrieved by {quoted_tool_name} with arguments {arguments}.
<code>
{tool_result}
</code>

You may call the provided tools to inspect related code (e.g. functions it calls or the
class it belongs to) before making your decision. Investigate first; do not answer yet.
"""  # noqa: B950 - exact text from the pinned upstream policy
        return [
            {"role": "system", "content": _PRUNE_SYSTEM},
            {"role": "user", "content": prompt},
        ]

    @property
    def prune_decision_prompt(self) -> str:
        return _PRUNE_DECISION


def build_cosil_protocol(problem_statement: str) -> CoSILProtocol:
    problem = str(problem_statement or "").strip()
    if not problem:
        raise ValueError("CoSIL requires a non-empty problem statement")
    return CoSILProtocol(problem_statement=problem)


__all__ = [
    "COSIL_PROTOCOL_REVISION",
    "CoSILProtocol",
    "build_cosil_protocol",
]
