<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Contributing a Language

CodeNib language support is deliberately split into layers. A language can
ship with chunking and retrieval first, then add graph indexing, incremental
patching, and optional C++ acceleration later. This guide keeps those steps
explicit so new language work does not become a scattered edit across chunkers,
routers, decoders, agent compile, and tests.

The central entry point is `codeminer/languages.py`. Add or update a
`LanguageSpec` there first, then wire only the layers the language actually
supports.

## Current Layers

| Layer | Purpose | Current integration point |
| --- | --- | --- |
| Language registry | Single declarative metadata record | `codeminer/languages.py::LanguageSpec` |
| Chunking | Tree-sitter chunks for retrieval, GT, and vector indexes | `codeminer/code_chunking/` and `codeminer/code_chunker.py` |
| Cold-start graph | Whole-repo symbol graph build | `codeminer/ls_router.py`, `codeminer/scip_interface/`, `codeminer/ls_index/` |
| Incremental graph | In-place graph patching after a diff | `codeminer/graph/incremental/` |
| Agent compile | Query-time language scenario normalization | `codeminer/agent/compile.py` |
| Dataset / GT | Extension-to-language mapping for patch analysis | `codeminer/dataset/gt_locate.py` |
| Core acceleration | Optional C++ mirror decoder | `core/`, `codeminer/scip_interface/scip_decode_core.py` |

## Backend Policy

Choose backends per language, not globally.

- `cold_start_backend`: the source for a fresh full-repo graph. Use SCIP where
  the indexer is mature, clangd for C/C++, and generic LSP only when it is the
  best available cold-start source.
- `scip_cold_start`: records active or candidate SCIP cold-start tools even
  when the language currently uses LSP or tree-sitter-only graph support. A
  `candidate` is planning metadata; it is not routed by `LSIndexer` until SCIP
  smoke, backend alignment, decoder support, and parity gates are green.
- `scip_candidate_indexer`: an explicit opt-in candidate SCIP indexer path.
  Use it from smoke/profiling flows through `graph_route="scip-candidate"`;
  do not replace the active graph route until promotion gates are satisfied.
- `incremental_backend`: the source for patching or filling gaps after a repo
  changes. LSP is often a better fit here because servers already maintain
  workspace state.
- `core_decoder`: optional acceleration. New languages may ship serial-only.
  If a language sets `core_decoder=True`, it must have serial/core parity tests.

Do not assume every LSP server exposes a clangd-style on-disk index. Most
generic LSP work means driving JSON-RPC methods such as
`textDocument/documentSymbol`, `textDocument/definition`,
`textDocument/references`, `callHierarchy/*`, and then normalizing the result
into the CodeGraph schema.

## Step 1: Register Metadata

Add a `LanguageSpec` in `codeminer/languages.py`.

```python
LanguageSpec(
    key="example",
    display_name="Example",
    aliases=("ex",),
    chunker_language="example",
    chunker_aliases=("example", "ex"),
    chunk_extensions=(".ex",),
    gt_language="example",
    gt_extensions=(".ex",),
    graph_language="example",
    graph_aliases=("example", "ex"),
    graph_extensions=(".ex",),
    agent_languages=("example",),
    agent_aliases=(("example", "example"), ("ex", "example")),
    cold_start_backend="lsp",
    scip_cold_start=ScipColdStartOption(
        tool="example-scip",
        status="candidate",
        command=("example-scip", "index"),
        command_env="CODEMINER_EXAMPLE_SCIP_CMD",
        note="Promote only after SCIP smoke, backend alignment, and parity gates pass.",
    ),
    scip_candidate_indexer=(
        "codeminer.scip_interface.scip_indexer_example:SCIPExampleIndexer"
    ),
    incremental_backend="lsp",
    lsp_language_id="example",
    lsp_command=("example-language-server", "--stdio"),
    lsp_command_env="CODEMINER_EXAMPLE_LSP_CMD",
    core_decoder=False,
)
```

Keep surface-specific differences explicit. Existing C/C++ is the model:
repository chunking does not accept raw `c`, graph routing maps `c` to `cpp`,
and agent compile keeps `c` as its own scenario key.

