import {
  materializedWikiMediaSlots,
  type Citation,
  type WikiVisualNode,
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

export interface JourneyStage {
  index: number;
  symbol: string;
  /** Owning area page, when the stage names one. */
  page?: { id: string; title: string };
  /** The stage's one admitted sentence, still Markdown (code spans, cites). */
  text: string;
  evidence: string[];
}

export interface Journey {
  title: string;
  stages: JourneyStage[];
}

const JOURNEY_ITEM =
  /^(\d+)\.\s+\*\*`([^`]+)`\*\*(?:\s*·\s*\[([^\]]+)\]\(\?p=([^)\s]+)\))?\s*:\s*(.*)$/;

/**
 * Take the Overview's recorded entry path out of the Markdown.
 *
 * The generator writes it as the first level-two section made only of
 * numbered `**`symbol`**: sentence` stages. When the page also carries a
 * system architecture, the path is drawn inside that card so the reader sees
 * one path, not two; the section is returned separately and removed here.
 * Anything else in the section (prose, other lists) leaves it in place.
 */
export function extractJourney(markdown: string): { journey: Journey | null; rest: string } {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  let start = -1;
  for (let index = 0; index < lines.length; index += 1) {
    if (!/^##\s+/.test(lines[index])) continue;
    let end = index + 1;
    while (end < lines.length && !/^##\s+/.test(lines[end])) end += 1;
    const body = lines.slice(index + 1, end).filter((line) => line.trim());
    const stages = body.map((line) => JOURNEY_ITEM.exec(line.trim()));
    if (stages.length >= 2 && stages.every(Boolean)) {
      start = index;
      const journey: Journey = {
        title: lines[index].replace(/^##\s+/, "").trim(),
        stages: stages.map((m) => {
          const match = m as RegExpExecArray;
          return {
            index: Number(match[1]),
            symbol: match[2],
            page: match[4] ? { id: decodeURIComponent(match[4]), title: match[3] } : undefined,
            text: match[5].trim(),
            evidence: [...match[5].matchAll(/\[(E\d+)\]\(#evidence-E\d+\)/g)].map((e) => e[1]),
          };
        }),
      };
      const rest = [...lines.slice(0, start), ...lines.slice(end)].join("\n");
      return { journey, rest };
    }
  }
  return { journey: null, rest: markdown };
}

/**
 * Which architecture role each journey stage belongs to, when the evidence
 * says so: a stage citing one of a role's evidence ids belongs to it; failing
 * that, a stage whose cited file only one role cites belongs to that role.
 * Anything else stays unassigned rather than guessed.
 */
export function stageRoles(
  stages: JourneyStage[],
  nodes: WikiVisualNode[],
  citations: Citation[] | undefined,
): (string | null)[] {
  const fileOf = (id: string) => {
    const index = Number(id.replace(/^E/, "")) - 1;
    return citations?.[index]?.file || null;
  };
  const roleFiles = new Map(
    nodes.map((node) => [
      node.id,
      new Set(node.evidence.filter((id) => id.startsWith("E")).map(fileOf).filter(Boolean)),
    ]),
  );
  return stages.map((stage) => {
    const byEvidence = nodes.find((node) =>
      node.evidence.some((id) => stage.evidence.includes(id)),
    );
    if (byEvidence) return byEvidence.id;
    const files = stage.evidence.map(fileOf).filter(Boolean);
    const owners = nodes.filter((node) => files.some((file) => roleFiles.get(node.id)?.has(file)));
    return owners.length === 1 ? owners[0].id : null;
  });
}

/** A stage sentence without the symbol its heading already shows:
 *  "`get()` forwards its URL" under a `get()` heading reads "Forwards its URL". */
export function stageSentence(stage: JourneyStage): string {
  const bare = stage.symbol.replace(/\(\)$/, "");
  const lead = new RegExp(
    "^`(?:" + [stage.symbol, bare].map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")(?:\\(\\))?`\\s+",
  );
  const text = stage.text.replace(lead, "");
  // Each stage reads as its own sentence under the symbol heading.
  return /^[a-z]/.test(text) ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}
