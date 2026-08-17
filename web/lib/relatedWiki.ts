import type { Citation, WikiPageRef } from "./api";

export interface RelatedWikiPage {
  id: string;
  title: string;
  breadcrumb: string;
  score: number;
}

const STOP_WORDS = new Set([
  "about",
  "after",
  "also",
  "and",
  "are",
  "code",
  "does",
  "file",
  "for",
  "from",
  "how",
  "into",
  "its",
  "method",
  "page",
  "repo",
  "repository",
  "source",
  "that",
  "the",
  "this",
  "what",
  "when",
  "where",
  "which",
  "with",
]);

function normalize(value: string): string {
  return value
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[^a-zA-Z0-9]+/g, " ")
    .trim()
    .toLowerCase();
}

function stem(token: string): string {
  let value = token;
  if (value.length > 6 && value.endsWith("ing")) {
    value = value.slice(0, -3);
  } else if (value.length > 5 && value.endsWith("ed")) {
    value = value.slice(0, -2);
  }
  if (
    value.length > 3 &&
    value.at(-1) === value.at(-2) &&
    !value.endsWith("ss")
  ) {
    value = value.slice(0, -1);
  }
  if (value.length > 5 && value.endsWith("ies")) {
    return `${value.slice(0, -3)}y`;
  }
  if (value.length > 4 && value.endsWith("s") && !value.endsWith("ss")) {
    return value.slice(0, -1);
  }
  return value;
}

function tokens(value: string): Set<string> {
  return new Set(
    normalize(value)
      .split(/\s+/)
      .map(stem)
      .filter((token) => token.length >= 3 && !STOP_WORDS.has(token)),
  );
}

function compact(value: string): string {
  return [...tokens(value)].join("");
}

function flattenPages(
  pages: readonly WikiPageRef[],
  parents: readonly string[] = [],
): RelatedWikiPage[] {
  const flattened: RelatedWikiPage[] = [];
  for (const page of pages) {
    const breadcrumb = [...parents, page.title].join(" / ");
    if (page.cache_state === "ready") {
      flattened.push({ id: page.id, title: page.title, breadcrumb, score: 0 });
    }
    flattened.push(...flattenPages(page.children ?? [], [...parents, page.title]));
  }
  return flattened;
}

/** Rank cached Wiki pages against the question and exact source citations. */
export function relatedWikiPages(
  pages: readonly WikiPageRef[],
  question: string,
  answer: string,
  citations: readonly Citation[],
  limit = 3,
): RelatedWikiPage[] {
  if (!pages.length || limit <= 0) return [];
  const queryTokens = tokens(question);
  const citationText = citations
    .map((citation) => `${citation.file ?? ""} ${citation.node_name ?? ""}`)
    .join(" ");
  const citationTokens = tokens(citationText);
  const answerTokens = tokens(answer);
  const normalizedQuery = normalize(question);
  const normalizedCitations = normalize(citationText);
  const compactQuery = compact(question);
  const compactCitations = compact(citationText);

  return flattenPages(pages)
    .filter((page) => page.id !== "overview")
    .map((page) => {
      const pageTokens = tokens(`${page.title} ${page.id}`);
      let score = 0;
      let strongMatches = 0;
      for (const token of pageTokens) {
        if (queryTokens.has(token)) {
          score += 7;
          strongMatches += 1;
        }
        if (citationTokens.has(token)) {
          score += 5;
          strongMatches += 1;
        }
        if (answerTokens.has(token)) score += 1;
      }
      const pagePhrase = normalize(page.title);
      const pageCompact = compact(page.title);
      if (pagePhrase && normalizedQuery.includes(pagePhrase)) {
        score += 16;
        strongMatches += 1;
      }
      if (pagePhrase && normalizedCitations.includes(pagePhrase)) {
        score += 12;
        strongMatches += 1;
      }
      if (
        pageCompact.length >= 6 &&
        (compactQuery.includes(pageCompact) ||
          compactCitations.includes(pageCompact))
      ) {
        score += 10;
        strongMatches += 1;
      }
      return { ...page, score: strongMatches ? score : 0 };
    })
    .filter((page) => page.score >= 5)
    .sort(
      (left, right) =>
        right.score - left.score ||
        left.breadcrumb.split(" / ").length -
          right.breadcrumb.split(" / ").length ||
        left.title.localeCompare(right.title),
    )
    .slice(0, limit);
}
