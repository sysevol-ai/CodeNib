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
  // A call path is drawn natively from its contract and needs no asset.
  const visible = (slots ?? []).filter(
    (slot) =>
      slot.render_contract?.adapter === "flow" ||
      materializedWikiMediaSlots([slot]).length > 0,
  );
  return {
    lead: visible.filter((slot) => slot.placement === "lead"),
    bridge: visible.filter((slot) => slot.placement === "aside"),
    body: visible.filter(
      (slot) => slot.placement !== "lead" && slot.placement !== "aside",
    ),
  };
}


/** Whether a stage has only the call site's fallback, with no written sentence. */
export function isBareHandoff(text: string): boolean {
  return /^(?:hands off to|receives the work from)\s+`[^`]+`\.?(?:\s*\[[ER]\d+\](?:\([^)]*\))?)*\s*$/i.test(
    text.trim(),
  );
}

export interface CallPathStep {
  id: string;
  label: string;
}

/** Order a flow contract's nodes along its one directed path. */
export function callPathSteps(slot: WikiMediaSlot): CallPathStep[] {
  const contract = slot.render_contract;
  if (contract?.adapter !== "flow") return [];
  const { nodes, edges } = contract.data;
  const targets = new Set(edges.map((edge) => edge.target));
  const nextOf = new Map(edges.map((edge) => [edge.source, edge.target]));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const steps: CallPathStep[] = [];
  const seen = new Set<string>();
  let cursor = nodes.find((node) => !targets.has(node.id))?.id;
  while (cursor && byId.has(cursor) && !seen.has(cursor)) {
    seen.add(cursor);
    const node = byId.get(cursor)!;
    steps.push({ id: node.id, label: node.label });
    cursor = nextOf.get(cursor);
  }
  return steps;
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

export interface PageLink {
  id: string;
  title: string;
}

const PAGE_LINK_ITEM = /^[-*]\s+\[([^\]]+)\]\(\?p=([^)\s]+)\)\s*$/;

/**
 * Take the link-only sections ("Related pages", "Explore the system") out of a
 * page. Each is a level-two heading over nothing but `- [Title](?p=id)` items;
 * the reader shows them once, as its own list after the text, instead of a
 * bullet list followed by a second list of the same pages.
 */
export function splitPageLinks(markdown: string): { body: string; links: PageLink[] } {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const keep: string[] = [];
  const links: PageLink[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!/^##(?!#)\s+/.test(line)) {
      keep.push(line);
      index += 1;
      continue;
    }
    let end = index + 1;
    while (end < lines.length && !/^#{1,2}(?!#)\s+/.test(lines[end])) end += 1;
    const items = lines.slice(index + 1, end).filter((item) => item.trim());
    const matches = items.map((item) => PAGE_LINK_ITEM.exec(item.trim()));
    if (items.length > 0 && matches.every(Boolean)) {
      for (const match of matches as RegExpExecArray[]) {
        let id = match[2];
        try {
          id = decodeURIComponent(id);
        } catch {
          // Keep the raw id; the page lookup reports it as missing.
        }
        if (!links.some((link) => link.id === id)) links.push({ id, title: match[1] });
      }
    } else {
      keep.push(...lines.slice(index, end));
    }
    index = end;
  }
  return { body: keep.join("\n").trimEnd(), links };
}

/**
 * The Overview's lead with the repository as its title. Beside the map the
 * page would otherwise open on the word "Overview" and never name what it is
 * an overview of. A lead that does not start with a title is left alone.
 */
export function titleOverviewLead(lead: string, repository: string | undefined): string {
  const name = repository?.trim();
  if (!name) return lead;
  return lead.replace(/^(\s*)#(?!#)\s+.*$/m, (line, indent: string, offset: number) =>
    lead.slice(0, offset).trim() ? line : `${indent}# ${name}`,
  );
}

/** What the index holds for a repository, as the counts shown under its title. */
export function repositoryFacts(
  map: { areas: { symbols: number; files: number }[]; links: { weight: number }[] },
  fileCount?: number,
): { label: string; value: number }[] {
  const areas = map.areas.filter((area) => area.symbols > 0);
  return [
    { label: "areas", value: areas.length },
    { label: "symbols", value: areas.reduce((sum, area) => sum + area.symbols, 0) },
    { label: "files", value: fileCount || areas.reduce((sum, area) => sum + area.files, 0) },
    { label: "recorded references", value: map.links.reduce((sum, link) => sum + link.weight, 0) },
  ];
}
