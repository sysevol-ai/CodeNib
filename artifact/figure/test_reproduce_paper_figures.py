import unittest
from pathlib import Path

from reproduce_paper_figures import SCRIPT_DIR, SOURCE_FILES, build_jobs


class ReproducePaperFiguresTest(unittest.TestCase):
    def test_reviewer_plot_sources_have_no_machine_paths(self):
        for source in SOURCE_FILES:
            text = (SCRIPT_DIR / source).read_text(encoding="utf-8")
            with self.subTest(source=source):
                self.assertNotIn("/mnt/", text)
                self.assertNotIn("/home/", text)

    def test_agent_job_passes_every_model_root_explicitly(self):
        inputs = {
            "haiku_roots": [Path("haiku")],
            "qwen9b_roots": [Path("qwen9")],
            "qwen27b_roots": [Path("qwen27")],
            "gemma12b_roots": [Path("gemma12")],
            "gemini25_roots": [Path("gemini25")],
            "profile_dir": Path("profile"),
            "query_dir": Path("query"),
            "eval_dir": Path("eval"),
            "graph_result_dir": Path("graph"),
            "graph_selection_results": Path("selection.json"),
            "dataset_stats": Path("stats.json"),
            "faiss_results": Path("faiss.json"),
            "lsp_reports": Path("lsp"),
            "lifecycle_summary": Path("lifecycle.json"),
            "incremental_graph_results": Path("graph.jsonl"),
            "incremental_vector_results": Path("vector.jsonl"),
            "incremental_summary": Path("incremental.json"),
            "model_scale_metadata": Path("scale.json"),
            "model_cache_provenance": Path("provenance.json"),
        }
        protocol = {
            "graph_bootstrap_samples": 20,
            "agent_bootstrap_samples": 10,
            "agent_expected_pairs": 500,
            "seed": 7,
            "recall_guardrail": -0.05,
            "accuracy_guardrail": 0.95,
        }

        jobs = build_jobs(inputs, protocol, Path("output"))
        job = next(item for item in jobs if item.name == "figure_8_agent")

        expected = {
            "--haiku-root": "haiku",
            "--qwen9b-root": "qwen9",
            "--qwen27b-root": "qwen27",
            "--gemma12b-root": "gemma12",
            "--gemini25-root": "gemini25",
        }
        for flag, path in expected.items():
            position = job.arguments.index(flag)
            self.assertEqual(job.arguments[position + 1], path)


if __name__ == "__main__":
    unittest.main()
