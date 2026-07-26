# Guardian Sandbox Runtime — Blueprint v3

**Scope.** The execution substrate in which a Guardian cycle runs. Two hard
requirements, from the session brief:

1. **Isolation.** The sandbox must not be able to harm the host or the repository.
2. **CodeNib reuse.** It must consume CodeNib's compiled infrastructure *without
   rebuilding it every time.*

Audited against `CodeMiner @ feat/repository-guardian-skeleton`, head `b3f5d40`.
Supersedes §4.3 of `prototype_design.md`, which named `--sandbox container` as the
required default but did not specify the mount topology, the probe-level boundary,
or the cache-reuse contract. §1–§3 of `codenib_for_guardian.md` still stand and are
the source of every cost constant used here.

### Changes from v1

- **§0 is new** — defines *view* and *manifest*, terms v1 used without introducing.
- **§1.1 is new** — names the three sandbox consumers. v1 implicitly assumed the
  investigator was the only one; it is not. Perception executes repo code today
  *outside* the sandbox seam, which is a wider hole than anything v1 listed
  (now defect 8).
- **§3.2 rewritten** — v1's "read-only repo" was imprecise to the point of being
  wrong. The repo the investigator sees is *writable*; the immutability lives in the
  overlay's lower layer.
- **§4.2(a) corrected** — v1 implied CodeNib does not cache. It does: a full
  incremental subsystem exists and works. The actual defect is narrower and better
  news: `IndexCompiler.compile_repo`, the entry point Guardian uses, never dispatches
  to it.
- **§2 rewritten against real source** — v1's mini-swe-agent section was written from
  documentation and asserted the shape of an interface it had not read. §2 now cites
  `mini-swe-agent` v2.4.6 by file and line, corrects one wrong claim (their `execute`
  takes a shell *string*, not argv), and surfaces defect 9 — a host-safety bug in our
  timeout handling that their `local.py` already solves.

## 0 · Terms

**View** — one compiled representation of the repository at one commit. CodeNib
compiles three:

| View | Representation | Retrieval route |
|---|---|---|
| `bm25` | lexical inverted index | A (lexical) |
| `vector` | dense embeddings over code chunks | B (semantic) |
| `symbol_graph` | `graph.pkl` — symbols and edges | D (structural) |

**Commit manifest** `M_c = ⟨c, V^lex_c, V^dense_c, G_c, K_c⟩` — the commit SHA, the
three views, and the derived capability set, serialized as `repo_manifest.json`. The
manifest is a **lookup boundary**: it records where each view lives and whether it
is `fresh`. The payloads live in sibling directories, not inside it.

**`views/<repo>/<commit>/`** in this blueprint therefore means: one directory per
commit holding that commit's manifest plus its three view payloads. Median footprint
~160 MiB per commit — which is why one copy shared across arms and seeds matters.

**Prebuilt views** — those artifacts already materialized on the host before any
container starts.

---

## 1 · Design principle: one write boundary

The whole design reduces to a single invariant:

> **Nothing durable is writable from inside the sandbox.** The container receives an
> *ephemerally* writable repository (an overlay whose base cannot change), read-only
> views, read-only memory, and exactly one writable channel that survives
> (`/out`, a tmpfs). Everything durable — repository memory, the view cache, the git
> mirror — is written by the *host*, after the container has exited and its
> filesystem is gone.

The distinction between *writable* and *durable* is what makes this work: the
investigator needs to write (§3.2), and nothing it writes outlives the container.

This is stronger than the current `-v {wt}:/repo:ro` mount, and it is what makes
the non-modifying invariant *structural* rather than a property we assert in prose.
It also has an evaluation payoff: a cycle that cannot write anything durable except
a validated `report.json` is a cycle whose replay is reproducible by construction.

Three planes, one boundary:

| Plane | Owner | Mutability | Contents |
|---|---|---|---|
| **Host** | trusted; no model-authored code executes here | read-write | git mirror, worktree provisioner, view builder, cycle persister |
| **View** | host-written, commit-addressed | immutable once built | `views/<repo>/<commit>/` (manifest + bm25 + vector + symbol_graph), toolchain cache |
| **Ephemeral** | the cycle container | discarded at exit | `/repo` overlay upper layer, `/out` tmpfs, all probe side-effects |

### 1.1 Who uses the sandbox

The sandbox is **one container per cycle**, with three consumers that need different
things from it. Getting this wrong is how the current code ended up with a hole:

