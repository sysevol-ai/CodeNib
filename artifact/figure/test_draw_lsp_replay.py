#!/usr/bin/env python3
"""Regression tests for LSP replay statistical units."""

from __future__ import annotations

import unittest

import matplotlib.pyplot as plt
from draw_lsp_replay import _median, draw_replay_latency, prepare_replay_data


def _row(
    request_id: str,
    static_ms: float,
    live_ms: float,
    *,
    same_result: bool = True,
) -> dict:
    return {
        "request": {
            "capability": "definition",
            "request_id": request_id,
        },
        "same_result": same_result,
        "static_call": {"duration_ms": static_ms},
        "reference_call": {"duration_ms": live_ms},
    }


class ReplayEstimatorTest(unittest.TestCase):
    def test_repetitions_are_summarized_before_requests(self) -> None:
        report = {
            "subject": {"language": "cpp"},
            "comparisons": [
                _row("r1", 1.0, 10.0),
                _row("r1", 3.0, 30.0),
                _row("r1", 100.0, 1000.0),
                _row("r2", 5.0, 5.0),
                _row("r2", 5.0, 5.0),
                _row("r2", 5.0, 5.0),
                _row("excluded", 1.0, 1.0, same_result=False),
            ],
        }

        data = prepare_replay_data([report])

        self.assertEqual(data.coverage[("cpp", "definition")], (2, 3))
        self.assertEqual(data.matched_row_count, 6)
        self.assertEqual(len(data.static_all), 2)
        self.assertEqual(_median(data.static_all), 4.0)
        self.assertEqual(_median(data.live_all), 17.5)
        self.assertEqual(_median(data.saving_all), 13.5)
        self.assertEqual(_median(data.speedup_all), 5.5)

    def test_matched_request_requires_complete_positive_timings(self) -> None:
        report = {
            "subject": {"language": "cpp"},
            "comparisons": [_row("broken", 0.0, 1.0)],
        }

        with self.assertRaisesRegex(ValueError, "invalid duration"):
            prepare_replay_data([report])

    def test_single_column_panel_can_suppress_redundant_title(self) -> None:
        report = {
            "subject": {"language": "cpp"},
            "comparisons": [_row("r1", 1.0, 2.0)],
        }
        data = prepare_replay_data([report])
        figure, axis = plt.subplots()
        try:
            draw_replay_latency(axis, data, panel_label="", title=None)
            self.assertEqual(axis.get_title(), "")
        finally:
            plt.close(figure)


if __name__ == "__main__":
    unittest.main()
