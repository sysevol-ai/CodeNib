"""Native Poly task binding and owner cleanup without providers or Docker."""

import csv
import dataclasses
import hashlib
import io
import json
import sys
import tarfile
from types import SimpleNamespace

import pytest

from scripts import contextbench_polybench as poly
from scripts.contextbench_multiswe import solver_cwd


@pytest.fixture
def study(tmp_path, monkeypatch):
    case = {
        "instance_id": "sveltejs__svelte-510",
        "repo": "sveltejs/svelte",
        "base_commit": "base",
        "patch": "reference-only code",
        "test_patch": "reference-only tests",
        "test_command": "native tests",
        "F2P": "['fix']",
        "P2P": "['stable']",
    }
    dataset = tmp_path / "native.csv"
    with dataset.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(case))
        writer.writeheader()
        writer.writerow(case)
    monkeypatch.setattr(poly, "DATASET_SHA256", poly.digest(dataset))
    folder = tmp_path / "solver"
    folder.mkdir()
    solver = {
        "instance_id": "context-task",
        "original_inst_id": case["instance_id"],
        "model_patch": "executed solver edit",
    }
    config = {
        "source": "Poly",
        "repo": case["repo"],
        "source_commit": "base",
        "source_tree": "tree",
        "image_id": "sha256:source-only",
        "implementation": {},
    }
    for name, value in [
        ("solver.json", solver),
        ("config.json", config),
        (
            "predictions.json",
            [
                {
                    "instance_id": case["instance_id"],
                    "model_patch": solver["model_patch"],
                }
            ],
        ),
    ]:
        (folder / name).write_text(json.dumps(value))
    args = SimpleNamespace(
        solver_run=folder,
        dataset=dataset,
        output=tmp_path / "grade",
        native_image="sha256:" + "a" * 64,
        native_upstream=None,
        timeout=60,
    )
    return args, case, solver, config


def test_poly_csv_hash_and_source_bind_before_output(study):
    args, case, solver, config = study
    assert poly.selected_native_case(args.dataset, solver, config) == (
        case,
        ["fix"],
        ["stable"],
    )
    for field, value in [
        ("repo", "other/repo"),
        ("source_commit", "wrong"),
        ("source", "Multi"),
    ]:
        with pytest.raises(ValueError, match="source snapshot"):
            poly.selected_native_case(args.dataset, solver, {**config, field: value})
    args.dataset.write_text(args.dataset.read_text() + "\n")
    with pytest.raises(ValueError, match="pinned dataset"):
        poly.selected_native_case(args.dataset, solver, config)
    assert not args.output.exists()
    assert solver_cwd("Poly", "sveltejs/svelte") == "/testbed"
    with pytest.raises(ValueError, match="Unsupported native repository"):
        solver_cwd("Poly", "other/repo")


