# Wiki Graph & Agent References — How CodeMiner Retrieves and Renders

> Personal notes — how the **wiki builds its dependency graph**, and how
> **references/citations are retrieved** for agents in **Ask** and over **MCP**.
> Grounded in the actual code (file:line verified). Figures are Mermaid — they
> render as images on GitHub and in VS Code with the *Markdown Preview Mermaid
> Support* extension (`bierner.markdown-mermaid`).

---

## 0. The big picture — one graph, three consumers

Everything hangs off **one compiler-precise artifact**: the `CodeGraph`
(`graph.pkl`), built at compile time, where every edge resolves to an exact
source span. Three surfaces consume it, plus retrieval indexes (BM25 + vector)
feed the text side.

```mermaid
flowchart TD
    subgraph COMPILE["Compile time (durable artifacts)"]
        GRAPH["CodeGraph — graph.pkl\n(symbols + reference edges,\nevery edge = exact span)"]
        BM25["BM25 index"]
        VEC["Vector index (L0/L2)"]
    end

    subgraph CONSUMERS["Serve time"]
        WIKI["Wiki\n(outline → pages → page-graph)"]
        ASK["Ask\n(agent tool-call loop)"]
        MCP["MCP server\n(external agents: Cursor, Claude Desktop)"]
    end

    GRAPH --> WIKI
    GRAPH --> ASK
    GRAPH --> MCP
    BM25 --> WIKI
    BM25 --> ASK
    BM25 --> MCP
    VEC --> WIKI
    VEC --> ASK
    VEC --> MCP
```

**Key fact for later:** the graph is queried through **two different subgraph
builders** that share **one traversal primitive**
(`RepoDependencySearcher.get_neighbors(..., etype_filter={"reference"})` in
`graph/traverse_graph.py`):

| Builder | File | Used by | Extras |
|---------|------|---------|--------|
| `build_codemap` / `build_page_subgraph` | `web/codemap.py` | Wiki page graph + `/codemap` | PageRank importance, Leiden communities, hierarchy overlay, mermaid |
| `DependencyAnalyzer` | `graph/dependency.py` | MCP `dependency_subgraph` tool | transitive BFS (impact/dependencies/both) |

Neither uses `graph/roi_subgraph.py`.

---

## 1. How the wiki is generated (and where its citations come from)

Two disk-cached stages in `codeminer/wiki/agent_wiki.py` (+ `outline.py`).
`AgentWiki` is built per-repo in `web/app.py:_wiki` when `config.wiki_agent` is on.

```mermaid
flowchart TD
    subgraph S1["Stage 1 — Outline (one LLM call)"]
        A1["symbols + salient files (40) + README (3500 chars) + languages"]
        A2["rank symbols by line count (top 70)\noutline.py:_top_symbols"]
        A3["LLM: propose conceptual subsystem TOC\noutline.py:generate_outline (temp 0.2, 4096 tok)"]
        A4["parse JSON → pages[{id,title,summary,keywords,files,children}]\nnormalize: kebab slugs, first page = 'overview'"]
        A1 --> A2 --> A3 --> A4
    end

    A4 --> B0

    subgraph S2["Stage 2 — Per page (agent_wiki.py:_generate_page)"]
        B0["query = title + summary + keywords + files"]
        B1["RETRIEVE: vector_store.search_with_content(query, top_k=32)\nfallback → vector.search → bm25.search   (_retrieve)"]
        B2["RERANK (heuristic, _rerank_for_page):\n+5.0 file-hint path match\n≤4.0 keyword phrase\n≤3.0 term hits\n→ take top 8 nodes"]
        B3["NARRATE: _context (≤1600 chars/node, ≤9000 total)\n_PAGE_PROMPT → LLM (temp 0.2, 2200 tok)\n'ground ONLY in these chunks'"]
        B4["ARCHITECTURE DIAGRAM:\nbuild_codemap(graph, symbol=first_node,\ndirection=both, depth=1, max_nodes=12)\n→ append ## Architecture ```mermaid"]
        B5["CITATIONS: _citation(node) for each of the 8\ndedup by (file, start_line, end_line), 1-based lines"]
        B0 --> B1 --> B2 --> B3 --> B4 --> B5
    end

    B5 --> PAGE["Wiki page:\n{id, title, markdown, citations[], diagram}"]