Update or add tests in `test/test_languages.py` whenever a new alias,
extension, backend, or parity status is added.

You can start from generated TODO stubs:

```bash
python scripts/scaffold_language.py java \
  --display-name Java \
  --extension .java \
  --alias jvm \
  --graph-backend lsp \
  --incremental-backend lsp
```

The scaffold is dry-run by default. Add `--write` after reviewing the
`LanguageSpec` snippet and planned files. Generated files are intentionally not
registered in routers yet; fill in the implementation and tests before wiring
the language into `create_chunker()`, `LSIndexer`, `GraphPatcher`, or core
bindings. For `--graph-backend lsp`, the scaffold points the registry at the
shared `GenericLSPIndexer` / `GenericLSPGraphDecoder` and does not generate
per-language indexer/decoder files unless a server-specific backend is needed.

## Step 2: Add Tree-Sitter Chunking

Add a chunker when the language should support retrieval or GT extraction.

1. Create `codeminer/code_chunking/{lang}_chunker.py`.
2. Export the chunker from `codeminer/code_chunking/__init__.py`.
3. Extend `create_chunker()` to instantiate the chunker from the registry
   normalized language key.
4. Add repository-level tests under `test/chunker/`.

Chunking-only support is valid. In that state:

- `chunker_language` and `chunk_extensions` should be set.
- `graph_language`, graph backends, and patchers can remain unset.
- User-facing code should advertise retrieval support, not full graph support.

## Step 3: Add Cold-Start Graph Support

Pick one cold-start backend:

- SCIP backend: add `codeminer/scip_interface/scip_indexer_{lang}.py` and
  `codeminer/scip_interface/scip_decode_{lang}.py`.
- clangd-style backend: add an `ls_index/` indexer/decoder only if the server
  exposes a stable artifact like clangd `.idx`.
- Generic LSP backend: use a shared LSP driver once it exists, with
  per-language server command, root markers, capabilities, and normalization
  rules coming from the language registry.
- Generic LSP graph backend: set `graph_indexer` to
  `codeminer.ls_index.lsp_indexer:GenericLSPIndexer`, `graph_decoder` to
  `codeminer.ls_index.lsp_graph_decode:GenericLSPGraphDecoder`, and register
  `lsp_language_id`, `lsp_command`, and an optional `lsp_command_env` override.

Do not drop a known SCIP cold-start path just because the first implementation
uses LSP. Record it as `scip_cold_start=ScipColdStartOption(...,
status="candidate")` and `scip_candidate_indexer="module:Class"` until it is
proven.
Use `codeminer.languages.scip_cold_start_command_for_language()` to consume the
registered command and any `CODEMINER_*_SCIP_CMD` override in smoke scripts.
Use `LSIndexer(..., graph_route="scip-candidate")` or
`build_graph_for_languages(..., graph_route="scip-candidate")` only in explicit
candidate evaluation flows; the default route must remain the active LSP/SCIP
backend. Use `graph_route="lsp"` for backend comparison and regression checks
when a language has an LSP command, especially before promoting a SCIP
candidate over an existing LSP graph route.

Remaining SCIP cold-start candidates that should be evaluated before treating
LSP or tree-sitter-only as final:

| Language | Candidate | Current active graph | Promotion gate |
| --- | --- | --- | --- |
| Kotlin | `scip-java index` | generic LSP / Kotlin LS | JVM smoke, LSP alignment, Kotlin symbol normalization |

Promoted SCIP cold-start routes are still expected to keep their LSP baseline
reachable through `graph_route="lsp"`:

| Language | Active SCIP route | LSP regression route |
| --- | --- | --- |
| Java | `scip-java index` | generic LSP / JDT LS |
| C# | `scip-dotnet` | generic LSP / csharp-ls |
| Scala | `scip-java index` | none registered |
| PHP | `PHPHybridIndexer` prefers `scip-php` for Composer projects | generic LSP / Intelephense |

Current generic LSP cold-start graph commands are:

