# CodeMiner

An LLM-adapted code indexing and retrieval system for multi-language codebases.

CodeMiner builds LSP-oriented symbol graphs and lightweight indexes from multi-language codebases, designed for efficient retrieval by LLM-based code agents. It supports **Python, Go, Rust, C/C++, JavaScript, and TypeScript**.

## Quick Start

```bash
conda create -n codeminer python=3.10
conda activate codeminer
pip install -e .

# Enable SCIP/LSP-based code intelligence
make scip
```

## Key Capabilities

- **LSP-oriented symbol graphs** -- structural code intelligence via SCIP protocol and language servers (scip-python, rust-analyzer, clangd, gopls, scip-typescript)
- **Incremental graph patching** -- update CodeGraph in-place via LSP without full re-indexing, enabling fast iteration on evolving codebases
- **Lightweight hybrid retrieval** -- BM25 sparse, regex, and FAISS/Milvus dense indexes with LLM re-ranking
- **Tree-sitter chunking** at file, symbol, and method granularity
- **SWE-bench integration** -- ground-truth extraction, multi-language dataset collection, and evaluation

## Development

```bash
make dev          # install with dev + test deps
make test         # run all tests
```

Tests use three pytest marker tiers:

```bash
pytest -m "not slow and not integration" -x   # unit (~1 min)
pytest -m integration                          # integration (~15 min)
pytest -m slow                                 # LLM/GPU (~15 min)
```

Pre-commit hooks (black, isort, flake8) are configured -- run `pre-commit install` after cloning.

## Documentation

Full docs are served via [mkdocs-material](https://squidfunk.github.io/mkdocs-material/). To run locally:

```bash
mkdocs serve
```

## License

CodeMiner is licensed under the [Apache License, Version 2.0](LICENSE).
Contributions previously made under the MIT License are retained under the
terms of Section 4 of Apache 2.0; see [NOTICE](NOTICE) for full attribution.
