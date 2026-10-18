"""Fixed context selection preserves frozen search and authenticated repair reads."""

import copy

import pytest

from scripts.contextbench_selected_context import selected_context


def records():
    prediction = {
        "instance_id": "case",
        "model_patch": "",
        "traj_data": {
            "pred_files": ["code.py", "filename-only.py", ".git/config"],
            "pred_spans": {
                "code.py": [{"start": 4, "end": 8}],
                ".git/config": [{"start": 1, "end": 1}],
            },
            "pred_steps": [{"files": ["code.py"], "spans": {}}],
        },
    }
    observations = {
        "instance_id": "case",
        "all_model_inputs_confirmed": True,
        "source_attribution": {
            "status": "partial",
            "base_source_lines": {
                "code.py": [2, 3, 4, 9, 15],
                "other.py": [10, 11, 12],
            },
            "events": [
                {
                    "file": "other.py",
                    "base_version": True,
                    "confirmed_turns": [1, 2],
                    "visible_source_lines": [10, 11, 12],
                },
                {
                    "file": "edited.py",
                    "base_version": False,
                    "confirmed_turns": [3],
                    "visible_source_lines": [1, 2],
                },
                {
                    "file": "unconfirmed.py",
                    "base_version": True,
                    "confirmed_turns": [],
                    "visible_source_lines": [1, 2],
                },
            ],
        },
    }
    return prediction, observations


def test_selection_unions_ranges_and_preserves_filename_only_and_legacy_metadata():
    prediction, observations = records()
    originals = copy.deepcopy((prediction, observations))
    result, receipt = selected_context(
        prediction, observations, "actual candidate patch"
    )
    assert result["model_patch"] == "actual candidate patch"
    traj = result["traj_data"]
    assert traj["pred_spans"]["code.py"] == [
        {"start": 2, "end": 9},
        {"start": 15, "end": 15},
    ]
    assert traj["pred_files"] == [
        ".git/config",
        "code.py",
        "filename-only.py",
        "other.py",
    ]
    assert traj["pred_spans"][".git/config"] == [{"start": 1, "end": 1}]
    assert len(traj["pred_steps"]) == 2
    assert traj["pred_steps"][-1] == {
        "files": ["other.py"],
        "spans": {
            "other.py": [{"start": 10, "end": 12}],
        },
    }
    assert receipt["selected_source_lines"] == 13
    assert receipt["repair_base_source_lines"] == 8
    assert (prediction, observations) == originals


def test_missing_repair_inputs_retain_search_without_inventing_extra_context():
    prediction, observations = records()
    observations.update(all_model_inputs_confirmed=False)
    observations["source_attribution"].update(base_source_lines={}, events=[])
    result, receipt = selected_context(prediction, observations, "")
    assert result["traj_data"]["pred_spans"] == prediction["traj_data"]["pred_spans"]
    assert not receipt["all_repair_model_inputs_confirmed"]
    assert receipt["repair_base_source_lines"] == 0


@pytest.mark.parametrize(
    "path", ["/absolute.py", "../outside.py", "a//b.py", "a/../b.py", "a\nb.py"]
)
def test_selection_rejects_nonrepository_source_paths(path):
    prediction, observations = records()
    observations["source_attribution"]["base_source_lines"] = {path: [1]}
    with pytest.raises(ValueError, match="repository-relative"):
        selected_context(prediction, observations, "")


@pytest.mark.parametrize("line", [0, -1, True, "2", 1.5])
def test_selection_rejects_nonbase_coordinates(line):
    prediction, observations = records()
    observations["source_attribution"]["base_source_lines"] = {"file.py": [line]}
    with pytest.raises(ValueError, match="coordinates"):
        selected_context(prediction, observations, "")


def test_selection_rejects_different_task():
    prediction, observations = records()
    observations["instance_id"] = "other"
    with pytest.raises(ValueError, match="another solver"):
        selected_context(prediction, observations, "")
