import unittest

import matplotlib.pyplot as plt
import numpy as np
from draw_maintenance_lifecycle import (
    _draw_horizontal_half_violin,
    _extract_graph_points,
    _extract_vector_points,
)


class MaintenanceLifecycleTest(unittest.TestCase):
    def test_horizontal_half_violin_returns_median_and_draws_summary(self):
        figure, axis = plt.subplots()
        try:
            median = _draw_horizontal_half_violin(
                axis,
                1.0,
                np.asarray([1.0, 2.0, 3.0, 4.0, 5.0]),
                "#336699",
            )
            self.assertEqual(median, 3.0)
            self.assertGreater(len(axis.collections), 0)
            self.assertEqual(len(axis.patches), 1)
        finally:
            plt.close(figure)

    def test_graph_points_retain_guard_failures_and_skip_no_source(self):
        rows = [
            {
                "instance_id": "repo-a",
                "step": 1,
                "language": "go",
                "delta_files": 1,
                "speedup_vs_fully_rebuild": 8.0,
                "equivalence_guarded_speedup_vs_fully_rebuild": 8.0,
                "fully-rebuild": {"t_s": 80.0},
                "file-level-patch": {"amortized_t_s": 20.0},
                "file_level_equivalence_guarded_speedup_vs_fully_rebuild": 4.0,
            },
            {
                "instance_id": "repo-a",
                "step": 2,
                "language": "go",
                "delta_files": 1,
                "speedup_vs_fully_rebuild": 4.0,
                "equivalence_guarded_speedup_vs_fully_rebuild": None,
                "fully-rebuild": {"t_s": 80.0},
                "file-level-patch": {"amortized_t_s": 40.0},
                "file_level_equivalence_guarded_speedup_vs_fully_rebuild": None,
            },
            {
                "instance_id": "repo-a",
                "step": 3,
                "language": "go",
                "delta_files": 0,
                "speedup_vs_fully_rebuild": None,
            },
        ]

        points = _extract_graph_points(rows)

        self.assertEqual(
            [(point["arm"], point["speedup"]) for point in points],
            [("symbol", 8.0), ("file", 4.0), ("symbol", 4.0), ("file", 2.0)],
        )
        self.assertEqual(
            [point["admitted"] for point in points],
            [True, True, False, False],
        )

    def test_vector_points_use_test_excluded_scope_and_raw_times(self):
        rows = [
            {
                "instance_id": "repo-b",
                "step": 1,
                "language": "rust",
                "delta_files_test_excluded": 2,
                "fully-rebuild": {"t_s": 20.0},
                "incremental": {"t_s": 0.5},
                "analysis_admitted": False,
            },
            {
                "instance_id": "repo-b",
                "step": 2,
                "language": "rust",
                "delta_files_test_excluded": 0,
                "fully-rebuild": {"t_s": 0.0},
                "incremental": {"t_s": 0.1},
                "analysis_admitted": True,
            },
        ]

        points = _extract_vector_points(rows)

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["arm"], "delta")
        self.assertEqual(points[0]["speedup"], 40.0)
        self.assertFalse(points[0]["admitted"])


if __name__ == "__main__":
    unittest.main()
