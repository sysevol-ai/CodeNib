# dataset/ — rules

Loads SWE-bench (incl. Multilingual) and derives the locator ground truth used
for evaluation. `swebench_multilingual.py` / `swebench.py` load instances;
`gt_locate.py` turns a patch into ground-truth code blocks; `collect/` samples
instances and `synthesize/` generates queries.

## Conventions

- **Line-number conversion happens HERE.** `gt_locate.py`'s
  `_chunk_to_code_block()` is the one place that converts the chunker's
  **0-based** `CodeChunk` lines into the **1-based** `CodeLocation` lines emitted
  to output/HuggingFace. Off-by-one bugs in eval almost always trace back to
  this boundary — change it deliberately and update the chunker side
  ([`code_chunking/CLAUDE.md`](../code_chunking/CLAUDE.md)) in lockstep.
- **`.c` → `cpp` chunker.** The language map sends `.c` to the `cpp` chunker; a
  missing mapping yields empty `code_blocks` (the original redis/jqlang bug).
- **Patches touch non-code files.** SWE-bench patches routinely edit `.md`,
  `.toml`, `CHANGELOG`, etc. Do **not** assert that every `target_file` matches a
  source-language extension — that assertion fails on real instances.

## Caches & test repos

- HuggingFace dataset cache: `~/.codenib/` (the `datasets` lib caches the
  multilingual set locally).
- Per-language fixtures used in tests:
  Go `caddyserver/caddy`, C/C++ `redis/redis` (real `.c`),
  Rust `tokio-rs/axum`, TypeScript/JS `preactjs/preact` (real `.js`).
  Prefer `redis/redis` over `fmtlib/fmt` for C/C++ — fmt's templated headers
  confuse the chunker.