```

**The page object** (`_generate_page` return, `agent_wiki.py:460`):

```jsonc
{
  "id": "graph-retrieval",
  "title": "Graph Retrieval",
  "markdown": "# Graph Retrieval\n\n{narration}\n\n## Architecture\n```mermaid ...```",
  "citations": [
    { "file": "codeminer/graph/code_graph.py",
      "start_line": 120, "end_line": 180,   // 1-based (graph is 0-based → +1)
      "node_name": "CodeGraph", "type": "class",
      "score": 7.5, "content": null }
  ],
  "diagram": ""
}
```

> The 8 citations are the **only** grounding the LLM sees. Retrieval quality
> here = documentation quality. (Today's rerank is pure string heuristic — a
> cross-encoder would be a future upgrade.)

### 1.1 Two kinds of "files" — how the ranks and the parsed outline are used

A common point of confusion: there are **two completely different sets of
"files"** in Stage 1, flowing in opposite directions. Don't conflate them.

```mermaid
flowchart TD
    subgraph IN["INPUT to the LLM (deterministic — a SAMPLE of the repo)"]
        S["top 70 symbols by size"]
        F["top 40 salient files by weight"]
    end
    S --> PROMPT["the prompt string"]
    F --> PROMPT
    PROMPT --> LLM["LLM call (temp 0.2)"]
    LLM --> OUT
    subgraph OUT["OUTPUT from the LLM (the PLAN — guesses, may be wrong)"]
        PAGES["pages[]: each has its OWN\nkeywords[] + files[] hints"]
    end
    PAGES --> STAGE2["Stage 2 uses these as SEARCH HINTS\n(real vector/BM25 search does the actual finding)"]
```

- **The ranked 40 files / 70 symbols = INPUT context.** They are a
  *representative sample* of the repo, formatted into the prompt so the model has
  something to reason over (you can't put 10,000 symbols in a prompt). Their
  **only** job is to shape what the LLM sees — they are never page content, and
  after the prompt is built the ranks are never looked at again.
- **The per-page `files` the LLM returns = OUTPUT guesses.** A different list
  entirely — the model's opinion of which files belong to each concept. These
  **can be inaccurate.**

**How the ranks are used:** only to build the prompt (the model's "briefing
material"). That's the whole role.

**How the parsed JSON is used:** it *is the plan* — the table of contents plus
per-page `keywords`+`files` search hints. It gets cached, then handed to
**Stage 2**, which builds a query (`title + summary + keywords + files`) and runs
**real vector + BM25 retrieval over the whole index**.

**Why the LLM's possibly-wrong file guesses don't break anything:** Stage 2 does
**not** trust them blindly. The hints only *build the query* and *boost* matching
results (the `+5.0` file-hint in `_rerank_for_page`); the actual code is found by
real search over the full index, which can locate the right chunks even if the
LLM guessed the wrong filename.

| Step | Trust level | What it does |
|------|-------------|--------------|
| Rank 70 symbols / 40 files | deterministic fact | sample the repo → prompt context |
| LLM proposes pages + `files`/`keywords` | **guess** (may be wrong) | conceptual TOC + search hints |
| Stage 2 retrieval | deterministic fact | real search finds the actual code; hints only nudge ranking |

The LLM is trusted to decide **what concepts exist** and **roughly what to search
for** — never **which exact code goes on the page**. That final decision is made
by real retrieval against the index.

### 1.2 Inside the search — how `search_with_content` actually finds chunks

`_retrieve` fetches a **pool of 32** (`pool_k = max(8, 8*4)`) so the reranker has
room to pick the best 8. Here is what one `search_with_content(query, top_k=32)`
call actually does (`vector_store.py:669` → `_search_index`):

```mermaid
flowchart TD
    Q["query string\n(title + summary + keywords + files)"]
    Q --> EMB["embedding.embed_query(query)\n→ one vector, e.g. 768 floats (bge-base)"]
    EMB --> FAISS["FAISS index.search(query_vec, k=32)\ncompares query vector to EVERY stored\nchunk vector → nearest 32"]
    FAISS --> DI["returns (distances, indices)\nindices = row numbers of the 32 closest chunks"]
    DI --> MAP["documents[idx] for each index\n→ the actual chunk + its metadata"]
    MAP --> NODE["build NodeInfo per hit:\n{node_name, type, file, start_line,\nend_line, score, content=page_content}"]
    NODE --> FILT["optional score_threshold / mask filter\n→ cut to top_k (32)"]
    FILT --> OUT["32 candidate NodeInfos\n→ _rerank_for_page → top 8"]
