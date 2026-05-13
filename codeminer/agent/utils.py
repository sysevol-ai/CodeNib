# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import fnmatch
from pathlib import Path
from typing import List, Tuple


def find_matching_files_from_list(file_list: List[str], file_pattern: str) -> List[str]:
    """
    Find and return file paths matching the given keyword or pattern.

    :param file_list: A list of file paths to search through.
    :param file_pattern: A keyword or glob-style pattern.

    :return: A list of matching file paths
    """
    # If the pattern contains any of these glob-like characters, treat it as a glob pattern.
    if "*" in file_pattern or "?" in file_pattern or "[" in file_pattern:
        matching_files = fnmatch.filter(file_list, file_pattern)
    else:
        # Otherwise, treat it as a keyword search
        matching_files = [file for file in file_list if file_pattern in file]

    return matching_files


def merge_intervals(intervals: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """
    Merge overlapping intervals (inclusive).

    :param intervals: List of (start, end) tuples representing intervals
    :return: List of merged intervals
    """
    if not intervals:
        return []

    # Sort the intervals based on the starting value of each tuple
    intervals.sort(key=lambda interval: interval[0])

    merged_intervals = [intervals[0]]

    for current in intervals[1:]:
        last = merged_intervals[-1]

        # Check if there is overlap
        if current[0] <= last[1]:
            # If there is overlap, merge the intervals
            merged_intervals[-1] = (last[0], max(last[1], current[1]))
        else:
            # If there is no overlap, just add the current interval to the result list
            merged_intervals.append(current)

    return merged_intervals


def get_all_python_files(repo_path: str) -> List[str]:
    # TODO: expand to C++
    all_file_paths = []
    repo_path_obj = Path(repo_path)
    for py_file in repo_path_obj.rglob("*.py"):
        relative_path = str(py_file.relative_to(repo_path_obj))
        all_file_paths.append(relative_path)
    return all_file_paths


def is_entity_definition(line: str, entity_name: str) -> bool:
    """
    Check if line contains the definition of the given entity.

    Args:
        line (str): The line to check.
        entity_name (str): The name of the entity to search for.

    Returns:
        bool: True if the line contains the definition of the given entity, False otherwise.
    """
    line_stripped = line.strip()
    return (
        line_stripped.startswith(f"class {entity_name}")
        or line_stripped.startswith(f"def {entity_name}")
        or f"class {entity_name}(" in line_stripped
        or f"def {entity_name}(" in line_stripped
        or entity_name in line_stripped
    )


def extract_entity_content(lines: List[str], start_index: int) -> str:
    """
    Extract the content of an entity (class/function) from the given start index.

    Args:
        lines (List[str]): The list of lines to search in.
        start_index (int): The index of the line to start searching from.

    Returns:
        str: The content of the entity.
    """
    start_line = start_index
    end_line = len(lines) - 1
    base_indent = len(lines[start_line]) - len(lines[start_line].lstrip())

    # Find the end of this entity by looking for next item at same or lower indentation
    for j in range(start_line + 1, len(lines)):
        next_line = lines[j]
        if (
            next_line.strip()
            and len(next_line) - len(next_line.lstrip()) <= base_indent
        ):
            if (
                next_line.strip().startswith("class ")
                or next_line.strip().startswith("def ")
                or next_line.strip().startswith("@")
            ):
                end_line = j - 1
                break

    return "\n".join(lines[start_line : end_line + 1])


def search_entity_occurrence(lines: List[str], entity_name: str, file_name: str) -> str:
    """
    Search for any occurrence of the entity name in the file.

    Args:
        lines (List[str]): The list of lines to search in.
        entity_name (str): The name of the entity to search for.
        file_name (str): The name of the file to search in.

    Returns:
        str: The search results.
    """
    for i, line in enumerate(lines):
        if entity_name in line:
            start = max(0, i - 3)
            end = min(len(lines), i + 4)
            context_lines = lines[start:end]
            matching_lines = [
                f"Line {start + j + 1}: {context_lines[j]}"
                for j in range(len(context_lines))
            ]
            return (
                f"**Found {entity_name!r} in {file_name}:**\n```python\n"
                + "\n".join(matching_lines)
                + "\n```\n\n"
            )

    return f"Entity {entity_name!r} not found in {file_name}\n\n"
