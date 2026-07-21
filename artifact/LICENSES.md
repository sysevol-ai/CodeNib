# License Boundaries

This artifact is a collection of code, datasets, retained model outputs, and
source-linked measurements. It is not relicensed as one homogeneous work.

- CodeMiner source and the bundled `figure/*.py` reproduction programs are
  distributed under Apache License 2.0; see `LICENSE-CODE`. This statement
  applies to the copies in this release, not to unrelated files in the source
  checkout from which the figure programs were selected.
- The bundled CodeMiner Base parquet comes from
  `fishmingyu/codeminer-base-dataset` at revision
  `4eb84e2e8918474969ce68c5b06facf14d6be604`, whose dataset card declares the
  MIT license.
- The bundled CodeMiner Synthesis parquets come from
  `sysevol-ai/codeminer-synthesis` at revision
  `5ac36c39ef69bbfe2e14dac58b6067b8c350c53e`, whose dataset card declares
  Apache License 2.0.
- Retained retrieval, graph, LSP, lifecycle, and agent records can contain
  source-linked paths, symbols, or excerpts derived from upstream repositories
  identified by each row's repository and base commit. Those materials remain
  subject to their upstream licenses and are not relicensed by this artifact.
- Third-party model outputs and model identifiers remain subject to their
  providers' applicable terms. No model weights are included.

Users are responsible for following the applicable license or terms for each
component they redistribute or reuse.
