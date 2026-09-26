<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->
<!-- mcp-name: ai.codenib/codenib -->

<div align="center">
  <img src="https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_logo.svg" alt="CodeNib" width="440">
  <h1>Find the right code. Trace the calls.</h1>
  <p>Give Claude Code and Codex source context and typed graph navigation across languages.</p>
  <p>
    <strong>71.4% code-block Recall@5 on 100 real issues.</strong><br>
    Model-planned grep → Jev: +12.8 percentage points over the same grep candidates without reranking.<br>
    <a href="https://docs.codenib.ai/evaluation/grep_jev/">Experimental result · Method and limitations</a>
  </p>
  <p>
    <a href="#quickstart">Use with your agent</a>
    &nbsp;&middot;&nbsp;
    <a href="https://demo.codenib.ai">Browse a Wiki</a>
    &nbsp;&middot;&nbsp;
    <a href="https://docs.codenib.ai/language_capabilities/">Languages</a>
    &nbsp;&middot;&nbsp;
    <a href="https://docs.codenib.ai/agent_integrations/">Reproductions</a>
    &nbsp;&middot;&nbsp;
    <a href="https://github.com/sysevol-ai/CodeNib/releases">Releases</a>
  </p>
  <p>
    <a href="https://github.com/sysevol-ai/CodeNib/actions/workflows/ci-full.yml"><img src="https://github.com/sysevol-ai/CodeNib/actions/workflows/ci-full.yml/badge.svg" alt="CI"></a>
    <a href="https://pypi.org/project/codenib/"><img src="https://img.shields.io/pypi/v/codenib.svg?cacheSeconds=300" alt="PyPI version"></a>
    <a href="https://github.com/sysevol-ai/CodeNib/releases/latest"><img src="https://img.shields.io/github/v/release/sysevol-ai/CodeNib" alt="Latest GitHub Release"></a>
    <a href="https://arxiv.org/abs/2607.25431"><img src="https://img.shields.io/badge/arXiv-2607.25431-b31b1b.svg" alt="arXiv paper"></a>
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="Apache 2.0 license"></a>
  </p>
</div>

<p align="center">
  <a href="https://docs.codenib.ai/codegraph/#recorded-agent-session">
    <img src="https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/landing/assets/demos/codegraph-claude.gif" alt="Recorded CodeGraph setup and Claude Code explore_context call on Requests, returning source references for authentication during redirects" width="100%">
  </a>
</p>
<p align="center">15-second replay of real CLI output · waits condensed ·
  <a href="https://docs.codenib.ai/codegraph/#recorded-agent-session">Pinned source, transcript and setup details</a>.
</p>

## Quickstart

Requires Python 3.10+, Git, a clean repository, and Claude Code or Codex.

```bash
python -m pip install "codenib[graph,mcp]"
codenib codegraph init /path/to/your/repo
```

Then ask your agent: **“Use CodeNib's `explore_context` to find where request
retry behavior is implemented. Cite the files and lines.”**

This shipping path builds local search and a typed symbol graph, then connects
installed agent clients. It needs no model, API key, or GPU for CodeNib;
your agent uses its own model. It is separate from the grep/Jev experiment
above. Language toolchain and project prerequisites vary.

