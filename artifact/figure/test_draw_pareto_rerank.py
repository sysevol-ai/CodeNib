#!/usr/bin/env python3
"""Regression tests for retrieval/build-cost alignment in Figure 5."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from draw_pareto_rerank import mean_build_time


class ParetoBuildCostTest(unittest.TestCase):
    def test_uses_complete_l2_cost_for_l2_retrieval(self) -> None:
        model = "example/embedder"
        profiles = [
            {
                "embedding_model": model,
                "profile_tag": "codeminer_base_test",
                "sections": [
                    {"label": "vector_store_add_documents_l0", "total": 1.0},
                    {"label": "vector_store_add_documents_l2", "total": 10.0},
                ],
            },
            {
                "embedding_model": model,
                "profile_tag": "codeminer_base_test",
                "sections": [
                    {"label": "embedding_encode_l0", "total": 2.0},
                    {"label": "faiss_index_add_l0", "total": 0.5},
                    {"label": "embedding_encode_l2", "total": 4.0},
                    {"label": "faiss_index_add_l2", "total": 1.0},
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for position, payload in enumerate(profiles):
                (root / f"profile-{position}.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
            observed = mean_build_time(model, root)

        self.assertEqual(observed, 7.5)


if __name__ == "__main__":
    unittest.main()
