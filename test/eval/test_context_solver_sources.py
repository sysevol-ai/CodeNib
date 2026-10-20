"""Actual source versions, native truncation and confirmed delivery boundaries."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import _contextbench_solver_read as source_helper
from scripts._contextbench_solver_read import read_source, search_line, search_source
from scripts.contextbench_solver_sources import (
    SourceEnvironment,
    initial_source_receipt,
    receipt_digest,
    search_output,
    source_attribution,
    source_output,
)


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "maintenance.auto", "false"], check=True
    )
    (repo / "source.txt").write_text(
        "original first\noriginal second\noriginal third\n"
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.org",
            "commit",
            "-qm",
            "Base source",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    return repo, commit


def test_empty_initial_context_has_no_source_credit(source_repo):
    repo, commit = source_repo
    row = {"initial_source_receipt": initial_source_receipt("[]", repo, commit)}
    confirmed = [
        {
            "turn": 1,
            "messages": [
                {"role": "system", "content": "Shell agent"},
                {
                    "role": "user",
                    "content": "<retrieved_source>\n[]\n</retrieved_source>",
                },
            ],
        }
    ]
    assert row["initial_source_receipt"]["receipt"]["sources"] == []
    assert source_attribution(row, confirmed)["base_source_line_count"] == 0


def test_source_snapshot_pins_original_commit_after_edits_and_new_commit(source_repo):
    repo, commit = source_repo
    original = read_source(repo, commit, "source.txt", 1, 10)
    assert original["lines"] == ["original first", "original second", "original third"]
    assert original["source_sha256"] == original["base_sha256"]
    (repo / "source.txt").write_text(
        "inserted\noriginal first\noriginal second\noriginal third\n"
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.org",
            "commit",
            "-qm",
            "Repair",
        ],
        check=True,
    )
    changed = read_source(repo, commit, "source.txt", 2, 3)
    assert changed["lines"] == original["lines"][:2]
    assert changed["source_sha256"] != changed["base_sha256"] == original["base_sha256"]
    (repo / "new.txt").write_text("newly authored source\n")
    assert read_source(repo, commit, "new.txt", 1, 1)["base_sha256"] is None


@pytest.mark.parametrize("start,end", [(0, 1), (2, 1), (1, 2001), (100, 101)])
def test_reader_rejects_invalid_or_unbounded_ranges(source_repo, start, end):
    repo, commit = source_repo
    with pytest.raises(ValueError):
        read_source(repo, commit, "source.txt", start, end)


@pytest.mark.parametrize(
    "kind", ["outside", "symlink", "git", "binary", "oversized", "unicode"]
)
def test_reader_rejects_unattributable_files(source_repo, tmp_path, kind):
    repo, commit = source_repo
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    file = repo / "invalid.txt"
    if kind == "outside":
        file = outside
    elif kind == "symlink":
        file.symlink_to(outside)
    elif kind == "git":
        file = repo / ".git/config"
    else:
        file.write_bytes(
            {
                "binary": b"a\0b",
                "oversized": b"x" * (2 * 1024**2 + 1),
                "unicode": b"\xff",
            }[kind]
        )
    with pytest.raises(ValueError):
        read_source(repo, commit, str(file), 1, 1)


def test_search_includes_tests_and_versions_changed_and_new_files(source_repo):
    repo, commit = source_repo
    (repo / "tests").mkdir()
    (repo / "tests/test_source.py").write_text("original test\n")
    (repo / "binary.dat").write_bytes(b"original\0binary")
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "ignored.txt").write_text("original ignored\n")
    result = search_source(repo, commit, "original", ".")
    files = {file["file"]: file for file in result["sources"]}
    assert set(files) == {"source.txt", "tests/test_source.py"}
    assert files["source.txt"]["source_sha256"] == files["source.txt"]["base_sha256"]
    assert files["tests/test_source.py"]["base_sha256"] is None
    assert result["skipped_files"] == 1 and not result["limits_reached"]
    (repo / "source.txt").write_text("inserted\noriginal first\n")
    changed = search_source(repo, commit, "original", "source.txt")["sources"][0]
    assert changed["matches"] == [{"line": 2, "text": "original first"}]
    assert changed["source_sha256"] != changed["base_sha256"]


@pytest.mark.parametrize("path", ["..", ".git", ".git/config", "escape"])
def test_search_rejects_paths_outside_source(source_repo, path):
    repo, commit = source_repo
    (repo / "escape").symlink_to(repo.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="inside the checkout"):
        search_source(repo, commit, "original", path)


def test_search_limits_are_explicit_and_preserve_complete_records(
    source_repo, monkeypatch
):
    repo, commit = source_repo
    result = search_source(repo, commit, "original", ".", limit=1)
    assert result["matches_returned"] == 1 and result["limits_reached"] == [
        "match_limit"
    ]
    monkeypatch.setattr(source_helper, "MAX_SEARCH_OUTPUT_CHARS", 1)
    result = search_source(repo, commit, "original", ".")
    assert result["sources"] == [] and result["limits_reached"] == ["output_limit"]
    monkeypatch.setattr(source_helper, "MAX_SEARCH_FILES", 1)
    (repo / "a.txt").write_text("no match\n")
    result = search_source(repo, commit, "original", ".")
    assert result["sources"] == [] and result["limits_reached"] == ["file_scan_limit"]
    assert result["files_considered"] == 2 and result["files_scanned"] == 1


def envelope(receipt):
    return {"receipt": receipt, "sha256": receipt_digest(receipt)}


def observed(source, rendered=None):
    output = source_output(source)
    return envelope(
        {
            "kind": "source_read",
            "source": source,
            "output": {"returncode": 0, "output": output},
            "message_index": 3,
            "rendered_observation": (
                rendered
                if rendered is not None
                else "<output>\n" + output + "</output>"
            ),
        }
    )


def observed_search(search, rendered=None):
    output = search_output(search)
    return envelope(
        {
            "kind": "source_search",
            "source": None,
            "search": search,
            "output": {"returncode": 0, "output": output},
            "message_index": 3,
            "rendered_observation": rendered if rendered is not None else output,
        }
    )


def confirmed(receipt):
    return [
        {
            "turn": 2,
            "messages": [
                {"role": "system", "content": "Repair"},
                {"role": "user", "content": "Issue"},
                {
                    "role": "assistant",
                    "content": "I read the entire file, including line 9000",
                },
                {"role": "user", "content": receipt["receipt"]["rendered_observation"]},
            ],
        }
    ]


def test_source_credit_requires_confirmed_input_and_exact_receipt(source_repo):
    repo, commit = source_repo
    event = observed(read_source(repo, commit, "source.txt", 1, 2))
    row = {"execution_receipts": [event]}
    assert source_attribution(row, [])["base_source_line_count"] == 0
    result = source_attribution(row, confirmed(event))
    assert result["base_source_lines"] == {"source.txt": [1, 2]}
    assert result["events"][0]["confirmed_turns"] == [2]
    # Same output at another index (e.g. copied into assistant prose) is not the receipt.
    inputs = confirmed(event)
    inputs[0]["messages"].insert(2, {"role": "assistant", "content": "extra"})
    assert source_attribution(row, inputs)["base_source_line_count"] == 0
    event["receipt"]["source"]["start_line"] = 90
    with pytest.raises(ValueError, match="receipt changed"):
        source_attribution(row, confirmed(event))


def test_modified_version_and_elided_or_partial_lines_have_no_base_credit(source_repo):
    repo, commit = source_repo
    base = read_source(repo, commit, "source.txt", 1, 3)
    event = observed(
        base,
        "<output_head>\nFile: source.txt\n1 | original first\n2 | orig\n"
        "</output_head>\n<output_tail>\ninal third\n</output_tail>",
    )
    result = source_attribution({"execution_receipts": [event]}, confirmed(event))
    assert result["base_source_lines"] == {"source.txt": [1]}
    (repo / "source.txt").write_text("inserted\noriginal first\noriginal second\n")
    event = observed(read_source(repo, commit, "source.txt", 2, 3))
    result = source_attribution({"execution_receipts": [event]}, confirmed(event))
    assert result["base_source_line_count"] == 0
    assert result["events"][0]["visible_source_lines"] == [2, 3]
    assert not result["events"][0]["base_version"]


def test_search_credit_requires_complete_file_qualified_lines_and_confirmed_input(
    source_repo,
):
    repo, _ = source_repo
    (repo / "a.py").write_text("original first\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    tree = subprocess.check_output(
        ["git", "-C", str(repo), "write-tree"], text=True
    ).strip()
    search = search_source(repo, tree, "original first", ".")
    event = observed_search(
        search,
        "<output_head>\n"
        + search_line("a.py", 1, "original first")[:20]
        + "\n</output_head>\n<output_tail>\n"
        + search_line("source.txt", 1, "original first")
        + "\n</output_tail>",
    )
    row = {"execution_receipts": [event]}
    assert source_attribution(row, [])["base_source_line_count"] == 0
    result = source_attribution(row, confirmed(event))
    assert result["base_source_lines"] == {"source.txt": [1]}
    assert result["events"][0]["visible_source_lines"] == []
    # Even a freshly hashed receipt must agree with its own complete source output.
    event["receipt"]["output"]["output"] += "invented\n"
    row["execution_receipts"] = [envelope(event["receipt"])]
    with pytest.raises(ValueError, match="search receipt output differs"):
        source_attribution(row, confirmed(row["execution_receipts"][0]))


def test_search_modified_and_new_files_never_gain_base_credit(source_repo):
    repo, commit = source_repo
    (repo / "source.txt").write_text("inserted\noriginal first\n")
    (repo / "new.txt").write_text("original first\n")
    event = observed_search(search_source(repo, commit, "original", "."))
    result = source_attribution({"execution_receipts": [event]}, confirmed(event))
    assert result["base_source_line_count"] == 0
    assert [e["visible_source_lines"] for e in result["events"]] == [[1], [2]]
    assert not any(e["base_version"] for e in result["events"])


def test_initial_context_is_validated_and_not_credited_without_model_delivery(
    source_repo,
):
    repo, commit = source_repo
    context = json.dumps([{"file": "source.txt", "content": "2 | original second\n"}])
    initial = initial_source_receipt(context, repo, commit)
    row = {"initial_source_receipt": initial}
    assert source_attribution(row, [])["base_source_line_count"] == 0
    inputs = [
        {
            "turn": 1,
            "messages": [
                {"role": "system", "content": "Repair"},
                {
                    "role": "user",
                    "content": f"<retrieved_source>\n{context}\n</retrieved_source>",
                },
            ],
        }
    ]
    assert source_attribution(row, inputs)["base_source_lines"] == {"source.txt": [2]}
    with pytest.raises(ValueError, match="differs"):
        initial_source_receipt(
            context.replace("original second", "invented"), repo, commit
        )


class LocalEnvironment:
    """Real subprocess/file boundary, with no external container or model."""

    def __init__(self, root):
        self.root = root
        self.commands = []

    def get_template_vars(self):
        return {}

    def execute(self, command):
        self.commands.append(command)
        result = subprocess.run(
            command,
            cwd=self.root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return {
            "returncode": result.returncode,
            "output": result.stdout + result.stderr,
        }


def test_wrapper_uses_controlled_read_and_never_executes_shell_in_reader_args(
    source_repo,
):
    repo, commit = source_repo
    shell = LocalEnvironment(repo)
    receipts, checkpoints = [], []
    env = SourceEnvironment(
        shell,
        root=str(repo),
        commit=commit,
        receipts=receipts,
        checkpoint=lambda: checkpoints.append(len(receipts)),
        observation=lambda command, output, timeout: (3, output["output"]),
    )
    assert (
        env.execute("codenib-read source.txt 2 3")["output"]
        == "File: source.txt\n2 | original second\n3 | original third\n"
    )
    assert (
        env.execute("codenib-read source.txt 1 2 && touch escaped")["returncode"] == 1
    )
    assert env.execute("codenib-read '$(touch escaped)' 1 2")["returncode"] == 1
    assert not (repo / "escaped").exists()
    assert env.execute("cat source.txt")["returncode"] == 0
    assert receipts[-1]["receipt"]["kind"] == "shell_unattributed"
    assert receipts[-1]["receipt"]["source"] is None
    assert checkpoints == [1, 2, 3, 4]


def test_wrapper_search_runs_stdlib_helper_with_literal_arguments(source_repo):
    repo, commit = source_repo
    shell, receipts = LocalEnvironment(repo), []
    env = SourceEnvironment(
        shell,
        root=str(repo),
        commit=commit,
        receipts=receipts,
        checkpoint=lambda: None,
        observation=lambda command, output, timeout: (3, output["output"]),
    )
    result = env.execute("codenib-grep 'original (first|third)' . 2")
    assert result["returncode"] == 0
    event = receipts[-1]
    result = source_attribution({"execution_receipts": [event]}, confirmed(event))
    assert result["base_source_lines"] == {"source.txt": [1, 3]}
    assert result["events"][0]["search_limits_reached"] == ["match_limit"]
    for command in (
        "codenib-grep '[' .",  # Invalid regex is actionable feedback, not a crash.
        "codenib-grep original . 201",
        "codenib-grep original . && touch escaped",
        "codenib-grep '$(touch escaped)' '$(touch escaped)'",
    ):
        assert env.execute(command)["returncode"] == 1
    assert not (repo / "escaped").exists()
    assert env.execute("grep original source.txt")["returncode"] == 0
    event = receipts[-1]
    result = source_attribution({"execution_receipts": [event]}, confirmed(event))
    assert result["base_source_line_count"] == 0
    assert result["events"][0]["kind"] == "shell_unattributed"


@pytest.mark.parametrize("prefix", ["cd {repo} && ", "pwd;\n", "echo ready | "])
@pytest.mark.parametrize(
    "tool,arguments",
    [("codenib-read", "source.txt 1 2"), ("codenib-grep", "original .")],
)
def test_compound_reader_attempt_explains_available_interface(
    source_repo, prefix, tool, arguments
):
    repo, commit = source_repo
    shell = LocalEnvironment(repo)
    receipts = []
    env = SourceEnvironment(
        shell,
        root=str(repo),
        commit=commit,
        receipts=receipts,
        checkpoint=lambda: None,
        observation=lambda command, output, timeout: (3, output["output"]),
    )
    # The real adoption probe first combined cd, two reads and git status. An
    # ordinary command-not-found error incorrectly tells the model to abandon
    # an available tool; reject the invocation with its actual interface.
    command = prefix.format(repo=repo) + f"{tool} {arguments} && git status --short"
    result = env.execute(command)
    assert result["returncode"] == 1 and "whole bash action" in result["output"]
    assert not shell.commands
    assert receipts[-1]["receipt"]["kind"] == (
        "source_read" if tool == "codenib-read" else "source_search"
    )
    assert receipts[-1]["receipt"]["source"] is None
    assert env.execute(f"{tool} {arguments}")["returncode"] == 0
    # A quoted mention of the command is ordinary shell output, not an attempt.
    assert env.execute(f"printf '%s\\n' {tool!r}")["output"] == f"{tool}\n"
    assert receipts[-1]["receipt"]["kind"] == "shell_unattributed"
    assert env.execute(f"printf '%s %s\\n' '&&' {tool}")["output"] == f"&& {tool}\n"
    assert receipts[-1]["receipt"]["kind"] == "shell_unattributed"


@pytest.mark.integration
@pytest.mark.parametrize("search", [False, True], ids=["read", "search"])
def test_native_loop_receipts_cover_truncation_edits_timeouts_and_terminal_output(
    monkeypatch, source_repo, search
):
    import yaml

    from scripts.benchmark_context_solver import (
        _HISTORY_PREFIX,
        CONTEXTBENCH_UPSTREAM_REVISION,
        model_observations,
        request_digest,
    )

    checkout = os.environ.get("CONTEXTBENCH_CHECKOUT")
    if not checkout:
        pytest.skip("Set CONTEXTBENCH_CHECKOUT to the pinned external checkout")
    upstream = Path(checkout)
    assert (
        subprocess.check_output(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
        ).strip()
        == CONTEXTBENCH_UPSTREAM_REVISION
    )
    mini = (
        upstream
        / "agent-frameworks/mini-swe-agent/multi-poly-pro-verified/mini-swe-agent/src"
    )
    monkeypatch.syspath_prepend(str(mini))
    from minisweagent.agents.default import DefaultAgent

    repo, commit = source_repo
    # This controlled fixture is the original source for the tested run.
    (repo / "large.txt").write_text(
        "".join(f"line {i}: " + "x" * 100 + "\n" for i in range(300))
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    tree = subprocess.check_output(
        ["git", "-C", str(repo), "write-tree"], text=True
    ).strip()
    calls, receipts = [], []
    commands = iter(
        [
            "codenib-grep line large.txt" if search else "codenib-read large.txt 1 300",
            "printf 'inserted\\noriginal first\\n' > source.txt",
            (
                "codenib-grep original source.txt"
                if search
                else "codenib-read source.txt 1 2"
            ),
            "fixture-timeout",
            # The next request is unconfirmed.
            (
                "codenib-grep '^line 15[0-9]:' large.txt"
                if search
                else "codenib-read large.txt 140 160"
            ),
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat source.txt",
        ]
    )

    class Model:
        cost, n_calls = 0, 0

        def get_template_vars(self):
            return {}

        def query(self, messages):
            self.n_calls += 1
            request = {
                "system_prompt": messages[0]["content"],
                "prompt": _HISTORY_PREFIX
                + json.dumps(
                    [{k: m[k] for k in ("role", "content")} for m in messages[1:]]
                ),
            }
            calls.append(
                {
                    "turn": self.n_calls,
                    "request": request,
                    "request_sha256": request_digest(request),
                    "input_delivery_confirmed": self.n_calls < 6,
                }
            )
            return {"content": "```bash\n" + next(commands) + "\n```"}

    config = yaml.safe_load(
        (mini / "minisweagent/config/extra/swebench.yaml").read_text()
    )["agent"]
    config.update(
        system_template="Return one bash command.",
        instance_template="{{task}}",
        step_limit=7,
    )
    shell = LocalEnvironment(repo)
    original_execute = shell.execute

    def execute(command):
        if command == "fixture-timeout":
            raise subprocess.TimeoutExpired(
                command, 1, output=b"partial test feedback\n"
            )
        return original_execute(command)

    shell.execute = execute

    def observation(command, output, timeout):
        rendered = (
            agent.render_template(
                config["timeout_template"],
                action={"action": command},
                output=output["output"],
            )
            if timeout
            else agent.render_template(
                config["action_observation_template"], output=output
            )
        )
        return len(agent.messages), rendered

    env = SourceEnvironment(
        shell,
        root=str(repo),
        commit=tree,
        receipts=receipts,
        checkpoint=lambda: None,
        observation=observation,
    )
    agent = DefaultAgent(Model(), env, **config)
    status, answer = agent.run("Repair")
    assert status == "Submitted" and answer == "inserted\noriginal first\n"
    report = model_observations(
        {"instance_id": "fixture", "calls": calls, "execution_receipts": receipts}
    )
    coverage = report["source_attribution"]
    assert report["unconfirmed_inputs"] == [6]
    assert 0 < coverage["base_source_line_count"] < 300
    visible = coverage["base_source_lines"]["large.txt"]
    captured = receipts[0]["receipt"].get("search")
    last_line = captured["sources"][0]["matches"][-1]["line"] if search else 300
    assert 1 in visible and last_line in visible and 150 not in visible
    if search:
        assert captured["limits_reached"] == ["output_limit"]
        assert len(visible) < captured["matches_returned"]
    assert "source.txt" not in coverage["base_source_lines"]
    assert coverage["events"][2]["visible_source_lines"] == ([2] if search else [1, 2])
    assert coverage["events"][3]["confirmed_turns"] == [5]
    assert not coverage["events"][4]["confirmed_turns"]
    assert not coverage["events"][5]["confirmed_turns"]
    assert receipts[3]["receipt"]["timeout"]
