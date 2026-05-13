# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import os
from typing import List, Optional

from ..search import CodeSearchEngine
from .utils import (
    extract_entity_content,
    find_matching_files_from_list,
    get_all_python_files,
    is_entity_definition,
    search_entity_occurrence,
)


def search_code_snippets(
    search_terms: Optional[List[str]] = None,
    line_nums: Optional[List] = None,
    file_path_or_pattern: Optional[str] = "**/*.py",
    repo_path: Optional[str] = None,
) -> str:
    """
    Searches the codebase to retrieve relevant code snippets based on given queries(terms or line numbers).
    This function supports retrieving the complete content of a code entity,
    searching for code entities such as classes or functions by keywords, or locating specific lines within a file.
    It also supports filtering searches based on a file path or file pattern.

    Note:
    1. If `search_terms` are provided, it searches for code snippets based on each term:
        - If a term is formatted as 'file_path:QualifiedName' (e.g., 'src/helpers/math_helpers.py:MathUtils.calculate_sum') ,
          or just 'file_path', the corresponding complete code is retrieved or file content is retrieved.
        - If a term matches a file, class, or function name, matched entities are retrieved.
        - If there is no match with any module name, it attempts to find code snippets that likely contain the term.
    2. If `line_nums` is provided, it searches for code snippets at the specified lines within the file defined by
       `file_path_or_pattern`.

    Args:
        search_terms (Optional[List[str]]): A list of names, keywords, or code snippets to search for within the codebase.
            Terms can be formatted as 'file_path:QualifiedName' to search for a specific module or entity within a file
            (e.g., 'src/helpers/math_helpers.py:MathUtils.calculate_sum') or as 'file_path' to retrieve the complete content
            of a file. This can also include potential function names, class names, or general code fragments.
        line_nums (Optional[List[int]]): Specific line numbers to locate code snippets within a specified file.
            When provided, `file_path_or_pattern` must specify a valid file path.
        file_path_or_pattern (Optional[str]): A glob pattern or specific file path used to filter search results
            to particular files or directories. Defaults to '**/*.py', meaning all Python files are searched by default.
            If `line_nums` are provided, this must specify a specific file path.
        repo_path (Optional[str]): Path to the repository. If not provided, uses current working directory.

    Returns:
        str: The search results, which may include code snippets, matching entities, or complete file content.

    Example Usage:
        # Search for the full content of a specific file
        result = search_code_snippets(search_terms=['src/my_file.py'])
        # Search for a specific function
        result = search_code_snippets(search_terms=['src/my_file.py:MyClass.func_name'])
        # Search for specific lines (10 and 15) within a file
        result = search_code_snippets(line_nums=[10, 15], file_path_or_pattern='src/example.py')
        # Combined search for a module name and within a specific file pattern
        result = search_code_snippets(search_terms=["MyClass"], file_path_or_pattern="src/**/*.py")
    """

    if repo_path is None:
        repo_path = os.getcwd()  # TODO: set default repo path (maybe from config)
    repo_path = os.path.abspath(repo_path)

    # TODO: expand to C++
    all_files = get_all_python_files(repo_path)
    if not file_path_or_pattern:
        include_files = all_files
    else:
        include_files = find_matching_files_from_list(all_files, file_path_or_pattern)

    result = ""

    # 1. Line Number Search
    if line_nums and file_path_or_pattern:
        result += _search_by_line_numbers(
            line_nums, file_path_or_pattern, repo_path, include_files
        )

    # 2. Search Terms
    if search_terms:
        for term in search_terms:
            term = term.strip().strip(".")
            if not term:
                continue

            result += f'## Searching for term "{term}"...\n### Search Result:\n'

            # 2.1 Check if term is file_path:entity format
            if ":" in term:
                result += _search_file_entity(term, repo_path, include_files)
            # 2.2 Check if term is a direct file path
            elif term in include_files:
                result += _search_file_content(term, repo_path)
            # 2.3 Use search engine
            else:
                result += _search_with_engine(term, repo_path, include_files)

    return result.strip() if result.strip() else "No results found."


