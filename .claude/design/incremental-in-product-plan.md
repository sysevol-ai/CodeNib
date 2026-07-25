# Making the paper's incremental mechanisms real in the product

**Status:** agreed, not started. **Target:** launch week (paper + code + HF + app).
**Owner:** Yash. **Written:** 2026-07-22.

Goal: move the paper's incremental machinery from *evaluated* to *running in the
shipped system*, so the demo demonstrates the contribution rather than
describing it.

---

## Where things actually stand (verified 2026-07-22)

The architecture already anticipates all of this. The decision layer is built;
the execution layer is stubbed.

| Layer | State | Evidence |
|---|---|---|
| Freshness **decision** | ✅ built | `resources.py:251-256` returns `ResourceDecision(state=STALE, action="incremental_update")` |
| Manifest commit tracking | ✅ built | `RepoManifest.commit`, `.last_indexed_commit` |
| Vector incremental **state** | ✅ built | `IncrementalState.load()` auto-resolves `last_commit`; falls back to full build |
| Graph patcher | ✅ built, 5 languages | `PatcherBase` (1293 lines) + per-language subclasses; measured 13–24x |
| Graph patcher **wired to compiler** | ❌ | `SymbolGraphBuilder.incremental_update` → `return self.build()` |
| Vector incremental **called by demo** | ❌ | demo only ever calls `build()` |
| Exactness guard | ❌ **not in this repo** | searched `codenib/`, `test/`, `scripts/` — lives in the eval artifact bundle |
| BM25 / Zoekt incremental | ❌ | both `return self.build()` |

**The one thing currently exercising the graph patcher in product code is
`scripts/build_commit_window.py`.** Everything else routes to a full rebuild.

---

## Decisions

| Decision | Choice | Reasoning |
|---|---|---|
| **Guard source** | Ask Zhongming for the eval implementation | The demo's guard should *be* the paper's guard. A re-implementation could admit at a different rate than published, which is worse than waiting. |
| **Worktree policy** | Compiler manages a disposable `git worktree` | Proven in `build_commit_window.py`. The patcher reads bodies off disk (`lsp_client.py:371`), so a checkout is required — and the served tree must never move under live requests. |
| **Ask scope** | Make Ask commit-aware too | Per-commit BM25 + vector is ~3s/commit (BM25 0.81s + vector incremental ~2.3s). Cheap, and it makes the commit selector change *answers*, not just the graph. |
| **BM25 incremental** | **Out of scope** | `langchain BM25Retriever` (rank_bm25) has no add/remove, and BM25 scoring is corpus-global. Median build is **0.81s** — engineering around an immutable backend to save <1s is not justified. Say so explicitly rather than leaving it looking unfinished. |

---

## The dependency problem, and how we break it

`C` (guard) is blocked on Zhongming. `A` (compiler wiring) *should* be gated by
the guard — shipping "we patch instead of rebuilding" without verification is
exactly the claim the paper refuses to make.

**Resolution:** define the guard as an interface now and ship `A` behind it.

```python
class UpdateVerifier(Protocol):
    def verify(self, patched: CodeGraph, fresh: CodeGraph) -> VerificationResult: ...

class NullVerifier:      # ships now: admits nothing, always reports "unverified"
class ExactnessVerifier: # drops in when Zhongming's code lands
```

Policy on the compiler: `incremental_update` runs the patch, asks the verifier,
and **falls back to a full rebuild when verification fails or is unavailable**
unless explicitly told otherwise. So the default is always correct; the fast
path is opt-in and provable. `A` proceeds today; `C` upgrades it without
re-architecting.

---

## Work items

### B — vector incremental in the demo (unblocked, smallest, do first)
The implementation already exists (`index_builders.py:255`) and manages its own
`IncrementalState`. Work is calling it: have the web/compile path pass
`last_commit` and choose `incremental_update` when the manifest's
`last_indexed_commit` differs from HEAD. Lights up the 25.44x result.

### A — graph patcher into the compiler (unblocked)
1. `SymbolGraphBuilder.incremental_update` → resolve `last_indexed_commit`,
   prepare a disposable worktree at the target commit, run `GraphPatcher` per
   language, persist.
2. Reuse the failure isolation already proven in `build_commit_window.py`: a
   language that cannot cold-build or whose LSP will not start is dropped, not
   fatal.
3. Route the result through the verifier (above).
4. Requires the LSP toolchain on the serving host — see *Prerequisites*.

### C — exactness guard (blocked on Zhongming)
Port the eval implementation. Paper definition (Sec. 4.2) for reference:
`F(G)` = tagged multiset of vertex identities, types, source/selection lines,
and typed edges with source anchors; **plus** a serving replay of deterministic
definition/reference requests after persistence and reload. Both must be exact.

Note when porting: vertex identity is `unified_name`, **not** the igraph vertex
key. The patcher writes keys as `{unified_name}:{start_line}` to keep keys
unique while the cold build does not — comparing raw keys produces false
differences (verified: 251 spurious vs 54 real).

### D — Zoekt incremental (unblocked, small)
`zoekt-git-index` maintains incremental shards natively. Likely wiring, not
algorithm work. Confirm before estimating.

### E — BM25 incremental — **dropped**, see Decisions.

### Ask commit-aware (depends on B)
Per-commit BM25 + vector indexes keyed by commit, mirroring how
`commit_window.py` serves per-commit graphs. Extend the window manifest to
record index paths per commit; thread `commit` through `/api/chat`.

---

## Prerequisites

Serving host needs the language servers, because patching requires live LSP:

```bash
make python-lsp-tool CODENIB_SCIP_TOOLS_DIR=$HOME/.codenib-tools   # basedpyright
make typescript-lsp-tool gopls-tool scip-go-tool CODENIB_SCIP_TOOLS_DIR=...
```

`CODENIB_SCIP_TOOLS_DIR` defaults to `${CODENIB_TEMP_DIR}/scip-tools` — **ephemeral
and world-writable on a shared box**. Always override it to a persistent path.

---

## Sequence

1. **Send the ask to Zhongming** (unblocks C; everything else proceeds meanwhile).
2. **B** — vector incremental in the demo. Smallest real win.
3. **Verifier interface + `NullVerifier`** — the seam C plugs into.
4. **A** — compiler wiring behind the verifier, rebuild-on-unverified default.
5. **Ask commit-aware** on top of B.
6. **C** when the eval code arrives; swap `NullVerifier` → `ExactnessVerifier`.
7. **D** if time allows.

---

## Risks

| Risk | Mitigation |
|---|---|
| C never arrives before launch | `NullVerifier` ships; default stays full-rebuild. We claim speed only where verified — never an unverified claim. |
| A destabilizes the compile path days before launch | Incremental is opt-in; `build()` remains the default path. Every change lands behind the manifest's commit comparison. |
| LSP missing on the serving host | Patch attempt fails soft → full rebuild. Same isolation already proven in the window builder. |
| Per-commit indexes blow up storage | Cap the window (currently 5). Graph ~2.9 MB/commit; measure vector before committing to a number. |
| Rust/TS patches fail the guard (paper: 0/9) | Expected. Those fall back to rebuild — correct behaviour, not a bug. |

---

## Explicitly not claimed

- BM25 and Zoekt are not incremental (BM25 deliberately).
- Without C, no correctness claim about patched graphs — only measured latency.
- The paper's guard admits Go/Python but **not** Rust/TS; the product will
  inherit that, and should surface it rather than hide it.
