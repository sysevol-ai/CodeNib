<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# SCIP Multi-Language Roadmap

**Status:** the original Phase 0--6 program is complete and archived. Its final
implementation slice merged through #558 / PR #593 at `d9996e88`; the
post-merge gate-stability reconciliation #594 / PR #595 is included at
`8362551c` on 2026-08-11.

This document is the durable history for CodeNib's multi-language graph
indexing and acceleration work. Keep it current when a backend is promoted, a
gate changes, or a related issue is reconciled. New consumer-boundary and
repository-batching experiments belong to the separate
[Consumer-boundary acceleration](https://github.com/sysevol-ai/CodeNib/milestone/3)
milestone tracked by [#601](https://github.com/sysevol-ai/CodeNib/issues/601);
they do not make the completed phases below incomplete again.

## Current Baseline

The language registry records SCIP cold-start state explicitly:

| State | Languages | Meaning |
| --- | --- | --- |
| `active` | Python, Go, Rust, C#, Java, Kotlin, Ruby, Scala, JavaScript, TypeScript, PHP | Routed through existing SCIP cold-start paths. Ruby and PHP use hybrid active routes: prepared Bundler/Composer projects prefer SCIP and loose or unprepared projects fall back to LSP. Kotlin and Scala are active through `scip-java`; Scala is limited to Scala 2.x Gradle/SBT projects without a registered LSP baseline. |
| `candidate` | none | No candidate SCIP backend is currently waiting on promotion gates. |
| `none` | C++, Swift, Lua | No accepted SCIP cold-start plan in CodeNib today. C++ uses clangd-style graph indexing. |

Generic LSP graph support for Java, C#, Kotlin, Ruby, and PHP remains available
through `graph_route="lsp"` for regression checks and for loose-file fallback.
Scala has no registered LSP graph route today; its active graph support is the
`scip-java` route only.

The 0.2.2 repository-source authority gate temporarily disables manifest-bound
query-time reuse of native clangd `.idx` files for C and C++, including the
default/legacy source policy. Those files commonly live below the
default-excluded `build/` tree and do not yet carry an authenticated generation
receipt plus allowed-file proof. C/C++ graph indexing remains available, while
queries fall back to the verified persisted symbol graph. Re-enabling the
native route requires exact `.idx` generation ownership, source-selection
closure, and parity tests; this is an acceleration gate change, not a language
support removal.

## Archived Layered Goal

The completed multi-language SCIP cold-start and acceleration program required:

1. Preserve the current LSP graph/index behavior and public language capability
   matrix.
2. Promote candidate SCIP cold-start backends only after gated smoke, decode,
   backend-alignment, and documentation work.
3. Keep active SCIP backends for Python, Go, Rust, C#, Java, Kotlin, Ruby,
   Scala, JavaScript, TypeScript, and PHP fast and parity-tested where a C++
   core decoder exists.
4. Add C++ acceleration only where profiling shows local decode or graph
   processing is a meaningful bottleneck.
5. Keep PRs, commits, and issues synchronized with this roadmap so multi-step
   work does not fragment into disconnected partial goals.

## Promotion Gates

A candidate SCIP language can become `active` only when all of these are true:

- Tool discovery works through `scip_cold_start_command_for_language()` and any
  `CODENIB_*_SCIP_CMD` override.
- A minimal real-project smoke test can produce `index.scip`, decode it, and
  write a CodeGraph.
- The serial Python decoder maps symbols, files, ranges, definition nodes,
  containment edges, and reference anchors into the existing CodeGraph schema.
- Backend alignment against the existing LSP graph is measured where an LSP
  backend exists. Any accepted reference/call-edge tolerance is documented.
- `docs/language_capabilities.md` and `codenib/languages.py` agree.
- Tests cover the registry metadata, command override, smoke behavior, and
  decoder parity surface.
- CI or an equivalent local validation run is recorded before merge.

C++ acceleration for a newly promoted language is a separate gate:

- End-to-end profiling shows local decode/build time is at least 20% of the
  cold-start graph time, or the decoder is already proven hot on large SCIP
  text files.
- A C++ decoder or helper mirrors the Python path with parity tests.
- The C++ implementation is a reusable core module under `core/`, not logic
  embedded only in pybind bindings.
- New decoder code starts from `SCIPDecoderBase`, `SubgraphBuilder`, and the
  language-neutral helpers in `core/scip_decode_common.h`; language-specific
  symbol policy stays in the owning `scip_decode_<language>.cpp` file.
- Decoder registration, aliases, and Python/C++ registry metadata must be
  updated together before the language is advertised as core-accelerated.

## Completed Work Queue

### Phase 0: Operating Contract

- [x] Keep repo-level agent rules in `AGENTS.md`.
- [x] Keep `.codex/README.md` as a pointer to shared rules instead of a second,
  conflicting policy file.
- [x] Record SCIP cold-start states in the language registry and capability
  matrix.
- [x] Keep Makefile targets for candidate SCIP and LSP smoke tool installation
  and generated smoke execution.
- [x] Keep tool environment variables, PATH wiring, output directories, and
  smoke timeouts versioned in Makefile targets rather than in chat-only notes.
- [x] Keep reproducible bootstrap targets for active SCIP/LSP tools, candidate
  SCIP/LSP tools, C++ core build prerequisites, and local toolchain command
  checks.
- [x] Keep project-local setup helpers in Makefile for tools that cannot be
  installed globally without mutating a target repo, including Ruby overlay
  bundles and optional PHP `vendor/bin/scip-php` prewarming.
- [x] Track this long-running roadmap in the docs nav.
- [x] Keep candidate SCIP routes explicit: `LSIndexer` defaults to the active
  graph backend, while `graph_route="scip-candidate"` opts into the candidate
  SCIP indexer for smoke/profiling without promoting the language.
- [x] Keep existing LSP graph routes explicitly addressable through
  `graph_route="lsp"` for backend comparison and post-promotion regression
  checks where a registry LSP command exists.
- [x] Keep route-level backend alignment reproducible through Makefile:
  `make graph-route-alignment` installs the pinned toolchain, builds isolated
  reference/candidate route outputs, and compares them with the shared
  backend-alignment harness.
- [x] Keep SCIP route-level path filtering centralized after decode so
  `target_dir` and `exclude_patterns` compare the same source surface across
  Java/Kotlin/Scala, C#, Ruby, and PHP candidate or active SCIP routes.

Exit condition: future Codex or agent sessions can recover the long-term
objective from versioned files without relying on chat history.

### Phase 1: Java First

Java was promoted first because JDT LS already existed as an LSP baseline and
`scip-java` was the most mature candidate among the new JVM paths.

- [x] Add a Java SCIP smoke harness that runs the registered command.
- [x] Produce and decode `index.scip` for a small Maven or Gradle project.
- [x] Add a Java SCIP serial decoder or shared JVM decoder surface.
- [x] Compare Java SCIP and JDT LS graphs with `scripts/check_backend_alignment.py`.
- [x] Document any accepted alignment tolerance.
- [x] Run at least one non-generated Maven or Gradle repository smoke before
  promotion.
- [x] Promote Java from `candidate` to `active` only after the above is green.

Exit condition: Java can cold-start through SCIP behind a documented gate
without regressing the existing JDT LS path.

Current Java active status: the generated Maven smoke produces `index.scip`,
`index.decoded`, and `graph.pkl` through `scripts/smoke_scip_cold_start.py`.
The SCIP graph has symbol and containment parity with the JDT LS smoke graph
under `scripts/check_backend_alignment.py`. The Java SCIP decoder skips
scip-java `<init>` constructor symbols and constructor references because the
tiny Maven smoke shows they are synthetic default constructors not present in
the current JDT LS graph surface; reference-count differences are informational
until Java reference/call-edge promotion explicitly defines stricter tolerances.

Real-repo Java smoke status: `jitpack/maven-simple` at commit
`1cd9e66a9be5037f3e5d9b2c9be92ecf82087c66` runs through
`scripts/smoke_scip_cold_start.py --project-root`, producing `index.scip`,
`index.decoded`, and `graph.pkl`. The candidate graph found
`App`, `App.greet(String)()`, and `App.main(String[])()`.

Route-level Java gate status: `make graph-route-alignment
PROJECT_LANGUAGE=java PROJECT_ROOT=${CODENIB_TEMP_DIR}/real-java/maven-simple
GRAPH_ALIGNMENT_OUTPUT_DIR=${CODENIB_TEMP_DIR}/java-route-alignment
GRAPH_ALIGNMENT_EXTRA_ARGS=--reference-include-references` builds both routes
from the registry and writes isolated artifacts under
`${CODENIB_TEMP_DIR}/java-route-alignment/maven-simple-java/`. The current JDT LS
reference graph has 21 vertices and 25 edges; the SCIP candidate graph has 29
vertices and 37 edges. Alignment is strict-green for symbols and containment:
`missing_symbols=[]`, `extra_symbols=[]`, `missing_containment=[]`,
`extra_containment=[]`. Reference counts differ (`scip-java`: 17, JDT LS: 5)
and remain informational until Java reference/call-edge tolerances are promoted.
Java is now `active` in the registry, so `LSIndexer(language="java")` uses
`SCIPJavaIndexer` by default. The existing JDT LS path remains available through
`LSIndexer(language="java", graph_route="lsp")`, and
`scripts/smoke_lsp_graph.py` pins `graph_route="lsp"` so LSP regression checks
do not depend on the active backend. A post-promotion route check with
`GRAPH_ALIGNMENT_REFERENCE_ROUTE=active`,
`GRAPH_ALIGNMENT_CANDIDATE_ROUTE=lsp`, and
`GRAPH_ALIGNMENT_EXTRA_ARGS=--candidate-include-references` is strict-green on
the same fixture: active SCIP has 29 vertices, 37 edges, and 17 reference edges;
JDT LS has 21 vertices, 25 edges, and 5 reference edges.

Explicit SCIP route validation: `scripts/smoke_scip_cold_start.py
--project-root` drives real-project smoke through the registry instead of a
script-local class map. A temporary Maven project with `Foo` and `Bar` produced
`index.scip`, `index.decoded`, and `graph.pkl` through the Java SCIP route with
11 vertices, 11 edges, 1 reference edge, and no missing expected `Foo` symbols.
LSP route validation: a temporary Python project built through
`LSIndexer(language="python", graph_route="lsp")` with basedpyright produced
`graph.pkl` with 5 vertices and 4 containment edges, proving active SCIP
languages can still use the generic LSP graph route for comparison.

### Phase 2: JVM Family

Kotlin and Scala should reuse the Java/JVM lessons where the SCIP output shape
allows it.

- [x] Run Kotlin smoke with `scip-java index` or the accepted JVM command.
- [x] Decide whether Kotlin needs language-specific symbol normalization.
- [x] Run Scala smoke and decide whether Metals alignment is required before
  graph support is enabled.
- [x] Promote Kotlin only after real-repo LSP-vs-SCIP alignment is acceptable.
- [x] Promote Scala independently from Java after generated Scala 2.13 smoke,
  real SBT project smoke, and the no-LSP-baseline decision are recorded.

Exit condition: each JVM language has an explicit graph backend decision and no
registry row implies unsupported behavior.

Current Kotlin active status: the generated Gradle smoke project runs
through `scip-java index`, decodes `index.scip`, and builds a CodeGraph via the
shared JVM decoder. Kotlin needs two normalization rules beyond Java:
`.kt` documents are accepted by the decoder, and member containment falls back
to the semanticdb owner descriptor when `enclosing_range` is absent. Top-level
Kotlin functions keep their display name, for example `normalize()` instead of
`normalize.normalize()`. Real-project probing also fixed nested owner display
and scip-java overload suffixes so members such as `TypeSpec#Builder#add(...)`
normalize toward `TypeSpec.Builder.add...` instead of leaking raw `#` segments
or `(+1)` suffixes into `unified_name`.

Generated Kotlin alignment status: the same Gradle smoke root runs through the
current Kotlin LS route and aligns strict-green against the SCIP candidate for
definition symbols and containment: `missing_symbols=[]`, `extra_symbols=[]`,
`missing_containment=[]`, `extra_containment=[]`. Reference counts differ
(`scip-java`: 6, Kotlin LS: 0) and remain informational. The active
`LSIndexer(language="kt")` route now uses `SCIPKotlinIndexer`; the generic
Kotlin LSP path remains available through `graph_route="lsp"` for regression
checks.

Kotlin tooling note: in local probes, a Maven-shaped Kotlin project did not emit
`index.scip` or `.semanticdb` artifacts through `scip-java`; the generated smoke
therefore uses Gradle Kotlin/JVM. `scip-java` may warn about Maven publication
extraction on the tiny Gradle project, but the index is still accepted when it
contains the expected Kotlin class, method, top-level function, and references.

Real-repo Kotlin smoke status: `Kotlin/kotlinx.cli` at commit
`32112b630b3f1e01c2b446173410be895d456e5e` produced a Kotlin LS baseline for
`core/commonMain/src` with 243 vertices and 242 containment edges, but
`scip-java` failed against Kotlin 1.9.10 with a SemanticDB compiler-plugin
`MESSAGE_COLLECTOR_KEY` linkage error. `square/kotlinpoet` current `main` at
commit `88d82910bb6751dd1fff88f4060fd431de633979` produced a Kotlin LS baseline
but failed `scip-java` against Kotlin 2.4.0 because the analyzer registrar API
requires `getPluginId()`. KotlinPoet tag `2.1.0` at commit
`1c63279201a09d922f74840fd2613d0d54044f95` failed `scip-java` against Kotlin
2.1.10 with a missing FIR renderer class.

KotlinPoet tag `2.2.0` at commit
`ab96a8d361a77649368f872c5758548eb8be5d34` is the current successful real
SCIP probe. `make scip-project-smoke PROJECT_LANGUAGE=kotlin` produced
`index.scip`, `index.decoded`, and `graph.pkl` with 7,477 vertices, 37,390
edges, and 31,613 reference edges; the generated candidate found `TypeSpec` and
`FileSpec`. A full-root Kotlin LS baseline produced 3,574 vertices and 3,573
containment edges. After nested-owner normalization, backend alignment is still
not promotion-green: symbols `ref=3050 cand=4507 missing=486 extra=1943`,
containment `ref=3050 cand=4716 missing=486 extra=2152`, and references
`ref=0 cand=31613`.

The next Kotlin gate fixed a route-level comparison flaw rather than promoting
the backend: SCIP decoded graphs now honor `target_dir` after decode, so
KotlinPoet's candidate route can be compared against the same
`kotlinpoet/src/jvmMain/kotlin` surface as the LSP route. Reprocessing the saved
KotlinPoet 2.2.0 decoded SCIP artifact with that target reduced the candidate
graph to 2,749 vertices, 9,835 edges, and 7,499 reference edges. Against the
matching LSP graph, alignment is still not promotion-green but is now scoped to
real modeling differences: symbols `ref=849 cand=1638 missing=146 extra=935`,
containment `ref=849 cand=1708 missing=146 extra=1005`, references
`ref=0 cand=7499`. The remaining gap is dominated by Kotlin object/companion
property modeling, generated getter/property symbols, and symbol display
normalization; Kotlin therefore remained `candidate` at this intermediate gate.

A Kotlin-specific decoder follow-up now removes the largest non-source
definition noise from that same target-dir gate: parameter descriptors including
overloaded `(+n)` parameters, type-parameter descriptors, companion object
classes/constructors, synthetic companion getters such as `getEMPTY()`, and
JVM overload descriptors that scip-java encodes without normal method syntax,
and metadata-confirmed enum helpers such as `entries`, `valueOf()`, and
`values()`. It also flattens companion members to the containing Kotlin type and
keeps Kotlin `<init>` constructors as `constructor()` definitions instead of
applying Java's default-constructor skip. Companion member containment is also
mapped back to the containing Kotlin type when scip-java omits usable
`enclosing_range` data. A subsequent source supplement adds enum entries,
top-level functions, explicit property getter nodes, and top-level `object`
override function aliases from `.kt` source when scip-java omits the LSP-facing
definition surface, using file-scoped keys when an external/reference vertex
already owns the raw SCIP key. Reprocessing the saved KotlinPoet 2.2.0 decoded
artifact with the real source checkout produced 1,562 vertices and 5,923
edges, with strict-green containment for the shared definition surface:
symbols `ref=849 cand=936 missing=0 extra=87`, containment `ref=849 cand=938
missing=0 extra=89`, references `ref=0 cand=4725`.

The final Kotlin promotion gate filters metadata-confirmed synthetic property
accessors such as `getANY()` and `setOut()` while preserving the source
property nodes they wrap. A fresh KotlinPoet 2.2.0 profile on the same
`kotlinpoet/src/jvmMain/kotlin` target now produces 1,483 vertices and 5,680
edges. Against the matching Kotlin LS graph, alignment has no missing
definition symbols and no missing containment: symbols `ref=849 cand=860
missing=0 extra=11`, containment `ref=849 cand=861 missing=0 extra=12`,
references `ref=0 cand=4568`. The same saved decoded artifact reprocesses to
1,477 vertices, 5,636 edges, and references `ref=0 cand=4524`; both gates have
the same 0-missing/11-extra definition surface. The accepted extra surface is
limited to source-valid definitions or compiler helpers that Kotlin LS omits
from document symbols: a `CodeBlock.Builder` constructor, the `Dynamic` object
and its explicit override methods, and `MemberName` data-class `copy()` /
`componentN()` helpers. A broad "generate getters for every field" rule was
rejected because it cuts missing symbols at the cost of a much larger
extra-symbol surface. Filtering top-level constants is also not accepted
because those are useful retrieval symbols even when a given LSP baseline does
not report them. Kotlin is now `active` in the registry, while
`graph_route="lsp"` remains available for Kotlin LSP regression checks.

Current Scala active status: the generated Gradle Scala 2.13 smoke runs
through `scip-java index`, decodes `index.scip`, and builds a CodeGraph via the
shared JVM decoder. The decoder accepts `.scala` documents and treats Scala
object members such as `app/Helpers.normalize().` as contained by the object
symbol `app/Helpers`. The smoke found `Invoice`, `Invoice.total()`, `Helpers`,
and `Helpers.normalize()`.

Real-repo Scala gate status: `sbt/io` at commit
`ca47d04466174ccf69adb16433b1b0f3f2521f05` was validated as the first active
Scala gate after adding a pinned Makefile SBT install under
`CODENIB_SCIP_TOOLS_DIR`. The command
`scripts/smoke_scip_cold_start.py --languages scala --project-root
${CODENIB_TEMP_DIR}/real-scala-sbtio --output-dir ${CODENIB_TEMP_DIR}/scala-sbtio-smoke
--expected-symbol IO --expected-symbol Path --json` produced `index.scip`,
`index.decoded`, and `graph.pkl` with 3,571 vertices, 18,611 edges, and 15,995
reference edges. `scip-java` indexing took about 74.527s, protoc decode took
about 0.099s, Python graph decode took about 2.156s, and range-index
construction took about 0.069s. There is no registered Scala LSP route in
CodeNib, so promotion uses generated Scala 2.13 smoke plus the real SBT smoke
instead of a Metals alignment baseline. A Scala 3 Gradle probe failed because
the injected SemanticDB scalac plugin did not match the compiler path, so Scala
3 support is not proven by this promotion.

### Phase 3: C# SCIP

C# should be evaluated against `.sln` and `.csproj` projects because restore
behavior changes graph quality.

- [x] Add `scip-dotnet` smoke with restore/setup guidance.
- [x] Decode a small solution and compare against the csharp-ls graph.
- [x] Document SDK/tool prerequisites and failure modes.
- [x] Promote C# only after decoder and alignment gates pass.

Exit condition: C# has an active SCIP path while the csharp-ls route remains
available through `graph_route="lsp"`.

Current C# active status: the generated `.csproj` smoke runs through
`scip-dotnet index`, decodes `index.scip`, and builds a CodeGraph through the
C# serial decoder. The decoder accepts `.cs` documents, ignores generated
`bin/` and `obj/` files, preserves namespace symbols such as `Smoke/`, and
normalizes display names to the current csharp-ls shape, for example
`Smoke.Invoice.Total()`. Because `scip-dotnet` does not emit scope ranges for
the tiny project, C# reference edges use the nearest preceding same-file
definition as the source when a precise range is unavailable.

Generated C# alignment status: the generated C# smoke root aligns strict-green
against `csharp-ls` for definition symbols and containment:
`missing_symbols=[]`, `extra_symbols=[]`, `missing_containment=[]`,
`extra_containment=[]`. Reference counts differ (`scip-dotnet`: 3,
`csharp-ls`: 2) and remain informational.

Real-repo C# smoke status: `dotnet/samples` at commit
`7178a58dc7401c5ef8ec8f679bc307ec3bb7d17e`, sparse path
`core/console-apps/HelloMsBuild`, runs through both
`make lsp-project-smoke PROJECT_LANGUAGE=csharp` and
`make scip-project-smoke PROJECT_LANGUAGE=csharp`. The csharp-ls graph found
`Program`; the SCIP graph found `Program` and `Program.Main(string[] args)()`.
Route-level promotion gate
`make graph-route-alignment PROJECT_LANGUAGE=csharp
PROJECT_ROOT=${CODENIB_TEMP_DIR}/real-csharp/samples/core/console-apps/HelloMsBuild
GRAPH_ALIGNMENT_OUTPUT_DIR=${CODENIB_TEMP_DIR}/csharp-route-alignment
GRAPH_ALIGNMENT_EXTRA_ARGS=--reference-include-references` is strict-green for
definition symbols and containment:
`missing_symbols=[]`, `extra_symbols=[]`, `missing_containment=[]`,
`extra_containment=[]`. The csharp-ls reference graph has 10 vertices, 10 edges,
and 1 reference edge; the SCIP graph has 7 vertices, 6 edges, and 2 reference
edges. Reference-count differences remain informational. Post-promotion,
`GRAPH_ALIGNMENT_REFERENCE_ROUTE=active`, `GRAPH_ALIGNMENT_CANDIDATE_ROUTE=lsp`,
and `GRAPH_ALIGNMENT_EXTRA_ARGS=--candidate-include-references` stays
strict-green on the same fixture. This validation also fixed two real-project
decoder gaps: scip-dotnet parameter pseudo-symbols such as `Main().(args)` are
skipped, and `using System` namespace occurrences are not treated as local
definitions.

C# tooling note: local validation used `scip-dotnet` 0.2.14 and csharp-ls
0.25.0. The installed csharp-ls binary required .NET runtime 10.0.0; .NET 8 was
enough for `scip-dotnet` but not for this LSP alignment run. C# remains
serial-only in Python for now: the real-repo profile spends about 4.3-4.8s in
`scip-dotnet` indexing and about 0.01s in Python decode/build, so a C++ decoder
does not satisfy the acceleration gate.

### Phase 4: Ruby And PHP Active Hybrid

Ruby and PHP use active hybrid routes. Prepared Bundler/Composer projects prefer
SCIP through `scip-ruby` or `scip-php`, while loose or unprepared projects retain
the generic Ruby LSP or Intelephense route.

- [x] Run Ruby smoke with `scip-ruby`, including Sorbet/setup requirements.
- [x] Compare Ruby SCIP graph quality against ruby-lsp output.
- [x] Run PHP smoke with `scip-php` and record community indexer limitations.
- [x] Compare PHP SCIP graph quality against Intelephense output.
- [x] Promote PHP with an explicit active-route policy that preserves
  `graph_route="lsp"` and keeps `graph_route="scip-candidate"` as a pure SCIP
  gate.
- [x] Promote Ruby only if symbol/reference coverage is better than or equal
  to the current LSP path for CodeNib use cases, or if accepted tolerances are
  documented.

Exit condition: Ruby and PHP active hybrid states reflect measured
graph quality, not tool existence alone.

Current Ruby active hybrid status: the registry routes Ruby through
`RubyHybridIndexer`. Explicit overlay bundles selected with
`CODENIB_RUBY_BUNDLE_GEMFILE` or `BUNDLE_GEMFILE`, and project Gemfiles that
declare `scip-ruby`, prefer `SCIPRubyIndexer`. Loose Ruby projects and ordinary
Gemfiles without `scip-ruby` keep the generic ruby-lsp route, and active SCIP
failures fall back to ruby-lsp. The explicit `graph_route="lsp"` path remains
the ruby-lsp regression route, while `graph_route="scip-candidate"` remains the
pure scip-ruby gate.

The generated Bundler gem smoke runs through `bundle exec scip-ruby`, decodes
`index.scip`, and builds a CodeGraph through the Ruby serial decoder.
`scip-ruby` rejects direct binary invocation, so the registry command is
`bundle exec scip-ruby` and the smoke prepares
`vendor/bundle` before indexing. The decoder accepts `.rb` documents, normalizes
symbols such as `Smoke#Invoice#total().`, and maps singleton class descriptors
for `<Class:Smoke>#normalize()` to `Smoke.normalize()`. The generated smoke found
`Smoke`, `Smoke.Invoice`, `Smoke.Invoice.total()`, and `Smoke.normalize()`.

Ruby LSP alignment status: the generic LSP smoke now writes a Bundler-shaped
Ruby project, prepares the bundle, starts ruby-lsp through `bundle exec
ruby-lsp`, and indexes only `lib/` so `vendor/bundle` does not pollute the
graph. The generated LSP smoke is green with 7 vertices and 6 containment edges.
The Ruby-specific LSP normalization preserves module parents and strips `self.`
from singleton methods, normalizes Ruby constructors to `initialize()`, lifts
`@ivar` definitions out of method scopes, treats attr-style field names as
methods, drops `class << self` as a graph scope, and normalizes `::` display
names. Generated SCIP-vs-ruby-lsp alignment is strict-green on the definition
surface: `symbols ref=4 cand=4 missing=0 extra=0` and `contain ref=4 cand=4
missing=0 extra=0`. The shared SCIP route filter trims the generated project
graph back to `target_dir=lib` and `exclude_patterns=vendor/**` after
`scip-ruby` writes dependency symbols, preventing `vendor/bundle` from polluting
source-route alignment. Reference counts remain informational in this gate
(`scip-ruby`: 2, ruby-lsp: 0).

Real-repo Ruby gate status: `ruby/rake` at commit
`2dce6007988b302e07049f36d8528459ecf7ff01` was validated with a non-invasive
`.codenib/Gemfile` overlay selected through `CODENIB_RUBY_BUNDLE_GEMFILE`;
the overlay uses `gemspec path: ".."` and adds pinned `ruby-lsp` 0.26.9 plus
`scip-ruby` 0.4.7. The generic LSP client now maps that overlay variable into
`BUNDLE_GEMFILE` for Ruby and removes `GEM_PATH`, matching the Ruby SCIP
indexer environment so ruby-lsp does not accidentally resolve the target
project's own Gemfile. Route alignment on `lib/` with `vendor/**` and
`.codenib/**` excluded now runs end-to-end with one accepted source-modeling
difference:
`symbols ref=598 cand=598 missing=1 extra=1`, `contain ref=598 cand=598
missing=1 extra=1`, and references differ (`ruby-lsp`: 0, `scip-ruby`: 2770).
The active route was rechecked with `--candidate-route active` and the explicit
Ruby tolerance gate `--max-missing-symbols 1 --max-extra-symbols 1
--max-missing-containment 1 --max-extra-containment 1`; that gate is green and
confirms the active hybrid route selects SCIP when the overlay bundle is present.
This pass fixed the largest mismatch classes by lifting Ruby LSP `@ivar`
definitions to class containment, keeping top-level Ruby class/module reopen
definitions file-scoped in the SCIP decoder, and normalizing `def
task.timestamp` from scip-ruby's `Object.timestamp()` display to ruby-lsp's
file-level `timestamp()`. It also synthesizes source-declared Ruby `alias`
methods when the original method exists in the same file and owner, bringing
`FileList.add()`, `FileList.kind_of?()`, `Task.prereqs()`, and
`TaskArguments.key?()` into parity without guessing across classes. It keeps
source-declared
`attr_writer`/`attr_accessor` generated writer definitions aligned with the
ruby-lsp method display while preserving explicit `def foo=` setters. The only
remaining symbol/containment difference is ruby-lsp's nested anonymous-module
definition `Rake.Application.load_debug_at_stop_feature().execute()` versus
scip-ruby's alternate receiver display `Rake.Application.execute()`. The active
hybrid promotion accepts this anonymous-module receiver modeling tolerance
because all other source definitions and containment align, source references
are materially richer in the SCIP graph, and loose Ruby projects still fall back
to ruby-lsp.

Ruby C++ acceleration status: the same `ruby/rake` decoded index now has
serial/core parity through `SCIPRubyGraphDecoder` and `SCIPDecoderCore` with
`language="ruby"`/`"rb"`. After filtering to `lib/`, both decoders produce 815
nodes and 3,466 edges with no missing/extra names, no edge-multiset difference,
and no vertex-attribute difference. Local `process_index` time went from 7.58s
serial to 1.04s through the C++ backend, so Ruby is covered by the accelerated
core decoder gate while the active hybrid route still falls back to ruby-lsp
when a Bundler SCIP setup is not available.

Current PHP active hybrid status: the registry routes PHP through
`PHPHybridIndexer`. Composer projects prefer `SCIPPHPIndexer`, which runs
project-local `vendor/bin/scip-php` from the validated
`davidrjenni/scip-php:0.0.2` package, decodes `index.scip`, and builds a
CodeGraph through the PHP serial decoder. Loose files and non-Composer projects
fall back to the generic Intelephense LSP indexer, and the same fallback is used
if SCIP setup or indexing fails. `graph_route="lsp"` remains the explicit
Intelephense regression route, while `graph_route="scip-candidate"` remains the
pure SCIP route for smoke, alignment, and profiling gates.

Local PHP/Composer are not required for the generated smoke when Docker is
available; the smoke uses the `composer:2` image, runs as the host UID/GID, and
can be overridden with `CODENIB_PHP_COMPOSER_IMAGE`. The pure SCIP route
prepares a throwaway Composer worktree under the route output directory before
running `vendor/bin/scip-php`, so Composer install/require operations and
`index.scip` generation do not mutate the source checkout. If the source
checkout already has a complete project-local `vendor/bin/scip-php`, the copied
worktree reuses it; otherwise the worktree installs the validated package with
local Composer or the same Docker fallback. The decoder accepts `.php` and
`.phtml` documents, synthesizes namespace nodes to match the Intelephense graph
surface, normalizes symbols such as `Smoke/Billing/Invoice#total().`, and
supplements top-level PHP functions from the source AST because `scip-php`
0.0.2 omits them from the SCIP index.

PHP tooling limitations recorded from validation: Packagist installation is
blocked by Composer security policy unless advisory blocking is disabled for the
generated smoke, `scip-php` expects the indexed root to have a git reference for
root package metadata, and the packaged `vendor/bin/scip-php` expects
`vendor/davidrjenni/scip-php/vendor` to exist. The generated smoke and pure SCIP
route worktree setup handle those constraints. The tiny smoke found
`Invoice`, `Invoice.total()`, and `normalize()` with reference edges.

Generated PHP alignment status: source-only Intelephense alignment on the same
tiny smoke is strict-green for namespace, class, method, and top-level function
symbols/containment after namespace synthesis and source-AST function
supplementation. `scripts/smoke_scip_cold_start.py --languages php
--output-dir ${CODENIB_TEMP_DIR}/php-normalize-smoke --json` produced 8 vertices, 9
edges, 2 references, and no missing expected symbols. `scripts/check_graph_route_alignment.py
--project-root ${CODENIB_TEMP_DIR}/php-normalize-smoke/php --language php
--reference-route lsp --candidate-route scip-candidate --target-dir src
--exclude-pattern 'vendor/**' --output-dir ${CODENIB_TEMP_DIR}/php-normalize-alignment
--json --clean` is strict-green: symbols `ref=4 cand=4 missing=0 extra=0`,
containment `ref=4 cand=4 missing=0 extra=0`, references `ref=0 cand=2`.
A full-root Intelephense run that includes `vendor/` scans thousands of
dependency files and times out during reference collection, so PHP alignment
should use source-only roots or explicit vendor exclusion until the LSP path has
ignore configuration. This source-only strict-green result is sufficient for
the active hybrid route because Composer projects use SCIP and explicit
`graph_route="lsp"` remains available for regression checks.

Real-repo PHP gate status: `php-fig/container` at commit
`707984727bd5b2b670e59559d3ed2500240cf875` was prepared in
`${CODENIB_TEMP_DIR}/real-php-container`. The pure SCIP route copies that
checkout to an output-local worktree before indexing, and the source checkout
does not receive a new `index.scip` artifact. Source-only route alignment on
`src/` with `vendor/**` excluded is strict-green:
`scripts/check_graph_route_alignment.py --project-root
${CODENIB_TEMP_DIR}/real-php-container --language php --reference-route lsp
--candidate-route scip-candidate --target-dir src --exclude-pattern 'vendor/**'
--output-dir ${CODENIB_TEMP_DIR}/php-container-alignment-noninvasive --json --clean`
reports
symbols `ref=8 cand=8 missing=0 extra=0`, containment
`ref=8 cand=8 missing=0 extra=0`, references `ref=0 cand=1`. This validation
fixed two real-project decoder gaps: namespace nodes are now synthesized per
file so repeated namespaces align with Intelephense, and `scip-php` parameter
pseudo-symbols such as `ContainerInterface#get().($id)` are skipped. A
`nikic/FastRoute` probe at commit `1c961398bef1ff6ecd8b273bef651d7afe90312b`
was not a usable gate because adding `scip-php` conflicted with the project's
locked `nikic/php-parser` and current Composer image PHP version. The accepted
promotion policy is the active hybrid route described above. The promoted
active route was rechecked against explicit LSP on the same project:

```bash
make graph-route-alignment \
  PROJECT_LANGUAGE=php \
  PROJECT_ROOT=${CODENIB_TEMP_DIR}/real-php-container \
  GRAPH_ALIGNMENT_REFERENCE_ROUTE=active \
  GRAPH_ALIGNMENT_CANDIDATE_ROUTE=lsp \
  GRAPH_ALIGNMENT_TARGET_DIR=src \
  GRAPH_ALIGNMENT_EXCLUDE_PATTERNS='vendor/**' \
  GRAPH_ALIGNMENT_OUTPUT_DIR=${CODENIB_TEMP_DIR}/php-active-vs-lsp-alignment \
  GRAPH_ALIGNMENT_EXTRA_ARGS='--clean'
```

The gate is strict-green with symbols `ref=8 cand=8 missing=0 extra=0`,
containment `ref=8 cand=8 missing=0 extra=0`, and references `ref=1 cand=0`.

### Phase 5: Acceleration And Parity

- [x] Keep existing Python/Go/Rust/Ruby/JS/TS SCIP C++ parity tests green.
- [x] Profile each newly promoted route before adding C++ decoder code.
- [x] Prefer shared normalization helpers when symbol shapes are language-family
  compatible.
- [x] Keep C++ files small and purpose-specific: parser/decoder logic,
  graph-layer helpers, pybind exposure, and tests should remain separated.
- [x] Keep C++ decoder registration and language aliases centralized so pybind
  bindings, smoke CLIs, and future language decoders do not duplicate dispatch
  logic.
- [x] Keep language-neutral SCIP text/string helpers in
  `core/scip_decode_common.h`/`.cpp` so Python, Go, Rust, Ruby, and TypeScript
  decoders share one implementation for common parsing primitives.
- [x] Keep language-specific normalization in the owning decoder file instead
  of hiding symbol policy in generic helpers.
- [x] Record speedup and parity in `docs/core_cpp.md` or an experiment doc.
- [x] Keep a manifest-driven large-repository profiling harness for active
  SCIP languages whose C++ acceleration status is still serial-only or needs
  revalidation.
- [x] Retire the [#558](https://github.com/sysevol-ai/CodeNib/issues/558)
  native Python chunk-span POC after exact parity passed but the 20%
  end-to-end promotion gate failed; retain its measurements here rather than
  retaining a dormant implementation.

Exit condition: acceleration claims are backed by parity tests and profile
numbers, and `core/` remains maintainable.

Current acceleration status: `make core-test` builds the pybind module and
passes C++ smoke tests, graph-layer tests, registry metadata tests, and
serial/core parity for the active accelerated SCIP backends. Ruby now has a
C++ decoder because `ruby/rake` made local decode the hot path: serial
`process_index` took 7.58s, the C++ backend took 1.04s, and the filtered
`lib/` graph matched exactly with 815 nodes, 3,466 edges, zero missing/extra
names, zero edge-multiset differences, and zero vertex-attribute differences.
Java, C#, Kotlin, PHP, and Scala are active SCIP routes but remain serial-only because
profiling shows external tooling dominates cold-start time: on
`jitpack/maven-simple`,
`scip-java` indexing took about 5.98s while protoc decode took 0.01s, Python
graph decode 0.007s, and range-index construction 0.001s; on the recorded C#
fixture, `scip-dotnet` indexing took about 4.3-4.8s while Python decode/build
was about 0.01s; on the small PHP Composer gate, LSP document-symbol collection
took about 16.245s, SCIP indexing took about 0.551s, protoc decode took about
0.008s, and Python graph decode took about 0.007s. On the `sbt/io` Scala gate,
`scip-java` indexing took about 74.527s while protoc decode took about 0.099s,
Python graph decode took about 2.156s, and range-index construction took about
0.069s. On the KotlinPoet 2.2.0 Kotlin promotion profile, `scip-java` indexing
took 61.914s, protoc decode took 0.150s, Python graph decode took 6.931s, and
range-index construction took 0.008s. Local decode/build is about 10% of the
end-to-end Kotlin cold-start graph time, so Kotlin does not cross the 20% C++
acceleration gate yet. Kotlin, PHP, and Scala should get C++ acceleration only
after larger real-repo profiles show local decode/build is a material
bottleneck.

C/C++ clangd artifact status: the provider-level LSP acceleration benchmark now
requires an index-quality report before latency measurement. The shared gate
checks compile DB size and resolved files, repository translation-unit coverage,
graph-to-compile-DB translation-unit coverage, range/unified metadata, and
optional old/new vertex and edge ratios. A
19-instance `codenib-base` calibration found one false-success artifact:
`micropython__micropython-10095` had only 2 compile commands and a graph at
12.6% of baseline vertices. The corrected `ports/unix` capture produced 286
compile commands and a graph with 9,151 vertices and 72,207 edges. clangd and
decode were not the dominant cold-start costs; repository preparation,
submodules, CMake, Bear, and Make remain the optimization surface. Bear
candidate selection and warning-as-error normalization therefore belong in the
Python indexing harness, not in the C++ decoder layer.

Python SCIP decoders now share descriptor extraction, descriptor suffix
normalization, and `unified_name` formatting helpers in
`codenib.scip_interface.scip_decode_utils` where symbol shapes are compatible.
Language-specific handling, such as Ruby singleton-class descriptors and Kotlin
synthetic symbol filters, stays in the owning decoder.

C++ SCIP decoders now follow the same boundary: common text-format primitives
such as integer extraction, whitespace splitting, suffix checks, trailing
character stripping, and backtick removal live in `core/scip_decode_common.h`
and `core/scip_decode_common.cpp`; each language decoder keeps only its
language policy, metadata loading, and symbol normalization. New accelerated
languages should extend that shared helper surface only for language-neutral
operations and must add registry/parity coverage before becoming accepted core
languages.

Large-repo acceleration watch status: `scripts/profiling/large_scip_repos.yml`
records representative real repositories for Java, C#, Kotlin, Scala, PHP, and
Ruby. Run `make large-scip-profile` with language/repo filters to clone those
targets, profile SCIP index generation, protoc decode, serial graph decode/build,
and optional C++ core decode, then write JSON and Markdown reports under
`LARGE_SCIP_PROFILE_OUTPUT_DIR`. The harness applies the same 20% local
decode/build gate before recommending any new C++ decoder work. This is the
preferred path for proving that a serial-only active language has outgrown its
current Python decoder.

Provider-neutral native boundary status: issue
[#559](https://github.com/sysevol-ai/CodeNib/issues/559) landed through PR #562.
The core decodes SCIP into flat `DecodedRecords` before igraph or Python object
materialization. The established `decode()` path materializes those rows into
the same graph, while capability-specific consumers can stop at
`decode_records()`. The immutable `FactBatch v1` model landed through #563/PR
#566, and the versioned native `FactBatchBuffer v1` ABI, exact graph projection,
validation, and read-only ownership contract landed through #564/PR #567.

FactBatchBuffer runtime promotion status
([#565](https://github.com/sysevol-ai/CodeNib/issues/565)): the candidate uses
ownership-safe zero-copy buffers but remains opt-in. Issue #565 is closed with
this design recorded as a negative promotion experiment, not unfinished
production work. The 2026-08-10 local gate alternated arm order, required graph
and semantic parity, and compared median end-to-end time against a 20%
threshold:

| SCIP subject | Consumer | Legacy | FactBatchBuffer | Improvement | Result |
| --- | --- | ---: | ---: | ---: | --- |
| CodeNib Python, 41.6 MB | graph-compatible | 0.3775s | 0.3937s | -4.3% | keep experimental |
| CodeNib Python, 41.6 MB | logical FactBatch | 0.6271s | 0.6772s | -8.0% | keep experimental |
| Ruff Rust, 130.5 MB | graph-compatible | 1.9756s | 2.0352s | -3.0% | keep experimental |
| Ruff Rust, 130.5 MB | logical FactBatch | 3.2654s | 3.2031s | +1.9% | below gate |

All four parity gates passed. Cyclic garbage collection was disabled inside
timed regions and collected before each arm; each generated JSON retains every
raw sample and the alternating arm order. The CodeNib report used 11 iterations
after two warmups; Ruff used seven after two warmups. Their index SHA-256 values
were `96e2c407c421d7eb72b2fd834c812c6b688b3634973f2fcdc865cb78bfe87227`
and `ab55627fb2f379bff19b3fe8123cb94b4d019732711b16c0ddaf5179898c8778`.
Adding unchanged external SCIP generation time can only dilute the sole 1.9%
local improvement, so neither cold-start gate can reach 20%. Keep
`CODENIB_CORE_FACT_BUFFER` at its default `off`; use `auto` for compatible
fallback experiments and `required` for fail-closed ABI/parity gates. Reproduce
the report with `make fact-buffer-profile` and add
`FACT_BUFFER_PROFILE_EXTRA_ARGS='--include-semantic-consumer'` for logical
facts. FactQuery indexing (#561) and clangd parsing (#555) remain separate
promotion and review decisions.

FactQueryIndex v1 promotion status
([#561](https://github.com/sysevol-ai/CodeNib/issues/561)): the graph-free index
owns `DecodedRecords` plus integer postings and exposes only symbol definitions
and fully anchored references. Position and route capabilities remain false;
invalid endpoints, definition ranges, duplicate names, and unanchored
references fail the candidate before it can return a partial result. The
existing `decode()` graph API is unchanged. `decode_query_index()` defaults to
`auto`, with compatible graph fallback; `required` fails closed.

The 2026-08-10 query-ready gate starts from a saved `index.decoded`, alternates
complete-CodeGraph and FactQueryIndex arms, includes an identical workload of
100 definition/reference symbol pairs, and requires exact results and public
errors across canonical, display, bare, quoted, ambiguous, and missing seeds:

| SCIP subject | Parity seeds | CodeGraph | FactQueryIndex | Index build | Improvement | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| CodeNib Python, 41.6 MB | 1,143 | 0.4006s | 0.2991s | 0.0171s | +25.3% | promote auto |
| Ruff Rust, 130.5 MB | 1,109 | 2.4401s | 1.7280s | 0.1377s | +29.2% | promote auto |

The Python report used 15 measured iterations after five warmups; Rust used
seven after three. Cyclic garbage collection was collected before each arm and
disabled inside timed regions, and both reports retain every raw sample and
arm order. The artifact SHA-256 values are
`96e2c407c421d7eb72b2fd834c812c6b688b3634973f2fcdc865cb78bfe87227` and
`ab55627fb2f379bff19b3fe8123cb94b4d019732711b16c0ddaf5179898c8778`.
This is a persisted-artifact query-readiness decision, not a claim that
unchanged external SCIP generation becomes faster. Use
`make fact-query-profile` and optionally pass `--external-index-seconds` for a
separate cold-start decision. Auto promotion is therefore restricted to
Python and Rust; every other core language stays on CodeGraph until it clears
the same 20% gate. clangd parsing (#555) remains a separate decision.

Native clangd FactQueryIndex promotion status
([#555](https://github.com/sysevol-ai/CodeNib/issues/555)): the baseline C/C++
slice reads the direct `*.idx` children of an existing project-local clangd
index in stable filename order, decodes RIFF records directly into
`DecodedRecords`, and builds `FactQueryIndex` without Python record dictionaries,
igraph, or `CodeGraph`. Its v1 contract exposes only definition and reference
lookup by symbol. A hybrid provider keeps those successful calls graph-free
and materializes the complete established graph exactly once for position or
route fallback. Existing graph decode, persistence, incremental checks, and
quality behavior remain unchanged. `auto` falls back on native candidate
errors, `off` forces the complete graph, and `required` fails closed.

The 2026-08-10 gate started from unchanged `.idx` shards, alternated complete
graph and native arms for 15 measured iterations after five warmups, disabled
cyclic garbage collection inside timed regions, used the same deterministic
definition/reference workload, and exhaustively compared public results and
errors for canonical, display, bare, quoted, ambiguous, and missing seeds:

| clangd subject | Graph shape | Parity seeds | CodeGraph | Native index | Improvement | Result |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `cpp_simple`, 0.9 MB | 33 vertices / 80 edges | 95 | 0.1162s | 0.0166s | +85.7% | promote auto |
| `fmt`, 2.3 MB | 9,375 vertices / 36,614 edges | 25,101 | 1.2135s | 0.0974s | +92.0% | promote auto |

Both parity gates passed: `cpp_simple` contained 26 definitions and 48
references, while `fmt` contained 9,319 definitions and 27,240 references.
The native graph-materialization stage was zero in every measured sample. The
stable shard-set SHA-256 values were
`ed4f34dc789611aa0f23cb1cfa6ad19a15d5bc36c43c7ac38adb1f0c42c70de4` and
`b42e63b7afa75e2e45642779ff27ecf99505e1a4ea187a9c65d5c069884e6140`.
Reproduce the report with `make clangd-fact-query-profile`.

This promotion does not include or accelerate clangd index generation. RIFF
version/resource hardening (#546), zlib CI enforcement (#547), and
content-bound generation receipts (#548) are independent production gates.
Serving integration and the mixed/RSS/concurrency matrix are tracked below.
Native position (#553), native route (#552), and durable FactBatch publication
(#551) keep independent promotion decisions; their landed evidence is recorded
below.

Native clangd RIFF compatibility and resource-safety status
([#546](https://github.com/sysevol-ai/CodeNib/issues/546)): the v1 reader now
requires one 4-byte `meta` chunk, accepts only parity-tested versions 18, 19,
and 20, and rejects duplicate known chunks before semantic decoding. Outer
RIFF lengths/padding, record truncation, varint overflow, string indexes,
header/reference counts, and exact zlib input/output completion are validated
deterministically. Upstream clangd's current-version-only policy is documented
from LLVM `Serialization.cpp` and `RIFF.h`; future versions remain fail-closed
until their layout clears the same parity gate.

The compiled contract publishes finite limits for direct file count, chunks,
per-file and aggregate bytes, decompressed strings, string entries, copied
string bytes, and decoded records. Discovery rejects impossible file sets
before reading; decompression, object expansion, copies, `reserve()`, and row
insertion consume their respective budgets before allocation. All parse
failures include the shard filename and cannot return a partial native index.
The deterministic matrix covers truncated headers/payloads, missing padding,
duplicate chunks, unsupported versions, zlib mismatch/trailing input,
oversized declarations, invalid counts, and sparse per-file/aggregate limits.
`auto` falls back with the error recorded, `required` fails closed, and `off`
preserves the established graph path. The same v18/v19/v20 fixture executes
the exact public definition/reference parity assertions.

Native clangd content-bound snapshot status
([#548](https://github.com/sysevol-ai/CodeNib/issues/548)): every native decode
now publishes a deterministic SHA-256 receipt over a length-delimited domain,
snapshot schema, query ABI/format, normalization profile, normalized project
root, exact RIFF allowlist, sorted direct shard names, shard lengths, and the
exact bytes already read for decoding. Directory enumeration order therefore
does not affect identity, while any filename, byte, supported-version set,
query schema, normalization profile, or root identity does. The first hash is
fused with the decoder's existing read buffers; a post-decode re-read proves
the directory did not change before the native index is published.

The receipt is exposed by the pybind index, decode payload, and
`clangd_fact_query_snapshot(...)`. Provider metadata uses that identity instead
of the old root/vertex/edge-count fallback. Before Python lazily collects the
complete graph, it verifies the receipt both before and after collection. A
changed file list or content permanently fails that provider session so symbol
results from one generation cannot be combined with a graph from another.
Native candidate failures before publication still follow the existing mode
policy: `auto` may build one compatible graph from the then-current generation,
while `required` fails closed. Callers restart the provider to adopt a later
generation.

The 2026-08-10 snapshot-enabled `cpp_simple` recheck used 223 shards and five
alternating rounds with exact result parity. Median complete-graph startup was
157.7 ms versus 23.9 ms for the native path, an 84.8% improvement after both
receipt passes were included. The fused first-pass hash took 2.94 ms (12.6% of
native startup); hashing plus publication verification took 7.09 ms. The
maintained profiler now reports both stages and their fraction of startup, and
the existing 20% acceleration decision includes their cost.

Native clangd MCP consumer-routing status
([#549](https://github.com/sysevol-ai/CodeNib/issues/549)):
`ServerContext` now owns one runtime-only LSP provider selection. A
source-verified, local, C/C++-only manifest may reuse its existing project-local
clangd shards through the native fact-query provider; startup never generates
or publishes a clangd index. Portable artifacts, mixed-language manifests,
disabled or unavailable native support, and unverified checkouts select the
persisted symbol graph with an explicit fallback reason. MCP definition,
reference, and route calls and the three agent LSP skills all use the same
provider resolver.

Native definition and reference requests remain graph-free. The first position
or route request materializes the snapshot-compatible complete graph exactly
once and later graph-requiring calls reuse it. Result rows and `get_manifest`
runtime metadata expose the selected backend, fallback reason, capabilities,
and native snapshot identity. The maintained profiler now validates both raw
query parity and MCP serialization/public-error parity and publishes a separate
`mcp_consumer_decision` over startup plus consumer work.

The 2026-08-10 consumer-boundary gate used 223 `cpp_simple` shards, five
alternating measured rounds, and 100 deterministic symbol requests per round.
Median complete-graph MCP consumer time was 178.269 ms versus 27.717 ms for the
native provider, an 84.45% improvement with exact result parity. Symbol-only
requests never imported or materialized igraph, and the first graph-requiring
request materialized the compatible graph once. Native position and route
indexes remained the independent #553 and #552 promotion gates at this stage;
the position result is recorded below.

Native clangd mixed-workload promotion status
([#550](https://github.com/sysevol-ai/CodeNib/issues/550)): first lazy graph
materialization is serialized with double-checked publication. The provider is
visible to waiting position/route callers only after the complete graph and its
range indexes are ready, and it records the successful materialization count
and duration. A concurrent first-route gate requires all callers to return the
same public result while exactly one graph is built.

`make clangd-workload-gate` runs symbol-only, position-first, route-first, and
mixed legacy/native sessions in fresh processes with alternating arm order. Its
versioned JSON includes inner and outer wall time, process CPU, start/peak/growth
RSS, shard/graph counts, provider and fallback selection, native and lazy-graph
stages, exact result/public-error digests, and pre/post snapshot receipts. The
default promotion budgets are at least 20% symbol-only acceleration, at most
20% regression for graph-requiring workloads, at most 1.25x legacy peak RSS,
at most 10% repeated-process peak spread, a 4 GiB absolute native cap, exact
parity, and exactly-once concurrent materialization. Filesystem page cache is
explicitly uncontrolled and clangd generation is separately labeled and
excluded from query-ready decisions.

The threshold decision is evaluated as `native <= legacy * (1 - threshold)`
rather than by comparing a derived improvement quotient. This preserves the
same default budgets while accepting an exact floating-point boundary. The
maintained one-round process-isolation smoke verifies parity, backend/fallback
selection, graph-free routing, concurrency, versions, receipts, and finite
timing telemetry without making a promotion claim. Promotion decisions remain
multi-round runs ([#594](https://github.com/sysevol-ai/CodeNib/issues/594)).

The subject manifest pins fmt 11.2.0, GoogleTest 1.17.0, and protobuf 31.1 at
full commits, covering template-, macro-, header-heavy, and multi-target C++.
Selecting a subject requires that exact clean revision. The same Make target
first exercises generated RIFF 18/19/20 public parity fixtures. This milestone
deliberately kept position-first, route-first, and mixed native arms on one
lazy complete graph; the later graph-free position gate is recorded below and
graph-free route remains isolated in #552.

The recorded `cpp_simple` evidence used 223 shards and five measured rounds
plus one warmup. Symbol-only improved from 177.96 ms to 37.28 ms (79.0%);
position-first, route-first, and mixed regressed 16.8%, 16.9%, and 13.9%, all
within budget. Native peak RSS stayed at or below 135.5 MiB and about 1.05x
legacy, repeated spread stayed below 1.4%, and every concurrent run built once.
On the existing 51-shard fmt checkout, three isolated rounds improved
symbol-only from 2.330 s to 0.137 s (94.1%); graph-workload regressions stayed
within 2.3%, native peak stayed below 246 MiB and 1.14x legacy, and all three
concurrent runs built once.

Native clangd position-query promotion status
([#553](https://github.com/sysevol-ai/CodeNib/issues/553)): the clangd query
contract is now `clangd-riff-fact-query-v2`. It adds provider-neutral
occurrence rows with zero-based half-open file ranges, role bits, and optional
target/container vertex ids. `FactQueryIndex` builds native per-file interval
postings and per-target postings without igraph or Python record dictionaries.
UTF-16 is the default position encoding; UTF-8 and UTF-32 are normalized at the
provider boundary, bound into the content receipt, and shared by full and
incremental clangd background commands.

The hybrid provider keeps its startup index symbol-only. Its first exact
position request builds the native occurrence view under the same publication
lock used for graph fallback, while route-first sessions retain the #550
cold-start path. Successful definition/reference positions remain on
`native-clangd-fact-query-v1` and materialize zero graphs. Invalid ranges,
missing sources, declaration-only rows, unsupported targets, missing
definitions, line ambiguity, unanchored references, and source-token mismatch
carry stable reasons into the established graph fallback. A route or fallback
still publishes one complete, range-indexed graph.

The 2026-08-11 `fmt` gate used the clean pinned revision
`b35de87ad91951c8269fe533dca6aebc3e0a25ba`, 51 shards / 2,392,564 bytes,
20 deterministic exact positions, three measured isolated rounds after one
warmup, and the unchanged 20% thresholds:

| Workload | Legacy | Native | Change | Native graphs | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| symbol-only | 1.9313s | 0.1169s | +93.9% | 0 | pass |
| position-first | 2.0626s | 0.2634s | +87.2% | 0 | pass |
| route-first | 1.9109s | 2.0381s | 6.7% regression | 1 | pass |
| mixed | 2.0475s | 2.3689s | 15.7% regression | 1 | pass |

The median lazy native position initialization was 0.1388s for 121,434
occurrence rows. Exact MCP result/public-error parity, snapshot receipts, RSS
budgets, RIFF 18/19/20 fixtures, and all three eight-thread concurrent
first-route runs passed; every concurrent run built exactly one graph. The
receipt remained
`clangd_fact_query:sha256:a88c9933461a2573a2c928eeeac8b734fcd5245d29f9d41e61d60f9c3d0b6693`.
This is a query-ready result over existing shards, not a clangd-generation
claim. At that v2 milestone, native route remained the independent #552 gate.

Native clangd route-query promotion status
([#552](https://github.com/sysevol-ai/CodeNib/issues/552)): the query contract is
now `clangd-riff-fact-query-v3`. Native normalization emits complete structural
containment, reference/relation adjacency, and the legacy vertex traversal
order. `FactQueryIndex` validates that the order covers every vertex exactly
once, builds compact incoming/outgoing postings, and preserves repeated
neighbors for exact igraph multi-edge parity. The raw index reports graph-route
support unavailable because it lacks source spans; the clangd hybrid adapter
adds lazy touched-node range enrichment and truthfully advertises
`native-clangd-route-adjacency-v1`.

Direct-symbol and query-only routes preserve legacy ordering, scoring, `top_k`,
and result snapshots without constructing `CodeGraph`. Query-only preparation
retains the established 10,000-row scan, 256-match, and 512-candidate bounds.
The provider verifies the content receipt before every route. Incomplete
adjacency or a native preparation/execution failure recomputes the entire route
on one complete graph with a stable reason; no partial route is returned, and a
snapshot mismatch fails closed.

The versioned `clangd_mixed_workload_gate_v3` promotion run on 2026-08-11 used
the clean manifest-pinned fmt 11.2.0 revision
`40626af88bd7df9a5fb80be7b25ac85b122d6c21`, 492 shards / 4,820,850 bytes,
20 deterministic query entries, one query-only route, and three isolated rounds
after one warmup:

| Workload | Legacy | Native | Improvement | Native graphs | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| symbol-only | 2.9039s | 0.1950s | 93.3% | 0 | pass |
| position-first | 3.0069s | 0.4308s | 85.7% | 0 | pass |
| route-first | 2.7913s | 0.7221s | 74.1% | 0 | pass |
| mixed | 2.8408s | 0.9655s | 66.0% | 0 | pass |

Exact public result/error parity, pre/post snapshot receipts, pinned revision,
RSS, RIFF 18/19/20, and source-cleanliness gates passed. Three eight-thread
concurrent native route rounds were deterministic and materialized zero graphs.
The route-first improvement exceeds the 20% promotion threshold, so `auto`
promotes compact native route adjacency and retains the full graph only as its
atomic compatibility fallback. This remains a query-ready result over existing
shards and does not claim faster clangd index generation.

Native clangd durable FactBatch publication status
([#551](https://github.com/sysevol-ai/CodeNib/issues/551)): the strict C/C++
dual-write adapter collects normalized records from the bounded native decoder
without constructing `CodeGraph`, then emits canonical per-file `FactBatch v1`
units. The profile fails closed unless analyzer, target, toolchain, compilation
database, build context, position encoding, RIFF contract, normalization,
adapter schema, and FactBatch schema all participate in its identity.

`codenib.fact-batch-generation.v1` composes the verified unit receipts into one
source- and profile-bound generation manifest. Every member is an immutable
catalog object and an explicit reachability/GC root. Path-aware upserts and
deletes reuse unchanged units, while profile changes, nondeterministic reuse,
tampered receipts, missing objects, and a failed ref compare-and-swap fail
closed without replacing the previous generation. Snapshot-local definition
lookup resolves unresolved SymbolID monikers only against the pinned generation
and keys its bounded cache by the complete snapshot identity.

The 2026-08-11 `fmt` gate used the clean pinned 11.2.0 revision
`40626af88bd7df9a5fb80be7b25ac85b122d6c21` and 492 `.idx` shards. It published
51 file units containing 9,783 definitions, 98,552 occurrences, 85,903 edges,
and 64,668 unresolved targets. The manifest and its 51 units produced 52
reachable objects. An unchanged publication reused 51/51 units, republished
zero units, matched the clean semantic digest, and materialized zero graphs.
Median end-to-end time improved from 9.9169s for the clean generation to
5.2349s for unchanged reuse (47.2%). Reproduce the gate with
`make clangd-fact-generation-profile`.

This is durable publication and incremental-reuse evidence over existing
clangd shards, not a claim that clangd index generation is faster. The legacy
materialized graph remains the public graph-query authority until the M4 parity
and cutover gates in `docs/storage_backend_roadmap.md` pass. The broader storage
RFC #199 also remains open for generic generation publication, jobs, leases,
retention/GC, remaining adapters, overlays, and server backends. With foundation
issues #554/#555 and production gates #546 through #553 merged, parent tracker
[#545](https://github.com/sysevol-ai/CodeNib/issues/545) closed on 2026-08-11.
That closure records completion of the query-acceleration program without
implying that storage RFC #199 or its dependent programs are complete.

Native core CI enforcement status
([#547](https://github.com/sysevol-ai/CodeNib/issues/547)): the trusted
`scip-core` job now declares zlib development headers alongside RE2 and CMake,
preserves the vendored-igraph and system-libstdc++ loader contracts, and calls
the maintained `make core-test` gate instead of one SCIP-only pytest file. The
gate runs all five C++ executables plus SCIP, Fact, native clangd, fallback, and
profiler-contract Python tests. It first asserts that the built extension
exports `decode_clangd_fact_query_index`, `clangd_fact_query_contract`, and
`clangd_fact_query_snapshot`, so a missing or stale extension cannot silently
skip the clangd module. CI-policy tests pin the dependency probe, Make target,
binding guard, and test inventory.
The same `make core-test` command is the documented local reproduction; slow,
billed, and external clangd-generation benchmarks remain outside this
deterministic job.

Native Python chunk-span POC status
([#558](https://github.com/sysevol-ai/CodeNib/issues/558)): issue #558 closed
the per-file design as a measured negative experiment, not a pending
production route. On 2026-08-24 its implementation, private binding, build and
runtime switches, profiler, and dedicated tests were retired under the
cognitive-debt program. The evidence below remains the authoritative record.

The final gate passed exact `CodeChunk` parity for decorators, async
definitions, Unicode and PEP 695 syntax, nested and conditional definitions,
L1/L2 selection, optional containers, headers, error recovery, and line
splitting. The balanced harness collects before and disables cyclic GC for
each arm, keeps candidate-only validation outside the stopwatch, retains every
raw sample and AB/BA order, rejects a Git identity change during measurement,
and promotes only at an exact 20% improvement.
Both recorded runs used `chunk_depth=2`, `l2_level_exclusive=true`,
`include_header_epilogue=false`, no line cap, and excluded test files.

The 2026-08-11 clean-checkout reports were both negative:

| Subject | Scope | Rounds / warmups | Existing median | Native POC median | Change | Decision |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| CodeNib `a33bb13118e3a04f8d3d76eabcfb2602f785477a` | 520 files / 6,630,346 bytes / 5,620 chunks | 6 / 1 | 0.6430s | 0.8088s | 25.8% slower | rejected |
| HTTPie `2105caa49bae87c5809c274e407619a0de2639d1` | 89 files / 338,976 bytes / 498 chunks | 20 / 4 | 0.03588s | 0.04294s | 19.7% slower | rejected |

Both reports had `git_dirty=false`, exact parity, and `promoted=false`. The
candidate missed the requirement for at least 20% acceleration and was slower
on both subjects. Per-file native boundary work is therefore not the next
promotion path; follow-up experiments should measure incremental parse-tree
reuse or repository-level batching.

### Phase 6: Multi-Graph Python Surface

Mixed-language repositories need a Python-side graph aggregation path before a
C++ acceleration path can be justified.

- [x] Add `CodeGraph.merge_from()` so independently built language graphs can
  be merged while preserving vertex attributes, structural edge deduplication,
  anchored reference multi-edges, and range indexes.
- [x] Add `build_graph_for_languages()` in `ls_router` so single-language
  builds keep the historical cache layout and multi-language builds write
  per-language artifacts under `graphs/<language>/` plus a combined
  `graph.pkl`.
- [x] Route BM25 retrieval, graph retrieval, and `SymbolGraphBuilder` through
  the shared multi-language graph builder instead of one-off single-language
  indexer calls.
- [x] Expose `graph_route` through BM25/Graph retrieval pipelines,
  BM25/Graph/eval baseline CLIs, and `SymbolGraphBuilder`, so LSP comparison
  and candidate SCIP backends can be evaluated explicitly without changing the
  active route.
- [x] Run a real mixed-language repository smoke with at least two active
  graph backends and record the node/edge counts plus query behavior.
- [x] Profile multi-graph merge/build time before adding any C++ merge or
  aggregation helper.

Exit condition: multi-language graph builds work through the same Python API
used by retrieval, compiler, MCP, and agent flows; C++ acceleration is added
only if measured graph aggregation overhead justifies it.

Current multi-graph smoke status: a temporary git-backed Python+Go repository
with `pyproject.toml`, `go.mod`, one Python module, and one Go package built
through `build_graph_for_languages(languages=["python", "go"])`. The Python and
Go per-language graphs each produced 4 vertices and 4 edges; the merged graph
produced 7 vertices and 8 edges and wrote the combined `graph.pkl` alongside
`graphs/python/graph.pkl` and `graphs/go/graph.pkl`. A no-metadata temporary
directory failed inside `scip-python` because project version was undefined, so
real or generated Python smoke repos should include package metadata and git
state.

Initial merge profile: merging the KotlinPoet 2.2.0 SCIP graph
(7,477 vertices, 37,390 edges) into an empty `CodeGraph` took about 0.39s on the
local workstation; merging the same graph again through structural/reference
dedup took about 0.27s. This does not currently justify a C++ graph-merge
helper. Revisit only if real mixed-language repositories show merge time as a
material fraction of cold-start graph build time.

## Follow-up: Consumer-Boundary Acceleration

Milestone 3 and [#601](https://github.com/sysevol-ai/CodeNib/issues/601) track a
new, ordered program rather than extending the archived Phase 0--6 queue:

1. [#597](https://github.com/sysevol-ai/CodeNib/issues/597) produced a
   graph-free SCIP FactQueryIndex import path, complete source/filter receipt,
   and exact filtered-graph parity proof.
2. [#598](https://github.com/sysevol-ai/CodeNib/issues/598) compared that
   candidate with the real MCP baseline of loading the existing `graph.pkl`.
   The earlier Python/Rust query-ready gains are API evidence, not yet a
   user-visible consumer claim.
3. [#599](https://github.com/sysevol-ai/CodeNib/issues/599) defined exact
   repository-level chunk parity and 20% p50/p95 gates before another native
   implementation is added.
4. [#600](https://github.com/sysevol-ai/CodeNib/issues/600) timeboxed one
   repository call with bounded native workers and an ordered flat buffer. A
   failed gate removed the POC implementation and retained only the receipt.

M1 implementation status ([#597](https://github.com/sysevol-ai/CodeNib/issues/597)):
the candidate boundary is deliberately split into two proofs. C++ records the
exact decoded-index and Rust Cargo bytes it consumed and performs an
O(F+V+E) filter-identity scan without deleting or remapping facts. The
order-sensitive native query-surface digest must exactly equal the compiler's
serial graph digest. The graph-free Python facade binds that evidence to a
current builder-schema-v4 compiler manifest, source fingerprint, repository
filter policy, resolved root, and graph-writer artifact receipt. Only the
publishing compiler build opts into the additional receipt scans; ordinary
graph pipelines retain their default behavior. Incremental, partial,
multi-language, source-coverage fallback, mutated, or otherwise unproven inputs
are rejected atomically. Reference-only external targets are retained only
when the exact serial filtered surface contains them and every incoming
reference has an allowed source anchor. The first admitted languages are
Python and Rust. This is safety/admission work only: it does not enable an MCP
route and does not replace the M2 consumer-boundary measurement in #598.

M2 implementation and gate status
([#598](https://github.com/sysevol-ai/CodeNib/issues/598)): the experimental
provider keeps symbol-shaped definitions and references on the admitted native
index and revalidates the bound snapshot before atomically lazy-loading the
existing `graph.pkl` for position and route calls. One condition state machine
publishes a complete graph provider once; invalid shapes do not trigger the
loader, concurrent callers reuse the publication, and symbol calls remain
native afterward. Snapshot and loader failures are sticky, `MemoryError`
propagates unchanged, and canonically normalized public payloads and
persisted-graph metadata remain identical across arms.
The independent runtime mode defaults to `off`; the selector is not connected
to production `ServerContext`.

The formal `scip_mcp_consumer_gate_v1` run on August 11, 2026 used benchmark
commit `d7dab128ca3ed320111f7ac293bf43902abb4c7e`, 20 measured samples per arm
after four warmups, a fresh process for every balanced ABBA sample, and 100
seeds: 17 each canonical, display, bare, and quoted, plus 16 each ambiguous
and missing. Each seed issued one definition and both reference declaration modes.
The promotion rule independently required candidate symbol-only p50 and
nearest-rank p95 to be no more than 80% of legacy. Position-first, route-first,
mixed, and 16-thread concurrent first-fallback workloads were correctness-only
gates. Filesystem page cache remained uncontrolled.

| Subject | Legacy p50 / p95 | Candidate p50 / p95 | p50 / p95 improvement | Decision |
| --- | ---: | ---: | ---: | --- |
| CodeNib Python `6cf61b08310e165574c52fa217c66f0b6ae2a36d` | 1.417629s / 2.009662s | 2.456498s / 2.569240s | -73.282% / -27.844% | reject promotion |
| Ruff Rust `75a24bbc67aa31b825b6326cfb6e6afdf3ca90d5` | 2.217309s / 2.702742s | 6.545577s / 7.005324s | -195.204% / -159.193% | reject promotion |

Negative improvement denotes a regression. The exact artifact identities were:

- CodeNib: 19,200 vertices and 90,102 edges; 82,776,883-byte
  `index.decoded` SHA-256
  `bcaebd3d5a053d0a9bcc70f840bb7fdc7c0ed3eafb64beb515cb5e7236e3f548`;
  6,099,865-byte `graph.pkl` SHA-256
  `7de7b0dde8c8d63cf44ebd2678922255867540edc757531c86b17b2e5262f870`;
  query-surface SHA-256
  `8f006b89bb41cdfbefae65a1efd118ca9c73c8ece50f92a9899f23aed255ab8b`;
  candidate receipt
  `4e601db58e7a5d3b6d988ccf5494956dc551dd58c565160dace29acd6dbb3ad9`;
  raw report SHA-256
  `af8404dcc2b64547279cdb27fb3241977c3957a0580c36e55750567bedb2ff4b`.
- Ruff: 31,837 vertices and 231,458 edges; 130,542,689-byte
  `index.decoded` SHA-256
  `9f17e90da1ac0cdecb1754e235c525cad0f95dfe1aae9ce29180f8545edaa40d`;
  13,388,156-byte `graph.pkl` SHA-256
  `6dc1743e39a201d7fd0aa65495240fe3478c7b3834645db9cb7f642088e30d36`;
  query-surface SHA-256
  `5870b26c99e76186d64bd321650c3c4cec5921e110e76b93e4044e7ad128d28b`;
  candidate receipt
  `1d3ec1bfc6cd9f00e78743453c0b3ea574c696a81ca3f0aa59236c6ae94975bc`;
  raw report SHA-256
  `aff1dc4e6dcfa581a540a574223d6e0aba9b414ca8b9153ee344010d7a1e82c7`.

For both languages, exact public result, error, order, ambiguity, and metadata
parity passed under canonical JSON serialization of the complete MCP
tool-result payload; raw JSON-RPC envelope byte parity was not claimed.
Symbol-only candidate runs imported no Python igraph and recorded zero graph
loads and fallbacks. Position-first,
route-first, mixed, 16-thread exactly-once publication, immutable input and
benchmark receipts, clean before/after identities, canonical protocol, and
process isolation all passed. Only the multiplicative p50 and p95 gates failed.
The raw machine JSON is not versioned; the report hashes above and the #598
issue record identify it.

Python and Rust are decided independently, but neither qualified. The
consumer-promoted set therefore remains empty, production `ServerContext`, MCP,
and agent routing for these Python/Rust SCIP paths stay on persisted
`CodeGraph`, and #598 is a measured negative promotion decision rather than
unfinished integration work. At that M2 decision, the ordered next issue was
[#599](https://github.com/sysevol-ai/CodeNib/issues/599), which defined the
repository-level chunk successor gate without adding a C++ batch
implementation or production route; #600 was the subsequent bounded
repository-native implementation experiment. Storage RFC #199 and Guardian
#309 remain independent programs.

M3 gate record
([#599](https://github.com/sysevol-ai/CodeNib/issues/599)): this issue is the
historical fail-closed controller and benchmark contract. It contained no C++
successor, did not reuse or retune the negative #558 per-file route, and could
not publish candidate performance until #600 supplied the fixed private
`run_python_repository_batch` adapter. In that pre-adapter state, the command
validated both pinned subjects and configurations, exercised the complete
controller through deterministic fake adapters in unit tests, wrote an atomic
negative report, and exited nonzero rather than measuring legacy against
itself.

The manifest fixes clean detached checkouts of CodeNib
`a33bb13118e3a04f8d3d76eabcfb2602f785477a` and HTTPie
`2105caa49bae87c5809c274e407619a0de2639d1`, their canonical remotes, and
ordered selected-source receipts. The four cells combine those subjects with
`continuity_l2_exclusive_unsplit` and
`bm25_v8_l2_exclusive_headers_300`. Both configurations use Python, repository
filter policy v3, excluded tests, strict processing, non-skeleton depth 2, and
L2-exclusive chunks; the BM25 cell additionally enables headers/epilogues and
the builder-schema-v8 300-line cap.

Candidate worker counts are exactly 1, 2, and 4. Each arm receives four
warmups and 20 measured fresh-process samples in alternating paired AB/BA
order, with symmetric GC treatment and uncontrolled filesystem page cache.
The clock spans cold construction, repository discovery/filtering and
minified-source inspection, reads, parser/worker setup, binding and native
batch work, ordered merge, decode, complete chunk construction, and node
materialization. Controller artifact, contract, and subject-receipt checks are
outside the front of the stopwatch; the candidate's runtime contract safety
check remains inside. Parity, backend checks, stage aggregation, receipt
observation, and report serialization are outside its end.

Every warmup and measured pair requires exact ordered parity for all seven
`CodeChunk` fields. Canonical node parity compares sorted unique symbolic IDs
because raw `CodeChunker.nodes` ordering is unstable across fresh processes;
chunk ordering is never normalized. Wall gates use median p50 and nearest-rank
p95, each requiring a candidate result no greater than 80% of established;
nearest-rank p95 absolute peak RSS may be no greater than 125%. Backend
identity, one batch call, zero fallback, unique PIDs, immutable clean receipts,
and the canonical protocol are also hard gates. A single worker count must
pass every cell, with the smallest qualifying count selected; per-cell tuning
or threshold averaging is forbidden.

#600 supplied the fixed adapter only long enough to run the frozen formal
four-cell gate. The measured outcome follows. On 2026-08-24 the completed
controller, subject manifest, and dedicated tests were retired together with
the already-removed candidate; the recorded report remains sufficient to
preserve the decision.

M4 implementation and gate outcome
([#600](https://github.com/sysevol-ai/CodeNib/issues/600)): the private
candidate accepted one ordered repository source sequence, released the GIL
around one bounded native batch, used worker-private parser/tree/result state,
and merged file/span buffers by original input ordinal. Python retained
repository filtering, UTF-8-with-replacement decoding, strict/skip behavior,
final `CodeChunk` construction, headers/epilogues, splitting, and node IDs.
The ABI, overflow/resource caps, malformed buffers, deterministic concurrency,
whole-repository fallback, `MemoryError`, and worker 1/2/4 exact parity tests
all passed before the formal run.

The authoritative `python_repository_chunk_successor_gate_v1` run on August
12, 2026 used experimental implementation commit
`8e922a3d9ae2132787f402e81aeafe930d84135c`. Each arm received four warmups
and 20 measured fresh-process samples for every worker/subject/configuration
cell: 576 samples and 576 unique PIDs in total, with 144 AB and 144 BA pairs.
The stopwatch and two configurations remained exactly as frozen by #599.
Linux peak RSS came from the process-scoped `/proc/self/status` `VmHWM` value;
an earlier run using inherited `getrusage()` high-water state was superseded
and is not promotion evidence. Filesystem page cache remained uncontrolled.

| Workers | Subject | Configuration | Legacy p50 / p95 | Candidate p50 / p95 | p50 / p95 improvement | RSS p95 legacy / candidate (ratio) | Cell |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- |
| 1 | CodeNib | BM25 | 0.814943s / 0.882297s | 0.970822s / 1.010272s | -19.128% / -14.505% | 51.000 / 64.270 MiB (1.2602x) | reject |
| 1 | CodeNib | continuity | 0.697553s / 0.712019s | 0.801416s / 0.822192s | -14.890% / -15.473% | 50.000 / 63.273 MiB (1.2655x) | reject |
| 1 | HTTPie | BM25 | 0.087597s / 0.110658s | 0.108079s / 0.144070s | -23.381% / -30.193% | 36.500 / 39.500 MiB (1.0822x) | reject |
| 1 | HTTPie | continuity | 0.088109s / 0.151746s | 0.104742s / 0.165491s | -18.878% / -9.058% | 36.000 / 39.500 MiB (1.0972x) | reject |
| 2 | CodeNib | BM25 | 0.702405s / 0.787032s | 0.473816s / 0.591675s | +32.544% / +24.822% | 51.000 / 66.438 MiB (1.3027x) | reject |
| 2 | CodeNib | continuity | 0.860423s / 0.872750s | 0.565708s / 0.608869s | +34.252% / +30.236% | 50.000 / 65.840 MiB (1.3168x) | reject |
| 2 | HTTPie | BM25 | 0.081361s / 0.084126s | 0.075882s / 0.082794s | +6.735% / +1.583% | 36.500 / 40.000 MiB (1.0959x) | reject |
| 2 | HTTPie | continuity | 0.080706s / 0.088504s | 0.078726s / 0.079568s | +2.453% / +10.097% | 36.000 / 39.500 MiB (1.0972x) | reject |
| 4 | CodeNib | BM25 | 0.703526s / 1.601690s | 0.330657s / 0.675341s | +53.000% / +57.836% | 51.000 / 69.457 MiB (1.3619x) | reject |
| 4 | CodeNib | continuity | 1.199357s / 1.598776s | 0.448330s / 0.670158s | +62.619% / +58.083% | 50.000 / 68.973 MiB (1.3795x) | reject |
| 4 | HTTPie | BM25 | 0.081310s / 0.081943s | 0.067394s / 0.071486s | +17.115% / +12.761% | 36.500 / 39.500 MiB (1.0822x) | reject |
| 4 | HTTPie | continuity | 0.157862s / 0.159411s | 0.114359s / 0.117342s | +27.557% / +26.390% | 36.000 / 39.500 MiB (1.0972x) | pass |

Negative improvement denotes a regression. All 288 pairs preserved the
complete ordered seven-field `CodeChunk` digest and the canonical
sorted-unique symbolic-node digest. All 288 candidate samples used backend
`native-repository-batch-poc`, one batch call, zero fallbacks, the requested
worker count, and valid contract/counter/stage telemetry. Subject, source,
benchmark, harness, adapter, binary, and contract receipts were clean, stable,
and unchanged. The gate completed as `status=rejected` with `failure=null`; it
was not a harness failure.

The durable identities are:

- benchmark commit
  `8e922a3d9ae2132787f402e81aeafe930d84135c`; harness SHA-256
  `fd4d083ccb7aefd3ecef12d0db5220d4f95c9c803128f441d95d63e94269ecd7`;
  subject-manifest SHA-256
  `d40a35aedc1afcc9980cf414f20a9d1b196e2be615bd44f665020ad6333e16a6`;
- candidate adapter SHA-256
  `795867839a0d3ad9927a68dead728c4b140b3f9d2e3aa564336262b28a460be5`;
  native binary SHA-256
  `6a0d14f8366a426ebc39b316849b7f1deb6c7d000befe09663a8559bf8405cd9`;
  contract SHA-256
  `7e46684ba7b0733bfdb164d44bc664a68c06befc45684641e7ea35103c198849`;
- CodeNib `a33bb13118e3a04f8d3d76eabcfb2602f785477a`: 520 selected
  files / 6,630,346 bytes; continuity source SHA-256
  `65e2ba3a8106cff5ba39a568d3bcad5f279f54f4aaabcf5cba5167d69cc21b09`;
  BM25 source SHA-256
  `2b3e1d607d9627d928b51b09af93d1cd5a02b224fca10ffd4840ba64f3bf7e29`;
  5,620 / 6,403 chunks and 5,619 canonical nodes;
- HTTPie `2105caa49bae87c5809c274e407619a0de2639d1`: 89 selected
  files / 338,976 bytes; continuity source SHA-256
  `d5feafe737d7d3bdfdb54b92883168fe32a255886fbb8fccbc140f6a43aa12a9`;
  BM25 source SHA-256
  `645911e349ec6bda45cb8a34688de74e0b3833a216a7dc29f09ed4a358840192`;
  498 / 595 chunks and 494 canonical nodes;
- final 8,596,802-byte raw report SHA-256
  `e580b29b5eb3e5c5373eb5b90bd107c7e5f7b6dfbaf326b64f941952ed9f01a4`.

Only worker 4's HTTPie continuity cell passed all three numeric gates. Worker
2 cleared both time gates for CodeNib but exceeded the 1.25x RSS limit; worker
4 cleared both CodeNib time gates but also exceeded that RSS limit, while its
HTTPie BM25 improvements remained below 20%. The global selection therefore
returned `qualifying_worker_counts=[]` and `selected_worker_count=null`.
Per-cell selection and averaging are forbidden, so no successor qualifies.

In accordance with #600's outcome rule, the private Python adapter, C++
implementation and tests, pybind entry points, CMake option, Make targets, CI
configuration, and runtime environment switch were removed from the merge
surface. On 2026-08-24, the now-closed #599 controller, subject manifest, and
dedicated tests were also retired. Its exact contract, process-scoped RSS and
failure-status decisions, cell results, and report receipt remain in this
roadmap. Production chunking remains unchanged and no integration issue was
opened. Milestone 3 completed with no consumer-boundary backend promoted;
future warm-session or incremental-reuse hypotheses require a new focused
issue and gate rather than reopening either rejected native design.

## PR And Issue Flow

For this roadmap, a PR is merge-ready only when its own gate is complete and
the follow-up work is recorded here or in an issue. Slow CI does not need to
block implementation progress, but final merge decisions must account for the
latest check state, local validation, and any known flaky runner behavior.

Issue triage rules:

- Close an issue only when the code, tests, and docs satisfy the issue's
  acceptance criteria.
- If an issue is only partially handled, update the issue with the remaining
  roadmap phase instead of closing it.
- If a PR changes a language state, update `docs/language_capabilities.md`,
  this roadmap, and any related issue in the same PR.
- If a PR changes graph schema or decoder semantics, run parity checks and
  inspect whether `_SCHEMA_VERSION` must change.

Current issue triage notes through August 12, 2026:

- PR #248 is merged (June 25, 2026). It was an agent-compile/Qwen local-backend
  PR outside this SCIP roadmap and landed as agent-compile work, not as a
  roadmap phase.
- #198 is closed. It no longer has open roadmap action for the multi-language
  SCIP cold-start and acceleration program.
- #252, the Repository Guardian RFC, is closed (July 7, 2026).
- #199 is an enterprise index-storage RFC and remains open.
- #545 is closed; its native query-acceleration foundation and production
  gates are complete.
- #565 is closed with FactBatchBuffer retained only as a default-off negative
  promotion experiment.
- #556 and #557 are closed after the RepoNavigator adapter and bounded MCP
  explore/session ledger merged.
- #558 is closed with exact parity but a negative per-file native chunking
  result; repository batching was evaluated separately and rejected by
  #599/#600.
- #598 is resolved with exact parity and safety but negative Python and Rust
  consumer performance gates. No language was promoted; the subsequent #599
  and #600 repository-chunk gates are now also decided.
- #599 is closed; its gate contract and exact receipts remain in this roadmap,
  while its executable controller, manifest, and dedicated tests are retired.
- #600 completed as a measured negative result. Its private batch POC was
  removed after no global worker count passed every fixed cell; no production
  integration follow-up was opened.
- #601 completes when this recorded negative outcome is merged and the exact
  main verification chain is green. The ordered consumer-boundary program then
  has zero promoted backends.
- #133 is closed. Its query-time skill-selection runtime landed through #149
  and the subsequent agent-runtime refactor; the original fitted A0--A6 table
  plan was retired in favor of the design-space cost study. Any new adaptive
  policy should start from the current benchmark in a focused follow-up.

## Required Local Checks

Use targeted checks while implementing, then widen before merge:

```bash
make bootstrap-ubuntu
make toolchain-doctor
make zoekt-tool
make scip-cold-start-tools
make lsp-smoke-tools
make scip-cold-start-smoke
make lsp-smoke
make multilang-registry-check
make scip-project-smoke \
  PROJECT_LANGUAGE=java \
  PROJECT_ROOT=${CODENIB_TEMP_DIR}/real-java/maven-simple \
  SCIP_PROJECT_OUTPUT_DIR=${CODENIB_TEMP_DIR}/java-real-scip \
  SCIP_PROJECT_EXTRA_ARGS="--expected-symbol App --expected-symbol 'App.greet(String)()' --expected-symbol 'App.main(String[])()'"
make lsp-project-smoke \
  PROJECT_LANGUAGE=java \
  PROJECT_ROOT=${CODENIB_TEMP_DIR}/real-java/maven-simple \
  LSP_PROJECT_OUTPUT_DIR=${CODENIB_TEMP_DIR}/java-real-lsp \
  LSP_PROJECT_EXTRA_ARGS="--expected-symbol App --expected-symbol 'App.greet(String)()' --expected-symbol 'App.main(String[])()' --reference-languages java --min-references java=1"
make graph-route-alignment \
  PROJECT_LANGUAGE=java \
  PROJECT_ROOT=${CODENIB_TEMP_DIR}/real-java/maven-simple \
  GRAPH_ALIGNMENT_REFERENCE_ROUTE=active \
  GRAPH_ALIGNMENT_CANDIDATE_ROUTE=lsp \
  GRAPH_ALIGNMENT_OUTPUT_DIR=${CODENIB_TEMP_DIR}/java-active-route-alignment \
  GRAPH_ALIGNMENT_EXTRA_ARGS=--candidate-include-references
make graph-route-alignment \
  PROJECT_LANGUAGE=csharp \
  PROJECT_ROOT=${CODENIB_TEMP_DIR}/real-csharp/samples/core/console-apps/HelloMsBuild \
  GRAPH_ALIGNMENT_REFERENCE_ROUTE=active \
  GRAPH_ALIGNMENT_CANDIDATE_ROUTE=lsp \
  GRAPH_ALIGNMENT_OUTPUT_DIR=${CODENIB_TEMP_DIR}/csharp-active-route-alignment \
  GRAPH_ALIGNMENT_EXTRA_ARGS=--candidate-include-references
python -m mkdocs build --strict
```

When touching SCIP decoders or C++ acceleration, add the relevant decoder,
profile, and parity commands from `docs/core_cpp.md` and
`docs/experiments/lsp_core_acceleration.md`.

The provider-level acceleration gate now has a complete CodeNib Base run over
100 unique snapshots and five languages. All artifact quality checks and live
warmup gates passed. Of 1,000 deterministic native requests, 632 were
agent-visible equivalent; the admitted measured rows had 0.62 ms static p50,
2.30 ms live JSON-RPC p50, and 4.71x paired speedup p50. Definition coverage was
87.4%; references remained lower at 39.0%, so promotion stays
language/capability guarded with explicit live fallback. C/C++ replay must bind
clangd to each artifact profile's compilation database and use a ten-second
idle grace. Reviewable aggregate artifacts are committed under
`docs/experiments/artifacts/lsp_replay_base_v3_100/`; the full per-snapshot report
set remains under `${CODENIB_RESULTS_DIR}/lsp_replay_base_v3_100/`. Full
protocol details are in `docs/experiments/lsp_core_acceleration.md`.