| # | Consumer | Executes | Needs from the sandbox |
|---|---|---|---|
| 1 | **Perception** (`signals/`) | the repo's *existing* pytest suite; `git log` | container isolation — it runs third-party repo code, including an arbitrary `conftest.py` |
| 2 | **Orchestrator** | nothing from the repo — LLM calls, ranking, fixed-argv git reads | no sandbox of its own; needs *not to be killable* by consumer 3 |
| 3 | **Investigator** (per hypothesis) | **model-authored** tests and patches | container **+ overlay + `reset()` + per-probe isolation.** The only writer. |

Three consequences:

- **The investigator is the only writer.** Overlay semantics and `reset()` exist for
  it alone. Perception and the orchestrator read.
- **The orchestrator's requirement is negative.** It needs a guarantee that a
  runaway probe cannot take down the cycle — which is the entire argument for the
  per-probe boundary in §5. Without it, one fork-bombing synthesized test loses every
  other hypothesis in the cycle and the report.
- **Perception currently bypasses the seam entirely** (defect 8). `cycle.py:415`
  calls `run_test_suite(repo_path)`, which at `signals/tests.py:73` runs
  `["python","-m","pytest","-q","-ra","-m",marker]` via bare
  `subprocess.run(cwd=repo_path)` — no `SandboxHandle`, no isolation, only a
  `timeout`. Compare the investigator's `run_existing_test` (`probes.py:152`), which
  takes a `SandboxHandle` and issues the *same* pytest invocation through it. Same
  command, one sandboxed and one not.

  This is arguably a wider hole than any in v1's table: with `run_tests=True`, an
  arbitrary monitored repository's test suite — `conftest.py` included — executes
  with the host user's privileges *before a single hypothesis is framed.* The fix is
  small: `run_test_suite` should take a `SandboxHandle` rather than a `repo_path`
  string.

Benign by contrast, and deliberately left as bare subprocess: `cycle.py:61`
(`git rev-parse HEAD`) and `orchestrator/runner.py:93` (`git log --oneline`) are
fixed-argv git reads with no model-controlled input. `signals/churn.py:80`
(`git log --since=… --name-only`) is the same. No effort should go into routing these.

---

## 2 · Prior art: mini-swe-agent's environment layer

**Source audited.** `mini-swe-agent` v2.4.6 (PyPI sdist, MIT, Lieret & Jimenez et al.),
`src/minisweagent/`. Its authors direct citation to the SWE-agent paper:

```bibtex
@inproceedings{yang2024sweagent,
  title={{SWE}-agent: Agent-Computer Interfaces Enable Automated Software Engineering},
  author={John Yang and Carlos E Jimenez and Alexander Wettig and Kilian Lieret and
          Shunyu Yao and Karthik R Narasimhan and Ofir Press},
  booktitle={The Thirty-eighth Annual Conference on Neural Information Processing Systems},
  year={2024}, url={https://arxiv.org/abs/2405.15793}
}
```

This section cites file and line so every claim below is checkable. **The specific
design lineage is the agent–computer interface (ACI) argument of that paper: the
interface through which an agent acts on a repository is a first-class design object,
and constraining it improves both reliability and analysability.** Guardian's
`SandboxHandle` is an ACI. mini-swe-agent is the reference implementation showing how
narrow that interface can be while still supporting five isolation technologies.

### 2.1 The whole environment contract is three methods

`minisweagent/__init__.py:61-70`:

```python
class Environment(Protocol):
    config: Any
    def execute(self, action: dict, cwd: str = "") -> dict[str, Any]: ...
    def get_template_vars(self, **kwargs) -> dict[str, Any]: ...
    def serialize(self) -> dict: ...
```

One execution primitive. Five interchangeable backends satisfy it —
`_ENVIRONMENT_MAPPING` at `environments/__init__.py:8` registers `local`,
`docker`, `singularity`, `bubblewrap`, `contree`, plus two `swerex` variants — resolved
by **string** through `get_environment_class` (`:19`) and constructed from a config dict
by `get_environment` (`:30`). The agent loop touches exactly one line:
`agents/default.py:156`, `outputs = [self.env.execute(action) for action in ...]`.

The evidence for the abstraction holding: the five backends isolate by entirely
different mechanisms — `subprocess` in a process group (`local.py:84`), a long-lived
container plus `exec` (`docker.py:107`), a `--writable` unpacked image
(`singularity.py:95`), and unprivileged user namespaces (`bubblewrap.py:86`) — and none
of them changes that line.