| Language | Command | Notes |
| --- | --- | --- |
| Java | `jdtls` | Eclipse JDT LS; Maven/Gradle-shaped projects provide better reference coverage than loose files. |
| C# | `csharp-ls` | Requires a .NET SDK. User-level installs under `~/.dotnet/tools` are auto-resolved. |
| Ruby | `ruby-lsp` | Requires Ruby headers/native extension support for the `prism` dependency. A user-level `mise` Ruby plus libyaml works without system Ruby headers. |
| PHP | `intelephense --stdio` | Install with npm; CodeNib uses stdio mode. |
| Kotlin | `kotlin-language-server --stdio` | The JetBrains standalone archive exposes `bin/intellij-server`; symlink or wrap it as `kotlin-language-server`. |

Swift and Lua currently ship as tree-sitter-only languages in the capability
matrix. Their graph backends should stay disabled until a real `sourcekit-lsp`
or LuaLS smoke test plus backend-alignment tolerance is landed for the target
server. Scala graph support is active through `scip-java`; no Metals/LSP route
is registered today.

Then update `LSIndexer` / `LSGraphDecoder` routing. The graph must conform to
the current CodeGraph contract:

- same vertex `type`, `file`, `start_line`, `end_line`, and `unified_name`
  conventions;
- same edge `type`, `anchor_file`, and `anchor_line` conventions;
- line bases handled deliberately (`CodeChunk` uses 0-based lines, graph query
  APIs use the CodeGraph conventions documented in `docs/graph_query.md`).

When two backends exist for the same language, compare them before trusting the
new backend's graph surface. Prefer the route-level helper so both graphs are
built from the same registry and Makefile-pinned tool environment:

```bash
make graph-route-alignment \
  PROJECT_LANGUAGE=java \
  PROJECT_ROOT=/path/to/project
```

The target runs `scripts/check_graph_route_alignment.py` through
`CODEMINER_TOOL_ENV`, defaulting to `graph_route="lsp"` as reference and
`graph_route="scip-candidate"` as candidate. Use
`GRAPH_ALIGNMENT_REFERENCE_ROUTE`, `GRAPH_ALIGNMENT_CANDIDATE_ROUTE`,
`GRAPH_ALIGNMENT_SKIP_LEVEL`, `GRAPH_ALIGNMENT_TARGET_DIR`,
`GRAPH_ALIGNMENT_EXCLUDE_PATTERNS`, and `GRAPH_ALIGNMENT_EXTRA_ARGS` to adjust a
real-repo gate. Pass
`GRAPH_ALIGNMENT_EXTRA_ARGS=--reference-include-references` when the reference
route is an LSP backend and the promotion gate should record reference-edge
counts in the same report. The lower-level `scripts/check_backend_alignment.py`
remains available when you already have two `graph.pkl` files.

The alignment harness compares definition symbols by `unified_name` and the
symbol containment hierarchy. It reports reference-edge counts but does not
require reference parity by default; server-specific reference/call edges need
explicit tolerances before they are treated as a blocking signal.

For a reproducible local toolchain, start with:

```bash
make bootstrap-ubuntu  # Ubuntu: system deps + Python dev deps + local toolchains
# or, when system packages are already present:
make bootstrap
make toolchain-doctor
```

The bootstrap targets install tools under `CODEMINER_SCIP_TOOLS_DIR`, defaulting
to `/tmp/codeminer-scip-tools`, instead of relying on global npm/go/gem/dotnet
state. `make multilang-tools` is the no-sudo toolchain subset used by the smoke
targets. It installs active SCIP/LSP tools for Python, Go, Rust, Java, C#,
Ruby, Scala, PHP, JavaScript, TypeScript, and C/C++ plus candidate SCIP/LSP
tools for Kotlin, plus Zoekt binaries used by MCP/search integration. The
TypeScript path also installs local `yarn` and `pnpm` wrappers for workspace
repositories. Use `make active-scip-env` to print the exact PATH, `GOBIN`,
`GOPATH`, `DOTNET_ROOT`, and gem environment needed by manual commands.

For generic LSP graph smoke on tiny generated projects, run:

```bash
make lsp-smoke-system-deps-ubuntu  # optional Ubuntu base packages
make lsp-smoke-tools
eval "$(
  make --no-print-directory lsp-smoke-env \
    | sed -n 's/^  export /export /p'
)"
python scripts/smoke_lsp_graph.py \
  --languages java csharp ruby php kotlin \
  --reference-languages java \
  --min-references java=1 \
  --json
```

