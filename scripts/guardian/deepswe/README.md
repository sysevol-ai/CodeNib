# DeepSWE Guardian tooling

This directory owns CodeNib's external DeepSWE experiment integration:

- `harness/` is the DeepSWE adapter and host-side sandbox controller;
- `ablation.py` and `solo_matrix.py` launch trials;
- `results.py` and `export_dashboard.py` are thin command-line adapters over
  `codenib.eval.benchmarks.deepswe`;
- `outputs.py` maintains externally produced artifacts;
- `dashboard/` provides the static results viewer.

The packaged `codenib.eval.benchmarks.deepswe` modules load, cost, aggregate,
and report DeepSWE results. They deliberately do not launch the benchmark or
depend on its runtime. The external harness boundary remains in this directory.

The harness deliberately lives outside `codenib`. The external benchmark host
imports the custom agent before it starts a task container. Guardian policy and
execution abstractions remain packaged under `codenib.clients`; only the Pier
lifecycle and commit-exchange adapter live here.

Run tools as modules from the repository root, for example:

```bash
python -m scripts.guardian.deepswe.ablation --help
python -m scripts.guardian.deepswe.solo_matrix --help
python -m scripts.guardian.deepswe.results --help
python -m scripts.guardian.deepswe.export_dashboard --help
```

The DeepSWE runner loads the Guardian wrapper through:

```text
scripts.guardian.deepswe.harness.agent:GuardianCodingAgent
```

The wrapper delegates local-specification discovery and aggregation to
`codenib.clients.guardian`. Its host-specific responsibility is limited to
launching the solver, validating commit-scoped exchange artifacts, and creating
independent sibling sandboxes for Guardian rollouts. CodeNib and its Python
environment are not mounted into the solver container.

Guardian performs at most three review cycles by default. Configure this with
`--guardian-max-cycles`. The final allowed report is marked terminal and keeps
its unresolved findings; subsequent solver edits can be acknowledged by the
checkpoint command but cannot launch another Guardian review.

Across those cycles, the host controller maintains a local-specification ledger
outside the solver repository and Guardian rollout sandboxes. Each remembered
specification retains snapshot-addressed evidence and per-snapshot assessments;
evidence is marked stale when its repository blob changes. Explorers and the
aggregator receive this ledger as fallible context and must revalidate it. The
live ledger stays in host-private storage while the solver runs, then is exported
to `agent_logs/guardian_memory/` with its append-only `events.jsonl` journal for
experiment analysis.

Configure the inference allocation independently with
`--guardian-explorer-model` and `--guardian-aggregator-model`. The legacy
`--guardian-model` option remains a shared fallback for both roles. When none
of these options is provided, both roles continue to use the solver model.

Generated trial data belongs under `data/deepswe_outputs/`; generated
`dashboard/data.json` is ignored and can be recreated by the exporter.
Set `CODENIB_DEEPSWE_ROOT` to use a DeepSWE checkout outside the default
sibling directory, and `CODENIB_DEEPSWE_OUTPUT_ROOT` to relocate trial data.