### 2.2 Guardian already has this seam; it should not invent a second one

`investigator/sandbox.py` (74 lines):

```python
@runtime_checkable
class SandboxHandle(Protocol):
    repo_path: str
    def run_command(self, cmd: List[str], *, timeout: int = 60) -> Tuple[int, str]: ...
    def write_file(self, rel_path: str, content: str) -> None: ...
    def read_file(self, rel_path: str) -> str: ...
```

Same shape, one execution primitive plus two file operations. So the work is **not**
designing an abstraction — it is supplying the second implementation and deleting the
hardcodes that bypass it (`cycle.py:566`, `cycle.py:657`).

Three structural choices to copy directly, each earned in their code:

| Borrowed | Their implementation | Guardian |
|---|---|---|
| **Backend selected by string, resolved through a registry** | `_ENVIRONMENT_MAPPING` + `get_environment_class` (`environments/__init__.py:8,19`) | `--sandbox {worktree,container}` already exists as a string; route it through a factory, not an `if` |
| **Container started once, commands `exec`'d into it** | `_start_container` runs `docker run -d … sleep 2h` (`docker.py:74-88`); every `execute` is `docker exec -w cwd` (`:107`) | pay container start once per cycle, not once per probe — this is what makes per-probe isolation affordable |
| **Environment config is serialized into the run record** | `serialize()` emits `environment_type` and the full config (`docker.py:64-72`) | fold into §3's determinism envelope: the report should name the exact backend and flags that produced it |

### 2.3 Two deliberate divergences

**(a) List-of-argv, not a shell string.** Their `execute` takes a command *string* and
interpolates it into an interpreter — `interpreter: ["bash", "-lc"]` (`docker.py:38`),
appended at `:113`. Reasonable for them: the *model* writes bash and the shell is the
action space. Guardian's probes issue *fixed* commands with parameterised arguments
(`["python","-m","pytest",pattern]`), so `List[str]` removes shell injection as a
category. Keep it.

**(b) Per-probe re-isolation.** Their unit of isolation is the *agent*: one environment
per instance, and every command shares it. `get_sb_environment`
(`run/benchmarks/swebench.py:78-93`) builds one per SWE-bench instance from a
per-instance image and hands it to the agent for the whole rollout. That is right for
their threat model — one task, one container, discard.

Guardian's cycle has **multiple hypotheses inside one container**, so a per-agent
boundary is insufficient in two ways their design never has to face: probe writes leak
between hypotheses (defect 5, hence `reset()`), and a runaway synthesized test kills
the orchestrator and every remaining hypothesis (§5). Their §2.2 pattern — one
container, many `exec` calls — is precisely what makes this cheap: the per-probe
boundary is a flag set on `exec`, not another container start.

### 2.4 A bug their code fixes that ours has

`local.py:78-92` uses `Popen(..., start_new_session=True)` and on timeout calls
`os.killpg(process.pid, signal.SIGKILL)` before re-raising. The docstring states the
reason plainly: kill the whole process group so no children are orphaned.

Guardian's `WorktreeSandbox.run_command` uses `subprocess.run(..., timeout=timeout)`
and on `TimeoutExpired` returns `(1, "(command timed out after 60s)")`. `subprocess.run`
kills the direct child only. A synthesized test that spawns a subprocess and hangs
therefore leaves **orphaned grandchildren running on the host** — and the probe
reports a clean timeout. This is a live defect in the debug path we use today, and it
is the same class of bug the container `--rm` would mask rather than fix.

*Adopt their fix verbatim in `WorktreeSandbox`; for `ContainerSandbox`, `--pids-limit`
plus container teardown covers it, but the process-group kill is still correct inside
the container.*

### 2.5 What we deliberately do not take

- **Their `local` backend as a supported mode.** It exists so `mini` can run against
  your own machine. Guardian's equivalent (`--sandbox worktree`) must stay
  debug-only, because our threat model includes an *arbitrary monitored repository*,
  not a benchmark instance with a curated image.
- **Bubblewrap/Singularity as the primary backend.** `bubblewrap.py:38-64` is a
  hand-written bind-mount list and the module marks itself experimental;
  `singularity.py:33` uses `--fakeroot`. Rootless Podman gives us overlay semantics
  (§3.2) with less bespoke configuration. Their existence is still the argument for
  keeping the seam narrow — if Podman's overlay support fails on the target host
  (risk 1), Bubblewrap becomes a new class rather than a redesign.
