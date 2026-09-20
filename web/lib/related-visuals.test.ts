import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import RelatedVisuals from "../components/RelatedVisuals";
import type { WikiPage, WikiVisualEvidence, WikiVisualEvidenceFact } from "./api";
import { relatedVisuals } from "./related-visuals";

const fact = (path: string, name = "FactBatch"): WikiVisualEvidenceFact => ({
  artifact_path: path, extractor: "test",
  entities: [{ name, type: "component", confidence: 0.9 }],
  relations: [{ source: name, target: "Graph", relation: "builds" }],
  claims: [{ text: "The diagram shows the graph construction flow.", confidence: 0.9 }],
});
const page: WikiPage = {
  id: "overview", title: "Overview", markdown: "# Example\nFactBatchBufferView builds graphs.", diagram: "",
  citations: [{ file: "src/facts.py", start_line: 2, end_line: 4, node_name: "FactBatchBufferView.build", type: "class", score: null, content: null }],
};
const evidence = (facts: WikiVisualEvidenceFact[]): WikiVisualEvidence => ({
  state: "ready", source_commit: "a".repeat(40), indexed_commit: "a".repeat(40),
  artifact_count: facts.length, fact_count: facts.length, binding_count: 0, facts, bindings: [],
});

describe("page-related visual context", () => {
  it("finds a diagram from its documentation section without matching entity names", () => {
    const visual = fact("delivery.svg", "Box A");
    visual.relations = [];
    visual.context = { caption: "Delivery flow", source_paths: [], references: [{
      file: "docs/delivery.md", line: 12, title: "Delivery flow", section: "Queue retries",
      excerpt: "Queue delivery retries wait for acknowledgement.",
    }] };
    const result = relatedVisuals(evidence([visual]), { ...page, markdown: "The queue retries delivery after acknowledgement.", citations: [] });
    expect(result).toHaveLength(1);
    expect(result[0].reason).toContain("Queue retries");
    expect(result[0].document?.file).toBe("docs/delivery.md");
  });

  it("uses an authenticated source module and keeps unrelated modules out", () => {
    const visual = fact("delivery.svg", "Box A");
    visual.context = { caption: "Delivery flow", references: [], source_paths: ["src/queue/worker.py"] };
    const matchingPage = { ...page, citations: [{ ...page.citations[0], file: "src/queue/consumer.py" }] };
    expect(relatedVisuals(evidence([visual]), matchingPage)[0].reason).toBe("Same source module: src/queue");
    expect(relatedVisuals(evidence([visual]), page)).toEqual([]);
  });

  it("does not use generic documentation words to resurrect unrelated images", () => {
    const visual = fact("unrelated.svg", "Box A");
    visual.context = { caption: "Overview", source_paths: [], references: [{
      file: "README.md", line: 3, title: "Architecture overview", section: "Repository documentation",
      excerpt: "This page shows the source code for this project.",
    }] };
    expect(relatedVisuals(evidence([visual]), { ...page, markdown: "This page shows source code and repository architecture.", citations: [] })).toEqual([]);
  });

  it("removes branding and other projects, and deduplicates image formats", () => {
    const result = relatedVisuals(evidence([
      fact("logo.svg"), fact("docs/caddy.png", "ReverseProxy"),
      fact("docs/flow.png"), fact("docs/flow.svg"),
    ]), page);
    expect(result.map((item) => item.fact.artifact_path)).toEqual(["docs/flow.png"]);
    expect(result[0].citations).toEqual(page.citations);
  });

  it("does not turn arbitrary substrings or generic labels into relevance", () => {
    const unrelated = { ...page, markdown: "A factory in a repository.", citations: [] };
    expect(relatedVisuals(evidence([fact("flow.png", "Fact"), fact("repo.png", "Repository")]), unrelated)).toEqual([]);
  });

  it("hides stale facts, title-only matches and irrelevant page content", () => {
    expect(relatedVisuals({ ...evidence([fact("flow.png")]), state: "stale" }, page)).toEqual([]);
    expect(relatedVisuals(evidence([fact("flow.png")]), { ...page, markdown: "# FactBatch\nUnrelated content", citations: [] })).toEqual([]);
  });

  it("bounds the section to two diagrams and ignores unverified binding scores", () => {
    const data = evidence([fact("a.png"), fact("b.png"), fact("c.png")]);
    data.bindings = [{ artifact_path: "a.png", entity_name: "FactBatch", symbol: "Unrelated", source_path: "wrong.py", line: 1, score: 1, evidence: "exact match" }];
    const result = relatedVisuals(data, page);
    expect(result).toHaveLength(2);
    expect(result.every((item) => item.citations.every((citation) => citation.file !== "wrong.py"))).toBe(true);
  });

  it("renders a closed disclosure, an original-image link, and real source navigation", () => {
    const html = renderToStaticMarkup(createElement(RelatedVisuals, {
      evidence: evidence([fact("flow.png")]), page, repoId: "example", onOpenCitation: () => {},
    }));
    expect(html).toContain("<details");
    expect(html).not.toMatch(/<details[^>]*\sopen(?:[\s=>])/);
    expect(html).toContain("View original");
    expect(html).toContain("FactBatchBufferView.build");
    expect(html).not.toContain("90%");
  });
});
