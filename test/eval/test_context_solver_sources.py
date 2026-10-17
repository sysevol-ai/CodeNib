"""Actual source versions, native truncation and confirmed delivery boundaries."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts._contextbench_solver_read import read_source
from scripts.contextbench_solver_sources import (
    SourceEnvironment,
    initial_source_receipt,
    receipt_digest,
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


@pytest.mark.integration
def test_native_loop_receipts_cover_truncation_edits_timeouts_and_terminal_output(
    monkeypatch, source_repo
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
            "codenib-read large.txt 1 300",
            "printf 'inserted\\noriginal first\\n' > source.txt",
            "codenib-read source.txt 1 2",
            "fixture-timeout",
            "codenib-read large.txt 140 160",  # The next request is unconfirmed.
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
    assert 1 in visible and 300 in visible and 150 not in visible
    assert "source.txt" not in coverage["base_source_lines"]
    assert coverage["events"][2]["visible_source_lines"] == [1, 2]
    assert coverage["events"][3]["confirmed_turns"] == [5]
    assert not coverage["events"][4]["confirmed_turns"]
    assert not coverage["events"][5]["confirmed_turns"]
    assert receipts[3]["receipt"]["timeout"]