Use `--skip-unavailable` for developer machines where a server cannot be
installed without system packages. Drop it in CI or release validation when the
toolchain image is expected to contain every listed language server. Add
`--output-dir /tmp/codeminer-lsp-smoke` when you need to keep generated projects
and `graph.pkl` files for `scripts/check_backend_alignment.py`.

For real-repository LSP baselines, run the same script with `--project-root`.
This path does not generate toy source files; it indexes the existing checkout
and can be paired with `scripts/smoke_scip_cold_start.py --project-root` for
candidate promotion gates or active-route regression checks:

```bash
make lsp-project-smoke \
  PROJECT_LANGUAGE=java \
  PROJECT_ROOT=/tmp/codeminer-real-java/maven-simple \
  LSP_PROJECT_OUTPUT_DIR=/tmp/codeminer-java-real-lsp \
  LSP_PROJECT_EXTRA_ARGS="--expected-symbol App --expected-symbol 'App.greet(String)()' --expected-symbol 'App.main(String[])()' --reference-languages java --min-references java=1"
```

Use `--target-dir` and repeated `--exclude-pattern` values when a real project
needs a source-only LSP baseline, for example PHP or Ruby projects with large
`vendor/` trees.

For a one-command real-repo promotion or regression gate, compare the LSP and
SCIP routes directly:

```bash
make graph-route-alignment \
  PROJECT_LANGUAGE=php \
  PROJECT_ROOT=/tmp/php-project \
  GRAPH_ALIGNMENT_TARGET_DIR=src \
  GRAPH_ALIGNMENT_EXCLUDE_PATTERNS="vendor/**"
```

`make lsp-smoke-tools` installs the LSP binaries used by the current alignment
surface under `CODEMINER_SCIP_TOOLS_DIR`: Eclipse JDT LS as `jdtls`, csharp-ls,
ruby-lsp, Intelephense, and a Kotlin LSP wrapper named
`kotlin-language-server`. The Makefile pins the downloaded package versions and
prints a PATH export through `make lsp-smoke-env`. Kotlin's official standalone
LSP distribution currently has a higher JDK requirement than the JDT LS path, so
Kotlin LSP smoke can still require a newer local JDK even after the wrapper is
installed. Use `CODEMINER_KOTLIN_LSP_CMD` if a CI image supplies Kotlin LSP
through a different launcher. Ruby LSP is run through a generated Bundler-shaped
project and the smoke temporarily starts it as `bundle exec ruby-lsp`; a bare
loose-file Ruby smoke is not a reliable alignment target. The Makefile installs
Ruby LSP as a wrapper before the raw gem bin on PATH so Bundler projects use
their local Gemfile while loose projects can still fall back to the tool-dir gem.

For SCIP cold-start smoke and candidate promotion gates, start with:

```bash
make scip-cold-start-system-deps-ubuntu  # optional Ubuntu base packages
make scip-cold-start-tools
eval "$(
  make --no-print-directory scip-cold-start-env \
    | sed -n 's/^  export /export /p'
)"
```

`make scip-cold-start-system-deps-ubuntu` installs the Ubuntu base packages for
the cold-start toolchain path, including a JDK package, Ruby headers, PHP/Composer
packages, `curl`, `git`, `gzip`, and `unzip`. Override `SCIP_JDK_PACKAGE` or
`SCIP_PHP_SYSTEM_PACKAGES` when the distribution uses different package names.
Use `make scip-jvm-compat-system-deps-ubuntu` when a compatibility probe needs
an older JDK such as OpenJDK 11 for legacy Gradle projects.

