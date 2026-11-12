# Use a CodeNib graph in SeeRepo

Export a Python repository's CodeNib symbol graph and generate SeeRepo
configuration with graph-query instructions.

Export maps `contain` to `contains` and the root ID `.` to `/`. Other IDs, node
and edge types, attributes and parallel references are retained. `reference` links
an element to a symbol it uses; `type-use` links an element to a type it uses.

Install CodeNib's graph runtime, SeeRepo's dependencies, `networkx`, Python
`graphviz` and the system `dot` executable. Clone SeeRepo:

```bash
git clone https://github.com/cslsolow/SeeRepo.git /path/to/SeeRepo
git -C /path/to/SeeRepo checkout c4a2c735abcf568aa9286dfcea001641ef0d6d1a
```

Check out the task's source revision, then export:

```bash
python -m codenib.integrations.seerepo \
  --upstream /path/to/SeeRepo --repo /path/to/task-repository \
  --build-if-missing --instance-id owner__repo-123 \
  --base-commit TASK_BASE_COMMIT --output-dir /path/to/graph-index \
  --config /path/to/seerepo-codenib.yaml
```

`--build-if-missing` uses CodeNib's toolchain preflight and graph preparation to
build only `symbol_graph`. Omit it to require an existing graph; `--manifest PATH`
selects an existing manifest. Existing indexes must match the checked-out source
revision. Export tasks into the same index directory; re-export replaces the
task's pickle. Existing configuration supplies model and environment settings.

From the installed SeeRepo checkout, run the agent:

```bash
python -m minisweagent.run.extra.swebench \
  --subset verified --split test --filter '^owner__repo-123$' \
  --config /path/to/seerepo-codenib.yaml \
  --workers 1 --output /path/to/trajectories
```
