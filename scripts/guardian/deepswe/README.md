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

The wrapper delegates multi-round local-specification search and patch checking
to `codenib.clients.guardian`. Its host-specific responsibility is limited to
launching the solver, validating commit-scoped exchange artifacts, and creating
independent sibling sandboxes for Guardian rollouts. CodeNib and its Python
environment are not mounted into the solver container.

`GuardianRequest` accepts provenance-preserving task context. Each item records
its source (`user_instruction`, `issue`, `ticket`, or `solver_summary`) and
fidelity (`verbatim` or `derived`). The DeepSWE adapter supplies the public task
instruction that the solver already receives. Generic callers may omit task
context entirely. Solver summaries remain derived interpretations and cannot
independently support a specification. This channel remains separate from
participant messages sent through `guardian-message`.

One Guardian review contains an internal multi-round search. A strong planner
generates four distinct first-round briefs, including one open-ended brief. Four
cheap explorers run independently, evidence is validated deterministically, and
a strong aggregator updates structured specification memory without checking the
patch. After trajectory distillation and frontier selection, two targeted
explorers run in round two. A separate strong patch checker then checks only
supported specifications. The 4/2 allocation and two-round limit are configurable
with `--guardian-explorer-count`, `--guardian-targeted-explorer-count`, and
`--guardian-search-rounds`.

Evidence source and authority are independent. Direct normative evidence, or two
independent conventional sources without direct counterevidence, can promote a
local specification to `supported`. Behavioral observations alone cannot.
Candidate-patch content, solver summaries, model agreement, and distilled search
experience are never sufficient support. Proposed and contested specifications
remain visible as uncertainty, but only supported specifications can produce
definite corrective findings.

Guardian performs at most three review cycles by default. Configure this with
`--guardian-max-cycles`. The final allowed report is marked terminal and keeps
its unresolved findings; subsequent solver edits can be acknowledged by the
checkpoint command but cannot launch another Guardian review.

Across outer solver-revision cycles, the host controller maintains structured
specification memory outside the solver repository and Guardian sandboxes. It
stores specification records, evidence and provenance, exploration experience,
round history, and per-stage inference usage. Repository evidence is marked stale
when its blob changes and must be reconsidered before reuse. The live memory stays
in host-private storage while the solver runs, then is exported to
`agent_logs/guardian_memory/` with an `events.jsonl` journal.

Every review also persists `request.json`, `config.json`, `memory.json`, per-round
brief/explorer/aggregation/distillation/frontier artifacts, `patch-check.json`,
`final-report.json`, and `usage.json`. Usage includes every planning, exploration,
aggregation, distillation, targeted-investigation, and patch-checking rollout,
including its normalized tool-call count.

Configure the inference allocation independently with
`--guardian-explorer-model` and `--guardian-aggregator-model`. The legacy
`--guardian-model` option remains a shared fallback for both roles. When none
of these options is provided, both roles continue to use the solver model.

Guardian rollouts deliberately disable Code Mode and use `unified_exec` for
direct commands inside their disposable sandboxes. Pier's solver keeps its own
Codex installation; Guardian installs a separate compatible CLI, pinned to
`0.145.0` by default because Codex `0.147.0` routes Luna through Code Mode even
when that feature is disabled. Override and record this compatibility setting
with `--guardian-codex-version` when a newer direct-shell-compatible release is
validated.

Generated trial data belongs under `data/deepswe_outputs/`; generated
`dashboard/data.json` is ignored and can be recreated by the exporter.
Set `CODENIB_DEEPSWE_ROOT` to use a DeepSWE checkout outside the default
sibling directory, and `CODENIB_DEEPSWE_OUTPUT_ROOT` to relocate trial data.