[Setup and troubleshooting](https://docs.codenib.ai/codegraph/)
· [Source exclusions](https://docs.codenib.ai/codegraph/#select-the-repository-source-surface)
· [All MCP tools](https://docs.codenib.ai/mcp/)
· [Local Wiki](https://docs.codenib.ai/quickstart/)

## Why CodeNib

- **Inspect the evidence.** Retrieve bounded code context with file and line
  references, then follow callers, callees, definitions, and references.
- **Use the same tools across languages.** The registry tracks 14 language
  entries, including 12 with graph backends. Check capability and setup
  differences in the [language matrix](https://docs.codenib.ai/language_capabilities/).
- **Compare methods on the same source.** Pinned agent contracts, datasets,
  and scorer checks make the [reproduction surface](https://docs.codenib.ai/agent_integrations/)
  inspectable.
- **Share what you found.** Export a Wiki with source citations to your own
  [GitHub Pages](https://docs.codenib.ai/github_pages/), or browse the
  [public examples](https://demo.codenib.ai).

## How it compares

“Local” and “key” describe the repository tool, excluding the agent's model.
This compares capabilities, not benchmark scores.

| Tool | Local processing | Tool needs a model/key | Graph / navigation | Languages | Updates |
| --- | --- | --- | --- | --- | --- |
| grep / read | Yes | No | Text and files | Any text | Current files |
| **CodeNib CodeGraph** | Yes | No | Typed SCIP/LSP graph | [14 chunkers / 12 graph entries](https://docs.codenib.ai/language_capabilities/) | Reuse unchanged views; rebuild changed views |
| [Serena](https://github.com/oraios/serena) | Yes | No retrieval model | LSP/IDE symbol navigation and editing | [Backend matrix](https://oraios.github.io/serena/01-about/020_programming-languages.html) | Backend-managed project state |
| [CodeGraph (Lordymine)](https://github.com/Lordymine/codegraph) | Yes | No retrieval model | Typed call graph | Go, TS/JS | Re-index on launch; no-op if unchanged |
| [DeepWiki public MCP](https://docs.devin.ai/work-with-devin/deepwiki-mcp) | Hosted | No user key | Wiki and generated answers | Public indexed repos | Service-managed |

[Detailed comparison and boundaries](https://docs.codenib.ai/comparison/).
CodeNib's **experimental grep → Jev** route uses OpenRouter for planning and
reranking; selected code goes to remote models. Its measured result does not
apply to the model-free CodeGraph row.

## Languages

Graph backends: **Python, Go, Rust, C/C++, C#, Java, Ruby, PHP, Kotlin,
Scala, JavaScript, and TypeScript**. Swift and Lua support chunking/retrieval.
Provider prerequisites and coverage differ by language.

The [generated language matrix](https://docs.codenib.ai/language_capabilities/)
separates chunking, graph backends, incremental-backend support, and decoder
parity. The product currently reuses or rebuilds views; file-level delta
repair is not enabled.

## Results and reproductions

| Start here | What you can inspect |
| --- | --- |
| [grep → Jev result](https://docs.codenib.ai/evaluation/grep_jev/) | 100-issue retrieval comparison, candidate controls, model use and limitations |
| [Agent integration matrix](https://docs.codenib.ai/agent_integrations/) | Revision-pinned LocAgent, Agentless, CoSIL, OrcaLoca and RepoNavigator contracts; compatibility does not imply reproduced paper scores |
| [Dataset and benchmark matrix](https://docs.codenib.ai/evaluation/) | CodeNib Base/Synthesis, SWE-bench variants, Loc-Bench and SWE-Explore support |
| [SWE-Explore validation](https://docs.codenib.ai/evaluation/swe_explore/) | 1,020/1,020 real-output metric cells match the pinned official evaluator on a fixed 20-case run |
| [DGX Spark deployment](https://docs.codenib.ai/guides/reference-deployments/dgx-spark/) | Local Wiki, CodeGraph and model serving on GB10; a deployment guide, not a token-saving benchmark |

## Documentation and community

[Documentation](https://docs.codenib.ai/)
· [Architecture](https://docs.codenib.ai/concepts/architecture/)
· [Contributing](https://github.com/sysevol-ai/CodeNib/blob/main/CONTRIBUTING.md)
· [CI and testing](https://docs.codenib.ai/ci_cd/)
· [Changelog](https://github.com/sysevol-ai/CodeNib/blob/main/CHANGELOG.md)
· [Discord](https://discord.gg/ySer6CGn4)

CodeNib is in beta; public interfaces may change before a stable release.
Historical research artifacts retain their published dataset identifiers.

## Citation

If you use CodeNib in your research, please cite our
[arXiv paper](https://arxiv.org/abs/2607.25431):

```bibtex
@misc{yu2026codenibmultiviewdataserving,
      title={CodeNib: A Multi-View Data System for Serving Repository Context to Coding Agents},
      author={Zhongming Yu and Hengjia Yu and Boqin Yuan and Shuting Zhao and Yizhao Chen and Aryan Dokania and Mihir Jagtap and Jiayu Chang and Yitong Ma and Yash Jayswal and Wentao Ni and Hejia Zhang and Zhaoling Chen and Gangda Deng and Jishen Zhao},
      year={2026},
      eprint={2607.25431},
      archivePrefix={arXiv},
      primaryClass={cs.SE},
      url={https://arxiv.org/abs/2607.25431},
}
```

CodeNib is licensed under [Apache 2.0](https://github.com/sysevol-ai/CodeNib/blob/main/LICENSE).
