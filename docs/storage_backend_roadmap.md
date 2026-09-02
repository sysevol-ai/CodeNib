<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Storage Scope and Subtraction Roadmap

## Current Decision

CodeNib has one product database boundary: `codenib.wiki.store.WikiStore`. Its
supported implementation is the domain-local `SQLiteWikiStore`, using SQLite
WAL and a deliberately small schema. Constructor injection and the Wiki store
conformance suite are the pluggable harness; a backend registry or generic
catalog is not part of the product design.

Repository search state remains in the formats that already serve it:

- `RepoManifest` records source identity and available views;
- BM25, FAISS, igraph, and portable context payloads remain file artifacts;
- product readers load those artifacts through their existing manifest-bound
  routes rather than copying their lifecycle into a database.

The optional Web `index_storage` vertical, including retained index jobs and
runtime activation, has been removed. It was not required by the protected
CodeGraph, Wiki, MCP, or `RepositoryContextExplorer` journeys.

`codenib.storage` and the explicitly selected retained-storage CLI commands
temporarily remain for compatibility and investigation. They are classified as
experimental/compatibility code, not as the canonical product persistence
layer. Their surface is frozen while production consumers are enumerated and
unused slices are removed in reviewable batches.

## Product Boundary

`WikiStore` owns only complete Wiki cache envelopes and the minimum operations
the Wiki consumer uses:

- read one entry;
- atomically publish one entry;
- scan entries for Wiki maintenance;
- serialize generation of the same entry.

`SQLiteWikiStore` remains a trusted, regenerable local cache. Its one-table
schema, bounded canonical JSON, record digest, short transactions, WAL mode,
and cross-process generation guard cover the current product need. It is not a
general repository catalog, object store, job scheduler, lease service, or
authorization database.

The following are explicitly outside this database boundary:

- repository refs, snapshots, view generations, build jobs, and worker leases;
- BM25 documents, vector indexes, graph payloads, and context bundles;
- distributed coordination, tenancy, or remote artifact publication;
- plugin discovery and backend capability negotiation.

If another product domain eventually needs persistence, it introduces a small
domain-local protocol only after a current non-test consumer needs an injectable
I/O boundary. Sharing SQLite does not make two domains one storage abstraction.

## Evidence for the Reset

The 2026-09-01 audit of `origin/main` at
`6eba4a39bc790e525c3a2e50ba9535b57b75f666` found:

| Observation | Consequence |
| --- | --- |
| `codenib/storage` contained about 25,996 production lines and its tests about 31,679 lines. | The maintenance and verification surface was larger than the Wiki database need. |
| `sqlite_catalog.py` alone contained about 10,572 lines and 86 methods; the resulting schema had 23 tables, 11 indexes, and 86 triggers. | Generic publication, scheduling, and retention invariants dominated a local Wiki product. |
| Several public streaming and job protocols had no non-test production call site; the remaining generic catalog consumer was outside the protected product journeys. | Public abstraction breadth was not evidence of product demand. |
| A `SQLiteCatalog(create=False)` open copied and validated the catalog, ran migration setup, and validated retained history. Web reconciliation could repeat that work several times per repository per poll. | A nominal read path amplified into full-history work and writer coordination. |
| Catalog and local-CAS history had no supported retention path, while validation imposed a 1 GiB bound. | The design could grow into its own availability failure instead of solving a current product problem. |
| Wiki storage already used one domain protocol and one SQLite WAL table, with contract, corruption, rollback, reopen, CLI, Web, manifest, export, and MCP coverage. | The smaller boundary was already sufficient for the protected Wiki journey. |

The earlier catalog/CAS implementation remains useful evidence about atomic
publication and validation, but that evidence does not justify keeping every
experiment on a product route. Historical implementation chronology was
removed from this roadmap; Git history and focused tests retain it where still
needed.

## Lifecycle Policy for Retained Generic Storage

Until removed, `codenib.storage` and explicit retained-storage commands follow
these rules:

1. No new generic protocol method, schema state, backend adapter, public
   re-export, or default product route is added.
2. Allowed changes are limited to high-severity correctness/security fixes,
   compatibility needed for a planned removal, and subtraction itself.
3. Tests and roadmap entries do not count as consumers. Every retained public
   capability must name a current non-test call site.
4. A slice with no current consumer is removed together with its implementation,
   exports, dedicated tests, and active documentation.
5. Compatibility-sensitive CLI or Python surfaces receive an explicit release
   decision before removal; compatibility is not an indefinite lifecycle.

This classification does not lower validation on data that the compatibility
surface still opens. It prevents that validation model from expanding the
product core.

## Active Work

### W1: Remove the Web retained-storage route

Status: complete.

- `index_storage` configuration and Web lifespan composition are gone.
- Retained index-job read/write routes, scheduling, reconciliation, runtime
  activation, and their route-specific tests are gone.
- Manifest-backed repository status and all protected journeys remain.

### W2: Shrink the compatibility surface by consumer

Status: in progress.

Current outcome:

- Default Web and portable-artifact validation no longer import
  `codenib.storage`; bounded exact-JSON validation lives in the
  artifact-neutral `_bounded_json` module.
- The unreleased reachability audit, catalog-selected MCP cold start, and
  `index --publish-retained` routes are gone. Ordinary `index`, `publish`, and
  `mcp --artifact [--repo]` behavior is unchanged.
- The published compatibility bridge remains explicit: `artifact import-cache`
  captures an existing compiler cache and `artifact materialize` produces a
  portable artifact. The released schema-v4 `JobCatalog` API remains readable
  pending an explicit compatibility decision.

Remaining gates:

- Remove the report-only retained benchmark and the unreleased durable jobs,
  worker, scheduler, publication, and schema-v5-v8 expansion.
- Inventory every remaining `codenib.storage` export against a non-test caller;
  delete zero-consumer protocols and intersection tests in focused batches.
- Give every surviving compatibility surface a release/removal decision without
  expanding it into a product storage layer.

### W3: Close the small Wiki operational gaps

Status: pending.

- Bound generation-lock acquisition so a stuck generator cannot wait forever
  without an actionable error.
- Add only the Wiki-specific lifecycle operation justified by measured cache
  growth; do not introduce generic CAS garbage collection.
- Make maintenance inspection physically read-only and document regeneration
  as the corruption-recovery path.

These items must keep the `WikiStore` protocol small. A maintenance need is not
permission to recreate snapshot, ref, job, or object-store machinery.

## Closed Plans

CodeNib no longer plans PostgreSQL, S3-compatible object storage, generic local
or remote garbage collection, a dynamic backend registry, distributed leases,
or a shared ANN database as part of this program. DuckDB and graph databases
are likewise not product persistence targets.

Reopening one of these options requires all of the following:

- a current product route that the Wiki SQLite or existing artifact model
  cannot serve;
- a documented deployment constraint and representative workload;
- measured operational evidence;
- an explicit scope decision that identifies the old path it replaces.

## Completion Gate

The storage subtraction is complete when:

- the default CodeGraph, Wiki, Web, MCP, and repository-context paths do not
  import or configure `codenib.storage`;
- Wiki persistence passes its domain contract and protected product journeys
  with SQLite WAL as the supported implementation;
- manifest-bound BM25, FAISS, igraph, and context artifacts remain compatible;
- every surviving generic storage surface has a named compatibility consumer
  and removal decision, or has been deleted with its dedicated tests; and
- documentation and the changelog describe the current boundary rather than a
  speculative backend program.