```

**The whole thing is 3 real operations:**

1. **Embed the query** — `embedding.embed_query(query)` turns the text into one
   vector (a list of ~768 floats). The chunk vectors were already embedded at
   compile time; now the *query* gets embedded into the same space.
2. **Nearest-neighbour lookup** — `index.search(query_vec, k=32)` asks FAISS
   "which 32 stored chunk-vectors are closest to this query-vector?" FAISS returns
   their distances (scores) and their row indices. This is the "search": pure
   vector proximity, no keywords.
3. **Map indices back to chunks** — index `idx` → `documents[idx]` gives the real
   chunk with its metadata (`name, file, start_line, end_line`) and its code text
   (`page_content`).

**`search` vs `search_with_content`** are *identical* except the latter attaches
`content=doc.page_content` (the actual code) to each result — which the wiki
needs, because the page prose is written from that code. `search` omits it (used
where only locations/scores matter).

`level="l2"` searches the function/method index (the default); `l0` would search
file-skeleton vectors. `score_threshold` and `mask_node_ids` are optional filters
(unused by the wiki path).

---

## 2. How the wiki builds its dependency graph

This is the `GET /api/repos/{id}/wiki/{page_id}/graph` endpoint
(`app.py:wiki_page_graph:131`). It does **not** re-search — it takes the page's
**already-computed citations** and induces a subgraph over them from the
`CodeGraph` via `web/codemap.py:build_page_subgraph`.

```mermaid
flowchart TD
    P["page['citations']  (the 8 cited symbols)"] --> R

    subgraph RESOLVE["Resolve citations → graph identities (_resolve_citation)"]
        R["for each citation:\n1-based line → 0-based (sl-1)"]
        R --> R1{"exact (file, start0)\nin graph?"}
        R1 -- yes --> SEED["= a SEED node"]
        R1 -- no --> R2["nearest symbol start (tol 3)\nelse readable-name resolve"]
        R2 --> SEED
    end

    SEED --> N

    subgraph NEIGH["Walk reference edges (RepoDependencySearcher.get_neighbors)"]
        N["for each seed:\nget_neighbors(seed, direction=all,\netype_filter={'reference'}, ignore_test_file=True)"]
        N --> DE["seed → seed reference edge\n= DIRECT EDGE (kept)"]
        N --> BR["neighbor is NOT a seed\n= bridge candidate"]
        BR --> BRK{"bridge touches\n≥ 2 cited seeds?"}
        BRK -- yes --> KEEP["admit bridge\n(ranked by call-site anchor weight,\nbudget min(2, max_nodes-|names|), max_nodes=18)"]
        BRK -- no --> DROP["drop"]
    end

    DE --> ENR
    KEEP --> ENR

    subgraph ENRICH["_enrich (codemap.py:228)"]
        ENR["+ PageRank importance\n+ Leiden community\n+ ref_count, entry_score\n+ edge weight, hub pruning\n+ hierarchy overlay (containment)"]
    end

    ENR --> OUT["{ nodes[], edges[], hierarchy, mermaid:'', ... }\nnode: {id, name, label, file, line(1-based),\nkind, depth, is_root(=seed), external}\nedge: {source, target, anchors:[{file,line}]}"]
```

**Why "≥2 seeds" for bridges?** A page's citations are scattered symbols. A
non-cited symbol is only worth adding to the picture if it *connects* at least
two of them — otherwise the graph fills with noise. This keeps the page graph
tight and about *the page's* concepts.

The general `GET /codemap?symbol=X` endpoint (`app.py:166` → `build_codemap`,
`codemap.py:277`) is the same machinery but seeded from **one** symbol with a
BFS radius (depth 1–4, direction → forward/backward/all) instead of a citation
set. `build_codemap` is also what generates the per-page **Architecture** mermaid
diagram in Stage 2 above.

---

## 3. Ask flow — how references reach the agent's answer

`POST /api/chat` (`app.py:chat:202`) → `AgentRunner.run` → tool-call loop →
retrieval skills return `List[QueriedNode]` → those become the answer's citations.

```mermaid
sequenceDiagram
    participant UI as Browser (Ask)
    participant API as POST /api/chat (app.py:202)
    participant AR as AgentRunner.run (runner.py:345)
    participant LLM as LLM
    participant SK as Retrieval skill executor
    participant RESP as agent_result_to_response (schemas.py:144)

    UI->>API: {messages:[...]}  (last = user)
    API->>AR: runner.run(query, chat_history)
    loop up to max_turns
        AR->>LLM: _call_raw(messages, tools, tool_choice)
        alt LLM returns tool_calls
            LLM-->>AR: call bm25_search / embedding_search / ...
            AR->>SK: executor_fn(**args)
            SK-->>AR: List[QueriedNode]  (node_name,file,start,end,score,content)
            Note over AR: store raw in ToolCallRecord.result\nappend to all_tool_calls (accumulates ALL turns)\nappend role:"tool" message with serialized result
        else no tool_calls
            LLM-->>AR: final assistant text → terminate
        end
    end
    AR-->>API: AgentResult{answer, tool_calls[]}
    API->>RESP: flatten every tool_call.result
    Note over RESP: to_agent_repr (0-based→1-based)\nrepo-relative path\ndedup by (file,start,end) across ALL tool calls\ntruncate content >2000 chars
    RESP-->>UI: {answer, citations[], tool_calls[], total_turns, total_duration_ms}
