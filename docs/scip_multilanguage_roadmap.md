<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# SCIP Multi-Language Roadmap

This is the long-running goal for CodeMiner's multi-language graph indexing and
acceleration work. Keep it current when a PR lands, when a candidate backend is
promoted, or when an issue is closed because the roadmap gate is satisfied.

The program is not complete until the candidate SCIP cold-start languages have
validated implementation paths, existing LSP graph behavior is preserved, and
the C++ acceleration surface is justified by measured bottlenecks instead of
guesswork.

## Current Baseline

The language registry records SCIP cold-start state explicitly:

| State | Languages | Meaning |
| --- | --- | --- |
| `active` | Python, Go, Rust, C#, Java, Kotlin, Ruby, Scala, JavaScript, TypeScript, PHP | Routed through existing SCIP cold-start paths. Ruby and PHP use hybrid active routes: prepared Bundler/Composer projects prefer SCIP and loose or unprepared projects fall back to LSP. Kotlin and Scala are active through `scip-java`; Scala is limited to Scala 2.x Gradle/SBT projects without a registered LSP baseline. |
| `candidate` | none | No candidate SCIP backend is currently waiting on promotion gates. |
| `none` | C++, Swift, Lua | No accepted SCIP cold-start plan in CodeMiner today. C++ uses clangd-style graph indexing. |

Generic LSP graph support for Java, C#, Kotlin, Ruby, and PHP remains available
through `graph_route="lsp"` for regression checks and for loose-file fallback.
Scala has no registered LSP graph route today; its active graph support is the
`scip-java` route only.

## Layered Goal

Complete the multi-language SCIP cold-start and acceleration program end to
end:

1. Preserve the current LSP graph/index behavior and public language capability
   matrix.
2. Promote candidate SCIP cold-start backends only after gated smoke, decode,
   backend-alignment, and documentation work.
3. Keep active SCIP backends for Python, Go, Rust, C#, Java, Ruby,
   JavaScript, TypeScript, and PHP fast and parity-tested where a C++ core
   decoder exists.
4. Add C++ acceleration only where profiling shows local decode or graph
   processing is a meaningful bottleneck.
5. Keep PRs, commits, and issues synchronized with this roadmap so multi-step
   work does not fragment into disconnected partial goals.

## Promotion Gates

A candidate SCIP language can become `active` only when all of these are true:

- Tool discovery works through `scip_cold_start_command_for_language()` and any
  `CODEMINER_*_SCIP_CMD` override.
- A minimal real-project smoke test can produce `index.scip`, decode it, and
  write a CodeGraph.
- The serial Python decoder maps symbols, files, ranges, definition nodes,
  containment edges, and reference anchors into the existing CodeGraph schema.
- Backend alignment against the existing LSP graph is measured where an LSP
  backend exists. Any accepted reference/call-edge tolerance is documented.
- `docs/language_capabilities.md` and `codeminer/languages.py` agree.
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

