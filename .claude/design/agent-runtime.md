# CodeNib Agent Runtime — coherent design

Status: **decided by experiment** (2026-06-19). Extends
`.claude/design/preload-vs-skill-architecture.md` (confirmed 2026-06-05). §1–§8
below are the design as proposed; §0 is what the runtime-probe data actually
decided — read §0 first, it overrides the speculative parts of §5/§7.

## 0. Results — what the data decided (runtime_probe, 4 langs, 1328 cells)

Per-query paired-bootstrap on codeminer-synthesis (Go/Rust/TS/C++; Python
excluded — its sweep hung under 5-way concurrency; reps=1). Headline answer to
"does our pre-load save tokens at equal accuracy?": **yes, significantly.**

| arm (vs grep_only) | Δrec@5 [95% CI] | Δcost% [95% CI] | verdict |
|---|---|---|---|
| **preinj_embed** | **+0.018 [−0.014,+0.050]** | **−17.0% [−20.4,−13.6]** | **SAVE** |
| preinj_graph | +0.016 [−0.019,+0.051] | −11.7% [−14.7,−8.6] | **SAVE** |
| preinj_graph_verify | −0.006 [−0.068,+0.058] | −8.1% | no-op |

**Three decisions the data forces:**
1. **SHIP: flat embedding pre-load.** Equal accuracy (CI straddles 0, point
   +0.018), **−17% cost**, significant at n≈400. Prior-minimal: one recipe, no
   per-category routing. This is the runtime's real, defensible edge.
2. **CUT: graph in the pre-load.** Dominated by embedding — saves less (−12% vs
   −17%), net-flat accuracy that *helps* reasoning (+0.075) / file_hint (+0.082)
   but *hurts* symbol_hint (−0.05) and even **traversal** (−0.025, 11/32/16
   win/tie/loss — graph loses on its supposed home turf). Reserve the graph for
   on-demand navigation, do not blanket-inject it.
3. **CUT: verify-expand (Layer 4).** A **no-op**: 0 / 309 verify cells triggered
   the re-run. The resolution check never fires because the agent always cites a
   *real* graph symbol — its failure mode is the *wrong real* symbol, which a
   GT-free check cannot catch. `preinj_graph_verify ≈ preinj_graph`. The
   "grep-can't-do via verify" thesis does not materialise in any prior-free form.

**Product (decided):** a flat **embedding pre-load context-engine** — compiled
index retrieval injected into the opening prompt + the standard grep/read loop.
Equal accuracy, ~17% cheaper. NOT a graph-orchestration runtime and NOT a
verify-runtime; both were implemented, measured, and cut. The novelty is
compiler-precise *pre-load*, not the loop.

Caveats: Python missing (rerun solo); reps=1; verify no-op is mechanism-proven
(0% trigger) not just CI. §5/§7 below are superseded by this section.

## 1. Thesis: split deterministic from stochastic

Every mainstream code agent (SWE-agent / OpenHands / Aider / Cursor / Claude
Code) treats the repo as text explored at runtime; the LLM is planner +
executor + memory; relationships are re-derived lossily each session. CodeNib's
only structural asset is a **compiled, SCIP-precise graph with edge provenance**
(`IndexCompiler.compile_repo` → `RepoManifest`). So the runtime's job is NOT a
smarter loop — it is:

> Do everything that can be computed deterministically against the compiled
> artifact deterministically (context assembly, navigation, verification);
> call the LLM only for genuinely ambiguous decisions.