`make scip-cold-start-tools` installs reproducible local copies of active
Java/C#/Ruby/Scala SCIP tools and the Kotlin candidate toolchain under
`CODEMINER_SCIP_TOOLS_DIR`, defaulting to `/tmp/codeminer-scip-tools`. It
installs `scip-java`, Gradle, SBT, .NET SDK channels 8.0 and 10.0,
`scip-dotnet`, `csharp-ls`, Bundler, and `scip-ruby`. Ruby gem installation uses
`RUBY_GEM ?= gem`; if the default `/usr/bin/gem` lacks Ruby headers for native
extensions, first run `make scip-ruby-system-deps-ubuntu` or invoke
`make scip-cold-start-tools RUBY_GEM=/path/to/gem` with a mise/asdf/rbenv Ruby.
PHP is
intentionally handled separately because `scip-php` is a Composer dev dependency
in the target PHP repository: run `make scip-php-system-deps-ubuntu` for Ubuntu
PHP/Composer packages when sudo is available, then `make scip-php-tool` to verify
PHP >= 8.2 and print the project-local Composer commands. On machines without
sudo PHP packages, `make scip-php-docker-tool` verifies the `composer:2` Docker
image used by generated PHP smoke; override it with
`CODEMINER_PHP_COMPOSER_IMAGE`. The PHP active hybrid route prepares a
throwaway Composer worktree under its output directory before running
`vendor/bin/scip-php`, so normal route-alignment gates do not need to mutate the
target repository. Explicit `graph_route="scip-candidate"` keeps the same pure
SCIP behavior for smoke and profiling gates. If you intentionally want to
prewarm a disposable checkout, run
`make php-project-scip-tool PROJECT_ROOT=/path/to/php/repo`; this prepares
project-local `vendor/bin/scip-php` with local PHP/Composer when available, or
with the same Docker image otherwise. The older `make scip-candidates` and
`make scip-candidate-env` targets remain compatibility aliases.

For active SCIP/LSP backends, the Makefile also pins and installs `scip-python`,
`scip-typescript`, `scip-go`, `scip-clang`, `scip-java`, `scip-dotnet`,
rust-analyzer, `gopls`, `basedpyright-langserver`, `ty`,
`typescript-language-server`, and a clangd wrapper/symlink under the same tools
directory. Go-side tools use a pinned local Go SDK because current `scip-go` and
`gopls` require a newer Go toolchain than some Ubuntu images provide.

```bash
python scripts/smoke_scip_cold_start.py \
  --languages java kotlin scala csharp ruby php \
  --skip-unavailable \
  --output-dir /tmp/codeminer-scip-smoke \
  --json
```

The SCIP smoke runner uses the registry command from
`scip_cold_start_command_for_language()`, including `CODEMINER_*_SCIP_CMD`
overrides. For Java, it writes a small Maven project. For Kotlin, it writes a
small Gradle Kotlin/JVM project because a Maven-shaped Kotlin probe did not
produce `index.scip` or `.semanticdb` artifacts locally. For Scala, it writes a
small Gradle Scala 2.13 project; Scala 3 is not implied by that smoke. For C#,
it writes a small SDK-style `.csproj` and runs `scip-dotnet index <project>
--output <index.scip> --working-directory <root>`. For Ruby, it writes a small
Bundler gem project and runs `bundle exec scip-ruby --dir . --index-file
<index.scip> --gem-metadata smoke@0.1.0`. For PHP, it writes a small Composer
project, creates a git commit so `scip-php` can derive root package metadata,
installs the project-local `vendor/bin/scip-php`, and uses Docker `composer:2`
when local PHP/Composer are unavailable. These smokes expect `index.scip`, decode
`index.decoded`, and build `graph.pkl` through the registered SCIP decoder. A
skipped result means the command is not installed; it does not promote a
candidate or replace the active graph route.

`make scip-project-smoke PROJECT_LANGUAGE=<lang> PROJECT_ROOT=<repo>` installs
only the tool targets needed for that language before running the project smoke.
For example, Scala project smoke prepares `scip-java`, Gradle, and SBT without
installing unrelated .NET, Ruby, or PHP tooling.

The Java SCIP decoder intentionally skips scip-java `<init>` constructor
symbols until real-repo alignment decides how explicit constructors should map
against JDT LS. On the tiny Maven smoke, this keeps symbol and containment
alignment strict while leaving constructor reference-count differences as an
informational metric.

The Kotlin candidate path uses the same scip-java command but requires Gradle
or another build path that emits SemanticDB. Its decoder accepts `.kt`
documents, uses owner descriptors for member containment when scip-java omits
`enclosing_range`, and keeps top-level function display names stable. Kotlin
generated-smoke alignment against Kotlin LS should stay strict-green for symbols
and containment; real-repo alignment is still required before promotion.

