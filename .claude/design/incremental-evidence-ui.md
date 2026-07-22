# Surfacing incremental maintenance in the web demo

**Status:** approved, not started. **Target:** launch week.
**Owner:** Mihir. **Written:** 2026-07-22.
**Depends on:** PR #332 (`yj/incremental-in-product`) — this branches off its head.

Goal: make the demo *show* the incremental contribution instead of describing it.
PR #332 makes the mechanism reachable and adds a commit selector; this makes the
result legible to someone who has never read the paper.

---

## Why this, and why now

The site currently says "last indexed `<sha>`" in three places. #332 replaces
one of them with a commit selector. The other two — the subsystem header and the
landing-page repo cards — still say nothing about incrementality, and the
landing page is where the first impression forms.

More usefully: the evidence is **already on the wire and being discarded**.
`CommitWindow.summary()` sends `method` (`cold`/`patched`), `build_seconds`,
`changed_files`, `node_count` and `edge_count` per commit, plus the full
`patch_stats` classifier breakdown. `page.tsx` renders `short · date · subject`
and drops the rest. Nothing new needs to be measured, indexed, or served.

---

## Scope

**In:**

| Surface | Today | After |
|---|---|---|
| Landing cards (`web/app/page.tsx`) | `indexed at 9d81e0` | `5 commits · 24× faster to re-index` |
| Rail, under the selector (`web/app/[repoId]/page.tsx`) | — | `patched in 4.00s · 3 files changed · +54 nodes` |

**Out** — these are #332's remaining-work list and stay there: commit-aware Ask,
`hierarchy_graph` snapshot consistency, source-view commit pinning, the
exactness verifier, Zoekt incremental, the subsystem-header label.

---

## Where the numbers come from

**One derivation, not three.** A speedup figure already exists in
`build_commit_window.py`'s closing log line. Adding a second in the API and a
third in the frontend is how a paper ends up with two different numbers on two
different slides.

`CommitWindow.summary()` gains a `stats` block, computed in exactly one function:

```json
"stats": {
  "commit_count": 5,
  "patched_count": 4,
  "cold_seconds": 96.5,
  "mean_patch_seconds": 4.0,
  "speedup": 24.1
}
```

Rules:

- `cold_seconds` — `build_seconds` of the single `method == "cold"` entry.
- `mean_patch_seconds` — arithmetic mean of `build_seconds` over
  `method == "patched"` entries, including zero-cost transitions (a transition
  that touched no indexed source legitimately cost 0.00s; dropping it would
  flatter the mean).
- `speedup` — `cold_seconds / mean_patch_seconds`, or **`null`** when there is
  no cold entry, no patched entries, or the mean is zero. Never `NaN`, never
  `Infinity`. The script can currently emit `float("nan")` here; the API must
  not inherit that.
- The whole `stats` block is omitted when `available` is false.

Both surfaces render this block. Neither computes anything from `commits[]`
except the node delta below, which is per-commit rather than aggregate.

---

## API

`WindowStats` is a new pydantic model in `codeminer/web/schemas.py` mirroring the
`stats` block above. `RepoInfo` (same module) gains one optional field:

```python
incremental: WindowStats | None = None
```

Populated in `app.py`'s `/api/repos` handler from the same per-repo
`CommitWindow` already cached in `app.state.commit_windows`. `_bundle()` is a
registry lookup with no graph loading and the manifest read is lazy and cached,
so this stays a single cheap request — no N+1 across repos.

