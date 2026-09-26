import { describe, expect, it } from "vitest";
import type { WikiMediaSlot, WikiPage } from "./api";
import {
  extractJourney,
  stageRoles,
  stageSentence,
  partitionWikiMediaSlots,
  splitWikiMarkdown,
  wikiRetryNotice,
} from "./wikiPresentation";

function slot(
  id: string,
  placement: WikiMediaSlot["placement"],
  materialized = true,
): WikiMediaSlot {
  return {
    id,
    kind: "image",
    placement,
    title: id,
    purpose: "Explain the page.",
    source_citations: [],
    prompt: "",
    human_prior: { editable: false, notes: [] },
    asset: materialized
      ? {
          slot_id: id,
          kind: "image",
          uri: `assets/${id}.png`,
          mime_type: "image/png",
          model: "repository",
          provider: "repository",
          prompt: "",
          source_citations: [],
        }
      : undefined,
  };
}

describe("splitWikiMarkdown", () => {
  it("keeps the title and thesis together before the first section", () => {
    expect(
      splitWikiMarkdown("# Overview\n\nA useful thesis.\n\n## Runtime\n\nDetails."),
    ).toEqual({
      lead: "# Overview\n\nA useful thesis.",
      body: "## Runtime\n\nDetails.",
    });
  });

  it("does not split on a heading-looking line inside a fence", () => {
    const markdown = "# Overview\n\n```text\n## Example\n```\n\n## Runtime\nBody";
    const parts = splitWikiMarkdown(markdown);

    expect(parts.lead).toContain("## Example");
    expect(parts.body).toBe("## Runtime\nBody");
  });
});

describe("partitionWikiMediaSlots", () => {
  it("puts only materialized lead assets before the story body", () => {
    const result = partitionWikiMediaSlots([
      slot("repository-map", "lead"),
      slot("reading-map", "aside"),
      slot("generated-detail", "section"),
      slot("planned-only", "lead", false),
    ]);

    expect(result.lead.map((item) => item.id)).toEqual(["repository-map"]);
    expect(result.bridge.map((item) => item.id)).toEqual(["reading-map"]);
    expect(result.body.map((item) => item.id)).toEqual(["generated-detail"]);
  });
});


describe("wikiRetryNotice", () => {
  function degraded(retry: NonNullable<WikiPage["generation"]>["retry"]): WikiPage {
    return {
      id: "p",
      title: "P",
      markdown: "",
      citations: [],
      generation: { mode: "degraded", model: null, reason: "quality_guard", retry },
    } as unknown as WikiPage;
  }

  it("names the cooldown when the next attempt is still ahead", () => {
    const notice = wikiRetryNotice(
      degraded({
        state: "scheduled",
        attempts: 1,
        max_attempts: 2,
        last_attempt_epoch: 1000,
        next_attempt_epoch: 5000,
      }),
      2000,
    );
    expect(notice.nextAttemptEpoch).toBe(5000);
    expect(notice.detail).toContain("after the cooldown ends");
    expect(notice.detail).toContain("1 of 2 automatic retries used");
    expect(notice.detail).not.toContain("background");
  });

  it("says the retry happens on the next open once the window is due", () => {
    const notice = wikiRetryNotice(
      degraded({
        state: "scheduled",
        attempts: 0,
        max_attempts: 2,
        last_attempt_epoch: 1000,
        next_attempt_epoch: 1500,
      }),
      2000,
    );
    expect(notice.nextAttemptEpoch).toBeNull();
    expect(notice.detail).toContain("the next time this page is opened.");
  });

  it("points the operator at the prewarm once retries are exhausted", () => {
    const notice = wikiRetryNotice(
      degraded({
        state: "exhausted",
        attempts: 2,
        max_attempts: 2,
        last_attempt_epoch: 1000,
        next_attempt_epoch: null,
      }),
      2000,
    );
    expect(notice.nextAttemptEpoch).toBeNull();
    expect(notice.detail).toContain("2 of 2 automatic retries used");
    expect(notice.detail).toContain("Re-run the wiki prewarm");
  });

  it("falls back to the cooldown wording without retry metadata", () => {
    const notice = wikiRetryNotice(degraded(undefined), 2000);
    expect(notice.detail).toContain("after a cooldown");
  });
});

describe("extractJourney", () => {
  const md = [
    "# Overview",
    "",
    "Lead. [E1](#evidence-E1)",
    "",
    "## From `get()` to `send()`",
    "",
    "1. **`get()`**: `get()` forwards to `request()`. [E8](#evidence-E8)",
    "2. **`Session.send()`** · [Session State](?p=session-management): sends it. [E14](#evidence-E14)",
    "",
    "## Next",
    "",
    "Body.",
  ].join("\n");

  it("lifts the numbered entry path out of the Markdown", () => {
    const { journey, rest } = extractJourney(md);
    expect(journey?.title).toBe("From `get()` to `send()`");
    expect(journey?.stages).toEqual([
      {
        index: 1,
        symbol: "get()",
        page: undefined,
        text: "`get()` forwards to `request()`. [E8](#evidence-E8)",
        evidence: ["E8"],
      },
      {
        index: 2,
        symbol: "Session.send()",
        page: { id: "session-management", title: "Session State" },
        text: "sends it. [E14](#evidence-E14)",
        evidence: ["E14"],
      },
    ]);
    expect(rest).not.toContain("From `get()`");
    expect(rest).toContain("## Next");
  });

  it("leaves a section with prose in it alone", () => {
    const withProse = md.replace("## Next", "A remark.\n\n## Next");
    expect(extractJourney(withProse).journey).toBeNull();
  });
});

describe("stageRoles", () => {
  const nodes = [
    { id: "api", label: "API", detail: "", evidence: ["E1"] },
    { id: "session", label: "Session", detail: "", evidence: ["E2"] },
    { id: "adapter", label: "Adapter", detail: "", evidence: ["E3", "E4"] },
  ];
  const citations = [
    { file: "api.py" },
    { file: "sessions.py" },
    { file: "adapters.py" },
    { file: "adapters.py" },
    { file: "sessions.py" },
    { file: "models.py" },
  ] as unknown as Parameters<typeof stageRoles>[2];
  const stage = (evidence: string[]) => ({ index: 1, symbol: "x()", text: "", evidence });

  it("assigns by shared evidence, then by a file only one role cites", () => {
    expect(
      stageRoles([stage(["E1"]), stage(["E5"]), stage(["E6"]), stage([])], nodes, citations),
    ).toEqual(["api", "session", null, null]);
  });
});

describe("stageSentence", () => {
  const stage = (symbol: string, text: string) => ({ index: 1, symbol, text, evidence: [] });
  it("drops a leading repeat of the stage symbol", () => {
    expect(stageSentence(stage("get()", "`get()` forwards its URL. [E8](#evidence-E8)"))).toBe(
      "Forwards its URL. [E8](#evidence-E8)",
    );
    expect(stageSentence(stage("Runtime.spawn_blocking", "`Runtime.spawn_blocking()` hands off."))).toBe(
      "Hands off.",
    );
    expect(stageSentence(stage("get()", "`request()` is next."))).toBe("`request()` is next.");
  });
});