def test_poly_csv_large_unselected_field_restores_limit(study, monkeypatch):
    args, case, solver, config = study
    previous = csv.field_size_limit()
    with args.dataset.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(case))
        writer.writeheader()
        writer.writerow({**case, "instance_id": "other", "patch": "x" * 200_000})
        writer.writerow(case)
    monkeypatch.setattr(poly, "DATASET_SHA256", poly.digest(args.dataset))
    assert poly.selected_native_case(args.dataset, solver, config)[0] == case
    assert csv.field_size_limit() == previous


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "startup",
        "tree",
        "test_patch",
        "invalid_patch",
        "patch_transport",
        "test_timeout",
        "returned_timeout",
        "partial_success",
        "no_terminal_footer",
        "wall_deadline",
        "cleanup",
        "empty",
        "empty_parse",
        "offline_cache",
        "cache_upload",
    ],
)
def test_native_poly_preserves_patch_outcome_and_removes_only_own_container(
    study, monkeypatch, fault
):
    args, case, solver, _ = study
    resource = b"pinned public tokenizer file\n"
    if fault in {"offline_cache", "cache_upload"}:
        args.cl100k_cache_file = args.solver_run.parent / "cl100k_base.tiktoken"
        args.cl100k_cache_file.write_bytes(resource)
        monkeypatch.setattr(poly, "CL100K_SHA256", hashlib.sha256(resource).hexdigest())
    if fault == "wall_deadline":
        ticks = iter([0, 0, 61, 62])
        monkeypatch.setattr(
            poly, "time", SimpleNamespace(monotonic=lambda: next(ticks))
        )
    if fault == "empty":
        solver["model_patch"] = ""
        (args.solver_run / "solver.json").write_text(json.dumps(solver))
        (args.solver_run / "predictions.json").write_text(
            json.dumps([{"instance_id": case["instance_id"], "model_patch": ""}])
        )
    events, containers = [], {"unrelated": object()}

    class MissingContainer(Exception):
        pass

    class Container:
        def __init__(self, options):
            self.name = options["name"]
            self.labels = options["labels"]

        def start(self):
            if fault == "startup":
                raise TimeoutError("start timed out")

        def exec_run(self, command, workdir):
            assert workdir == "/testbed"
            return SimpleNamespace(
                exit_code=0,
                output=b"base\nwrong\n" if fault == "tree" else b"base\ntree\n",
            )

        def put_archive(self, path, data):
            assert path == "/tmp"
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                members = archive.getmembers()
                assert len(members) == 2 and members[0].isdir()
                assert members[1].name == (
                    "codenib-tiktoken-cache/"
                    + hashlib.sha1(poly.CL100K_URL.encode()).hexdigest()
                )
                assert members[1].isfile()
                assert archive.extractfile(members[1]).read() == resource
            events.append(("offline_cache", self.name))
            return fault != "cache_upload"

        def remove(self, **kwargs):
            if fault == "cleanup":
                raise RuntimeError("removal failed")
            events.append(("remove", self.name))
            del containers[self.name]

    def create(**options):
        assert options["network_disabled"] and options["mem_limit"] == "3g"
        assert options["command"] == ["300"]
        if fault in {"offline_cache", "cache_upload"}:
            assert options["environment"] == {
                "TIKTOKEN_CACHE_DIR": poly.CL100K_CACHE_DIR
            }
        else:
            assert "environment" not in options
        result = Container(options)
        containers[result.name] = result
        events.append(("create", result.name))
        return result

    def get(name):
        if name not in containers:
            raise MissingContainer("absent")
        return containers[name]

    metadata = {
        "Id": args.native_image,
        "Config": {"WorkingDir": "/testbed"},
        "RepoDigests": [
            "ghcr.io/timesler/swe-polybench.eval.x86_64."
            + case["instance_id"]
            + "@sha256:"
            + "b" * 64
        ],
    }
    client = SimpleNamespace(
        images=SimpleNamespace(get=lambda image: SimpleNamespace(attrs=metadata)),
        containers=SimpleNamespace(create=create, get=get),
    )
    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(
            from_env=lambda **kwargs: client,
            errors=SimpleNamespace(NotFound=MissingContainer),
        ),
    )

    class Manager:
        def __init__(self, image_id, *, delete_image, client):
            assert image_id == args.native_image and delete_image is False
            self.image_id, self.client = image_id, client
            self.container, self.run_logs = None, []

        def apply_patch_to_container(self, patch, kind):
            events.append((kind, patch))
            if (kind == "test" and fault == "test_patch") or (
                kind == "code" and fault == "invalid_patch"
            ):
                raise ValueError("Failed to apply patch.")
            if kind == "code" and fault == "patch_transport":
                raise TimeoutError("transport failed")

        def docker_run(self, command, timeout):
            assert command == case["test_command"] and timeout == 60
            if fault == "test_timeout":
                raise TimeoutError("test timed out")
            self.run_logs = ["native results", "Container exited with status code: 4"]
            if fault == "returned_timeout":
                self.run_logs.append("Container operation timed out")
                return 1
            if fault == "no_terminal_footer":
                self.run_logs = ["native results"]
            return 4  # Unscored baseline failures must not replace native judgment.

        def _cleanup(self):
            if self.container and fault != "cleanup":
                self.container.remove()
            self.container = None

    @dataclasses.dataclass
    class Outcome:
        resolved: bool

    def score(key, result, f2p, p2p, applied, generated):
        assert (key, f2p, p2p) == (case["instance_id"], ["fix"], ["stable"])
        return Outcome(bool(result) and applied and generated)

    native = SimpleNamespace(
        manager=Manager,
        hashes=poly.HARNESS_HASHES,
        score=score,
        parser=lambda text: SimpleNamespace(
            parse=lambda: (
                {}
                if fault == "empty_parse"
                else {
                    "passed_tests": (
                        ["fix"] if fault == "partial_success" else ["fix", "stable"]
                    ),
                    "failed_tests": [],
                }
            )
        ),
    )
    monkeypatch.setattr(poly, "native_environment", lambda path: native)
    poly.grade_poly(args)
    result = json.loads((args.output / "resolution.json").read_text())
    assert result["resolved"] is (fault in {None, "offline_cache"})
    assert result["status"] == (
        "graded"
        if fault in (None, "empty", "empty_parse", "invalid_patch", "offline_cache")
        else "error"
    )
    assert result["container_removed"] is (fault != "cleanup")
    assert "unrelated" in containers
    assert set(containers) == {"unrelated"} or fault == "cleanup"
    assert ("remove", "unrelated") not in events
    assert all(event[1] != case["patch"] for event in events)
    if ("code", solver["model_patch"]) in events:
        assert events.index(("test", case["test_patch"])) < events.index(
            ("code", solver["model_patch"])
        )
    if fault is None:
        assert result["native_test_returncode"] == 4
    if fault == "empty_parse":
        assert json.loads((args.output / "parsed-tests.json").read_text()) == {}
        assert not result["native_report"]["resolved"]
        assert result["missing_required_tests"] == ["fix", "stable"]
        assert result["native_test_collection_observed"] is False
    if fault == "offline_cache":
        assert [event[0] for event in events][:3] == ["create", "offline_cache", "test"]
    if fault in {"offline_cache", "cache_upload"}:
        saved = json.loads((args.output / "config.json").read_text())
        assert saved["image_id"] == args.native_image
        assert saved["offline_tokenizer_cache"]["resource_sha256"] == poly.CL100K_SHA256
    if fault == "cache_upload":
        assert not any(event[0] in {"test", "code"} for event in events)
    if fault in {
        "returned_timeout",
        "partial_success",
        "no_terminal_footer",
        "wall_deadline",
    }:
        assert result["native_report"]["resolved"]
        assert result["native_execution_incomplete"]


def test_poly_grader_requires_native_checkout():
    with pytest.raises(ValueError, match="native-upstream"):
        poly.native_environment(None)


def test_poly_rejects_wrong_offline_cache_before_native_import(study, monkeypatch):
    args, _, _, _ = study
    args.cl100k_cache_file = args.solver_run.parent / "incorrect-tokenizer"
    args.cl100k_cache_file.write_bytes(b"incorrect public resource")
    monkeypatch.setattr(
        poly,
        "native_environment",
        lambda path: pytest.fail(
            "Must reject before importing native harness or Docker"
        ),
    )
    with pytest.raises(ValueError, match="pinned SHA-256"):
        poly.grade_poly(args)
    assert not args.output.exists()
