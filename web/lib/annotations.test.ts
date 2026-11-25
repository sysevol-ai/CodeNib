import { describe, expect, it } from "vitest";
import { keyProvider, linkCodeRefs, passageSimilarity, squash, threadsOf, usageLine, type Annotation } from "./annotations";

function note(id: string, extra: Partial<Annotation> = {}): Annotation {
  return {
    id,
    kind: "question",
    text: id,
    author: "human",
    status: "draft",
    on: { page: "p", quote: "q" },
    reply_to: null,
    task: null,
    files: [],
    created: `2026-11-23T10:00:0${id.length}Z`,
    updated: "",
    ...extra,
  };
}

describe("linkCodeRefs", () => {
  it("turns path:lines into evidence links, once per reference", () => {
    const { markdown, refs } = linkCodeRefs(
      "See `src/a/sessions.py:870-881` and src/a/sessions.py:870-881, then `x.py:5`.",
    );
    expect(markdown).toBe("See [E1](#evidence-E1) and [E1](#evidence-E1), then [E2](#evidence-E2).");
    expect(refs).toEqual([
      { path: "src/a/sessions.py", start_line: 870, end_line: 881 },
      { path: "x.py", start_line: 5, end_line: 5 },
    ]);
  });

  it("leaves fenced code and plain identifiers alone", () => {
    const text = "```\nsrc/a.py:1-2\n```\n`Session.send()` at 3:4";
    expect(linkCodeRefs(text)).toEqual({ markdown: text, refs: [] });
  });
});

describe("threadsOf", () => {
  it("groups replies under the first annotation of their thread, in order", () => {
    const root = note("A");
    const answer = note("BB", { author: "agent", kind: "answer", reply_to: "A" });
    const follow = note("CCC", { reply_to: "BB" });
    const other = note("D");
    const threads = threadsOf([follow, other, answer, root]);
    expect(threads.map((t) => [t.root.id, t.replies.map((r) => r.id)])).toEqual([
      ["A", ["BB", "CCC"]],
      ["D", []],
    ]);
  });

  it("treats a reply whose parent is on another page as its own thread", () => {
    const orphan = note("E", { reply_to: "missing" });
    expect(threadsOf([orphan]).map((t) => t.root.id)).toEqual(["E"]);
  });
});

describe("usageLine", () => {
  it("names turns, tokens, cost and time", () => {
    expect(
      usageLine({ turns: 5, input_tokens: 20024, output_tokens: 1080, cost_usd: 0.041576, duration_ms: 8488 }),
    ).toBe("5 turns · 21.1k tokens · $0.042 · 8 s");
    expect(usageLine({})).toBe("");
  });
});

describe("squash", () => {
  it("collapses whitespace the way the page text is compared", () => {
    expect(squash("  hands it\n to an   adapter ")).toBe("hands it to an adapter");
  });
});

describe("passageSimilarity", () => {
  it("scores the words two passages share, ignoring common ones", () => {
    expect(passageSimilarity("Session send picks the adapter", "Session send picks the adapter")).toBe(1);
    expect(passageSimilarity("send picks an adapter by URL", "send then picks the adapter for each URL")).toBeGreaterThan(0.5);
    expect(passageSimilarity("send picks an adapter", "cookies are merged into jars")).toBe(0);
    expect(passageSimilarity("", "anything")).toBe(0);
  });
});

describe("keyProvider", () => {
  it("tells an OpenRouter key from an Anthropic key, and refuses anything else", () => {
    expect(keyProvider("sk-or-v1-" + "0".repeat(40))).toBe("OpenRouter");
    expect(keyProvider("  sk-ant-api03-" + "1".repeat(40) + "  ")).toBe("Anthropic");
    expect(keyProvider("sk-proj-" + "2".repeat(40))).toBeNull();
    expect(keyProvider("sk-or-short")).toBeNull();
    expect(keyProvider("sk-or-v1-has a space in it 0000000")).toBeNull();
  });
});