- **Their timeout defaults.** `timeout: int = 30` per command (`docker.py:32`) suits
  short bash actions; a repo's pytest suite needs the longer per-probe budgets in §5.

---

## 3 · Architecture

![Guardian sandbox runtime: three planes and the write boundary]({{artifact:art_3cac166b-f99f-4a2a-bf2e-5769ebe52010}})

### 3.1 Mount table (the normative contract)

| Mount | Mode | Backing | Rationale |
|---|---|---|---|
| `/repo` | **overlay**: immutable lower + tmpfs upper | `git worktree add --detach <commit>` | investigator must write (`_guardian_synth_test.py`, `_guardian_fix.patch`, and tracked files via `patch`); the checkout must not change |
| `/repo_prior` | overlay, no upper needed | `git worktree add --detach <commit>~1` | `differential_run` executes a test at the prior commit (§3.4) |
| `/views` | `:ro` | `views/<repo>/<commit>/` | prebuilt `M_c` for this commit |
| `/toolchain` | `:ro` | `HF_HOME`, scip binaries, pip wheelhouse | no network install at runtime |
| `/memory` | `:ro` | per-cycle `VACUUM INTO` snapshot (empty file when `--arm memoryless`) | memory is read in-cycle, written post-exit |
| `/out` | `:rw` | tmpfs, harvested by host | the only channel out |

Container flags: `--rm --network none --read-only` (rootfs), `--cap-drop=ALL`,
`--security-opt no-new-privileges`, `--pids-limit`, `--memory`, `--cpus`, plus a
wall-clock timeout enforced by the host, not the container.

**Runtime: rootless Podman, with Docker as fallback.** `prototype_design.md` already
prefers Podman; all current code and tests hardcode `docker`. Resolve by selecting
the binary once (`shutil.which("podman") or shutil.which("docker")`) and keeping the
argv identical — the flags used here are common to both. Rootless matters: a
container escape lands as an unprivileged user, not root, on a host that holds the
git mirror.

### 3.2 "Read-only repo" is the wrong description — it is a writable overlay

A plain `:ro` repo mount is **not** viable, and saying "read-only repo" invites the
correct objection that the investigator has to write. Both statements below are true
simultaneously, and the overlay is what makes them compatible.

The investigator writes into the repository. Concretely:

- `run_synthesized_test` → `sandbox.write_file("_guardian_synth_test.py", …)`
- `fix_probe` → writes `_guardian_fix.patch`, then applies it with
  `patch -p1 --forward --batch -i`, which **modifies tracked source files**

Today's `-v {wt}:/repo:ro` makes both fail. Removing `:ro` makes both mutate the
checkout, destroying the non-modifying invariant. The resolution is that read-only
applies to the **lower layer**, not to what the process sees:

```
/repo  =  overlay
    lower  =  worktree @ commit     immutable, never written
    upper  =  tmpfs                 writable, discarded on --rm
```

The investigator sees a fully writable `/repo` and needs no special-casing. Writes
land in the upper layer; overlayfs **copies up on write**, so `fix_probe` may patch
tracked files freely while the lower layer stays byte-identical. On `--rm` the upper
layer evaporates and the worktree is provably unchanged.

So the accurate phrasing is **"a writable view over an immutable base"** — not a
read-only repo. The overlay is therefore load-bearing: it is the only configuration
in which the probe surface works *and* the repository is safe. It is not a
performance choice and cannot be dropped for simplicity.

### 3.3 Per-hypothesis reset

`fix_probe` applies a real diff to the tree, and **nothing reverts it.** A grep for
`checkout|git clean|reset|revert|restore` across `codeminer/guardian/investigator/`
returns only prose in docstrings. So hypothesis *k+1* inspects a tree still carrying
hypothesis *k*'s patch and stale synthesized test — a silent evidence-contamination
bug that gets worse the more probes we add.

Fix at the sandbox layer, not in probe code: give `SandboxHandle` a `reset()` that
discards the overlay upper layer, and call it between hypotheses. For
`WorktreeSandbox`, `reset()` is `git checkout -- . && git clean -fdq`; for the
container sandbox it is remounting a fresh upper. Add `snapshot()`/`restore()` only
if a probe later needs mid-investigation rollback.

