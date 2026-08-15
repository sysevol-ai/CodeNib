# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Order-independent explorer overlap and scaling measurements."""

from __future__ import annotations

from itertools import combinations
from statistics import mean

from .types import (
    MAX_EXPLORERS,
    ExplorerOutput,
    SpecificationRecord,
    SpecificationStatus,
)


def _round_specifications(
    outputs: tuple[ExplorerOutput, ...], records: tuple[SpecificationRecord, ...]
) -> tuple[SpecificationRecord, ...]:
    candidate_ids = {
        candidate.candidate_id for output in outputs for candidate in output.candidates
    }
    return tuple(
        record
        for record in records
        if any(item.candidate_id in candidate_ids for item in record.provenance)
    )


def explorer_contribution_report(
    outputs: tuple[ExplorerOutput, ...], records: tuple[SpecificationRecord, ...]
) -> dict[str, object]:
    """Compute reproducible contribution statistics without completion ordering.

    Aggregator provenance defines candidate equivalence. Aggregator relation edges
    define support and contradiction. Subset coverage includes invalid explorers so
    it estimates the actual return from allocating ``k`` parallel rollouts.
    """

    explorers = tuple(output.explorer for output in outputs)
    if len(explorers) > MAX_EXPLORERS:
        raise ValueError(
            f"contribution analysis supports at most {MAX_EXPLORERS} explorers"
        )
    candidate_owner = {
        candidate.candidate_id: output.explorer
        for output in outputs
        for candidate in output.candidates
    }
    round_records = _round_specifications(outputs, records)
    accepted = tuple(
        item
        for item in round_records
        if item.status is not SpecificationStatus.REJECTED
    )
    candidate_to_spec = {
        provenance.candidate_id: record.specification_id
        for record in round_records
        for provenance in record.provenance
    }
    discovered: dict[str, set[str]] = {explorer: set() for explorer in explorers}
    rejected: dict[str, set[str]] = {explorer: set() for explorer in explorers}
    for record in round_records:
        target = (
            rejected if record.status is SpecificationStatus.REJECTED else discovered
        )
        for provenance in record.provenance:
            owner = candidate_owner.get(provenance.candidate_id)
            if owner in target:
                target[owner].add(record.specification_id)

    by_id = {item.specification_id: item for item in accepted}

    def related(left: str, right: str, field: str) -> bool:
        return right in getattr(by_id[left].related_entries, field) or left in getattr(
            by_id[right].related_entries, field
        )

    pairwise = []
    for left, right in combinations(explorers, 2):
        left_specs = discovered[left]
        right_specs = discovered[right]
        shared = left_specs & right_specs
        distinct = tuple(
            (a, b)
            for a in left_specs
            for b in right_specs
            if a != b and a in by_id and b in by_id
        )
        supporting_pairs = {
            tuple(sorted((a, b)))
            for a, b in distinct
            if related(a, b, "equivalent") or related(a, b, "entailed_by")
        }
        contradicting_pairs = {
            tuple(sorted((a, b)))
            for a, b in distinct
            if related(a, b, "conflicts_with")
        }
        pairwise.append(
            {
                "explorer_a": left,
                "explorer_b": right,
                "corroborated_specifications": len(shared),
                "supporting_relation_pairs": len(supporting_pairs),
                "contradicting_relation_pairs": len(contradicting_pairs),
                "unique_to_a": len(left_specs - right_specs),
                "unique_to_b": len(right_specs - left_specs),
            }
        )

    conflict_pairs = {
        tuple(sorted((specification_id, other)))
        for specification_id, record in by_id.items()
        for other in record.related_entries.conflicts_with
        if other in by_id and other != specification_id
    }
    per_explorer = []
    for output in outputs:
        own = discovered[output.explorer]
        other = set().union(
            *(values for name, values in discovered.items() if name != output.explorer)
        )
        per_explorer.append(
            {
                "explorer": output.explorer,
                "valid_output": not bool(output.error),
                "attempts": len(output.attempts),
                "candidate_count": len(output.candidates),
                "accepted_specifications": len(own),
                "corroborated_specifications": len(own & other),
                "unique_specifications": len(own - other),
                "rejected_specifications": len(rejected[output.explorer]),
                "duration_seconds": sum(
                    attempt.duration_seconds for attempt in output.attempts
                ),
            }
        )

    subsets = []
    coverage_by_k = []
    prior_mean = 0.0
    for size in range(1, len(explorers) + 1):
        rows = []
        for names in combinations(explorers, size):
            counts = {
                specification_id: sum(
                    specification_id in discovered[name] for name in names
                )
                for specification_id in by_id
            }
            row = {
                "explorers": list(names),
                "k": size,
                "covered_specifications": sum(value > 0 for value in counts.values()),
                "corroborated_specifications": sum(
                    value >= 2 for value in counts.values()
                ),
                "contradicting_relation_pairs": sum(
                    left in counts
                    and counts[left] > 0
                    and right in counts
                    and counts[right] > 0
                    for left, right in conflict_pairs
                ),
            }
            rows.append(row)
            subsets.append(row)
        average = mean(row["covered_specifications"] for row in rows) if rows else 0.0
        coverage_by_k.append(
            {
                "k": size,
                "subset_count": len(rows),
                "mean_covered_specifications": average,
                "expected_distinct_specifications": average,
                "minimum_covered_specifications": min(
                    (row["covered_specifications"] for row in rows), default=0
                ),
                "maximum_covered_specifications": max(
                    (row["covered_specifications"] for row in rows), default=0
                ),
                "mean_corroborated_specifications": (
                    mean(row["corroborated_specifications"] for row in rows)
                    if rows
                    else 0.0
                ),
                "mean_contradicting_relation_pairs": (
                    mean(row["contradicting_relation_pairs"] for row in rows)
                    if rows
                    else 0.0
                ),
                "mean_marginal_coverage": average - prior_mean,
            }
        )
        prior_mean = average

    discovery_probabilities = []
    for specification_id in sorted(by_id):
        discoverers = {
            name for name, values in discovered.items() if specification_id in values
        }
        discovery_probabilities.append(
            {
                "specification_id": specification_id,
                "discoverers": sorted(discoverers),
                "single_rollout_discovery_probability": (
                    len(discoverers) / len(explorers) if explorers else 0.0
                ),
                "discovery_probability_by_k": [
                    {
                        "k": size,
                        "probability": mean(
                            any(name in discoverers for name in row["explorers"])
                            for row in subsets
                            if row["k"] == size
                        ),
                    }
                    for size in range(1, len(explorers) + 1)
                ],
            }
        )

    total_discoveries = sum(len(values) for values in discovered.values())
    return {
        "schema_version": 1,
        "method": {
            "ordering": "order-independent",
            "equivalence_basis": "shared aggregated specification provenance",
            "relation_basis": "aggregated equivalent, entailed_by, and conflicts_with edges",
            "subset_policy": "all allocated explorers, including invalid outputs",
            "coverage_policy": "aggregated specifications not adjudicated as rejected",
        },
        "summary": {
            "allocated_explorers": len(explorers),
            "valid_explorers": sum(not output.error for output in outputs),
            "accepted_specifications": len(by_id),
            "corroborated_specifications": sum(
                sum(specification_id in values for values in discovered.values()) >= 2
                for specification_id in by_id
            ),
            "contradicting_relation_pairs": len(conflict_pairs),
            "total_explorer_specification_discoveries": total_discoveries,
            "redundancy_rate": (
                1.0 - len(by_id) / total_discoveries if total_discoveries else 0.0
            ),
        },
        "explorers": per_explorer,
        "pairwise": pairwise,
        "subsets": subsets,
        "coverage_by_k": coverage_by_k,
        "specification_discovery": discovery_probabilities,
        "unrepresented_candidate_ids": sorted(
            candidate.candidate_id
            for output in outputs
            for candidate in output.candidates
            if candidate.candidate_id not in candidate_to_spec
        ),
    }


__all__ = ["explorer_contribution_report"]