## Work Queue

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
PROJECT_LANGUAGE=java PROJECT_ROOT=/tmp/codeminer-real-java/maven-simple
GRAPH_ALIGNMENT_OUTPUT_DIR=/tmp/codeminer-java-route-alignment
GRAPH_ALIGNMENT_EXTRA_ARGS=--reference-include-references` builds both routes
from the registry and writes isolated artifacts under
`/tmp/codeminer-java-route-alignment/maven-simple-java/`. The current JDT LS
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
`CODEMINER_SCIP_TOOLS_DIR`. The command
`scripts/smoke_scip_cold_start.py --languages scala --project-root
/tmp/codeminer-real-scala-sbtio --output-dir /tmp/codeminer-scala-sbtio-smoke
--expected-symbol IO --expected-symbol Path --json` produced `index.scip`,
`index.decoded`, and `graph.pkl` with 3,571 vertices, 18,611 edges, and 15,995
reference edges. `scip-java` indexing took about 74.527s, protoc decode took
about 0.099s, Python graph decode took about 2.156s, and range-index
construction took about 0.069s. There is no registered Scala LSP route in
CodeMiner, so promotion uses generated Scala 2.13 smoke plus the real SBT smoke
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
PROJECT_ROOT=/tmp/codeminer-real-csharp/samples/core/console-apps/HelloMsBuild
GRAPH_ALIGNMENT_OUTPUT_DIR=/tmp/codeminer-csharp-route-alignment
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
  to the current LSP path for CodeMiner use cases, or if accepted tolerances are
  documented.

Exit condition: Ruby and PHP active hybrid states reflect measured
graph quality, not tool existence alone.

Current Ruby active hybrid status: the registry routes Ruby through
`RubyHybridIndexer`. Explicit overlay bundles selected with
`CODEMINER_RUBY_BUNDLE_GEMFILE` or `BUNDLE_GEMFILE`, and project Gemfiles that
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
`.codeminer/Gemfile` overlay selected through `CODEMINER_RUBY_BUNDLE_GEMFILE`;
the overlay uses `gemspec path: ".."` and adds pinned `ruby-lsp` 0.26.9 plus
`scip-ruby` 0.4.7. The generic LSP client now maps that overlay variable into
`BUNDLE_GEMFILE` for Ruby and removes `GEM_PATH`, matching the Ruby SCIP
indexer environment so ruby-lsp does not accidentally resolve the target
project's own Gemfile. Route alignment on `lib/` with `vendor/**` and
`.codeminer/**` excluded now runs end-to-end with one accepted source-modeling
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
can be overridden with `CODEMINER_PHP_COMPOSER_IMAGE`. The pure SCIP route
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
--output-dir /tmp/codeminer-php-normalize-smoke --json` produced 8 vertices, 9
edges, 2 references, and no missing expected symbols. `scripts/check_graph_route_alignment.py
--project-root /tmp/codeminer-php-normalize-smoke/php --language php
--reference-route lsp --candidate-route scip-candidate --target-dir src
--exclude-pattern 'vendor/**' --output-dir /tmp/codeminer-php-normalize-alignment
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
`/tmp/codeminer-real-php-container`. The pure SCIP route copies that
checkout to an output-local worktree before indexing, and the source checkout
does not receive a new `index.scip` artifact. Source-only route alignment on
`src/` with `vendor/**` excluded is strict-green:
`scripts/check_graph_route_alignment.py --project-root
/tmp/codeminer-real-php-container --language php --reference-route lsp
--candidate-route scip-candidate --target-dir src --exclude-pattern 'vendor/**'
--output-dir /tmp/codeminer-php-container-alignment-noninvasive --json --clean`
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
  PROJECT_ROOT=/tmp/codeminer-real-php-container \
  GRAPH_ALIGNMENT_REFERENCE_ROUTE=active \
  GRAPH_ALIGNMENT_CANDIDATE_ROUTE=lsp \
  GRAPH_ALIGNMENT_TARGET_DIR=src \
  GRAPH_ALIGNMENT_EXCLUDE_PATTERNS='vendor/**' \
  GRAPH_ALIGNMENT_OUTPUT_DIR=/tmp/codeminer-php-active-vs-lsp-alignment \
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
- [x] Record speedup and parity in `docs/core_cpp.md` or an experiment doc.

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
19-instance `codeminer-base` calibration found one false-success artifact:
`micropython__micropython-10095` had only 2 compile commands and a graph at
12.6% of baseline vertices. The corrected `ports/unix` capture produced 286
compile commands and a graph with 9,151 vertices and 72,207 edges. clangd and
decode were not the dominant cold-start costs; repository preparation,
submodules, CMake, Bear, and Make remain the optimization surface. Bear
candidate selection and warning-as-error normalization therefore belong in the
Python indexing harness, not in the C++ decoder layer.

Python SCIP decoders now share descriptor extraction, descriptor suffix
normalization, and `unified_name` formatting helpers in
`codeminer.scip_interface.scip_decode_utils` where symbol shapes are compatible.
Language-specific handling, such as Ruby singleton-class descriptors and Kotlin
synthetic symbol filters, stays in the owning decoder.

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

Current issue triage notes from June 20, 2026:

- PR #248 remains open and is outside this SCIP roadmap. It is an
  agent-compile/Qwen backend PR with green CodeQL checks; its `unit` job is
  still queued on the offline self-hosted runner, so it should be evaluated as
  agent-compile work rather than merged as part of this roadmap.
- #198 is closed. It no longer has open roadmap action for the multi-language
  SCIP cold-start and acceleration program.
- #252 is a Repository Guardian RFC and remains open.
- #199 is an enterprise index-storage RFC and remains open.
- #133 is an agent-compile RFC and remains open because the query-time
  skill-selection mechanism is still tracked as follow-up work.

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
  PROJECT_ROOT=/tmp/codeminer-real-java/maven-simple \
  SCIP_PROJECT_OUTPUT_DIR=/tmp/codeminer-java-real-scip \
  SCIP_PROJECT_EXTRA_ARGS="--expected-symbol App --expected-symbol 'App.greet(String)()' --expected-symbol 'App.main(String[])()'"
make lsp-project-smoke \
  PROJECT_LANGUAGE=java \
  PROJECT_ROOT=/tmp/codeminer-real-java/maven-simple \
  LSP_PROJECT_OUTPUT_DIR=/tmp/codeminer-java-real-lsp \
  LSP_PROJECT_EXTRA_ARGS="--expected-symbol App --expected-symbol 'App.greet(String)()' --expected-symbol 'App.main(String[])()' --reference-languages java --min-references java=1"
make graph-route-alignment \
  PROJECT_LANGUAGE=java \
  PROJECT_ROOT=/tmp/codeminer-real-java/maven-simple \
  GRAPH_ALIGNMENT_REFERENCE_ROUTE=active \
  GRAPH_ALIGNMENT_CANDIDATE_ROUTE=lsp \
  GRAPH_ALIGNMENT_OUTPUT_DIR=/tmp/codeminer-java-active-route-alignment \
  GRAPH_ALIGNMENT_EXTRA_ARGS=--candidate-include-references
make graph-route-alignment \
  PROJECT_LANGUAGE=csharp \
  PROJECT_ROOT=/tmp/codeminer-real-csharp/samples/core/console-apps/HelloMsBuild \
  GRAPH_ALIGNMENT_REFERENCE_ROUTE=active \
  GRAPH_ALIGNMENT_CANDIDATE_ROUTE=lsp \
  GRAPH_ALIGNMENT_OUTPUT_DIR=/tmp/codeminer-csharp-active-route-alignment \
  GRAPH_ALIGNMENT_EXTRA_ARGS=--candidate-include-references
python -m mkdocs build --strict
```

When touching SCIP decoders or C++ acceleration, add the relevant decoder,
profile, and parity commands from `docs/core_cpp.md` and
`docs/experiments/lsp_core_acceleration.md`.