The Scala active SCIP path also uses scip-java. Generated smoke proves a Gradle
Scala 2.13 project, and the real `sbt/io` gate proves an SBT Scala 2.x project
after `make scip-cold-start-tools` installs a pinned SBT launcher under
`CODEMINER_SCIP_TOOLS_DIR`. The shared JVM decoder accepts `.scala` documents
and treats Scala object members as contained by their object symbol. No
Metals/LSP route is registered for Scala today, so use generated smoke plus
real-project SCIP smoke as the active-route gate. Scala 3 remains unproven until
a dedicated Scala 3 smoke passes.

The C# active SCIP path uses `scip-dotnet` and requires a .NET SDK that can restore
the target `.csproj` or `.sln`. Its decoder accepts `.cs` documents, skips
generated `bin/` and `obj/` files, preserves namespace symbols, and normalizes
display names to match csharp-ls. Local csharp-ls alignment may require a newer
.NET runtime than `scip-dotnet` itself; in the current validation,
`scip-dotnet` worked with .NET 8 while csharp-ls required .NET 10. C# remains
serial-only until profiling shows Python decode/build time is a material
fraction of cold-start time.

The Ruby active hybrid SCIP path uses `bundle exec scip-ruby` because the native
`scip-ruby` binary rejects direct invocation outside Bundler. Its decoder
accepts `.rb` documents, normalizes Ruby descriptors such as
`Smoke#Invoice#total().`, and maps singleton class descriptors for
`<Class:Smoke>#normalize()` to `Smoke.normalize()`. It keeps top-level Ruby
class/module reopen definitions file-scoped internally so repeated declarations
such as per-file `module Rake` do not collapse into one graph vertex, and it
normalizes source-declared `attr_writer`/`attr_accessor` generated writer
definitions to the ruby-lsp method display without changing explicit `def foo=`
setters. Generated ruby-lsp alignment now runs on a matching Bundler-shaped
project, preserves Ruby module parents in the generic LSP decoder, normalizes
constructors, instance variables, attr-style methods, `class << self`, and `::`
names, and is strict-green against the SCIP route for symbols and containment
(`4/4`, no missing or extra definitions). The Ruby SCIP route unsets
`GEM_PATH` for Bundler commands and filters generated graphs by `target_dir` and
`exclude_patterns`, so dependency gems under `vendor/bundle` do not pollute
source-route alignment. For real repositories that should not have their Gemfile
mutated, create an overlay such as `.codeminer/Gemfile` with `gemspec path: ".."`
plus pinned `ruby-lsp` and `scip-ruby`, then export
`CODEMINER_RUBY_BUNDLE_GEMFILE=/path/to/repo/.codeminer/Gemfile` before running
LSP or SCIP route gates. The current `ruby/rake` gate promotes Ruby as an active
hybrid route with one accepted anonymous-module receiver modeling tolerance; all
other source definitions and containment align, and scip-ruby contributes
material source references. Ruby is also covered by the C++ core decoder: on the
same `ruby/rake` decoded index, serial/core parity after `lib/` filtering is
exact with 815 nodes and 3,466 edges, and local `process_index` time drops from
7.58s serial to 1.04s through the C++ backend.
Use `make ruby-project-bundle PROJECT_ROOT=/path/to/ruby/repo` for this overlay
setup. It creates `.codeminer/Gemfile` only when absent, installs the bundle
under `.codeminer/vendor/bundle`, and prints the
`CODEMINER_RUBY_BUNDLE_GEMFILE` export consumed by the Ruby LSP and SCIP routes.

The PHP active hybrid path uses project-local `vendor/bin/scip-php` pinned to
the validated `davidrjenni/scip-php:0.0.2` package for Composer projects and
generated smoke. Loose files and non-Composer projects use the generic
Intelephense LSP route, and the active route falls back to LSP if SCIP setup or
indexing fails. Explicit `graph_route="scip-candidate"` still runs the pure
SCIP route for smoke, route alignment, and profiling.

