"""Explicit native dependency caches must retain the exact task/source boundary."""

import copy

import pytest

from scripts import contextbench_polybench as poly


@pytest.fixture
def cache_binding():
    settings = {
        "WorkingDir": "/testbed",
        "Cmd": ["/bin/bash"],
        "Entrypoint": None,
        "Env": ["PATH=/usr/bin:/bin"],
        "User": "",
    }
    case = {
        "instance_id": "microsoft__vscode-123",
        "repo": "microsoft/vscode",
        "base_commit": "d" * 40,
    }
    config = {
        "image_id": "sha256:" + "c" * 64,
        "source_commit": case["base_commit"],
        "source_tree": "e" * 40,
    }
    original = {
        "Id": "sha256:" + "b" * 64,
        "Config": copy.deepcopy(settings),
        "RootFS": {"Layers": ["sha256:" + "1" * 64]},
        "RepoDigests": [
            "ghcr.io/timesler/swe-polybench.eval.x86_64."
            + case["instance_id"]
            + "@sha256:"
            + "f" * 64
        ],
    }
    derived = {
        "Id": "sha256:" + "a" * 64,
        "Config": copy.deepcopy(settings),
        "RootFS": {"Layers": [*original["RootFS"]["Layers"], "sha256:" + "2" * 64]},
        "RepoDigests": [],
    }
    identity = "SWE-PolyBench__typescript__maintenance__bugfix__fixture"
    proof = {
        "status": "prepared",
        "instance_id": identity,
        "original_native_image_id": original["Id"],
        "derived_native_image_id": derived["Id"],
        "source_image_unchanged": config["image_id"],
        "source_commit": config["source_commit"],
        "source_tree": config["source_tree"],
        "owned_container_removed": True,
        "all_tracked_source_and_test_bytes_unchanged": True,
        "original_source_and_native_images_unchanged": True,
        "native_command_parser_scorer_and_limits_unchanged": True,
        "network_enabled_only_during_preparation": True,
        "pinned_Electron_version": "9.3.3",
        "embedded_executable_Electron_version": "9.3.3",
        "executable_sha256": "f" * 64,
    }
    return {
        "derived": derived,
        "original": original,
        "case": case,
        "config": config,
        "identity": identity,
        "proof": proof,
    }


def validate(binding):
    return poly.validated_electron_cache(
        binding["derived"],
        binding["original"],
        binding["case"],
        binding["config"],
        binding["identity"],
        binding["proof"],
    )


def test_explicit_cache_extends_its_own_separate_task_image(cache_binding):
    profile = validate(cache_binding)
    assert profile["original_native_image_id"] == cache_binding["original"]["Id"]
    assert profile["derived_native_image_id"] == cache_binding["derived"]["Id"]
    assert profile["pinned_Electron_version"] == "9.3.3"


def test_docker_empty_entrypoint_forms_are_equivalent(cache_binding):
    cache_binding["derived"]["Config"]["Entrypoint"] = []
    assert validate(cache_binding)["pinned_Electron_version"] == "9.3.3"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("proof.instance_id", "another-task"),
        ("proof.source_commit", "0" * 40),
        ("proof.source_tree", "0" * 40),
        ("proof.source_image_unchanged", "sha256:" + "0" * 64),
        ("proof.original_native_image_id", "sha256:" + "0" * 64),
        ("proof.derived_native_image_id", "sha256:" + "0" * 64),
        ("proof.owned_container_removed", False),
        ("proof.all_tracked_source_and_test_bytes_unchanged", "true"),
        ("proof.native_command_parser_scorer_and_limits_unchanged", False),
        ("proof.embedded_executable_Electron_version", "10.0.0"),
        ("proof.executable_sha256", None),
        ("derived.RootFS.Layers", ["sha256:" + "0" * 64]),
        ("derived.Config.Env", ["CHANGED=1"]),
        ("derived.Config.Entrypoint", ["sleep"]),
        ("original.RepoDigests", ["unrelated/task@sha256:" + "f" * 64]),
        ("case.repo", "sveltejs/svelte"),
    ],
)
def test_cache_attestation_cannot_override_task_or_execution_settings(
    cache_binding, path, value
):
    target = cache_binding
    names = path.split(".")
    for name in names[:-1]:
        target = target[name]
    target[names[-1]] = value
    with pytest.raises(ValueError):
        validate(cache_binding)


def test_cache_is_not_the_source_image_or_original_image(cache_binding):
    cache_binding["derived"]["Id"] = cache_binding["config"]["image_id"]
    cache_binding["proof"]["derived_native_image_id"] = cache_binding["config"][
        "image_id"
    ]
    with pytest.raises(ValueError, match="task or source"):
        validate(cache_binding)
