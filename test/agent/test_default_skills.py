# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for default tool primitives: file_read + file_search (issue #145).

Two skills, both always-on:

- ``file_read`` — bounded reader with opencode-style line numbers.
- ``file_search`` — multi-mode primitive dispatching to grep / glob /
  bash back-ends via the ``mode`` argument (``content`` / ``files`` /
  ``shell``).

Test layout:
- ``TestFileRead``                 — file_read happy path + token-bound edges.
- ``TestRegexSearchContent``       — grep-style content search back-end.
- ``TestRegexSearchFiles``         — glob-style filename enumeration back-end.
- ``TestRegexSearchShell``         — shell-execution back-end.
- ``TestRegexSearchDispatch``      — the public ``_file_search`` dispatcher.
- ``TestGetDefaultSkillMetadata``  — registry-facing metadata.
- ``TestEnsureDefaultsRegistered`` — idempotent registration.
- ``TestAgentRunnerDefaults``      — defaults survive allow/exclude filters
                                     and dispatch end-to-end through the runner.
- ``TestSmokeRealFile``            — smoke test against the live runner.py.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.defaults import (
    _BASH_MAX_OUTPUT_CHARS,
    DEFAULT_SKILL_IDS,
    _file_read,
    _file_search,
    _file_search_content,
    _file_search_files,
    _file_search_shell,
    ensure_defaults_registered,
    get_default_skill_metadata,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.llm.litellm_chat import LiteLLMChat

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    """A small multi-line Python file for line-range / content tests."""
    content = textwrap.dedent(
        """\
        def add(a, b):
            return a + b

        def subtract(a, b):
            return a - b

        class Calculator:
            def multiply(self, x, y):
                return x * y
        """
    )
    p = tmp_path / "calculator.py"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    """Directory with mixed file types for content / files mode tests."""
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import foo\nfoo.run()\n", encoding="utf-8")
    (tmp_path / "readme.md").write_text(
        "# Project\nSee foo for details.\n", encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# file_read tests
# ---------------------------------------------------------------------------


class TestFileRead:
    def test_reads_full_file_with_opencode_line_format(self, sample_file):
        """All lines present, formatted as `{lineno:6d} | {content}`, 1-based."""
        result = _file_read(str(sample_file))
        assert "def add(a, b):" in result
        assert "class Calculator:" in result
        lines = result.splitlines()
        # First line is line-1, formatted with the ' | ' delimiter.
        assert lines[0].lstrip().startswith("1 |")
        for line in lines:
            assert " | " in line, f"missing ' | ' in: {line!r}"

    def test_start_and_end_line(self, sample_file):
        result = _file_read(str(sample_file), start_line=4, end_line=5)
        lines = result.splitlines()
        assert len(lines) == 2
        assert "subtract" in lines[0] and "4" in lines[0]
        assert "5" in lines[1]

    def test_end_line_clamp_to_eof(self, sample_file):
        result = _file_read(str(sample_file), end_line=9999)
        assert "Calculator" in result

    def test_max_lines_truncation_emits_next_start(self, sample_file):
        result = _file_read(str(sample_file), start_line=1, max_lines=3)
        lines = result.splitlines()
        # 3 content lines + 1 truncation notice
        assert len(lines) == 4
        assert "more lines" in lines[-1]
        assert "start_line=4" in result

    def test_missing_file_returns_error(self, tmp_path):
        result = _file_read(str(tmp_path / "nonexistent.py"))
        assert result.startswith("Error:") and "not found" in result

    def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.py"
        empty.write_text("", encoding="utf-8")
        assert "empty" in _file_read(str(empty)).lower()

    def test_invalid_range_returns_error(self, sample_file):
        """Both start>EOF and start>end report a clear error."""
        assert _file_read(str(sample_file), start_line=9999).startswith("Error:")
        assert _file_read(str(sample_file), start_line=10, end_line=5).startswith(
            "Error:"
        )


# ---------------------------------------------------------------------------
# file_search — content mode (grep-style)
# ---------------------------------------------------------------------------


class TestRegexSearchContent:
    def test_basic_match_1_based(self, sample_dir):
        result = _file_search_content("def foo", path=str(sample_dir))
        assert "a.py:1:" in result and "def foo" in result

    def test_no_match_returns_message(self, sample_dir):
        assert "No matches found" in _file_search_content(
            "ZZZNOMATCH_XYZ", path=str(sample_dir)
        )

    def test_case_sensitive_flag(self, sample_dir):
        """Default is case-insensitive; case_sensitive=True flips it."""
        (sample_dir / "cls.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
        assert "cls.py" in _file_search_content("CLASS", path=str(sample_dir))
        assert "No matches found" in _file_search_content(
            "CLASS", path=str(sample_dir), case_sensitive=True
        )

    def test_literal_mode_escapes_regex_chars(self, sample_dir):
        (sample_dir / "special.py").write_text(
            "x = a.b()\ny = a.c()\n", encoding="utf-8"
        )
        result = _file_search_content("a.b()", path=str(sample_dir), use_regex=False)
        assert "special.py" in result

    def test_include_filter(self, sample_dir):
        """include='*.py' filters out the .md file."""
        result = _file_search_content("foo", path=str(sample_dir), include="*.py")
        assert "readme.md" not in result
        assert "a.py" in result or "b.py" in result

    def test_max_results_cap(self, sample_dir):
        many = sample_dir / "many.py"
        many.write_text(
            "\n".join(f"match_{i} = 1" for i in range(100)), encoding="utf-8"
        )
        result = _file_search_content("match_", path=str(sample_dir), max_results=5)
        assert "max_results=5" in result
        hits = [line for line in result.splitlines() if "many.py" in line]
        assert len(hits) <= 5

    def test_invalid_regex_returns_error(self, sample_dir):
        result = _file_search_content("[invalid", path=str(sample_dir))
        assert result.startswith("Error:") and "invalid regex" in result

    def test_nonexistent_path_returns_error(self, tmp_path):
        result = _file_search_content("x", path=str(tmp_path / "missing"))
        assert result.startswith("Error:") and "does not exist" in result


# ---------------------------------------------------------------------------
# file_search — files mode (glob-style)
# ---------------------------------------------------------------------------


class TestRegexSearchFiles:
    def test_basic_glob_lists_matching_names(self, sample_dir):
        result = _file_search_files("*.py", path=str(sample_dir))
        names = set(result.splitlines())
        assert {"a.py", "b.py"}.issubset(names)
        assert "readme.md" not in names

    def test_recursive_double_star(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "x.py").write_text("# x", encoding="utf-8")
        (tmp_path / "src" / "nested").mkdir()
        (tmp_path / "src" / "nested" / "y.py").write_text("# y", encoding="utf-8")
        result = _file_search_files("**/*.py", path=str(tmp_path))
        lines = set(result.splitlines())
        assert "src/x.py" in lines and "src/nested/y.py" in lines

    def test_no_match_returns_message(self, sample_dir):
        result = _file_search_files("*.rs", path=str(sample_dir))
        assert result.startswith("No files match")

    def test_max_results_cap(self, tmp_path):
        for i in range(10):
            (tmp_path / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
        result = _file_search_files("*.py", path=str(tmp_path), max_results=3)
        assert "max_results=3" in result
        names = [line for line in result.splitlines() if line.endswith(".py")]
        assert len(names) <= 3

    def test_nonexistent_root_returns_error(self, tmp_path):
        result = _file_search_files("*.py", path=str(tmp_path / "missing"))
        assert result.startswith("Error:") and "does not exist" in result


# ---------------------------------------------------------------------------
# file_search — shell mode (bash-style)
# ---------------------------------------------------------------------------


class TestRegexSearchShell:
    def test_basic_echo(self):
        result = _file_search_shell("echo hello")
        assert "hello" in result and "exit code: 0" in result

    def test_stdout_and_stderr_sections(self):
        result = _file_search_shell("echo out; echo err 1>&2")
        assert "--- stdout ---" in result and "out" in result
        assert "--- stderr ---" in result and "err" in result

    def test_non_zero_exit_and_command_not_found(self):
        """`false` → exit 1; unknown command → exit 127."""
        assert "exit code: 1" in _file_search_shell("false")
        not_found = _file_search_shell("nonexistent_cmd_xyz_zzz")
        assert "exit code:" in not_found and "127" in not_found

    def test_timeout_returns_error(self):
        result = _file_search_shell("sleep 2", timeout=1)
        assert result.startswith("Error:") and "timed out" in result

    def test_cwd_argument(self, tmp_path):
        result = _file_search_shell("pwd", cwd=str(tmp_path))
        # macOS may symlink /var → /private/var; check tail.
        assert str(tmp_path) in result or tmp_path.name in result

    def test_output_truncation(self):
        result = _file_search_shell("yes hello | head -n 20000")
        assert "(output truncated)" in result
        assert len(result) <= _BASH_MAX_OUTPUT_CHARS + 200


# ---------------------------------------------------------------------------
# file_search — dispatcher
# ---------------------------------------------------------------------------


class TestRegexSearchDispatch:
    def test_mode_default_is_content(self, sample_dir):
        """Calling without `mode` should grep file contents."""
        result = _file_search("def foo", path=str(sample_dir))
        assert "a.py:1:" in result

    def test_files_mode_routes_to_glob(self, sample_dir):
        result = _file_search("*.py", mode="files", path=str(sample_dir))
        assert "a.py" in result.splitlines()

    def test_shell_mode_runs_command(self):
        result = _file_search("echo dispatched", mode="shell")
        assert "dispatched" in result and "exit code: 0" in result

    def test_invalid_mode_returns_error(self):
        result = _file_search("x", mode="bogus")
        assert result.startswith("Error:") and "invalid mode" in result


# ---------------------------------------------------------------------------
# get_default_skill_metadata
# ---------------------------------------------------------------------------


class TestGetDefaultSkillMetadata:
    def test_returns_exactly_two_defaults(self):
        ids = {m.skill_id for m in get_default_skill_metadata()}
        assert ids == DEFAULT_SKILL_IDS == {"file_read", "file_search"}

    def test_file_read_requires_path(self):
        meta = {m.skill_id: m for m in get_default_skill_metadata()}["file_read"]
        required = {i.name for i in meta.inputs if i.required}
        assert "path" in required

    def test_file_search_requires_pattern_exposes_mode(self):
        meta = {m.skill_id: m for m in get_default_skill_metadata()}["file_search"]
        required = {i.name for i in meta.inputs if i.required}
        all_inputs = {i.name for i in meta.inputs}
        assert "pattern" in required
        # mode is optional but must appear in the schema for the LLM to use.
        assert "mode" in all_inputs and "mode" not in required


# ---------------------------------------------------------------------------
# ensure_defaults_registered
# ---------------------------------------------------------------------------


class TestEnsureDefaultsRegistered:
    def test_registers_all_default_skills(self):
        reg = SkillRegistry()
        ensure_defaults_registered(reg)
        for skill_id in DEFAULT_SKILL_IDS:
            assert reg.has(skill_id), f"{skill_id!r} not registered"

    def test_idempotent_does_not_overwrite(self):
        """Second call is a no-op and preserves the original executor_fn."""
        reg = SkillRegistry()
        ensure_defaults_registered(reg)
        original_fn = reg.get("file_read").executor_fn
        ensure_defaults_registered(reg)  # would ValueError if not idempotent
        assert reg.get("file_read").executor_fn is original_fn


# ---------------------------------------------------------------------------
# AgentRunner integration: defaults always present
# ---------------------------------------------------------------------------


def _make_llm():
    llm = MagicMock(spec=LiteLLMChat)
    msg = SimpleNamespace(role="assistant", content="ok", tool_calls=None)
    llm._call_raw.return_value = SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    return llm


def _make_tool_call_response(tc):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(role="assistant", content=None, tool_calls=[tc])
            )
        ]
    )


def _make_final_response(content):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant", content=content, tool_calls=None
                )
            )
        ]
    )


