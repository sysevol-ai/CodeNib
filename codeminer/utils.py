# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0


def is_test_file(nid):
    """Check if a node ID belongs to a test file."""
    if ":" in nid:
        file_path = nid.split(":")[0]
    else:
        file_path = nid

    normalized = file_path.replace("\\", "/").lower()
    path_parts = [part for part in normalized.split("/") if part]
    if not path_parts:
        return False

    file_name = path_parts[-1]
    dir_parts = path_parts[:-1]

    # Directory-based heuristics.
    test_dirs = {"test", "tests", "__tests__", "spec", "specs"}
    if any(part in test_dirs for part in dir_parts):
        return True

    # Filename-based heuristics.
    if file_name.startswith("test"):
        return True
    if file_name.endswith(
        ("_test.py", "_test.js", "_test.ts", "_spec.py", "_spec.js", "_spec.ts")
    ):
        return True
    if ".test." in file_name or ".spec." in file_name:
        return True

    return False


def wrap_code_snippet(code_snippet, start_line, end_line):
    """Wrap code snippet with line numbers"""
    lines = code_snippet.split("\n")

    # Remove trailing empty lines caused by trailing newlines
    while lines and lines[-1] == "":
        lines.pop()

    # Handle None values for files (use 0-based line numbering)
    if start_line is None:
        start_line = 0
    if end_line is None:
        end_line = len(lines) - 1

    max_line_number = start_line + len(lines) - 1
    number_width = len(str(max_line_number))
    return "\n".join(
        f"{str(i + start_line).rjust(number_width)} | {line}"
        for i, line in enumerate(lines)
    )