```

**Response shape** (`schemas.py:ChatResponse:80`):

```jsonc
{
  "answer": "…prose…",
  "citations": [
    { "file": "codeminer/index/sparse_idx/bm25_index.py",
      "start_line": 40, "end_line": 88,          // 1-based at API boundary
      "node_name": "BM25CodeIndexer", "type": "class",
      "score": 12.3, "content": "…≤2000 chars…" }
  ],
  "tool_calls": [
    { "skill_id": "bm25_search", "arguments": {...},
      "result_count": 25, "duration_ms": 41, "error": null }
  ],
  "total_turns": 3,
  "total_duration_ms": 4210
}
```

**The chain in one line:** skill returns `List[QueriedNode]` →
`ToolCallRecord.result` (raw) → `all_tool_calls` accumulates across every turn →
`agent_result_to_response` flattens + dedups → `Citation[]` shown as the
`file:line` boxes in the UI.

**Skills registered as tools** (from `agent/skills/`, demo sets
`include_default_tools=False` so no read/grep/glob/bash):
`bm25_search`, `embedding_search`, `hybrid_search` (→ `List[QueriedNode]`),
`find_callers`, `find_callees`, `trace` (call-graph nav),
`codeminer_context`, `code_to_query`, `crossencoder_rerank`, `llm_rerank`.

> Demo forces `first_turn_tool_choice="required"` (`repo_registry.py:379`) so the
> agent must retrieve before answering — this is why answers are grounded and
> cited rather than hallucinated.

---

## 4. MCP flow — references for external agents

`codeminer-mcp <manifest.json>` (`mcp/server.py:main:328`) runs a `FastMCP`
stdio server. It loads the **same index classes** as the web path, just onto a
`ServerContext` instead of a `RepoBundle`.

```mermaid
flowchart LR
    M["repo_manifest.json"] --> L

    subgraph L["ServerContext.load (mcp/context.py:47)"]
        L1["_load_symbol_graph → CodeGraph.load_graph"]
        L2["_load_bm25 → BM25CodeIndexer.load_index"]
        L3["_load_regex_index → RegexNodeIndex(symbol_graph)"]
        L4["_load_zoekt → ZoektSearcher.start (soft dep)"]
        L5["_load_vector → CodeVectorStore.load"]
    end

    L --> T

    subgraph T["Registered @mcp.tool tools (server.py)"]
        T1["search_semantic → ctx.vector.search_with_content"]
        T2["search_bm25 → ctx.bm25.search"]
        T3["search_regex → ctx.regex_index.search"]
        T4["search_zoekt → ctx.zoekt.search (file-level)"]
        T5["dependency_subgraph → DependencyAnalyzer(graph)"]
        T6["get_manifest → manifest.to_dict()"]
    end

    T --> EXT["External agent\n(Cursor / Claude Desktop)\ndecides its own reranking"]
```

**Tool return shapes** — the 5 index-backed tools serialize via
`node.model_dump(exclude_none=True)` — the **same `NodeInfo` type** the agent
skills use, so an external agent gets identical `{node_name, type, file,
start_line, end_line, content, score}` records.

| MCP tool | Impl | Returns |
|----------|------|---------|
| `search_semantic` | `tools/search.py:27` | `list[NodeInfo]` (vector dot-product) |
| `search_bm25` | `search.py:81` | `list[NodeInfo]` (lexical) |
| `search_regex` | `search.py:120` | `list[NodeInfo]` (symbol-level pattern) |
| `search_zoekt` | `search.py:174` | `list[dict]` **file-level** (`type="file"` + snippet) |
| `dependency_subgraph` | `tools/dependency.py:20` | `{root, direction, nodes[], edges[], truncated, note}` |
| `get_manifest` | `server.py:204` | repo metadata |

**Dependency subgraph over MCP** uses `graph/dependency.py:DependencyAnalyzer`
(a *different* builder from the web codemap, but the same reference-edge
primitive):

```mermaid
flowchart TD
    Q["dependency_subgraph(symbol, direction, depth, max_nodes)"] --> RS["CodeGraph.resolve_symbol (fuzzy)"]
    RS --> D{direction}
    D -- "impact / callers" --> IMP["analyzer.impact()\ntransitive callers (backward BFS)"]
    D -- "dependencies / callees" --> DEP["analyzer.dependencies()\ntransitive callees (forward BFS)"]
    D -- "both" --> SUB["analyzer.subgraph(radius=depth)\n1-hop caller+callee neighborhood"]
    IMP --> O["{nodes:[{name,file,line,kind,depth}],\nedges:[{source,target,type}], truncated}"]
    DEP --> O
    SUB --> O
