import {
  materializedWikiMediaSlots,
  type WikiMediaSlot,
  type WikiPage,
} from "./api";

export interface WikiMarkdownParts {
  lead: string;
  body: string;
}

/** Split a Wiki document before its first level-two section.
 *
 * The title and opening thesis stay together as the editorial lead. Fenced
 * code is tracked so a Markdown-looking line inside an example cannot split
 * the page accidentally.
 */
export function splitWikiMarkdown(markdown: string): WikiMarkdownParts {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  let fence: { marker: "`" | "~"; length: number } | null = null;
  let splitAt = -1;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    const fenceMatch = line.match(/^\s{0,3}(`{3,}|~{3,})/);
    if (fenceMatch) {
      const token = fenceMatch[1];
      const marker = token[0] as "`" | "~";
      if (!fence) {
        fence = { marker, length: token.length };
      } else if (fence.marker === marker && token.length >= fence.length) {
        fence = null;
      }
      continue;
    }
    if (!fence && /^\s{0,3}##(?!#)\s+/.test(line)) {
      splitAt = index;
      break;
    }
  }

  if (splitAt < 0) {
    return { lead: lines.join("\n").trim(), body: "" };
  }
  return {
    lead: lines.slice(0, splitAt).join("\n").trim(),
    body: lines.slice(splitAt).join("\n").trim(),
  };
}

export function partitionWikiMediaSlots(
  slots: WikiMediaSlot[] | null | undefined,
): { lead: WikiMediaSlot[]; bridge: WikiMediaSlot[]; body: WikiMediaSlot[] } {
  const visible = materializedWikiMediaSlots(slots);
  return {
    lead: visible.filter((slot) => slot.placement === "lead"),
    bridge: visible.filter((slot) => slot.placement === "aside"),
    body: visible.filter(
      (slot) => slot.placement !== "lead" && slot.placement !== "aside",
    ),
  };
}


export interface WikiRetryNotice {
  headline: string;
  detail: string;
  /** When the next automatic regeneration becomes possible; null when none is. */
  nextAttemptEpoch: number | null;
}

/** Explain a withheld page in terms of what the backend will actually do.
 *
 * A degraded page is regenerated only when a reader opens it after its
 * persisted cooldown, a bounded number of times. Nothing runs in the
 * background, so the notice must not promise that.
 */
export function wikiRetryNotice(
  page: WikiPage | null | undefined,
  nowEpoch: number = Date.now() / 1000,
): WikiRetryNotice {
  const headline = "A readable source-linked explanation is not available yet.";
  const cause =
    "Source evidence was retrieved, but the generated prose did not pass validation, so the diagnostic draft is hidden.";
  const retry = page?.generation?.retry;
  if (!retry) {
    return {
      headline,
      detail: `${cause} It is regenerated the next time this page is opened after a cooldown.`,
      nextAttemptEpoch: null,
    };
  }
  const used = `${retry.attempts} of ${retry.max_attempts} automatic retries used.`;
  if (retry.state === "exhausted" || retry.attempts >= retry.max_attempts) {
    return {
      headline,
      detail: `${cause} ${used} Re-run the wiki prewarm for this repository to regenerate it.`,
      nextAttemptEpoch: null,
    };
  }
  const next = retry.next_attempt_epoch;
  if (typeof next === "number" && next > nowEpoch) {
    return {
      headline,
      detail: `${cause} It is regenerated the next time this page is opened after the cooldown ends. ${used}`,
      nextAttemptEpoch: next,
    };
  }
  return {
    headline,
    detail: `${cause} It is regenerated the next time this page is opened. ${used}`,
    nextAttemptEpoch: null,
  };
}
