<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# H1 Hybrid-index Persistence Experiment

## Lifecycle

| Field | Value |
| --- | --- |
| Class | `experimental` |
| Owner | index/artifact maintainers |
| Decision date | 2026-09-03 |
| Promote-or-delete deadline | 2026-10-04 |
| Tracker | #765 |
| Implementation PR | #766 |
| Stable API or CLI | none |
| Current non-test consumer | none; promotion blocker |
| Package location | none; source checkout only |

H1 asks one narrow question: does a local SQLite WAL control plane plus a local
content-addressed archive improve a real BM25 workflow enough to justify a
maintained persistence boundary? It does not reopen the stable storage API.

The candidate lives only in:

- `scripts/experimental/hybrid_index/`;
- `scripts/experimental/index_persistence.py`;
- `test/scripts/hybrid_index/`;
- `test/scripts/test_experimental_index_persistence.py`.

It accepts exactly one verified portable context artifact whose only view is
BM25. FAISS, igraph, jobs, leases, hot switching, xref changes, per-file units,
GC, overlays, PostgreSQL, and object storage are later gated hypotheses.

The complexity budget is one concrete backend composition, one repository
harness, three operations, four tables, two developer commands, no dependency,
no public export, and no product route.

## Store and Schema

The selected root contains `catalog.sqlite3` and
`objects/sha256/<first-two-hex>/<remaining-hex>`. The object name is the SHA-256
of one deterministic `ZIP_STORED` archive of the already verified portable
artifact. The catalog uses application id `CNIX`, schema version 1, WAL mode,
foreign keys, and `synchronous=FULL`.

The H1 CAS requires same-filesystem hard-link publication. It has no fallback
backend and intentionally omits generic byte reads, arbitrary materialization,
deletion, discovery, and adapter APIs.

The schema has exactly four tables:

| Table | Identity and role |
| --- | --- |
| `generations` | Immutable `generation_id` row binding repository, commit, source fingerprint, `view_type=bm25`, metadata digest, archive digest/size, and artifact counts |
| `snapshots` | Immutable `snapshot_id` row binding one repository, commit, and source fingerprint |
| `snapshot_generations` | Immutable membership; primary key `(snapshot_id, view_type)` and foreign keys to the snapshot and generation |
| `refs` | The only mutable state; primary key `(repository, ref_name)`, pointing to a same-repository snapshot with a positive revision |

H1 snapshots contain exactly one BM25 generation. `generation_id` is derived
from the portable context metadata digest; `snapshot_id` is derived from the
repository/source identity and generation membership. Opening an existing
catalog requires the exact application id, schema version, and canonical schema
signature. This is a trusted local cache, not an authorization or tenancy
boundary.

## Publication and Read Linearization

Publication proceeds in this order:

1. Verify the input with the ordinary portable-artifact verifier and reject any
   view set other than exactly `("bm25",)`.
2. Re-read the authenticated artifact tree into a deterministic archive. Store
   it under its SHA-256, flush its bytes, publish or verify the exact immutable
   winner, and flush the containing directory.
3. Extract and verify that CAS archive in a private preflight directory against
   repository, commit, source fingerprint, view, and metadata identity.
4. Start SQLite `BEGIN IMMEDIATE`; insert or validate the immutable generation,
   snapshot, and membership; compare the expected ref revision; then insert or
   update the ref and commit.

The ref insert or guarded update chooses the logical winner inside the write
transaction; the containing commit is the external visibility point. A reader
pins the ref and its complete snapshot closure in one SQLite read transaction.
Object bytes are durable before catalog publication begins, so a failure may
leave an unreachable CAS object but cannot publish a ref to an incomplete
transaction. H1 deliberately has no deletion or orphan-reclamation path.
Export verifies the CAS digest, size, metadata digest, payload file count, and
payload byte count before the ordinary extractor prepares its destination.

For a new ref, `expected_revision=0` creates revision 1. A different snapshot
requires the exact current revision and advances it by one. Retrying the exact
already-published snapshot is idempotent and does not advance the revision.

## Required Failure Matrix

No row is considered verified until its focused test and the final combined H1
command pass at the same revision.

| Boundary | Required observable result | Status |
| --- | --- | --- |
| Unrelated, identified, or drifted SQLite file | Fail closed without adopting or rewriting it | Verified at `247b1dc5` |
| Concurrent first catalog open | One canonical four-table CNIX/WAL database | Verified at `178c3a35`; 100 repeated 16-thread and 30 repeated eight-process review runs |
| Invalid or non-BM25 portable input | No ref or catalog publication | Verified at `247b1dc5` |
| Artifact changes while archived | Publication fails; no ref becomes visible | Verified at `247b1dc5` |
| CAS temporary write or publish failure | No corrupt canonical object; temporary state is cleaned | Verified at `247b1dc5` |
| Concurrent identical CAS puts | All callers receive the same verified digest without false mutation errors | Verified at `247b1dc5`; 20 repeated eight-writer runs |
| Failure after durable CAS and before SQLite commit | At most an unreachable object; old ref remains readable | Verified at `247b1dc5` |
| Injected failure immediately before ref mutation | Generation, snapshot, membership, and ref transaction rolls back | Verified at `247b1dc5` |
| Two publishers use the same expected revision | Exactly one different snapshot wins; loser reports conflict | Verified at `247b1dc5`; 20 repeated runs |
| Exact publication retry | Same snapshot and revision are returned | Verified at `247b1dc5` |
| Reader overlaps a publisher | Reader observes one complete old or new snapshot closure | Verified at `247b1dc5` |
| Missing, replaced, or corrupt CAS object on export | Fail before a verified destination is published | Verified at `0bf6e752` |
| Catalog generation counts or archive identity drift | Fail before creating the export destination or an extraction stage | Verified at `178c3a35` |
| Symlinked store root or catalog file | Fail without writing through the final symlink | Verified at `178c3a35` |
| Symlinked destination ancestor or interrupted extraction | No path escape, foreign write, or leaked stage | Verified at `247b1dc5`, including the existing archive stage-swap test |
| Store is removed after successful export | Exported artifact still verifies and serves through ordinary BM25/MCP | Verified at `247b1dc5` |
| SQLite failure through the developer command | Exit 1 with one concise error and no traceback | Verified at `178c3a35` |
| Wheel and stable command inspection | No `codenib` experimental module, export, command, or default import | Verified at `86fbe2cb`, including the actual source-only script paths |

