"""Tests for default tool primitives: file_read and grep (issue #145).

Covers:
- file_read: happy path, line range, token-bound truncation, edge cases
- grep: match, no-match, regex/literal, include filter, max_results cap
- AgentRunner integration: defaults always present, not removable by exclude_skills
- Smoke test: read a real file from this repo
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.defaults import (
    DEFAULT_SKILL_IDS,
    _MAX_LINES_DEFAULT,
    _MAX_RESULTS_DEFAULT,
    _file_read,
    _grep,
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
    """Write a small Python-like file for testing."""
    content = textwrap.dedent(
        """\
        def add(a, b):
            return a + b

        def subtract(a, b):
            return a - b

        class Calculator:
            def multiply(self, x, y):
                return x * y

            def divide(self, x, y):
                if y == 0:
                    raise ValueError("division by zero")
                return x / y
        """
    )
    p = tmp_path / "calculator.py"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    """Directory with multiple files for grep tests."""
    (tmp_path / "a.py").write_text(
        "def foo():\n    pass\n\ndef bar():\n    return foo()\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "import foo\nfoo.run()\n",
        encoding="utf-8",
    )
    (tmp_path / "readme.md").write_text(
        "# Project\nSee foo for details.\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# file_read tests
# ---------------------------------------------------------------------------


class TestFileRead:
    def test_reads_full_file(self, sample_file):
        result = _file_read(str(sample_file))
        # Every line present
        assert "def add(a, b):" in result
        assert "class Calculator:" in result

    def test_line_numbers_are_1_based(self, sample_file):
        result = _file_read(str(sample_file))
        lines = result.splitlines()
        # First line should have line number 1
        assert lines[0].lstrip().startswith("1 |")

    def test_line_number_format_opencode_style(self, sample_file):
        """Lines must be formatted as `{lineno:6d} | {content}`."""
        result = _file_read(str(sample_file))
        for line in result.splitlines():
            # Each output line must contain ' | '
            assert " | " in line, f"Missing ' | ' in: {line!r}"

    def test_start_and_end_line(self, sample_file):
        result = _file_read(str(sample_file), start_line=4, end_line=5)
        lines = result.splitlines()
        assert len(lines) == 2
        assert "4" in lines[0]
        assert "subtract" in lines[0]
        assert "5" in lines[1]

    def test_start_line_default_is_1(self, sample_file):
        full = _file_read(str(sample_file))
        from_1 = _file_read(str(sample_file), start_line=1)
        assert full == from_1

    def test_end_line_clamp_to_eof(self, sample_file):
        # end_line beyond EOF should be silently clamped
        result = _file_read(str(sample_file), end_line=9999)
        assert "Calculator" in result  # last lines still present

    def test_max_lines_truncation(self, sample_file):
        result = _file_read(str(sample_file), max_lines=3)
        lines = result.splitlines()
        # 3 content lines + 1 truncation notice
        assert len(lines) == 4
        assert "more lines" in lines[-1]
        assert "start_line=" in lines[-1]

    def test_truncation_notice_has_next_start(self, sample_file):
        result = _file_read(str(sample_file), start_line=1, max_lines=3)
        assert "start_line=4" in result

    def test_missing_file_returns_error(self, tmp_path):
        result = _file_read(str(tmp_path / "nonexistent.py"))
        assert result.startswith("Error:")
        assert "not found" in result

    def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.py"
        empty.write_text("", encoding="utf-8")
        result = _file_read(str(empty))
        assert "empty" in result.lower()

    def test_start_beyond_eof_returns_error(self, sample_file):
        result = _file_read(str(sample_file), start_line=9999)
        assert result.startswith("Error:")
        assert "exceeds" in result

    def test_start_gt_end_returns_error(self, sample_file):
        result = _file_read(str(sample_file), start_line=10, end_line=5)
        assert result.startswith("Error:")

    def test_single_line_read(self, sample_file):
        result = _file_read(str(sample_file), start_line=1, end_line=1)
        lines = result.splitlines()
        assert len(lines) == 1
        assert "add" in lines[0]

    def test_max_lines_zero_treated_as_cap(self, sample_file):
        # max_lines=1 → only first line + truncation notice
        result = _file_read(str(sample_file), max_lines=1)
        content_lines = [l for l in result.splitlines() if " | " in l]
        assert len(content_lines) == 1


# ---------------------------------------------------------------------------
# grep tests
# ---------------------------------------------------------------------------


class TestGrep:
    def test_basic_match(self, sample_dir):
        result = _grep("def foo", path=str(sample_dir))
        assert "a.py" in result
        assert "def foo" in result

    def test_1_based_line_numbers(self, sample_dir):
        result = _grep("def foo", path=str(sample_dir))
        # a.py first line has `def foo():` → should be line 1
        assert "a.py:1:" in result

    def test_no_match_returns_message(self, sample_dir):
        result = _grep("ZZZNOMATCH_XYZ", path=str(sample_dir))
        assert "No matches found" in result

    def test_case_insensitive_default(self, sample_dir):
        # lowercase "class" should match even if capitalised in file
        (sample_dir / "cls.py").write_text("class Foo:\n    pass\n")
        result = _grep("CLASS", path=str(sample_dir))
        assert "cls.py" in result

    def test_case_sensitive_no_match(self, sample_dir):
        (sample_dir / "cls.py").write_text("class Foo:\n    pass\n")
        result = _grep("CLASS", path=str(sample_dir), case_sensitive=True)
        assert "No matches found" in result

    def test_literal_mode(self, sample_dir):
        # Pattern with regex special chars should be treated literally
        (sample_dir / "special.py").write_text("x = a.b()\ny = a.c()\n")
        result = _grep("a.b()", path=str(sample_dir), use_regex=False)
        assert "special.py" in result

    def test_include_filter_py_only(self, sample_dir):
        result = _grep("foo", path=str(sample_dir), include="*.py")
        # readme.md has 'foo' but should be filtered out
        assert "readme.md" not in result
        assert "a.py" in result or "b.py" in result

    def test_include_filter_md_only(self, sample_dir):
        result = _grep("foo", path=str(sample_dir), include="*.md")
        assert "readme.md" in result
        assert ".py" not in result

    def test_max_results_cap(self, sample_dir):
        # Create a file with many matches
        many = sample_dir / "many.py"
        many.write_text("\n".join(f"match_{i} = 1" for i in range(100)))
        result = _grep("match_", path=str(sample_dir), max_results=5)
        lines = [l for l in result.splitlines() if "many.py" in l]
        assert len(lines) <= 5
        assert "max_results=5" in result

    def test_invalid_regex_returns_error(self, sample_dir):
        result = _grep("[invalid", path=str(sample_dir))
        assert result.startswith("Error:")
        assert "invalid regex" in result

    def test_nonexistent_path_returns_error(self, tmp_path):
        result = _grep("foo", path=str(tmp_path / "no_such_dir"))
        assert result.startswith("Error:")
        assert "does not exist" in result

    def test_single_file_path(self, sample_file):
        result = _grep("def", path=str(sample_file))
        assert "def add" in result
        assert "def subtract" in result

    def test_match_format_file_lineno_content(self, sample_dir):
        """Each result line must be `{file}:{lineno}: {content}`."""
        result = _grep("def foo", path=str(sample_dir))
        for line in result.splitlines():
            if "def foo" in line:
                parts = line.split(":", 2)
                assert len(parts) >= 3, f"bad format: {line!r}"
                # second part must be an integer line number
                assert parts[1].strip().isdigit(), f"non-numeric lineno in: {line!r}"


# ---------------------------------------------------------------------------
# get_default_skill_metadata
# ---------------------------------------------------------------------------


class TestGetDefaultSkillMetadata:
    def test_returns_both_defaults(self):
        metas = get_default_skill_metadata()
        ids = {m.skill_id for m in metas}
        assert ids == DEFAULT_SKILL_IDS

    def test_returns_fresh_list(self):
        a = get_default_skill_metadata()
        b = get_default_skill_metadata()
        assert a is not b

    def test_executor_fns_callable(self):
        for meta in get_default_skill_metadata():
            assert callable(meta.executor_fn)

    def test_file_read_has_required_path_input(self):
        metas = {m.skill_id: m for m in get_default_skill_metadata()}
        file_read = metas["file_read"]
        required_inputs = [i for i in file_read.inputs if i.required]
        names = [i.name for i in required_inputs]
        assert "path" in names

    def test_grep_has_required_pattern_input(self):
        metas = {m.skill_id: m for m in get_default_skill_metadata()}
        grep = metas["grep"]
        required_inputs = [i for i in grep.inputs if i.required]
        names = [i.name for i in required_inputs]
        assert "pattern" in names


# ---------------------------------------------------------------------------
# ensure_defaults_registered
# ---------------------------------------------------------------------------


class TestEnsureDefaultsRegistered:
    def test_registers_both_skills(self):
        reg = SkillRegistry()
        ensure_defaults_registered(reg)
        for skill_id in DEFAULT_SKILL_IDS:
            assert reg.has(skill_id), f"{skill_id!r} not registered"

    def test_idempotent_second_call(self):
        reg = SkillRegistry()
        ensure_defaults_registered(reg)
        # Should not raise ValueError("already registered")
        ensure_defaults_registered(reg)

    def test_does_not_overwrite_existing(self):
        reg = SkillRegistry()
        ensure_defaults_registered(reg)
        original_fn = reg.get("file_read").executor_fn
        ensure_defaults_registered(reg)  # second call
        assert reg.get("file_read").executor_fn is original_fn


# ---------------------------------------------------------------------------
# AgentRunner integration: defaults always present
# ---------------------------------------------------------------------------


def _make_llm():
    llm = MagicMock(spec=LiteLLMChat)
    msg = SimpleNamespace(role="assistant", content="ok", tool_calls=None)
    llm._call_raw.return_value = SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    return llm


class TestAgentRunnerDefaults:
    def test_defaults_in_tools_on_empty_registry(self):
        runner = AgentRunner(_make_llm(), SkillRegistry())
        tool_names = {t["function"]["name"] for t in runner.tools}
        assert "file_read" in tool_names
        assert "grep" in tool_names

    def test_defaults_survive_exclude_skills(self):
        """Default tools must be present even if explicitly listed in exclude_skills."""
        runner = AgentRunner(
            _make_llm(),
            SkillRegistry(),
            exclude_skills={"file_read", "grep"},
        )
        tool_names = {t["function"]["name"] for t in runner.tools}
        assert "file_read" in tool_names
        assert "grep" in tool_names

    def test_non_default_skill_still_excluded(self):
        """exclude_skills should still work for non-default skills."""
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
        tool_names = {t["function"]["name"] for t in runner.tools}
        assert "custom_tool" not in tool_names
        # Defaults still present
        assert "file_read" in tool_names
        assert "grep" in tool_names

    def test_file_read_tool_schema_has_path_parameter(self):
        runner = AgentRunner(_make_llm(), SkillRegistry())
        file_read_schema = next(
            t for t in runner.tools if t["function"]["name"] == "file_read"
        )
        params = file_read_schema["function"]["parameters"]["properties"]
        assert "path" in params

    def test_grep_tool_schema_has_pattern_parameter(self):
        runner = AgentRunner(_make_llm(), SkillRegistry())
        grep_schema = next(
            t for t in runner.tools if t["function"]["name"] == "grep"
        )
        params = grep_schema["function"]["parameters"]["properties"]
        assert "pattern" in params

    def test_runner_executes_file_read_tool(self, tmp_path):
        """End-to-end: runner dispatches file_read and gets file content."""
        target = tmp_path / "hello.py"
        target.write_text("x = 1\ny = 2\n", encoding="utf-8")

        tc = SimpleNamespace(
            id="tc_1",
            type="function",
            function=SimpleNamespace(
                name="file_read",
                arguments=f'{{"path": "{target}"}}',
            ),
        )
        llm = _make_llm()
        resp1 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content=None,
                        tool_calls=[tc],
                    )
                )
            ]
        )
        resp2 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content="Done.",
                        tool_calls=None,
                    )
                )
            ]
        )
        llm._call_raw.side_effect = [resp1, resp2]

        runner = AgentRunner(llm, SkillRegistry())
        result = runner.run("Read the file")

        tool_record = result.tool_calls[0]
        assert tool_record.error is None
        assert "x = 1" in tool_record.result

    def test_runner_executes_grep_tool(self, tmp_path):
        """End-to-end: runner dispatches grep and returns matches."""
        (tmp_path / "src.py").write_text("def target_fn():\n    pass\n")

        tc = SimpleNamespace(
            id="tc_2",
            type="function",
            function=SimpleNamespace(
                name="grep",
                arguments=f'{{"pattern": "target_fn", "path": "{tmp_path}"}}',
            ),
        )
        llm = _make_llm()
        resp1 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content=None,
                        tool_calls=[tc],
                    )
                )
            ]
        )
        resp2 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content="Found it.",
                        tool_calls=None,
                    )
                )
            ]
        )
        llm._call_raw.side_effect = [resp1, resp2]

        runner = AgentRunner(llm, SkillRegistry())
        result = runner.run("Find target_fn")

        tool_record = result.tool_calls[0]
        assert tool_record.error is None
        assert "target_fn" in tool_record.result


# ---------------------------------------------------------------------------
# Smoke test — reads a real source file from this repo
# ---------------------------------------------------------------------------


class TestSmokeRealFile:
    def test_read_runner_py(self):
        """Smoke: read the runner module itself and verify line numbers appear."""
        import codeminer.agent.runner as runner_module

        source = Path(runner_module.__file__)
        assert source.exists(), f"runner.py not found at {source}"

        result = _file_read(str(source), start_line=1, end_line=10)
        assert " | " in result, "opencode-style line numbers missing"
        lines = result.splitlines()
        assert lines[0].lstrip().startswith("1 |")
        assert len(lines) == 10

    def test_grep_for_agent_runner_in_runner_py(self):
        """Smoke: grep for 'AgentRunner' in runner.py."""
        import codeminer.agent.runner as runner_module

        source = Path(runner_module.__file__)
        result = _grep("AgentRunner", path=str(source))
        assert "AgentRunner" in result
        # At least one match should have a line number
        assert any(":" in line for line in result.splitlines())
