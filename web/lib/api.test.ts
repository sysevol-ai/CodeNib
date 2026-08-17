import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchEdgeLabel,
  fetchWikiTree,
  fetchWikiPage,
  isSourceCheckedWikiPage,
  materializedWikiMediaSlots,
  shouldWithholdWikiPage,
  type WikiMediaSlot,
} from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchEdgeLabel", () => {
  it("sends only the call-site sample consumed by the server", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ label: "calls helper", cached: false, disabled: false }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await fetchEdgeLabel("repo", {
      source: { file: "src/a.py", line: 1, end_line: 2, label: "a" },
      target: { file: "src/b.py", line: 3, end_line: 4, label: "b" },
      anchors: Array.from({ length: 20 }, (_, index) => ({
        file: "src/a.py",
        line: index + 1,
      })),
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const payload = JSON.parse(String(init.body));
    expect(payload.anchors).toHaveLength(3);
    expect(payload.anchors[2]).toEqual({ file: "src/a.py", line: 3 });
  });
});

describe("fetchWikiPage", () => {
  it("surfaces a FastAPI error detail", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      text: async () =>
        JSON.stringify({
          detail: "Retrieval index unavailable; rebuild the vector artifact.",
        }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchWikiPage("repo", "overview")).rejects.toThrow(
      "Failed to load page (503): Retrieval index unavailable; rebuild the vector artifact.",
    );
  });

  it("bounds an untrusted response detail", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      text: async () => JSON.stringify({ detail: "x".repeat(600) }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchWikiPage("repo", "overview")).rejects.toThrow(
      `Failed to load page (502): ${"x".repeat(500)}`,
    );
  });

  it("keeps the status when the error body cannot be read", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      text: async () => {
        throw new Error("body stream failed");
      },
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchWikiPage("repo", "overview")).rejects.toThrow(
      "Failed to load page (500)",
    );
  });

  it("coalesces warm navigation and supports an explicit refresh", async () => {
    const payload = {
      id: "runtime",
      title: "Runtime",
      markdown: "Cached page",
      citations: [],
      diagram: "",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all([
      fetchWikiPage("cache-test", "runtime"),
      fetchWikiPage("cache-test", "runtime"),
    ]);
    await fetchWikiPage("cache-test", "runtime");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await fetchWikiPage("cache-test", "runtime", { refresh: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("fetchWikiTree", () => {
  it("requests a read-only cached tree for optional related links", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ repo: "owner/repo", pages: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await fetchWikiTree("owner/repo", { cachedOnly: true });

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(
        /\/api\/repos\/owner%2Frepo\/wiki\?cached_only=true$/,
      ),
    );
  });
});

describe("shouldWithholdWikiPage", () => {
  const page = {
    id: "runtime",
    title: "Runtime",
    markdown: "Readable text",
    citations: [],
    diagram: "",
  };

  it("withholds fallback and quality-invalid degraded drafts", () => {
    expect(
      shouldWithholdWikiPage({
        ...page,
        generation: {
          mode: "degraded",
          model: "test",
          fallback: "fact_plan",
        },
      }),
    ).toBe(true);
    expect(
      shouldWithholdWikiPage({
        ...page,
        generation: { mode: "degraded", model: "test" },
        quality: {
          valid: false,
          planned_sections: 1,
          required_sections: 2,
          rendered_sections: 1,
          substantive_blocks: 1,
          required_blocks: 2,
          covered_claims: 1,
          planned_claims: 2,
          claim_coverage: 0.5,
        },
      }),
    ).toBe(true);
  });

  it("keeps readable degraded pages whose source checks remain valid", () => {
    expect(
      shouldWithholdWikiPage({
        ...page,
        generation: { mode: "degraded", model: "test" },
        grounding: {
          valid: true,
          citation_coverage: 1,
          evidence_count: 1,
          relation_count: 0,
        },
        quality: {
          valid: true,
          planned_sections: 1,
          required_sections: 1,
          rendered_sections: 1,
          substantive_blocks: 2,
          required_blocks: 2,
          covered_claims: 1,
          planned_claims: 1,
          claim_coverage: 1,
        },
      }),
    ).toBe(false);
  });
});

describe("isSourceCheckedWikiPage", () => {
  const checkedPage = {
    id: "runtime",
    title: "Runtime",
    markdown: "Readable text",
    citations: [],
    diagram: "",
    generation: { mode: "degraded" as const, model: "test" },
    grounding: {
      valid: true,
      citation_coverage: 1,
      evidence_count: 2,
      relation_count: 1,
    },
    quality: {
      valid: true,
      planned_sections: 1,
      required_sections: 1,
      rendered_sections: 1,
      substantive_blocks: 2,
      required_blocks: 2,
      covered_claims: 2,
      planned_claims: 2,
      claim_coverage: 1,
    },
  };

  it("accepts review-mode prose when both strict checks pass", () => {
    expect(isSourceCheckedWikiPage(checkedPage)).toBe(true);
  });

  it("rejects offline, fallback, and invalid pages", () => {
    expect(
      isSourceCheckedWikiPage({
        ...checkedPage,
        generation: { mode: "offline", model: null },
      }),
    ).toBe(false);
    expect(
      isSourceCheckedWikiPage({
        ...checkedPage,
        generation: {
          mode: "degraded",
          model: "test",
          fallback: "fact_plan",
        },
      }),
    ).toBe(false);
    expect(
      isSourceCheckedWikiPage({
        ...checkedPage,
        quality: { ...checkedPage.quality, valid: false },
      }),
    ).toBe(false);
  });
});

describe("materializedWikiMediaSlots", () => {
  const planned = {
    id: "overview-concept",
    kind: "image",
    placement: "section",
    title: "Overview concept",
    purpose: "Explain the source-linked flow.",
    source_citations: ["src/runtime.py"],
    prompt: "Draw the runtime flow.",
    human_prior: { editable: true, notes: [] },
  } satisfies WikiMediaSlot;

  it("hides internal plans and keeps only generated reader assets", () => {
    const generated = {
      ...planned,
      id: "overview-generated",
      asset: {
        slot_id: "overview-generated",
        kind: "image",
        uri: "/api/repos/demo/wiki-media/overview/generated.svg",
        mime_type: "image/svg+xml",
        model: "local/svg",
        provider: "local",
        prompt: "Draw the runtime flow.",
        source_citations: ["src/runtime.py"],
      },
    } satisfies WikiMediaSlot;

    expect(materializedWikiMediaSlots([planned, generated])).toEqual([generated]);
    expect(materializedWikiMediaSlots(undefined)).toEqual([]);
  });
});