The catalog's `_before_ref_update` hook exists only for the named transaction
failure test. New arbitrary line-by-line fault seams require a reproduced bug.

## Developer Commands

These commands require a source checkout. The two experimental subcommands are
not part of the `codenib` CLI contract.

```bash
codenib artifact pack /path/to/repo \
  --output /tmp/codenib-bm25-artifact \
  --repository owner/repository \
  --view bm25

python scripts/experimental/index_persistence.py publish-bm25 \
  /tmp/codenib-bm25-artifact \
  --store /tmp/codenib-h1-store \
  --ref main \
  --expected-revision 0

python scripts/experimental/index_persistence.py export-ref \
  owner/repository \
  /tmp/codenib-bm25-export \
  --store /tmp/codenib-h1-store \
  --ref main

codenib artifact verify /tmp/codenib-bm25-export
codenib mcp --artifact /tmp/codenib-bm25-export \
  --repository owner/repository
```

The focused H1 command passed at `178c3a35`: **40 passed**. The repository unit
tier additionally reached **6,919 passed, 34 skipped**; its only unavailable
cases were two pre-existing tests that resolve `/usr/bin/docker`, which is not
installed on the benchmark host. The other 38 Docker tests passed when those
two environment-bound cases were deselected.

```bash
.venv/bin/pytest -q \
  test/scripts/hybrid_index \
  test/scripts/test_experimental_index_persistence.py
```

## Benchmark Receipt

The first diagnostic receipt uses thresholds fixed before its run: exact
top-20 BM25 projection parity; ten identical republishes add no CAS object,
object bytes, generation, snapshot, membership, or ref revision; warm ref
resolve p95 is at most 25 ms; republish p95 is at most 5 seconds; export p95 is
at most 2 seconds for an artifact no larger than 50 MiB; and retained store
bytes are at most archive bytes plus 1 MiB. Passing these diagnostics cannot
replace the missing real-consumer gate.

| Field | Required value | Current value |
| --- | --- | --- |
| Repository and immutable commit | Representative real BM25 workload | CodeNib `247b1dc5f495d444ed65280b60e28921c933b845`; 573 Python files, 8,682 chunks |
| Host/filesystem/Python/CodeNib revision | Reproducible environment | Linux 6.17 aarch64, Lustre, Python 3.14.6, CodeNib `247b1dc5` |
| Repetitions and cold/warm policy | 1 cold operation followed by 10 warm repetitions | Fixed as stated above |
| Baseline | Direct verified portable artifact load and query | Verify p50/p95 559.4/562.6 ms; load 1,308.9 ms; query p50/p95 5.40/8.29 ms |
| Candidate | Publish, resolve/export, then the same ordinary load and query | Initial publish 1,810.4 ms; republish p50/p95 1,788.8/1,799.0 ms; resolve 0.47/0.59 ms; export 1,182.4/1,194.6 ms; load 1,301.1 ms; query 5.63/7.93 ms |
| Correctness | Exact manifest identity and deterministic query-result parity | Exact top-20 parity; source fingerprint `sha256-v2:44c8bd30...e280957` |
| Latency | Publish, resolve, export, cold query, and warm-query p50/p95 | All diagnostic limits passed |
| Resources | Peak RSS and retained bytes, including SQLite WAL and CAS | Peak process RSS 167,744 KiB; 10,943,912-byte archive; 10,993,064-byte store |
| Concurrency | Same-ref conflict and same-digest dedup workload | Unit gates passed; 20 repeated runs each |
| Promotion thresholds | Numeric latency/storage limits agreed before run | All diagnostic limits passed; real-consumer threshold remains TBD |

Ten identical republishes kept exactly one object, one row in each table, ref
revision 1, and zero retained-byte growth. The archive held three files and
10,941,994 uncompressed bytes; store overhead was 49,152 bytes. The benchmark
query returned 20 identical projected results before and after persistence.

The export-integrity fix was rechecked from clean `86fbe2cb` on the same host
and workload class: 573 Python files and 8,684 chunks. Initial publish was
1,788.1 ms; ten exact republishes had p50/p95 1,782.1/1,788.4 ms; 20 ref
resolves had p50/p95 0.417/0.476 ms; and ten exports had p50/p95
1,175.9/1,180.4 ms. The 10,947,167-byte archive digest was
`38154aef6212bdd89a82ecc66b0d2275046349fd3648f2d867b12ebcf4d54b4a`.
The changed export path therefore remains below the pre-fixed 2-second p95
threshold without a second extraction pass.

## Decision

Promotion requires every failure row, package boundary, portable round trip,
real consumer, and benchmark threshold to pass. Promotion must also identify
the old product path it replaces; it does not automatically authorize H2.

If those conditions are not recorded by 2026-10-04, remove the implementation,
developer script, and dedicated tests. Keep this document with the negative or
inconclusive receipt and mark H1 retired in the storage roadmap.
