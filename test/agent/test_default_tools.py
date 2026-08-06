# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for default tool primitives: read / grep / glob / bash.

Four always-on tools, shaped after the mainstream Claude Code tool reference
(one focused tool per concern instead of the old ``file_search`` multiplexer):

- ``read``  — bounded reader with opencode-style 1-based line numbers.
- ``grep``  — regex content search (content / files_with_matches / count).
- ``glob``  — filename enumeration.
- ``bash``  — shell command execution.

Test layout:
- ``TestRead``                     — read happy path + token-bound edges.
- ``TestGrep``                     — content / files / count modes + filters.
- ``TestGlob``                     — filename enumeration.
- ``TestBash``                     — shell execution.
- ``TestGetDefaultToolSpecs``      — registry-facing metadata + enum schema.
- ``TestEnsureDefaultToolsRegistered`` — idempotent registration.
- ``TestAgentRunnerDefaults``      — defaults survive allow/exclude filters
                                     and dispatch end-to-end through the runner.
- ``TestSmokeRealFile``            — smoke test against the live runner.py.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codenib.agent.runner import AgentRunner
from codenib.agent.skills.registry import SkillRegistry
from codenib.agent.tool_schema import tool_to_schema
from codenib.agent.tools.defaults import (
    _BASH_MAX_OUTPUT_CHARS,
    _MAX_LINES_LIMIT,
    _MAX_RESULTS_LIMIT,
    _READ_MAX_OUTPUT_CHARS,
    DEFAULT_TOOL_IDS,
    _bash,
    _BoundedPipeOutput,
    _glob,
    _grep,
    _read,
    ensure_default_tools_registered,
    get_default_tool_specs,
)
from codenib.agent.tools.spec import ToolRegistry
from codenib.llm.litellm_chat import LiteLLMChat

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
# read
# ---------------------------------------------------------------------------


class TestRead:
    def test_reads_full_file_with_opencode_line_format(self, sample_file):
        """All lines present, formatted as `{lineno:6d} | {content}`, 1-based."""
        result = _read(str(sample_file))
        assert "def add(a, b):" in result
        assert "class Calculator:" in result
        lines = result.splitlines()
        assert lines[0].lstrip().startswith("1 |")
        for line in lines:
            assert " | " in line, f"missing ' | ' in: {line!r}"

    def test_offset_and_limit_window(self, sample_file):
        result = _read(str(sample_file), offset=4, limit=2)
        lines = result.splitlines()
        # 2 content lines (limit=2); line 6 is blank so no truncation notice for
        # this tiny file is asserted here — just the window contents.
        assert "subtract" in lines[0] and "4" in lines[0]
        assert "5" in lines[1]

    def test_limit_truncation_emits_next_offset(self, sample_file):
        result = _read(str(sample_file), offset=1, limit=3)
        lines = result.splitlines()
        # 3 content lines + 1 truncation notice
        assert len(lines) == 4
        assert "more lines" in lines[-1]
        assert "offset=4" in result

    def test_missing_file_returns_error(self, tmp_path):
        result = _read(str(tmp_path / "nonexistent.py"))
        assert result.startswith("Error:") and "not found" in result

    def test_directory_returns_error(self, tmp_path):
        result = _read(str(tmp_path))
        assert result.startswith("Error:") and "directory" in result

    def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.py"
        empty.write_text("", encoding="utf-8")
        assert "empty" in _read(str(empty)).lower()

    def test_offset_beyond_eof_returns_error(self, sample_file):
        assert _read(str(sample_file), offset=9999).startswith("Error:")

    def test_stops_after_detecting_one_omitted_line(self, tmp_path, monkeypatch):
        source = tmp_path / "source.py"
        source.write_text("placeholder\n", encoding="utf-8")

        def bounded_lines(_handle):
            yield b"first\n", False
            yield b"second\n", False
            raise AssertionError("read scanned beyond the first omitted line")

        monkeypatch.setattr(
            "codenib.agent.tools.defaults._bounded_binary_lines",
            bounded_lines,
        )

        result = _read(str(source), limit=1)

        assert "first" in result
        assert "offset=2" in result

    def test_bounds_long_lines_and_total_output(self, tmp_path):
        source = tmp_path / "long.py"
        source.write_bytes((b"x" * 1_000_000 + b"\n") * 4)

        result = _read(str(source), limit=4)

        assert "[line truncated]" in result
        assert len(result) <= _READ_MAX_OUTPUT_CHARS + 100
        assert "use offset=" in result

    def test_rejects_nonregular_files(self, tmp_path):
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO creation is unavailable")
        fifo = tmp_path / "input.fifo"
        os.mkfifo(fifo)

        assert "not a regular file" in _read(str(fifo))

    def test_rejects_unbounded_line_limits(self, sample_file):
        result = _read(str(sample_file), limit=_MAX_LINES_LIMIT + 1)
        assert result.startswith("Error: limit must not exceed")


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


