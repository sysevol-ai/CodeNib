import { describe, expect, it } from "vitest";
import type { Citation, WikiPageRef } from "./api";
import { relatedWikiPages } from "./relatedWiki";

const pages: WikiPageRef[] = [
  { id: "overview", title: "Overview", cache_state: "ready", children: [] },
  {
    id: "configuration-and-evaluation",
    title: "Configuration and Evaluation",
    cache_state: "ready",
    children: [
      {
        id: "stacks-runtime",
        title: "Stacks Runtime",
        cache_state: "ready",
        children: [],
      },
      {
        id: "stack-planning",
        title: "Stack Planning",
        cache_state: "ready",
        children: [],
      },
    ],
  },
  {
    id: "time-and-date-handling",
    title: "Time and Date Handling",
    cache_state: "ready",
    children: [
      {
        id: "date-formatting-and-parsing",
        title: "Date Formatting and Parsing",
        cache_state: "ready",
        children: [],
      },
    ],
  },
];

function citation(file: string, node_name: string): Citation {
  return {
    file,
    start_line: 1,
    end_line: 10,
    node_name,
    type: "function",
    score: 1,
    content: "source",
  };
}

describe("relatedWikiPages", () => {
  it("links a source-backed Stack question to the stacks Wiki", () => {
    const related = relatedWikiPages(
      pages,
      "What does Stack.Components do?",
      "Stack.Components lazily builds the runtime component map.",
      [
        citation(
          "internal/stacks/stackruntime/internal/stackeval/stack.go",
          "Stack.Components()",
        ),
      ],
    );

    expect(related[0].id).toBe("stacks-runtime");
    expect(related.map((page) => page.id)).toContain("stack-planning");
    expect(related.map((page) => page.id)).not.toContain("overview");
  });

  it("links time formatting evidence to date Wiki pages", () => {
    const related = relatedWikiPages(
      pages,
      "How is SystemTime formatted?",
      "The shared time module formats zoned dates.",
      [citation("src/uucore/src/lib/features/time.rs", "format_system_time()")],
    );

    expect(related.map((page) => page.id)).toEqual([
      "time-and-date-handling",
      "date-formatting-and-parsing",
    ]);
  });

  it("does not manufacture links without a question or citation match", () => {
    expect(relatedWikiPages(pages, "Where is the parser?", "It is here.", [])).toEqual([]);
  });

  it("does not recommend cold, retryable, or degraded pages", () => {
    const unavailable: WikiPageRef[] = [
      {
        id: "stacks-cold",
        title: "Stacks Cold",
        cache_state: "cold",
        children: [],
      },
      {
        id: "stacks-retryable",
        title: "Stacks Retryable",
        cache_state: "retryable",
        children: [],
      },
      {
        id: "stacks-degraded",
        title: "Stacks Degraded",
        cache_state: "degraded",
        children: [],
      },
      {
        id: "stacks-ready",
        title: "Stacks Ready",
        cache_state: "ready",
        children: [],
      },
    ];

    expect(
      relatedWikiPages(
        unavailable,
        "How do stacks work?",
        "Stacks are described by the runtime.",
        [citation("src/stacks.ts", "Stacks")],
      ).map((page) => page.id),
    ).toEqual(["stacks-ready"]);
  });
});