The community indexer currently has stricter setup assumptions than the other
promoted routes: Composer security blocking must be disabled for the generated
smoke dependency set, the target root needs a git reference, and the packaged
`scip-php` copy expects its own vendor directory. The generated smoke and pure
SCIP route handle those constraints by preparing Composer state in a generated
project or an output-local worktree; source checkouts are used as the
decode/source mapping root and should not receive `index.scip` artifacts from
route-level alignment. Source-only Intelephense alignment on the tiny smoke is
strict-green for namespace, class, method, and top-level function
symbols/containment after namespace synthesis and source-AST function
supplementation. A source-only `php-fig/container` real-repo gate is also
strict-green after per-file namespace synthesis, filtering `scip-php` parameter
pseudo-symbols such as `ContainerInterface#get().($id)`, and running the SCIP
route in a throwaway worktree. PHP remains serial-only until a larger real-repo
profile shows local Python decode/build time is a material bottleneck.

For a real existing Java project, use `--project-root` and keep SCIP artifacts
outside the target repository:

```bash
python scripts/smoke_scip_cold_start.py \
  --languages java \
  --project-root /tmp/codeminer-real-java/maven-simple \
  --output-dir /tmp/codeminer-java-real-scip \
  --expected-symbol App \
  --expected-symbol 'App.greet(String)()' \
  --expected-symbol 'App.main(String[])()' \
  --json
```

Java method names should include JDT-LS-compatible parameter displays when
signature documentation provides them, for example `App.greet(String)()` rather
than only `App.greet()`.

## Step 4: Add Incremental Graph Patching

Add `codeminer/graph/incremental/patcher_{lang}.py` only after graph support
exists. Register it in the graph patcher router and add tests under
`test/graph/incremental/`.

The patcher should use registry-derived graph extensions so file detection does
not drift from cold-start graph support.

## Step 5: Add Agent and Dataset Coverage

Most alias and extension maps should come from `LanguageSpec`, not local
hard-coded tables.

Verify these surfaces:

- `codeminer/agent/compile.py` recognizes the intended scenario key.
- `codeminer/dataset/gt_locate.py` maps target file extensions correctly.
- repository chunking discovers the intended extensions through
  `RepoChunkingConfig`.
- synthesis or benchmark-specific language groups are updated only when the
  dataset actually contains that language group.

## Step 6: Add Optional Core Acceleration

Core acceleration is not required for first support.

If you add it:

1. Add the C++ decoder implementation under `core/`.
2. Thread every schema field through both serial and core decoders in the same
   PR.
3. Add a `test_core_{lang}` parity case or extend `test/scip/test_scip_core.py`.
4. Keep parity bit-for-bit for:
   - vertex name set;
   - edge multiset over source, target, type, anchor file, and anchor line;
   - per-vertex `type`, `file`, `start_line`, `end_line`, `unified_name`.

If a language is serial-only, track that explicitly in the registry and tests
instead of skipping parity indefinitely.

## Required Tests

For a chunking-only PR:

```bash
python -m pytest -q test/test_languages.py test/chunker/test_{lang}_chunker.py
```

For repository discovery changes:

```bash
python -m pytest -q test/test_languages.py test/chunker/test_repo_chunking_config.py
```

For graph cold-start changes:

```bash
python -m pytest -q test/scip/test_scip_indexer.py
python -m pytest -q test/scip/test_scip_multilingual.py -k {lang}
```

For incremental graph changes:

```bash
python -m pytest -q test/graph/incremental -k {lang}
```

For core acceleration changes:

```bash
python -m pytest -q test/scip/test_scip_core.py -k {lang}
```

Local environments may skip integration tests when external tools or fixture
repositories are unavailable. CI must run the parity jobs for accelerated
languages.

## PR Checklist

- [ ] `LanguageSpec` added or updated first.
- [ ] Aliases and extensions are tested in `test/test_languages.py`.
- [ ] Chunking support and graph support are capability-gated separately.
- [ ] New local maps are avoided unless they are dataset-specific.
- [ ] Cold-start backend and incremental backend are named explicitly.
- [ ] Backend alignment tolerances are documented when both SCIP and LSP exist.
- [ ] Serial/core parity is green for every accelerated language.
- [ ] Docs mention whether the language is chunking-only, serial graph, or
      accelerated graph.
