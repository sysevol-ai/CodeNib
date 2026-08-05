# DeepSWE Guardian tooling

This directory owns CodeNib's external DeepSWE experiment integration:

- `harness/` is the dependency-isolated Pier adapter and Guardian bridge;
- `ablation.py` and `solo_matrix.py` launch trials;
- `results.py` and `export_dashboard.py` are thin command-line adapters over
  `codenib.eval.benchmarks.deepswe`;
- `outputs.py` maintains externally produced artifacts;
- `dashboard/` provides the static results viewer.

The packaged `codenib.eval.benchmarks.deepswe` modules load, cost, aggregate,
and report DeepSWE results. They deliberately do not launch the benchmark or
depend on its runtime. The external harness boundary remains in this directory.

The harness deliberately lives outside `codenib`. The external benchmark host
imports the custom agent before it starts a task container, so that adapter
must remain dependency-isolated from CodeNib's packaged runtime.

Run tools as modules from the repository root, for example:

```bash
python -m scripts.guardian.deepswe.ablation --help
python -m scripts.guardian.deepswe.solo_matrix --help
python -m scripts.guardian.deepswe.results --help
python -m scripts.guardian.deepswe.export_dashboard --help
```

Pier loads the Guardian wrapper through:

```text
scripts.guardian.deepswe.harness.agent:GuardianCodingAgent
```

Generated trial data belongs under `data/deepswe_outputs/`; generated
`dashboard/data.json` is ignored and can be recreated by the exporter.
