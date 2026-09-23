import { describe, expect, it } from "vitest";
import type { WikiMediaSlot, WikiPage } from "./api";
import {
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
