import type { Citation, WikiPage, WikiVisualEvidence, WikiVisualEvidenceFact } from "./api";

const genericTopics = new Set([
  "repository", "component", "source", "code", "wiki", "media", "asset",
  "architecture", "overview", "image", "diagram", "system", "application",
]);

function mentions(text: string, topic: string): boolean {
  const escaped = topic.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // Whole names in prose, or named prefixes of CamelCase code identifiers
  // (e.g. FactBatch in FactBatchBufferView). Never arbitrary substrings.
  return new RegExp(`\\b${escaped}\\b`, "i").test(text) ||
    new RegExp(`\\b${escaped}(?=[A-Z][a-z])`).test(text);
}

export interface RelatedVisual {
  fact: WikiVisualEvidenceFact;
  topics: string[];
  citations: Citation[];
  reason: string;
  document?: NonNullable<WikiVisualEvidenceFact["context"]>["references"][number];
}

const commonWords = new Set([
  ...genericTopics, "readme", "this", "that", "with", "from", "into", "each",
  "have", "will", "when", "which", "their", "these", "those", "only", "also",
  "used", "uses", "using", "below", "above", "such", "more", "same", "page",
  "pages", "file", "files", "data", "example", "project", "preview", "show",
  "shows", "shown", "build", "built", "local", "implementation", "documentation",
]);

function terms(text: string): string[] {
  return [...new Set(text.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase().match(/[a-z][a-z0-9]{3,}/g) ?? [])]
    .filter((term) => !commonWords.has(term));
}

function sourceModule(path: string): string | undefined {
  const parts = path.split("/");
  // A bare src/ or lib/ directory is too broad to establish a relationship.
  return parts.length >= 3 ? parts.slice(0, -1).join("/") : undefined;
}

/** Select useful page context, not a gallery of everything extracted. */
export function relatedVisuals(evidence: WikiVisualEvidence, page: WikiPage): RelatedVisual[] {
  if (evidence.state !== "ready") return [];
  // Ignore the title: the repository's brand alone is not page relevance.
  const prose = page.markdown.replace(/^#\s+.*$/m, "");
  const pageTerms = new Set(terms(prose));
  const candidates = evidence.facts.flatMap((fact) => {
    const name = fact.artifact_path.split("/").pop() ?? "";
    if (!/\.(png|svg|jpe?g|webp)$/i.test(name)) return [];
    if (/(?:^|[-_.])(logo|icon|favicon|badge)(?:[-_.]|$)/i.test(name)) return [];
    if (!fact.claims.some((claim) => claim.text.trim())) return [];
    const topics = [...new Set(fact.entities.map((entity) => entity.name.trim()))]
      .filter((topic) => topic.length >= 4 && !genericTopics.has(topic.toLowerCase()))
      .filter((topic) => mentions(prose, topic) || page.citations.some(
        (citation) => mentions(citation.node_name, topic),
      ));
    const paths = fact.context?.source_paths ?? [];
    const sourceCitations = page.citations.filter((citation) => citation.file && paths.includes(citation.file));
    const modules = [...new Set(paths.map(sourceModule).filter((value): value is string => Boolean(value)))];
    const module = modules.find((directory) => mentions(prose, directory) || page.citations.some(
      (citation) => citation.file?.startsWith(`${directory}/`),
    ));
    const document = fact.context?.references.find((reference) => {
      if (page.citations.some((citation) => citation.file === reference.file)) return true;
      const labelTerms = terms(`${reference.title} ${reference.section}`);
      const overlap = terms(`${reference.title} ${reference.section} ${reference.excerpt}`)
        .filter((term) => pageTerms.has(term));
      // A caption/section topic plus corroborating prose, not one generic word.
      return overlap.length >= 2 && labelTerms.some((term) => pageTerms.has(term));
    });
    if (!topics.length && !sourceCitations.length && !module && !document) return [];
    // Navigation uses real page citations. Extraction scores alone do not
    // establish that an image entity is a verified implementation symbol.
    const citations = page.citations.filter((citation) => citation.file && (
      paths.includes(citation.file) ||
      (module && citation.file.startsWith(`${module}/`)) ||
      topics.some((topic) => mentions(citation.node_name, topic))
    )).slice(0, 2);
    const reason = sourceCitations.length ? "References a source file cited on this page" :
      module ? `Same source module: ${module}` :
      document ? `Related documentation: ${document.section || document.title || document.file}` :
      `Topics on this page: ${topics.slice(0, 3).join(", ")}`;
    return [{
      fact, topics: topics.length ? topics.slice(0, 3) : [fact.context?.caption || module || document?.title || document?.section || "Related diagram"],
      citations, reason, document: document ?? fact.context?.references[0],
    }];
  });
  candidates.sort((a, b) => b.citations.length - a.citations.length || b.topics.length - a.topics.length);
  const seen = new Set<string>();
  return candidates.filter(({ fact }) => {
    const identity = fact.artifact_path.replace(/\.[^.\/]+$/, "");
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  }).slice(0, 2);
}
