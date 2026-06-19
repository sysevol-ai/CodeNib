# code_chunking/ — rules

Tree-sitter chunkers, one per language (`python`, `go`, `cpp`, `rust`, `java`, `js`).
The authoritative reference for chunk depths and the full per-language
`chunk_type` tables is [`README.md`](./README.md) — read it before adding or
changing a chunker. This file is the short list of rules that bite.

## Conventions

- **Line numbers are 0-based here.** `CodeChunk.start_line`/`end_line` come
  straight from tree-sitter and are **0-based**. The +1 conversion to the
  1-based `CodeLocation` happens downstream in `dataset/gt_locate.py`
  (`_chunk_to_code_block()`) — do **not** add an offset inside a chunker.
- **`.c` files use the `cpp` chunker.** There is no separate C chunker; the
  language→chunker map sends `.c` to `cpp`. Forgetting this produces empty
  `code_blocks` (the bug that silently broke redis/jqlang instances).
- **Java is chunker/GT/agent-only for now.** The `.java` chunker is registered
  for tree-sitter chunking and ground-truth extraction, but graph/LSP/core
  backend support is still intentionally unset in the language registry.
- **Chunk depth** (`chunk_depth` on `BaseCodeChunker`): L0 = whole file
  (skeleton when `skeleton_mode`), L1 = top-level symbols, L2 = nested
  methods/members (default; with `l2_level_exclusive=True` the L1 containers are
  dropped, keeping only their L2 members).
- **Symbol chunk types** (`SYMBOL_CHUNK_TYPES`): `function`, `method`, `class`,
  `struct`, `type`, `interface`, `object`, `enum`, `trait`, `impl`, `var`,
  `const`, `static`, `declaration`, `macro`, `variable`, `record`. Per-language L1
  var/const kinds: Go `var`/`const`; Rust `const`/`static`/`type`; C++
  `declaration`/`macro`; Java `record`; JS/TS `variable`.

## When adding a language / chunk type

- Add it to the per-language table in `README.md` in the same PR.
- Heavily-templated C++ headers (e.g. fmtlib/fmt) confuse the chunker — use
  `redis/redis` (plain `.c`) for C/C++ test coverage instead.
