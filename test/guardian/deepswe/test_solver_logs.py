import json

from scripts.guardian.deepswe.harness.solver_logs import (
    capture_codex_turn,
    persist_codex_logs,
    restore_codex_accounting,
)


def _turn(input_tokens, cached_input_tokens, output_tokens, reasoning_tokens):
    return (
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning_tokens,
                },
            }
        ).encode()
        + b"\n"
    )


def test_solver_usage_survives_codex_log_replacement(tmp_path):
    first = _turn(100, 80, 10, 3)
    second = _turn(200, 150, 20, 7)

    (tmp_path / "codex.txt").write_bytes(first)
    first_capture = capture_codex_turn(tmp_path, 1)
    assert first_capture is not None

    # A later installed-agent invocation replaces the conventional log and may
    # remove intermediate files. The controller's byte snapshot remains valid.
    (tmp_path / "codex_turn_01.txt").unlink()
    (tmp_path / "codex.txt").write_bytes(second)
    second_capture = capture_codex_turn(tmp_path, 2)
    assert second_capture is not None

    turn_logs = [(1, first_capture[1]), (2, second_capture[1])]
    persist_codex_logs(tmp_path, turn_logs)

    assert (tmp_path / "codex.txt").read_bytes() == first + second
    assert (tmp_path / "codex_turn_01.txt").read_bytes() == first
    assert (tmp_path / "codex_turn_02.txt").read_bytes() == second
    assert json.loads((tmp_path / "codex_tokens.json").read_text()) == {
        "input_tokens": 300,
        "cached_input_tokens": 230,
        "output_tokens": 30,
        "reasoning_output_tokens": 10,
    }


def test_post_run_inner_log_replacement_cannot_erase_cumulative_usage(tmp_path):
    first = _turn(100, 80, 10, 3)
    second = _turn(200, 150, 20, 7)

    # Simulate the installed agent's post-run hook replacing conventional logs.
    (tmp_path / "codex.txt").write_bytes(second)
    (tmp_path / "codex_tokens.json").write_text("{}")
    (tmp_path / "codex.txt").chmod(0o444)
    (tmp_path / "codex_tokens.json").chmod(0o444)

    totals = restore_codex_accounting(tmp_path, [(1, first), (2, second)])

    assert totals == {
        "input_tokens": 300,
        "cached_input_tokens": 230,
        "output_tokens": 30,
        "reasoning_output_tokens": 10,
    }
    assert (tmp_path / "codex.txt").read_bytes() == first + second
    assert (tmp_path / "codex_turn_01.txt").read_bytes() == first
    assert (tmp_path / "codex_turn_02.txt").read_bytes() == second