### 3.4 `differential_run` needs a second sandbox

`probes.py:377` takes `sandbox_current` **and** `sandbox_prior`. Nothing in
`codeminer/guardian/` ever constructs the prior one, so the probe is unreachable —
which matters because the corroboration policy names it as one of only two
acceptable corroborations (`fix_probe` FAIL→PASS or `differential_run` PASS→FAIL).
The runtime should provision **two** worktrees per cycle — `<commit>` and
`<commit>~1` — sharing one container, mounted `/repo` and `/repo_prior`. The prior
side needs no overlay: `differential_run` only executes a test there.

---

## 4 · CodeNib reuse: build on the host, load in the container

### 4.1 The contract

- **Views are built outside every cycle, on the host.** `IndexCompiler.compile_repo`
  at the base commit; `GraphPatcher.patch_files` + vector `delta_update` for each
  subsequent commit.
- **Views are commit-addressed and immutable:** `views/<repo>/<commit>/`. One build
  serves every arm × seed × cycle that replays that commit.
- **Inside the container, `refresh` is load-only.** A missing required view is a
  hard abort, never a build. This is exactly CodeNib's own stance — the commit
  manifest is a lookup boundary and the system never builds an index online — so
  Guardian inherits it rather than inventing a policy.

### 4.2 Two blockers in the current code

Mounting a host-built cache read-only does **not** work today. Two concrete defects:

**(a) `compile_repo` never dispatches to the incremental path that already exists.**

State this precisely, because the surrounding claim is easy to overstate. **CodeNib
does cache, and its incremental update machinery is real and working:**

- `codeminer/graph/incremental/` holds `GraphPatcher` plus per-language patchers
  (`patcher_python.py`, `patcher_go.py`, `patcher_rust.py`, `patcher_ts.py`,
  `patcher_cpp.py`); `GraphPatcher.patch_files` is at `graph_patcher.py:107`.
- `CodeVectorStore.delta_update` exists at `vector_store.py:1210`.
- Every builder implements `incremental_update(scope, **kwargs)`
  (`index_builders.py:102, 243, 463, 511`).
- `VectorIndexBuilder.incremental_update` loads persisted `IncrementalState`,
  `IncrementalChunkStore`, and `EmbeddingsCache` from `output_dir`, auto-resolves
  `last_commit`, runs the update pipeline, and saves state back — with an explicit
  fallback to full `build()` when the state files are absent.
- `graph.pkl` round-trips through `CodeGraph.save_graph` / `load_graph` with an
  on-disk schema-version check.

That subsystem is what produces the 8.67× and 25.44× speedups in §4.3.

**The defect is narrower: `IndexCompiler.compile_repo` — the entry point Guardian
calls — never uses it.** Its loop calls `self._build_one(builder, …)` →
`builder.build(...)` for every type in `types_to_build`, unconditionally. It never
reads an existing `repo_manifest.json`, never consults `last_indexed_commit` (a field
it *writes*), and never dispatches to `incremental_update`. So the incremental path is
reachable from other callers (`scripts/index_repo.py` among them) but not from
Guardian's. **Guardian always full-builds because of which method it calls, not
because the capability is missing.**

This is much better news than "we must build incremental indexing": the work is
wiring an existing, tested capability into one caller.

*Fix:* a reuse/repair path in `compile_repo` that (i) loads `repo_manifest.json` if
present, (ii) returns it untouched when `repo.commit` matches the requested commit
and every required `IndexEntry` is `fresh`, and (iii) otherwise dispatches
`incremental_update(last_commit=manifest.last_indexed_commit)` rather than `build()`,
falling back to `build()` only when the builder reports incremental state is missing.
Guardian's loaders (`_load_bm25_from_manifest`, `_load_current_graph`) already consume
manifest entries, so nothing downstream changes.

**(b) The manifest stores unrebased absolute paths.** `IndexEntry.path` is written
as `os.path.join(cache, idx_type)` where `cache` derives from
`os.path.abspath(repo_path)`. `RepoManifest.from_dict` restores it verbatim — no
rebasing. A cache built at `/host/views/<sha>/` and mounted at `/views` therefore
carries paths pointing at a directory that does not exist in the container.

This defect is independent of (a) and bites *any* relocation, not just containers —
copying a cache between machines, or into a shared read-only store, breaks the same
way.