**Concession (load-bearing, keeps us honest):** for "find a named thing"
(symbol_hint, string/error lookup) grep + a frontier model already wins, often
cheaper. We do **not** compete there. The runtime earns its keep only on what
grep *structurally* cannot do: symbol **resolution** (which `process()` binds
here), **typed edges** (implementations / callers as a set), **transitive
closure** (k-hop blast radius), and **completeness** ("are these ALL the
callers?"). Value ∝ task stakes, not query count.

## 2. The runtime is four layers (3.5 already exist)

| layer | what | home in code | status |
|------|------|------|------|
| **Compile** | repo → precise typed graph + indexes | `compiler/index_compiler.py`, `compiler/manifest.py` (`RepoManifest`) | done |
| **Route** | `classify(query, sctx)` → which pre-load recipe | `codeminer/agent/compile.py::classify`, `harness.scenario_for` | done (recipe table thin) |
| **Pre-load** | run recipe (embedding [+bm25] [+graph expand], NO rerank) → inject `(file,span,snippet)` into the opening prompt | `lib/preload.py::assemble_preload`, wired in `harness.run_cell` (`effective_query = query + preamble`) | done for embedding; **graph supported but unconfigured** (`_PRELOAD_RETRIEVER_SKILL["graph"]="codeminer_context"`) |
| **Verify-expand** | after the agent commits, check answer spans/edges against the graph; on miss, deterministically expand pre-load with graph neighbors and re-run; bounded | NEW — wraps `run_cell` / `AgentRunner.run` | **TODO (the runtime's novelty)** |

Layers 1–3 are "a great context engine." Layer 4 is what makes it an *agent
runtime* rather than an MCP context provider — and it is the only part that
needs the loop.

## 3. The router worry, resolved

We do **not** ship a new "router paradigm" the ecosystem must adopt. Two faces,
one codebase:

- **Outside = standard tool-calling / MCP** (`codeminer/mcp/`). Expose at most
  one coarse `get_context(query)` + one `expand_context`; drops into any host
  (Claude Code, Cursor). Full compatibility — this is the adoption surface.
- **Inside = the deterministic router over *recipes*** (not a menu of tools the
  LLM must choose). `classify → recipe → pre-load → loop → verify`. The router
  picks *which retrievers / top-k / graph-hops / whether to verify*, invisibly.

The thing that failed in our own data — offering skills and hoping the LLM
routes to them (`embedding_search` 0–4% adoption, `find_*` ignored) — is what
mainstream is *also* moving away from (too many tools degrade tool-calling). We
are early on "fewer tools + better context," not idiosyncratic.

## 4. Why this is not a shell (套壳)

A shell dies when the model improves and is clonable via the same API. Inverse
here: the **LLM is the swappable policy**; the weight is in compile + graph +
verify, none of which is reachable by calling a model API. Test: (a) swap a
better model → value *compounds* (same harness, stronger policy, verify still
on); (b) a competitor on the same API *cannot* replicate without building the
compiler + verifier; (c) we provide what the model alone never will —
determinism, no-hallucinated-edges, cost/latency control, reproducible eval.
We **become** a shell only if Layer 4 is "a ReAct loop around GPT" with nothing
deterministic in it — which is exactly why Layer 4 is verify-expand, not prompt
scaffolding.

## 5. Verify-expand loop — spec (the novel layer)

Plugs in right after `runner.run(effective_query)` in `run_cell`:

```
answer  = runner.run(effective_query)
spans   = parse_answer_spans(answer) + resolve_symbol_spans(answer, idx)   # exists
verdict = graph_verify(spans, manifest)        # NEW, deterministic
  - every claimed symbol resolves to a real graph node?         (resolution)
  - if the task is edge-shaped (callers/impl/impact): is the claimed
    set complete vs the graph's typed edge set?                 (completeness)
if verdict.ok or budget exhausted: return answer
else:
  extra = graph_expand(verdict.missing, hops=1)  # exact neighbors of the miss
  effective_query += render(extra)               # deterministic, no new LLM call
  retry (bounded: <=1-2 expansions)
```

Key properties that keep it self-consistent:
- **Deterministic** (graph lookups, no LLM in the verify/expand step) → it adds
  cost only as *bounded* extra agent turns, and only when verification fails.
- **Additive** → floor = the pre-load arm (can't score worse; it only adds
  retries on detected misses).
- The metric it must move: **wrong-target rate down** and **hard-category
  answer_rec up**, at acceptable Δcost. If it doesn't, it is cut (see §7).

## 6. Validation — per-category Pareto on codeminer-synthesis (500)

Harness already exists: `run_synthesis_sweep.py` (per-query, groups by instance
so the index amortizes across a repo's ~20 queries — the reuse advantage),
records per-cell `category`, `answer_blocks`/`retrieval_blocks` recall,
`total_turns`, `total_tokens`, `cost_usd`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, and `preload_contribution`.

Arms (one config, same model/dataset/scorer):
1. `grep_only` — read/grep/glob/bash (baseline; the honest ceiling on easy half).
2. `preinj_embed` — + embedding candidates pre-injected (current confirmed win).
3. `preinj_graph` — + embedding seeds **graph-expanded 2-hop** pre-injected.
4. `preinj_graph_verify` — (3) + the §5 verify-expand loop.

Read it per **category** (easy→hard for grep): symbol_hint / file_hint /
module_hint / behavioral / reasoning / traversal.

Decision rule ("等精度省 token"): for a category the win holds iff
**Δaccuracy CI overlaps 0 (≈ equal) AND Δcost < 0**. Expected shape:
- hint-rich → all arms ≈ tie (don't over-invest; pre-load may cost slightly
  more — fine).
- exploration-heavy (behavioral / reasoning / **traversal**) → pre-load arms
  move **down-and-left** (cost down at equal/again accuracy) because they
  collapse multi-turn grep exploration into one cached injection; graph adds
  lift where resolution/closure matters; verify adds lift on wrong-target.

Cost must be read cache-adjusted: `cost_usd` is litellm's cache-aware price;
output (`completion_tokens`) dominates (~5x input), so the win is *fewer turns* +
*cached stable repo context* (same repo's ~20 queries reuse the injected block).

## 7. Kill conditions (so the conclusion is honest, not motivated)

- If `preinj_graph` ≈ `preinj_embed` everywhere → graph in pre-load isn't pulling
  weight on this data; ship embedding-only pre-load, keep graph for the verifier.
- If `preinj_graph_verify` ≈ `preinj_graph` (no wrong-target / hard-cat gain) →
  **do not ship Layer 4.** Then the honest product is an **MCP context engine**,
  not an "agent runtime." Don't maintain a loop that doesn't beat open-loop.
- If even `preinj_*` ≈ `grep_only` on every category → grep is the ceiling here;
  prune and reposition. (We already expect this on the hint-rich half.)

## 8. Build order (incremental, on top of what exists)

1. **Add the graph arm** — config `configs/runtime_probe.yaml` with arms
   `grep_only` / `preinj_embed` / `preinj_graph` (graph supported already;
   recipe `retrievers:[embedding,graph]`). *(no new code)*
2. **Derisk run** — 10 queries/category (~60) `grep_only` vs `preinj_embed` vs
   `preinj_graph`; confirm harness + cache cost + directional per-category read.
3. **Aggregator Pareto** — generalize the Δ table to every preinj arm and add
   **Δcost / Δturns** columns (accuracy-and-cost together = the Pareto view).
4. **Implement Layer 4** (verify-expand) behind a recipe flag `verify: true`;
   add arm `preinj_graph_verify`; unit-test `graph_verify` / `graph_expand`
   against the manifest before any LLM run.
5. **Full 500 × arms**, per-category Pareto; apply §7 to decide the shipped form.

The product that "appears" at the end = `codeminer serve` (MCP context engine,
always) + `codeminer agent <task>` (the verify-expand controller, *iff* §7 step 4
earns it), specialized on resolution / impact / completeness tasks.
