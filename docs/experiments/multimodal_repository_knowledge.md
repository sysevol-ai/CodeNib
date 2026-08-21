# Source-grounded multimodal repository knowledge

This experiment extends the wiki media slot work into a first-class multimodal
repository knowledge pipeline.

The goal is not to make wiki pages more decorative. The goal is to make
repository-native images, diagrams, and screenshots reusable as source-grounded
context for wiki pages and coding agents.

## Pipeline

```text
repository images / svg / screenshots
  -> MediaManifest
  -> VisualFactPack
  -> VisualGroundingManifest
  -> MultimodalKnowledgeView
  -> Wiki / future MCP query APIs
```

## Components

### MediaManifest

`codenib.wiki.media_artifacts.discover_media_manifest()` scans a repository for
supported visual artifacts:

- `.png`
- `.jpg`
- `.jpeg`
- `.svg`
- `.webp`

It respects the shared repository traversal and source-selection policy, skips
symlinks, reads bounded stable regular files, hashes media content, and records
bounded markdown references, alt text, captions, and surrounding documentation
context. Repository-contained parent references such as
`../assets/architecture.svg` are normalized while paths that escape the
repository are ignored.

### VisualFactPack

`codenib.wiki.media_facts` defines the structured output expected from a VLM:

- visual entities
- visual relations
- source-grounded claims
- grounding candidates

The local `deterministic_visual_facts()` fallback extracts conservative facts
from artifact metadata only. Future VLM backends can replace that extractor
while keeping the same schema.

`OpenAICompatibleVisualFactExtractor` provides the first provider-neutral VLM
adapter. It targets an OpenAI-compatible `/chat/completions` endpoint, sends a
bounded local artifact as a data URL, asks for JSON-only structured visual
facts, and normalizes the response into the same `VisualFactPack` schema. This
keeps the multimodal knowledge pipeline independent of a specific model family.

### VisualGroundingManifest

`codenib.wiki.media_grounding` grounds extracted visual entities to repository
files and symbols. The first implementation uses deterministic lexical scoring
against a bounded source-symbol inventory derived from the shared language
registry. Later versions can replace the scorer with BM25, embeddings,
CodeGraph, LSP facts, or `FactQueryIndex` / `FactBatch`.

### MultimodalKnowledgeView

`codenib.wiki.media_knowledge` joins artifacts, facts, and source bindings into
a queryable view. It exposes three functions that future MCP tools can wrap:

- `search_visual_context`
- `get_visual_evidence`
- `find_visual_code_links`

## Why evidence stays server-side

Media generation may use bounded source snippets inside provider prompts. Those
prompts should not be returned to the browser. Public asset payloads expose
safe provenance such as source citations and evidence-pack hashes, while the
full evidence pack remains a transient server-side input.

## Minimal local example

```python
from codenib.wiki.media_artifacts import discover_media_manifest
from codenib.wiki.media_facts import build_visual_facts_manifest
from codenib.wiki.media_grounding import (
    discover_source_symbol_candidates,
    ground_visual_facts_to_sources,
)
from codenib.wiki.media_knowledge import build_multimodal_knowledge_view

repo = "/path/to/repository"

media = discover_media_manifest(repo)
facts = build_visual_facts_manifest(media)
sources = discover_source_symbol_candidates(repo)
grounding = ground_visual_facts_to_sources(facts, sources)
view = build_multimodal_knowledge_view(media, facts, grounding)

print(view["entry_count"])
```

This creates a deterministic local view. A VLM extractor can be added later by
passing a custom extractor into `build_visual_facts_manifest()`.

```python
from codenib.wiki import OpenAICompatibleVisualFactExtractor

extractor = OpenAICompatibleVisualFactExtractor(
    model="qwen-vl",
    api_base="http://localhost:8000/v1",
    api_key=None,
)

facts = build_visual_facts_manifest(
    media,
    extractor=lambda artifact: extractor.extract(artifact, repo_path=repo),
)
```