*Fix:* make artifact paths **relative to the manifest's own directory** and resolve
against it at load time. This is a portability fix worth making in CodeNib proper,
not a Guardian workaround — a commit manifest that cannot be relocated cannot be
cached, shipped, or shared between processes, which is a limitation of the view
abstraction rather than of Guardian.

Note `ManifestIndexStateStore` is already read-only and raises on `set_status`, so
the read path is consistent with a read-only mount once (a) and (b) land.

### 4.3 What this buys

![Setup cost by design: total ablation wall-clock, and why incremental repair works]({{artifact:art_a1251fd7-1980-410a-89a5-83ccb0a057ba}})

Using CodeNib's measured medians (full materialization **116.7 s**; view load into a
fresh process **7.40 s**; admitted graph repair **8.67×**, admitted vector delta
**25.44×**), for one ablation at 30 commits × 2 arms × 3 seeds:

| Design | Setup wall-clock | vs today |
|---|---|---|
| **A** build inside each cycle container *(today)* | **6.21 h** | 1× |
| **B** host build, full rebuild per commit, cache shared by all 6 runs | 1.34 h | 4.6× |
| **C** host build + `incremental_update`, shared read-only cache | **0.46 h** | **13.4×** |
| **D** C + resident view server on the host | 0.11 h | 58.9× |

Per commit, incremental repair costs **7.5 s** against **98.3 s** for a full build of
the same three views — **15.5×**. Two caveats, both inherited: these are CodeNib's
hardware- and cache-specific medians, not a portable benchmark; and the repair
speedups hold only on *admitted* transitions (graph 15/33, vector 28/31 — Python and
Go pass, Rust and TS/JS fail). The Python-only pilot sits inside the admitted set,
which is a citation rather than a shrug when a reviewer asks why.

