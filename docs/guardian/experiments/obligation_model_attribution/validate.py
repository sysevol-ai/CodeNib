"""Validate the obligation-model attribution ledger against raw run artifacts."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
ROOTS = {
    "python": REPO / "data/deepswe_outputs",
    "nonpython": REPO / "data/deepswe_outputs_nonpython_language_stratified5",
}
VALID_CATEGORIES = {
    "obligation_model",
    "implementation",
    "mixed",
    "uncertain",
    "operational_invalid",
}
OBSIDIAN_NAME_MISMATCH_TRIALS = {
    "luna_1",
    "luna_2",
    "luna_3",
    "luna_4",
    "terra_1",
    "terra_2",
    "terra_3",
    "sol_2",
    "sol_3",
    "sol_4",
}


def load_rows() -> list[dict[str, str]]:
    with (HERE / "trial_attributions.csv").open(newline="") as file:
        return list(csv.DictReader(file))


def metadata_path(row: dict[str, str]) -> Path:
    model, job = row["trial"].split("_")
    return (
        ROOTS[row["corpus"]]
        / f"gpt-5.6-{model}_medium"
        / row["task"]
        / "solo"
        / f"job_{job}"
        / "metadata.json"
    )


def trial_dir(metadata: dict[str, object]) -> Path:
    source = Path(str(metadata["source_result_json"])).parent
    candidates = [
        path
        for path in source.iterdir()
        if path.is_dir() and (path / "artifacts/model.patch").exists()
    ]
    assert len(candidates) == 1, (source, candidates)
    return candidates[0]


def agent_log(row: dict[str, str], metadata_file: Path) -> Path:
    local = metadata_file.parent / "agent_logs/codex.txt"
    if local.exists():
        return local
    relative = metadata_file.relative_to(REPO)
    return (
        Path("/home/xiangye/deep-swe/tasks")
        / row["task"]
        / "environment"
        / relative.parent
        / "agent_logs/codex.txt"
    )


def agent_message_count(path: Path) -> int:
    count = 0
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = record.get("item", {})
        if (
            record.get("type") == "item.completed"
            and item.get("type") == "agent_message"
        ):
            count += 1
    return count


def validate_inventory(rows: list[dict[str, str]]) -> dict[str, object]:
    all_metadata = [
        path
        for root in ROOTS.values()
        for path in root.glob("gpt-5.6-*_medium/*/solo/job_*/metadata.json")
    ]
    assert len(all_metadata) == 120
    failed_metadata = [
        path
        for path in all_metadata
        if not json.loads(path.read_text())["reward"]
    ]
    assert len(failed_metadata) == 70
    assert len(rows) == 70

    expected = set(failed_metadata)
    observed: set[Path] = set()
    for row in rows:
        assert row["category"] in VALID_CATEGORIES
        assert row["agent_evidence"]
        assert row["artifact_verifier_evidence"]
        assert row["counterfactual"]

        path = metadata_path(row)
        assert path not in observed
        observed.add(path)
        metadata = json.loads(path.read_text())
        assert metadata["reward"] == 0
        assert metadata["returncode"] == 0

        log = agent_log(row, path)
        assert log.exists() and log.stat().st_size > 0
        count = agent_message_count(log)
        cited = [
            int(number)
            for number in re.findall(r"\bM(\d+)", row["agent_evidence"])
        ]
        assert not cited or max(cited) <= count, (path, cited, count)

        patch = trial_dir(metadata) / "artifacts/model.patch"
        is_empty = patch.stat().st_size == 0
        assert is_empty == (row["category"] == "operational_invalid"), path

    assert observed == expected

    categories = Counter(row["category"] for row in rows)
    assert categories == {
        "obligation_model": 41,
        "implementation": 15,
        "mixed": 9,
        "operational_invalid": 5,
    }
    return {
        "trial_count": len(all_metadata),
        "failed_trial_count": len(failed_metadata),
        "category_counts": dict(categories),
    }


def validate_obsidian_name_artifact(rows: list[dict[str, str]]) -> int:
    recovered = 0
    affected = 0
    for row in rows:
        if (
            row["task"] != "obsidian-linter-auto-table-of-contents"
            or row["trial"] not in OBSIDIAN_NAME_MISMATCH_TRIALS
        ):
            continue
        affected += 1
        metadata = json.loads(metadata_path(row).read_text())
        output = (
            trial_dir(metadata) / "verifier/test-stdout.txt"
        ).read_text(errors="replace")
        before_grade = output.split("===== grade =====", 1)[0]
        matches = re.findall(
            r"Tests:\s+(?:(\d+) failed,\s*)?(?:(\d+) passed,\s*)?(\d+) total",
            before_grade,
        )
        failed, passed, total = matches[-1]
        assert int(total) == 41
        assert int(failed) + int(passed) == 41
        recovered += int(passed)
    assert affected == 10
    assert recovered == 351
    return recovered


def main() -> None:
    rows = load_rows()
    result = validate_inventory(rows)
    result["obsidian_recovered_passes"] = validate_obsidian_name_artifact(rows)

    analyzable = [
        row for row in rows if row["category"] != "operational_invalid"
    ]
    result["analyzable_failed_trials"] = len(analyzable)
    result["obligation_incidence"] = sum(
        row["category"] in {"obligation_model", "mixed"} for row in analyzable
    )
    result["implementation_incidence"] = sum(
        row["category"] in {"implementation", "mixed"} for row in analyzable
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