Deliberately **not** in `repo_registry.py`: that module carries local
uncommitted operator changes (noted in #332), so feature code stays out of it.

`GET /api/repos/{id}/commits` is unchanged apart from the added `stats` block.

---

## Frontend

### Landing cards — `web/app/page.tsx`

`indexed at <sha>` becomes `<commit_count> commits · <speedup>× faster to
re-index` — the window's total commit count, not `patched_count`, since the
count describes what the selector offers.

The ratio is the headline here because card space is tight and the ratio is what
makes someone stop scrolling. Its inputs appear in the rail, one click away, so
the claim is never unbacked — just deferred.

Repos with no window keep the existing string verbatim.

### Rail evidence line — `web/app/[repoId]/page.tsx`

#332's `<select>` is left exactly as it is. A line is added *beneath* it,
reflecting the currently selected commit:

```
VIEWING COMMIT
[ a4f2c1 · 2026-07-19 · fix(compiler): resolve skill index_… ▾ ]
patched in 4.00s · 3 files changed · +54 nodes
```

Rationale for placing it below rather than inside the option labels: native
`<select>` options cannot be styled internally in most browsers and truncate
inconsistently across platforms. A sibling line renders identically everywhere
and has room for real numbers.

Per-commit content:

| Selected commit | Line |
|---|---|
| `method == "cold"` (the anchor) | `cold build · 96.5s` |
| `method == "patched"` | `patched in 4.00s · 3 files changed · +54 nodes` |

**Node delta.** `commits[]` is newest-first, so the predecessor of `commits[i]`
is `commits[i+1]`; delta is `commits[i].node_count - commits[i+1].node_count`,
rendered `+54` / `−12` / `±0`. The anchor has no predecessor and shows no delta.
Omit the delta segment entirely when either `node_count` is absent.

### Types — `web/lib/api.ts`

`WindowStats` added; `CommitWindowResponse` gains `stats?: WindowStats`;
`RepoInfo` gains `incremental?: WindowStats | null`.

---

## Degradation

| Condition | Behaviour |
|---|---|
| No window on disk (`available: false`) | Both surfaces keep today's text. Same check #332 established. |
| Window present, stats underivable | Selector and evidence line render; card keeps today's text; `stats` omitted. |
| Single-commit window (anchor only) | Selector renders one entry, `cold build · Xs`, no card stat. |
| Snapshot unloadable | Handled by #332's `/codemap` fix — see ordering below. |

No surface may render `NaN`, `Infinity`, or `undefined×`.

---

## The honesty constraint

Every string says **measured**, never **verified**. `patched in 4.00s` is
defensible today. Anything implying the patch was checked against a fresh
rebuild is not — until the exactness guard lands, every incremental result is
recorded `verified=false, checked=false`, and the UI must not out-claim the
manifest.

This reasoning goes in a comment at the component boundary, so the next person
adding a label inherits it rather than rediscovering it.

---

## Ordering against #332

One real dependency: **#332's `/codemap` mislabel fix must land first.** Today
`graph_for()` returns `None` on an unloadable snapshot while the response still
reports the *requested* sha, and the demo's `graph.pkl` is `schema_version=3`
against an expected `4`, so `CodeGraph.load_graph` raises and that path is live.
Building an evidence UI on top of a view that can misreport which commit it is
showing would make this PR actively misleading rather than merely incomplete.

Everything else here is additive and independent.

---

## Testing

Unit — `test/web/test_commit_window.py` (new file):

- stats over a normal window (1 cold + N patched)
- no cold entry → `speedup: null`
- single-commit window → `speedup: null`
- mean patch seconds of zero → `speedup: null`, no division
- zero-cost transition included in the mean

API — `test/web/`:

- `/api/repos` carries `incremental` for a windowed repo
- `/api/repos` omits it for a windowless repo
- `/api/repos/{id}/commits` carries `stats`

This PR is also where `test/web/` coverage for the **whole** feature lands.
#332 adds no web tests, so the following belong here rather than being left
uncovered: `CommitWindow.resolve()` by short sha and by prefix, missing
manifest, unloadable snapshot, and the `graph_for → None` fallback.

Frontend — `tsc --noEmit` clean; manual check of both surfaces with a window
present and absent.

---

## Not doing

- Rendering `patch_stats` (the symbol-level classifier breakdown). It is the
  most paper-legible artifact available and it is already in the manifest, but
  it is a panel's worth of design and this is launch week. Deliberately held
  for a follow-up rather than shipped half-finished.
- Touching the subsystem-header `Last indexed` label. It sits inside the graph
  modal's header where there is no room for evidence, and changing it buys
  nothing the rail line does not already say.