```

Reference edges only (`_REFERENCE_EDGES={"reference"}`); containment excluded.
No separate "codemap" tool over MCP — `dependency_subgraph` **is** the MCP
equivalent of the web `/codemap` + wiki page-graph endpoints.

---

## 5. Side-by-side — the three reference paths

```mermaid
flowchart TD
    GRAPH["CodeGraph (graph.pkl)"]
    PRIM["RepoDependencySearcher.get_neighbors\netype_filter={'reference'}\n(graph/traverse_graph.py)"]
    GRAPH --> PRIM

    PRIM --> CM["web/codemap.py\nbuild_codemap / build_page_subgraph\n+PageRank +Leiden +hierarchy +mermaid"]
    PRIM --> DA["graph/dependency.py\nDependencyAnalyzer\ntransitive BFS"]

    CM --> WIKIG["Wiki page graph\n(/wiki/{page}/graph)"]
    CM --> CODEMAP["/codemap + page Architecture diagram"]
    DA --> MCPTOOL["MCP dependency_subgraph tool"]

    subgraph TEXT["Text retrieval (separate from graph)"]
        BM25["BM25"] & VEC["Vector"] --> QN["List[QueriedNode]"]
    end
    QN --> ASKC["Ask citations\n(flatten+dedup across turns)"]
    QN --> WIKIC["Wiki page citations\n(top-8 per page)"]
    QN --> MCPS["MCP search_* tools"]
```

| Surface | Text references from | Graph from | Builder |
|---------|---------------------|------------|---------|
| **Wiki page** | vector→bm25 top-8 (heuristic rerank) | page citations → induced subgraph | `build_page_subgraph` |
| **Wiki page graph** | (reuses citations) | reference edges + ≥2-seed bridges | `build_page_subgraph` |
| **Ask** | agent-chosen skills, all turns | (via `find_callers`/`trace` skills) | skill-side |
| **MCP** | `search_*` tools, caller-chosen | `dependency_subgraph` | `DependencyAnalyzer` |

---

## Key file reference (verified)

| Concern | File:line |
|---------|-----------|
| Wiki outline (Stage 1) | `codeminer/wiki/outline.py:generate_outline:96` |
| Wiki per-page gen (Stage 2) | `codeminer/wiki/agent_wiki.py:_generate_page:445` |
| Wiki retrieve (32→8) | `agent_wiki.py:_retrieve:203`, `_rerank_for_page:289` |
| Wiki citations | `agent_wiki.py:_citation:380` |
| Wiki page graph endpoint | `codeminer/web/app.py:wiki_page_graph:131` |
| Page subgraph builder | `codeminer/web/codemap.py:build_page_subgraph:476` |
| Codemap / architecture diagram | `codeminer/web/codemap.py:build_codemap:277` |
| Shared traversal primitive | `codeminer/graph/traverse_graph.py:RepoDependencySearcher.get_neighbors` |
| Graph load | `codeminer/web/repo_registry.py:code_graph:170` |
| Ask endpoint | `codeminer/web/app.py:chat:202` |
| Agent loop | `codeminer/agent/runner.py:run:345`, `_execute_tool_call:637` |
| Ask citation assembly | `codeminer/web/schemas.py:agent_result_to_response:144` |
| MCP entry | `codeminer/mcp/server.py:main:328` |
| MCP context load | `codeminer/mcp/context.py:ServerContext.load:47` |
| MCP dependency tool | `codeminer/mcp/tools/dependency.py:dependency_subgraph_impl:20` |
| MCP dependency analyzer | `codeminer/graph/dependency.py:DependencyAnalyzer` |

---

## See also

- [[rerank-architecture]] — how the retrieval candidates feeding these citations are ranked (BM25/vector/cross-encoder/LLM).
- [[incremental-indexing-plan]] — how the CodeGraph + indexes stay fresh from a git diff.
