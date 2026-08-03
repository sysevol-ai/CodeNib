# DeepSWE Guardian tooling

This directory owns CodeNib's DeepSWE experiment integration:

- `harness/` is the dependency-isolated Pier adapter and Guardian bridge;
- `ablation.py` and `solo_matrix.py` launch trials;
- `results.py` and `outputs.py` summarize and maintain artifacts;
- `export_dashboard.py` and `dashboard/` provide the static results viewer.

The harness deliberately lives outside `codeminer`. Pier imports the custom
agent on the host before it starts a task container. Importing an adapter below
`codeminer` would first execute `codeminer/__init__.py` and require CodeNib's
tree-sitter and LLM dependencies in Pier's host environment.

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
