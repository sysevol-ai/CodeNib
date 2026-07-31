"""Validate the behavioral-claim attribution ledger against raw metadata."""

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


def validate_ledger(rows: list[dict[str, str]]) -> dict[str, object]:
    all_metadata = [
        path
        for root in ROOTS.values()
        for path in root.glob("gpt-5.6-*_medium/*/solo/job_*/metadata.json")
    ]
    assert len(all_metadata) == 120

    failed_metadata = []
    for path in all_metadata:
        metadata = json.loads(path.read_text())
        if not metadata["reward"]:
            failed_metadata.append(path)
    assert len(failed_metadata) == 70
    assert len(rows) == len(failed_metadata)

    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (row["corpus"], row["task"], row["trial"])
        assert key not in seen
        seen.add(key)

        metadata = json.loads(metadata_path(row).read_text())
        assert metadata["reward"] == 0
        f2p_deficit = int(metadata["f2p_total"] - metadata["f2p_passed"])
        p2p_deficit = int(metadata["p2p_total"] - metadata["p2p_passed"])
        attributed_f2p = sum(
            int(row[field])
            for field in (
                "f2p_normative",
                "f2p_implementation",
                "f2p_measurement",
            )
        )
        attributed_p2p = sum(
            int(row[field])
            for field in ("p2p_normative", "p2p_implementation")
        )
        assert attributed_f2p == f2p_deficit, (key, attributed_f2p, f2p_deficit)
        assert attributed_p2p == p2p_deficit, (key, attributed_p2p, p2p_deficit)

    reward = Counter(row["reward_attribution"] for row in rows)
    metric_totals = {
        field: sum(int(row[field]) for row in rows)
        for field in (
            "f2p_normative",
            "f2p_implementation",
            "f2p_measurement",
            "p2p_normative",
            "p2p_implementation",
        )
    }
    assert reward == {"normative": 15, "implementation": 47, "mixed": 8}
    assert metric_totals == {
        "f2p_normative": 108,
        "f2p_implementation": 406,
        "f2p_measurement": 351,
        "p2p_normative": 0,
        "p2p_implementation": 3428,
    }
    return {
        "trial_count": len(all_metadata),
        "failed_trial_count": len(failed_metadata),
        "reward_attribution": dict(reward),
        "metric_attribution": metric_totals,
    }


def validate_obsidian_measurement_artifact(rows: list[dict[str, str]]) -> int:
    recovered = 0
    affected = 0
    for row in rows:
        measurement = int(row["f2p_measurement"])
        if not measurement:
            continue
        affected += 1
        metadata = json.loads(metadata_path(row).read_text())
        result_path = Path(metadata["source_result_json"])
        trial_dir = next(
            path
            for path in result_path.parent.iterdir()
            if path.is_dir() and (path / "verifier/test-stdout.txt").exists()
        )
        output = (trial_dir / "verifier/test-stdout.txt").read_text(errors="replace")
        before_grade = output.split("===== grade =====", 1)[0]
        matches = re.findall(
            r"Tests:\s+(?:(\d+) failed,\s*)?(?:(\d+) passed,\s*)?(\d+) total",
            before_grade,
        )
        failed, passed, total = matches[-1]
        assert int(total) == 41
        assert int(failed) + int(passed) == 41
        assert int(passed) == measurement
        recovered += int(passed)
    assert affected == 10
    assert recovered == 351
    return recovered


def main() -> None:
    rows = load_rows()
    result = validate_ledger(rows)
    result["obsidian_recovered_passes"] = validate_obsidian_measurement_artifact(rows)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