class TestGrep:
    def test_basic_match_1_based(self, sample_dir):
        result = _grep("def foo", path=str(sample_dir))
        assert "a.py:1:" in result and "def foo" in result

    def test_content_output_has_stable_field_spacing(self, sample_dir):
        result = _grep("def foo", path=str(sample_dir))

        assert "a.py:1: def foo():" in result.splitlines()

    def test_no_match_returns_message(self, sample_dir):
        assert "No matches found" in _grep("ZZZNOMATCH_XYZ", path=str(sample_dir))

    def test_case_insensitive_default_then_disabled(self, sample_dir):
        """Default is case-insensitive; case_insensitive=False flips it."""
        (sample_dir / "cls.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
        assert "cls.py" in _grep("CLASS", path=str(sample_dir))
        assert "No matches found" in _grep(
            "CLASS", path=str(sample_dir), case_insensitive=False
        )

    def test_glob_filter(self, sample_dir):
        """glob='*.py' filters out the .md file."""
        result = _grep("foo", path=str(sample_dir), glob="*.py")
        assert "readme.md" not in result
        assert "a.py" in result or "b.py" in result

    def test_type_filter(self, sample_dir):
        """type='py' restricts to Python files by extension."""
        result = _grep("foo", path=str(sample_dir), type="py")
        assert "readme.md" not in result
        assert "a.py" in result or "b.py" in result

    def test_type_filter_keeps_the_public_extension_contract(self, tmp_path):
        (tmp_path / "app.js").write_text("needle\n", encoding="utf-8")
        (tmp_path / "component.vue").write_text("needle\n", encoding="utf-8")

        result = _grep("needle", path=str(tmp_path), type="js")

        assert "app.js" in result
        assert "component.vue" not in result

    def test_output_mode_files_with_matches(self, sample_dir):
        result = _grep("foo", path=str(sample_dir), output_mode="files_with_matches")
        lines = set(result.splitlines())
        # File paths only — no "lineno:" content rows.
        assert "a.py" in lines and "b.py" in lines
        assert not any(line.count(":") >= 2 for line in lines)

    def test_output_mode_count(self, sample_dir):
        result = _grep("foo", path=str(sample_dir), output_mode="count")
        # "{rel}:{count}" rows; a.py has one "foo" occurrence.
        assert any(line.startswith("a.py:") for line in result.splitlines())

    def test_multiline_spans_lines(self, tmp_path):
        (tmp_path / "m.py").write_text("start\nmiddle\nend\n", encoding="utf-8")
        result = _grep("start.*end", path=str(tmp_path), multiline=True)
        assert "m.py:1:" in result
        # Without multiline, the dot does not cross newlines → no match.
        assert "No matches found" in _grep("start.*end", path=str(tmp_path))

    def test_multiline_preserves_one_row_per_match(self, tmp_path, monkeypatch):
        (tmp_path / "m.py").write_text("start\nmiddle\nend\n", encoding="utf-8")
        monkeypatch.setattr(
            "codenib.agent.tools.defaults.shutil.which", lambda _command: "/bin/rg"
        )

        result = _grep("start.*end", path=str(tmp_path), multiline=True)

        assert result.splitlines() == ["m.py:1: start"]

    def test_head_limit_cap(self, sample_dir):
        many = sample_dir / "many.py"
        many.write_text(
            "\n".join(f"match_{i} = 1" for i in range(100)), encoding="utf-8"
        )
        result = _grep("match_", path=str(sample_dir), head_limit=5)
        assert "head_limit=5" in result
        hits = [line for line in result.splitlines() if "many.py" in line]
        assert len(hits) <= 5

    def test_rejects_unbounded_head_limit(self, sample_dir):
        result = _grep(
            "foo",
            path=str(sample_dir),
            head_limit=_MAX_RESULTS_LIMIT + 1,
        )

        assert result == f"Error: head_limit must not exceed {_MAX_RESULTS_LIMIT} rows"

    @pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is unavailable")
    def test_ripgrep_bounds_a_long_matching_line(self, tmp_path):
        (tmp_path / "long.py").write_text(
            "needle " + ("x" * 100_000) + "\n",
            encoding="utf-8",
        )

        result = _grep("needle", path=str(tmp_path), head_limit=1)

        assert len(result) < 1_000
        assert "Omitted long matching line" in result

    def test_invalid_regex_returns_error(self, sample_dir):
        result = _grep("[invalid", path=str(sample_dir))
        assert result.startswith("Error:") and "invalid regex" in result

    @pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is unavailable")
    def test_ripgrep_syntax_is_not_rejected_by_python(self, sample_dir):
        result = _grep(
            r"\p{L}+",
            path=str(sample_dir),
            glob="*.py",
            head_limit=1,
        )

        assert not result.startswith("Error:")
        assert ".py:" in result

    def test_ripgrep_timeout_is_enforced(self, tmp_path, monkeypatch):
        slow_rg = tmp_path / "slow-rg"
        slow_rg.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
        slow_rg.chmod(0o755)
        monkeypatch.setattr(
            "codenib.agent.tools.defaults.shutil.which",
            lambda _command: str(slow_rg),
        )
        monkeypatch.setattr("codenib.agent.tools.defaults._GREP_TIMEOUT_SECONDS", 0.05)

        result = _grep("needle", path=str(tmp_path))

        assert result == "Error: grep timed out after 0.05s"

    def test_invalid_output_mode_returns_error(self, sample_dir):
        result = _grep("x", path=str(sample_dir), output_mode="bogus")
        assert result.startswith("Error:") and "output_mode" in result

    def test_nonexistent_path_returns_error(self, tmp_path):
        result = _grep("x", path=str(tmp_path / "missing"))
        assert result.startswith("Error:") and "does not exist" in result

    def test_python_fallback_when_ripgrep_is_unavailable(self, sample_dir, monkeypatch):
        monkeypatch.setattr(
            "codenib.agent.tools.defaults.shutil.which", lambda _command: None
        )
        result = _grep("def foo", path=str(sample_dir))
        assert "a.py:1:" in result and "def foo" in result

    def test_python_fallback_timeout_isolated_from_agent(self, tmp_path, monkeypatch):
        (tmp_path / "catastrophic.txt").write_text(
            ("a" * 5_000) + "!\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "codenib.agent.tools.defaults.shutil.which", lambda _command: None
        )
        monkeypatch.setattr("codenib.agent.tools.defaults._GREP_TIMEOUT_SECONDS", 0.25)

        started = time.monotonic()
        result = _grep(r"(a+)+$", path=str(tmp_path))

        assert result == "Error: grep timed out after 0.25s"
        assert time.monotonic() - started < 2


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


class TestGlob:
    def test_basic_glob_lists_matching_names(self, sample_dir):
        result = _glob("*.py", path=str(sample_dir))
        names = set(result.splitlines())
        assert {"a.py", "b.py"}.issubset(names)
        assert "readme.md" not in names

    def test_recursive_double_star(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "x.py").write_text("# x", encoding="utf-8")
        (tmp_path / "src" / "nested").mkdir()
        (tmp_path / "src" / "nested" / "y.py").write_text("# y", encoding="utf-8")
        result = _glob("**/*.py", path=str(tmp_path))
        lines = set(result.splitlines())
        assert "src/x.py" in lines and "src/nested/y.py" in lines

    def test_no_match_returns_message(self, sample_dir):
        result = _glob("*.rs", path=str(sample_dir))
        assert result.startswith("No files match")

    def test_result_cap(self, tmp_path):
        for i in range(120):
            (tmp_path / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
        result = _glob("*.py", path=str(tmp_path))
        assert "cap reached" in result
        names = [line for line in result.splitlines() if line.endswith(".py")]
        assert len(names) <= 100

    def test_nonexistent_root_returns_error(self, tmp_path):
        result = _glob("*.py", path=str(tmp_path / "missing"))
        assert result.startswith("Error:") and "does not exist" in result


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------


class TestBash:
    def test_basic_echo(self):
        result = _bash("echo hello")
        assert "hello" in result and "exit code: 0" in result

    def test_stdout_and_stderr_sections(self):
        result = _bash("echo out; echo err 1>&2")
        assert "--- stdout ---" in result and "out" in result
        assert "--- stderr ---" in result and "err" in result

    def test_non_zero_exit_and_command_not_found(self):
        """`false` → exit 1; unknown command → exit 127."""
        assert "exit code: 1" in _bash("false")
        not_found = _bash("nonexistent_cmd_xyz_zzz")
        assert "exit code:" in not_found and "127" in not_found

    def test_timeout_returns_error_ms(self):
        """timeout is in milliseconds: 1000ms = 1s kills a 2s sleep."""
        result = _bash("sleep 2", timeout=1000)
        assert result.startswith("Error:") and "timed out" in result

    def test_timeout_kills_background_process_group(self):
        started = time.monotonic()
        result = _bash("sleep 5 &", timeout=1000)

        assert result.startswith("Error:") and "timed out" in result
        assert time.monotonic() - started < 3

    def test_description_accepted(self):
        """The reference `description` metadata field is accepted and ignored."""
        result = _bash("echo labelled", description="say hello")
        assert "labelled" in result and "exit code: 0" in result

    def test_cwd_argument(self, tmp_path):
        result = _bash("pwd", cwd=str(tmp_path))
        # macOS may symlink /var → /private/var; check tail.
        assert str(tmp_path) in result or tmp_path.name in result

    def test_output_truncation(self):
        result = _bash("yes hello | head -n 20000")
        assert "(output truncated)" in result
        assert len(result) <= _BASH_MAX_OUTPUT_CHARS + 200

    def test_output_cap_does_not_cancel_command_side_effects(self, tmp_path):
        marker = tmp_path / "completed"
        script = "import sys; sys.stdout.write('x' * 1000000)"
        command = (
            f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}; "
            f"printf done > {shlex.quote(str(marker))}"
        )

        result = _bash(command, timeout=5000)

        assert "(output truncated)" in result
        assert marker.read_text(encoding="utf-8") == "done"

    def test_pipe_capture_drains_beyond_retained_prefix(self):
        class TrackingStream(io.BytesIO):
            final_position = 0

            def close(self) -> None:
                self.final_position = self.tell()
                super().close()

        payload = b"x" * (_BASH_MAX_OUTPUT_CHARS * 8)
        source = TrackingStream(payload)
        capture = _BoundedPipeOutput(_BASH_MAX_OUTPUT_CHARS)

        capture.drain(source)

        assert source.final_position == len(payload)
        assert len(capture.text()) == _BASH_MAX_OUTPUT_CHARS
        assert capture.truncated is True

    @pytest.mark.parametrize("command", ["", "   ", None])
    def test_rejects_empty_or_non_string_commands(self, command):
        assert _bash(command).startswith("Error: command must be")

    @pytest.mark.parametrize("timeout", [False, 0, -1, 600_001])
    def test_rejects_invalid_timeouts(self, timeout):
        assert _bash("true", timeout=timeout).startswith("Error: timeout must be")


# ---------------------------------------------------------------------------
# get_default_tool_specs
# ---------------------------------------------------------------------------


class TestGetDefaultToolSpecs:
    def test_returns_exactly_four_defaults(self):
        ids = {m.tool_id for m in get_default_tool_specs()}
        assert ids == set(DEFAULT_TOOL_IDS) == {"read", "grep", "glob", "bash"}

    def test_read_requires_file_path(self):
        spec = {m.tool_id: m for m in get_default_tool_specs()}["read"]
        required = {i.name for i in spec.inputs if i.required}
        assert "file_path" in required

    def test_grep_requires_pattern_exposes_output_mode_enum(self):
        spec = {m.tool_id: m for m in get_default_tool_specs()}["grep"]
        required = {i.name for i in spec.inputs if i.required}
        assert "pattern" in required
        # output_mode is optional and must carry an enum constraint in-schema.
        schema = tool_to_schema(spec)
        props = schema["function"]["parameters"]["properties"]
        assert "output_mode" in props
        assert props["output_mode"].get("enum") == [
            "content",
            "files_with_matches",
            "count",
        ]

    def test_glob_requires_pattern(self):
        spec = {m.tool_id: m for m in get_default_tool_specs()}["glob"]
        required = {i.name for i in spec.inputs if i.required}
        assert "pattern" in required

    def test_bash_requires_command(self):
        spec = {m.tool_id: m for m in get_default_tool_specs()}["bash"]
        required = {i.name for i in spec.inputs if i.required}
        assert "command" in required


# ---------------------------------------------------------------------------
# ensure_default_tools_registered
# ---------------------------------------------------------------------------


class TestEnsureDefaultToolsRegistered:
    def test_registers_all_default_tools(self):
        reg = ToolRegistry()
        ensure_default_tools_registered(reg)
        for tool_id in DEFAULT_TOOL_IDS:
            assert reg.has(tool_id), f"{tool_id!r} not registered"

    def test_idempotent_does_not_overwrite(self):
        """Second call is a no-op and preserves the original executor_fn."""
        reg = ToolRegistry()
        ensure_default_tools_registered(reg)
        original_fn = reg.get("read").executor_fn
        ensure_default_tools_registered(reg)  # would ValueError if not idempotent
        assert reg.get("read").executor_fn is original_fn


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
        assert set(DEFAULT_TOOL_IDS).issubset(names)

    def test_defaults_survive_exclude_skills(self):
        runner = AgentRunner(
            _make_llm(), SkillRegistry(), exclude_skills=set(DEFAULT_TOOL_IDS)
        )
        names = {t["function"]["name"] for t in runner.tools}
        assert set(DEFAULT_TOOL_IDS).issubset(names)

    def test_defaults_survive_allow_skills(self):
        """allow_skills with a disjoint set still surfaces the defaults."""
        runner = AgentRunner(
            _make_llm(), SkillRegistry(), allow_skills={"some_other_skill"}
        )
        names = {t["function"]["name"] for t in runner.tools}
        assert set(DEFAULT_TOOL_IDS).issubset(names)

    def test_non_default_skill_still_excluded(self):
        from codenib.agent.skills.core import SkillMetadata, SkillType

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
        assert set(DEFAULT_TOOL_IDS).issubset(names)

    def test_skill_id_colliding_with_default_tool_raises(self):
        """A skill named like a default tool would emit a duplicate function
        name and shadow the tool — the runner must reject it at construction."""
        from codenib.agent.skills.core import SkillMetadata, SkillType

        reg = SkillRegistry()
        reg.register(
            SkillMetadata(
                skill_id="read",  # collides with the default `read` TOOL
                skill_type=SkillType.CUSTOM,
                executor_fn=lambda: "hi",
            )
        )
        with pytest.raises(ValueError, match="collide with default tool"):
            AgentRunner(_make_llm(), reg)

    def test_empty_default_tool_ids_means_all(self):
        """An explicitly-empty default_tool_ids restrictor means ALL defaults
        (mirrors the empty->full skills contract); the off-switch is
        include_default_tools=False."""
        runner = AgentRunner(_make_llm(), SkillRegistry(), default_tool_ids=set())
        names = {t["function"]["name"] for t in runner.tools}
        assert set(DEFAULT_TOOL_IDS).issubset(names)

    def test_runner_executes_read(self, tmp_path):
        """End-to-end: runner dispatches a read tool call."""
        target = tmp_path / "hello.py"
        target.write_text("x = 1\ny = 2\n", encoding="utf-8")

        tc = SimpleNamespace(
            id="tc_read",
            type="function",
            function=SimpleNamespace(
                name="read",
                arguments=json.dumps({"file_path": str(target)}),
            ),
        )
        llm = _make_llm()
        llm._call_raw.side_effect = [
            _make_tool_call_response(tc),
            _make_final_response("Done."),
        ]

        record = AgentRunner(llm, SkillRegistry()).run("Read it").tool_calls[0]
        assert record.error is None and "x = 1" in record.result

    def test_runner_executes_bash(self):
        """End-to-end: runner dispatches a bash tool call."""
        tc = SimpleNamespace(
            id="tc_bash",
            type="function",
            function=SimpleNamespace(
                name="bash",
                arguments=json.dumps({"command": "echo runner_dispatch"}),
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

    def test_read_runner_py(self):
        """Read the runner module itself, verify per-row line numbers appear."""
        source = self._runner_source_path()
        assert source.exists(), f"runner.py not found at {source}"

        result = _read(str(source), offset=1, limit=10)
        lines = result.splitlines()
        assert lines[0].lstrip().startswith("1 |") and len(lines) == 11

    def test_grep_for_agent_runner_in_runner_py(self):
        """Content-mode regex-search for 'AgentRunner' in runner.py."""
        source = self._runner_source_path()
        result = _grep("AgentRunner", path=str(source))
        assert "AgentRunner" in result
        assert any(":" in line for line in result.splitlines())