def _search_by_line_numbers(
    line_nums: List[int], file_path: str, repo_path: str, include_files: List[str]
) -> str:
    """
    Search for specific line numbers in a file.

    Args:
        line_nums (List[int]): List of line numbers to search for.
        file_path (str): Path to the file to search in.
        repo_path (str): Path to the repository.
        include_files (List[str]): List of files to search in.

    Returns:
        str: The search results.
    """
    if isinstance(line_nums, int):
        line_nums = [line_nums]

    if file_path not in include_files:
        return f"File {file_path} not found in included files.\n\n"

    full_path = os.path.join(repo_path, file_path)
    if not os.path.exists(full_path):
        return f"File not found: {file_path}\n\n"

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        result = f'## Searching for lines {line_nums} in file "{file_path}"...\n### Search Result:\n'

        for line_num in line_nums:
            if 1 <= line_num <= len(lines):
                # Show context around the line (±3 lines)
                start = max(1, line_num - 3)
                end = min(len(lines), line_num + 3)

                result += f"**Line {line_num} context:**\n```python\n"
                for i in range(start, end + 1):
                    prefix = ">>>" if i == line_num else "   "
                    result += f"{prefix} {i:4d}: {lines[i-1]}"
                result += "```\n\n"
            else:
                result += (
                    f"Line {line_num} is out of range (file has {len(lines)} lines)\n\n"
                )

        return result
    except Exception as e:
        return f"Error processing line numbers in {file_path}: {str(e)}\n\n"


def _search_file_entity(term: str, repo_path: str, include_files: List[str]) -> str:
    """
    Search for a specific entity (class/function) in a file.

    Args:
        term (str): The term to search for.
        repo_path (str): Path to the repository.
        include_files (List[str]): List of files to search in.

    Returns:
        str: The search results.
    """
    file_part, entity_part = term.split(":", 1)

    if file_part not in include_files:
        return f"File {file_part} not found in included files.\n\n"

    full_path = os.path.join(repo_path, file_part)
    if not os.path.exists(full_path):
        return f"File not found: {file_part}\n\n"

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            file_content = f.read()

        lines = file_content.split("\n")

        # Look for class or function definitions matching the entity
        for i, line in enumerate(lines):
            if is_entity_definition(line, entity_part):
                entity_content = extract_entity_content(lines, i)
                return f"**Entity: {entity_part} in {file_part}**\n```python\n{entity_content}\n```\n\n"

        # If entity not found, search for any occurrence
        return search_entity_occurrence(lines, entity_part, file_part)

    except Exception as e:
        return f"Error reading file {file_part}: {str(e)}\n\n"


def _search_file_content(file_path: str, repo_path: str) -> str:
    """
    Search and return the complete content of a file.

    Args:
        file_path (str): Path to the file to search in.
        repo_path (str): Path to the repository.

    Returns:
        str: The search results.
    """
    full_path = os.path.join(repo_path, file_path)
    if not os.path.exists(full_path):
        return f"File not found: {file_path}\n\n"

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            file_content = f.read()
        return f"**File: {file_path}**\n```python\n{file_content}\n```\n\n"
    except Exception as e:
        return f"Error reading file {file_path}: {str(e)}\n\n"


def _search_with_engine(term: str, repo_path: str, include_files: List[str]) -> str:
    """
    Use the CodeSearchEngine to find code snippets.

    Args:
        term (str): The term to search for.
        repo_path (str): Path to the repository.
        include_files (List[str]): List of files to search in.

    Returns:
        str: The search results.
    """
    try:
        search_engine = CodeSearchEngine(
            repo_path=repo_path,
            instance_id="default_search_session",
        )

        search_results = search_engine.search(
            problem_statement=term, use_keyword_extraction=False
        )

        if not search_results:
            return f"No results found for term: {term}\n\n"

        result = ""
        match_counter = 0

        for search_result in search_results:
            score = search_result.get("score", 0.0)
            file_path = search_result.get("file", search_result.get("name", ""))
            node_type = search_result.get("node_type", "unknown")

            if include_files and file_path not in include_files:
                continue

            match_counter += 1
            result += f"**Match {match_counter}: {file_path}** (Type: {node_type}, Score: {score:.3f})\n"

            try:
                code_context = search_engine.get_code_context(search_result)
                if code_context:
                    # TODO: limit context to reasonable size?
                    result += f"```python\n{code_context}\n```\n\n"
                else:
                    result += "No code context available.\n\n"
            except Exception as ctx_e:
                result += f"Error getting code context: {str(ctx_e)}\n\n"

        if match_counter == 0:
            return f"No results found for term: {term} in specified file pattern.\n\n"

        return result

    except Exception as e:
        return f"Error in fallback search: {str(e)}\n\n"


# TODO
# 'get_entity_contents',
# 'explore_tree_structure',