class TestAgentRunnerDefaults:
    def test_defaults_in_tools_on_empty_registry(self):
        runner = AgentRunner(_make_llm(), SkillRegistry())
        names = {t["function"]["name"] for t in runner.tools}
        assert DEFAULT_SKILL_IDS.issubset(names)

    def test_defaults_survive_exclude_skills(self):
        runner = AgentRunner(
            _make_llm(), SkillRegistry(), exclude_skills=set(DEFAULT_SKILL_IDS)
        )
        names = {t["function"]["name"] for t in runner.tools}
        assert DEFAULT_SKILL_IDS.issubset(names)

    def test_defaults_survive_allow_skills(self):
        """allow_skills with a disjoint set still surfaces the defaults."""
        runner = AgentRunner(
            _make_llm(), SkillRegistry(), allow_skills={"some_other_skill"}
        )
        names = {t["function"]["name"] for t in runner.tools}
        assert DEFAULT_SKILL_IDS.issubset(names)

    def test_non_default_skill_still_excluded(self):
        from codeminer.agent.skills.core import SkillMetadata, SkillType

        reg = SkillRegistry()
        reg.register(
            SkillMetadata(
                skill_id="custom_tool",
                skill_type=SkillType.CUSTOM,
                executor_fn=lambda: "hi",
            )
        )
        runner = AgentRunner(_make_llm(), reg, exclude_skills={"custom_tool"})
        names = {t["function"]["name"] for t in runner.tools}
        assert "custom_tool" not in names
        assert DEFAULT_SKILL_IDS.issubset(names)

    def test_runner_executes_file_read(self, tmp_path):
        """End-to-end: runner dispatches a file_read tool call."""
        target = tmp_path / "hello.py"
        target.write_text("x = 1\ny = 2\n", encoding="utf-8")

        tc = SimpleNamespace(
            id="tc_fr",
            type="function",
            function=SimpleNamespace(
                name="file_read",
                arguments=json.dumps({"path": str(target)}),
            ),
        )
        llm = _make_llm()
        llm._call_raw.side_effect = [
            _make_tool_call_response(tc),
            _make_final_response("Done."),
        ]

        record = AgentRunner(llm, SkillRegistry()).run("Read it").tool_calls[0]
        assert record.error is None and "x = 1" in record.result

    def test_runner_executes_file_search_shell_mode(self):
        """End-to-end with the dispatcher: shell mode via mode argument."""
        tc = SimpleNamespace(
            id="tc_rs",
            type="function",
            function=SimpleNamespace(
                name="file_search",
                arguments=json.dumps(
                    {"pattern": "echo runner_dispatch", "mode": "shell"}
                ),
            ),
        )
        llm = _make_llm()
        llm._call_raw.side_effect = [
            _make_tool_call_response(tc),
            _make_final_response("Ran it."),
        ]

        record = AgentRunner(llm, SkillRegistry()).run("Run echo").tool_calls[0]
        assert record.error is None
        assert "runner_dispatch" in record.result
        assert "exit code: 0" in record.result


# ---------------------------------------------------------------------------
# Smoke test — reads a real source file from this repo
# ---------------------------------------------------------------------------


class TestSmokeRealFile:
    @staticmethod
    def _runner_source_path() -> Path:
        """Resolve runner.py via the already-imported AgentRunner.

        Avoids the mixed ``import ... as`` / ``from ... import`` styles
        flagged by static analysis; ``AgentRunner.__module__`` plus
        ``sys.modules`` gives the same module object without a second import.
        """
        return Path(sys.modules[AgentRunner.__module__].__file__)

    def test_file_read_runner_py(self):
        """Read the runner module itself, verify per-row line numbers appear."""
        source = self._runner_source_path()
        assert source.exists(), f"runner.py not found at {source}"

        result = _file_read(str(source), start_line=1, end_line=10)
        lines = result.splitlines()
        assert lines[0].lstrip().startswith("1 |") and len(lines) == 10

    def test_file_search_for_agent_runner_in_runner_py(self):
        """Default (content) mode regex-search for 'AgentRunner' in runner.py."""
        source = self._runner_source_path()
        result = _file_search("AgentRunner", path=str(source))
        assert "AgentRunner" in result
        assert any(":" in line for line in result.splitlines())