**Recommendation: build C, defer D.** C is a pure win — it removes 5.75 h of
indexing from every ablation, and given §4.2(a) it requires wiring an existing
subsystem into one caller plus a path-rebasing fix, not new indexing machinery. D's remaining
gain is the `7.40 s` load term, and buying it costs a long-lived host process
holding view state, a query protocol across the boundary, and a new failure mode
(stale resident state serving a commit it wasn't built for) — for ~20 min per
ablation. Revisit if cycle counts grow an order of magnitude.

This also settles the `symbol_graph` question from the re-audit: enabling it (it is
currently off by default in all four drivers, leaving the drift signal inert) adds
**38 s per commit** under A but **4.4 s** under C. C is what makes the structural
view affordable enough to turn on.

---

## 5 · Probe execution boundary

A synthesized test is model-authored code. It runs inside a container that is
already untrusted, so the container boundary alone gives the *host* protection but
gives the *cycle* none — a runaway test can exhaust the container's CPU, fill its
tmpfs, or fork-bomb the cycle, taking down the orchestrator and every other
hypothesis with it. This is consumer 2's negative requirement from §1.1: the
orchestrator's only ask of the sandbox is that consumer 3 cannot kill it.

Each probe therefore executes under nested unprivileged isolation inside the
container: `uid nobody`, `no-new-privileges`, seccomp default, its own
`pids`/`cpu`/`memory` cap, and its own timeout (already parameterised —
`timeout=60` for tests, `30` for `patch`). Failure of a probe must be a *typed
observation* handed back to the investigator, never an exception that aborts the
cycle. `WorktreeSandbox.run_command` already models this correctly, returning
`(1, "(command timed out after …s)")` rather than raising; the container
implementation must preserve that contract exactly.

### Egress

Default `--network none`. The one exception is the model endpoint: with `--use-llm`,
add a single route to the host LLM proxy — a mounted unix socket preferred over a
`127.0.0.1:<port>` allow-rule, because a socket needs no network namespace at all.
No DNS, no general network, and in particular **no PyPI or HuggingFace at runtime** —
that is what `/toolchain` is for.

---

## 6 · Defects to fix (ordered by whether they block M1)

| # | Defect | Evidence | Blocks M1 |
|---|---|---|---|
| 1 | Container path has **never run**: `provision_container` invokes `--repo/--out/--arm/--index-types/--since`; the entrypoint `scripts/guardian_cycle.py` takes a *positional* `repo_dir` and defines none of those flags | `guardian_replay.py:83`; 13 `add_argument` calls, first is positional | **yes** |
| 2 | `cycle.py` hardcodes `WorktreeSandbox` at **566** and **657**, so even `--sandbox container` runs probes against the worktree | `grep WorktreeSandbox cycle.py` | **yes** |
| 3 | `compile_repo` never calls the `incremental_update` that exists, and manifest paths are unrebased ⇒ read-only cache mount cannot work | §4.2 | **yes** |
| 4 | `/repo:ro` vs the investigator writing into `repo_path` ⇒ needs a real overlay | §3.2 | **yes** |
| 5 | No overlay reset between hypotheses ⇒ probe state leaks across investigations | §3.3 | no (correctness of evidence — fix early) |
| 6 | `differential_run` unreachable: nothing constructs `sandbox_prior` | `probes.py:377` | no |
| 7 | Image installs only `git` — no SCIP toolchain, no HF cache; `setup-scip.sh` hard-requires `x86_64` | `Dockerfile` (10 lines); `setup-scip.sh` | no (Python-only pilot needs no scip-clang) |
| **8** | **Perception executes repo code outside the sandbox seam**: `cycle.py:415` → `run_test_suite(repo_path)` → bare `subprocess.run` at `signals/tests.py:73`. An arbitrary repo's `conftest.py` runs with host privileges when `run_tests=True` | §1.1 | no, but it is the **widest isolation hole in the tree** — fix with 2 |
| **9** | **`WorktreeSandbox` timeout orphans grandchildren.** `subprocess.run(timeout=…)` kills the direct child only; a hanging test that spawned a subprocess leaves it running on the host while the probe reports a clean timeout. mini-swe-agent's `local.py:84,89` solves this with `start_new_session=True` + `os.killpg` | §2.4 | no, but it is a **host-safety bug in the path we run today** — 2-line fix |

Defect 1 deserves emphasis: the only test that would have caught it is
`@pytest.mark.integration test_container_sandbox_runs_one_cycle`, which skips when
`shutil.which("docker")` is falsy. The other three container tests mock
`subprocess.run` and assert on the argv Guardian *constructs* — they verify the
mounts and flags, and cannot observe that the inner command is unparseable. **The
acceptance test for this blueprint must run a real container in CI.**

---

## 7 · Implementation plan

**Step 0 — process-group kill (defect 9).** Two lines in `WorktreeSandbox`: switch to
`Popen(..., start_new_session=True)` and on `TimeoutExpired` call
`os.killpg(p.pid, signal.SIGKILL)` before returning the timeout observation, exactly as
`mini-swe-agent`'s `local.py:84,89`. Do this first — it is independent of everything
else and closes a host-safety bug in the path we run today.
*Test:* run a probe that spawns a `sleep 600` grandchild and hangs; assert the
grandchild's pid is gone after the timeout observation returns.

**Step 1 — entrypoint contract.** Make `scripts/guardian_cycle.py` accept the flags
`provision_container` actually passes (`--repo`, `--out`, `--arm`, `--index-types`,
`--since`, `--sandbox`, `--views`, `--memory`), keeping the positional form as a
deprecated alias. Have the container branch of `guardian_replay.py` construct the
same `GuardianConfig` fields the worktree branch does (`index_cache_dir`,
`episode_dir`, `use_llm`, `llm_model`, `graph_snapshot`) — today it passes none.
*Test:* argv round-trip — the exact list `provision_container` builds parses without
error, asserted directly against the parser.

**Step 2 — `ContainerSandbox`, and route perception through it.** New class in
`investigator/sandbox.py` implementing `SandboxHandle` by `podman exec`/`docker exec`
into the running cycle container. Follow mini-swe-agent's lifecycle
(`docker.py:74-88,107`): start the container **once** per cycle detached, then `exec`
each probe into it — this is what makes the per-probe boundary in §5 affordable.
Select the backend through a registry keyed by the `--sandbox` string
(`environments/__init__.py:8,19`), not an `if`, so a third backend is a new class.
Add `reset()` to the protocol; implement for both backends. Replace both hardcodes in
`cycle.py` with that factory. In the same step close defect 8: change `run_test_suite`
to take a `SandboxHandle` instead of a `repo_path` string, and update the
`cycle.py:415` call site — it then issues the same pytest invocation the investigator's
`run_existing_test` already does, through the same seam.
*Test:* protocol conformance — `isinstance(ContainerSandbox(...), SandboxHandle)`,
plus a parametrised suite running the identical probe sequence against both
implementations and asserting identical `(rc, output)` shapes and identical
verdicts. Plus a guard test: grep `codeminer/guardian/` for `subprocess.run` and
assert the only hits are the fixed-argv git reads listed in §1.1 — so a future
bare-subprocess execution path fails CI rather than silently reopening defect 8.

**Step 3 — overlay + reset.** Provision `/repo` as ro-lower + tmpfs-upper; call
`reset()` between hypotheses.
*Test:* write a file via a probe, `reset()`, assert it is gone; run the full cycle,
then assert `git -C <worktree> status --porcelain` is empty **and** the mirror HEAD
is unchanged (extend the existing `_assert_mirror_unchanged`).

**Step 4 — view reuse.** Land the reuse/repair dispatch and relative-path rebasing in
`codeminer/compiler/`. Add a host-side `build_views(repo, commit)` that full-compiles
at the base commit and then calls `incremental_update` forward, recording per commit
whether repair was admitted or fell back to a full build.
*Test:* three assertions. (i) Build views at commit *c* on the host, mount read-only,
run a cycle, assert **zero** `builder.build` invocations (spy on
`IndexBuilderRegistry`) **and** a non-null retriever — i.e. it neither rebuilt nor
silently degraded to `_NullRetriever`. (ii) Relocation: build a cache at path *P₁*,
move the directory to *P₂*, load the manifest, assert every resolved view path exists.
(iii) Repair dispatch: advance one commit, assert `incremental_update` was called and
`build` was not.

**Step 5 — probe isolation + egress.** Nested unprivileged exec per probe; `--network
none` with the single socket exception.
*Test:* a deliberately hostile synthesized test (fork bomb, `while True`, 10 GB
write, outbound socket) yields a typed failure observation and a surviving cycle.

**Step 6 — image.** Pin by digest; bake `/toolchain`; record the determinism
envelope (image digest, `PYTHONHASHSEED`, `TZ=UTC`, `SOURCE_DATE_EPOCH`, seed,
frozen wheel set) into `report.json`.
*Test:* two runs of the same commit + seed produce byte-identical `report.json`
modulo timestamps.

Steps 1–4 are the M1-blocking set. Steps 1 and 2 are independent of 4 and can land
first; step 4 is the one that touches CodeMiner outside `guardian/`. Defect 8 rides
along with step 2 at near-zero marginal cost, which is the argument for not deferring
it despite being non-blocking.

---

## 8 · Open risks

1. **Overlay in rootless Podman.** Rootless overlayfs needs `fuse-overlayfs` or a
   kernel new enough for unprivileged overlay mounts. Verify on the target host
   early; the fallback is a per-hypothesis worktree copy, which costs disk and IO
   but preserves the invariant.
2. **Relative-path rebasing is a CodeMiner-wide change.** There are at least eight
   manifest consumers outside the compiler — `codeminer/web/config.py`,
   `codeminer/agent/runner.py`, `codeminer/mcp/server.py`,
   `scripts/index_repo.py`, `scripts/build_qa_index.py`,
   `scripts/bench_rerank_latency.py`, and two `examples/` scripts — and some may
   assume absolute paths. Keep a compatibility read path that accepts both forms
   rather than a flag-day migration.
3. **Repair admission is not guaranteed.** Graph repair was admitted on only 15/33
   transitions. `VectorIndexBuilder.incremental_update` already falls back to
   `build()` when incremental state is missing, but "state present yet repair not
   admitted" is a different condition and must be handled explicitly, with the
   decision recorded per commit so a cycle never runs against a silently degraded
   view. Log which commits fell back — that distribution is itself a result.
4. **`memory.sqlite` snapshot semantics.** Mounting a live SQLite file read-only is
   safe only if no host writer holds it mid-transaction. Snapshot to a per-cycle
   copy (`VACUUM INTO`) rather than mounting the live file.
5. **Nested isolation may not be available.** If the container cannot re-isolate
   probes (no user namespaces available inside), fall back to per-probe rlimits +
   a dedicated uid, and state the weaker guarantee explicitly rather than implying
   the stronger one.
6. **Perception's isolation need is not identical to the investigator's.** Consumer 1
   runs *repo-authored* code, consumer 3 runs *model-authored* code. Both belong in
   the container, but only consumer 3 needs overlay + `reset()`. Resist collapsing
   them into one policy: perception writing to an overlay it never resets would
   reintroduce cross-cycle contamination through a different door.
