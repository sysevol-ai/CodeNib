// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { allWikiPages, wikiStages, type SavedWiki } from "./visitorWiki";

/** Stages a chapter goes through; any of them means "writing this chapter". */
export const PAGE_STAGES = new Set(["retrieving", "planning_page", "writing", "editing", "reviewing", "checking"]);
const PHASES = ["queued", "connecting", "downloading", "analyzing", "indexing", "outline", "pages", "saving_graphs", "complete"];

export type RunStepState = "done" | "current" | "next" | "withheld";

export interface RunStep {
  id: string;
  label: string;
  detail?: string;
  state: RunStepState;
  /** When the step started, in seconds since the epoch. */
  at?: number;
  /** A finished chapter's writing time, or how long the current step has run. */
  seconds?: number;
  pageId?: string;
}

const phaseOf = (stage: string) => (PAGE_STAGES.has(stage) ? "pages" : stage === "paused" ? "complete" : stage);
const count = (value: number, one: string, many = `${one}s`) => `${value.toLocaleString()} ${value === 1 ? one : many}`;

/** When the latest run started; a resumed run counts from its own start. */
export function runStarted(wiki: SavedWiki): number {
  const start = wiki.history.map((event) => event.stage).lastIndexOf("connecting");
  return wiki.history[Math.max(start, 0)]?.at ?? wiki.created_at;
}

/** The run as a list of steps, each with the facts the server reported for it. */
export function runSteps(wiki: SavedWiki, now: number): RunStep[] {
  const running = wiki.status === "running" || wiki.status === "queued";
  // A resumed run repeats setup; only the latest run's events describe it.
  const start = wiki.history.map((event) => event.stage).lastIndexOf("connecting");
  const events = start > 0 ? wiki.history.slice(start) : wiki.history;
  const reached = Math.max(
    ...events.map((event) => PHASES.indexOf(phaseOf(event.stage))),
    running ? PHASES.indexOf(phaseOf(wiki.stage)) : -1,
  );
  const first = (stage: string) => events.find((event) => event.stage === stage)?.at;
  const pages = allWikiPages(wiki.pages);
  const steps: RunStep[] = [];

  const setup = (stage: string, done: string, current: string, detail?: string) => {
    const at = first(stage);
    const phase = PHASES.indexOf(stage);
    if (running && wiki.stage === stage) {
      steps.push({ id: stage, label: current, detail, state: "current", at, seconds: at === undefined ? undefined : now - at });
    } else if (at !== undefined) {
      steps.push({ id: stage, label: done, detail, state: "done", at });
    } else if (running && phase > reached && stage !== "indexing") {
      steps.push({ id: stage, label: current, state: "next" });
    }
  };

  if (running && wiki.stage === "queued") {
    steps.push({ id: "queued", label: "Waiting for a generation slot", state: "current" });
  }
  setup("connecting", "Connected to your model account", "Connecting to your model account");
  setup(
    "downloading",
    "Downloaded the snapshot",
    "Downloading the snapshot",
    wiki.commit ? wiki.commit.slice(0, 8) : undefined,
  );
  const tree = wiki.source_tree;
  const skipped = wiki.skipped_files?.length ?? 0;
  const read = [
    wiki.languages?.length ? wiki.languages.join(", ") : "",
    skipped ? `${count(skipped, "file")} over 4 MiB skipped` : "",
    wiki.code_index && !wiki.code_index.available && wiki.code_index.reason === "unsupported_language"
      ? "no code graph: hosted graphs cover Python only"
      : "",
  ].filter(Boolean).join(" · ");
  setup("analyzing", tree ? `Read ${count(tree.files, "code file")}` : "Read the source", "Reading the source files", read || undefined);
  const index = wiki.code_index;
  setup(
    "indexing",
    index?.available && index.files
      ? `Indexed ${count(index.files, "Python file")}`
      : index
        ? "Code index not built"
        : "Indexed code relationships",
    "Indexing code relationships",
    index?.available && index.nodes != null && index.edges != null
      ? `${count(index.nodes, "graph node")} · ${count(index.edges, "edge")}`
      : running && wiki.stage === "indexing"
        ? "No model request in this step"
        : index?.note,
  );
  setup("outline", pages.length ? `Planned ${count(pages.length, "chapter")}` : "Planned the chapters", "Planning the chapters");

  if (!pages.length && running && reached <= PHASES.indexOf("outline")) {
    steps.push({ id: "chapters", label: "Chapters", detail: "Appear here and on the map once planned", state: "next" });
  }
  pages.forEach((page) => {
    const state = wiki.page_states[page.id] ?? "pending";
    const own = events.filter((event) => event.page === page.id);
    const at = own[0]?.at;
    if (state === "ready") {
      const last = own.at(-1);
      const after = last ? events.find((event) => event.at >= last.at && event.page !== page.id) : undefined;
      const seconds = at !== undefined && after ? after.at - at : undefined;
      steps.push({ id: `page:${page.id}`, pageId: page.id, label: page.title, state: "done", at, seconds });
    } else if (state === "needs_review") {
      steps.push({ id: `page:${page.id}`, pageId: page.id, label: page.title, detail: "Withheld: it did not pass source or reader checks", state: "withheld", at });
    } else if (running && (state === "running" || page.id === wiki.active_page)) {
      steps.push({
        id: `page:${page.id}`,
        pageId: page.id,
        label: page.title,
        detail: wikiStages[wiki.stage],
        state: "current",
        at,
        seconds: at === undefined ? undefined : now - at,
      });
    } else {
      steps.push({ id: `page:${page.id}`, pageId: page.id, label: page.title, state: "next" });
    }
  });

  const saving = first("saving_graphs");
  if (running && wiki.stage === "saving_graphs") {
    steps.push({ id: "saving_graphs", label: "Saving the maps", state: "current", at: saving, seconds: saving === undefined ? undefined : now - saving });
  } else if (wiki.status === "complete") {
    steps.push({ id: "saving_graphs", label: "Saved the maps", state: "done", at: saving });
  } else if (running) {
    steps.push({ id: "saving_graphs", label: "Save the maps and finish", state: "next" });
  }
  return steps;
}

/** The run's headline, from the reader's point of view. */
export function runTitle(wiki: SavedWiki, ready: number): string {
  if (wiki.status === "complete") return "Your Wiki is ready";
  if (wiki.status === "partial") return ready === 0 ? "Generation stopped" : "Generation paused";
  const phase = phaseOf(wiki.stage);
  if (phase === "queued") return "Waiting to start";
  if (phase === "outline") return "Planning the chapters";
  if (phase === "pages") return ready === 0 ? "Writing the first chapter" : "Writing the Wiki";
  if (phase === "saving_graphs") return "Saving the maps";
  return `Reading ${wiki.repository.split("/").pop() || wiki.repository}`;
}

/** What the map shows right now, said in one sentence. */
export function runLead(wiki: SavedWiki, ready: number): string {
  if (wiki.status === "partial")
    return ready === 0
      ? "Nothing runs until the owner resumes it."
      : "Ready chapters stay saved and readable. Nothing runs until the owner resumes it.";
  if (wiki.status === "complete") return "Every chapter passed its source checks.";
  const phase = phaseOf(wiki.stage);
  if (phase === "pages" || phase === "saving_graphs")
    return ready === 0
      ? "Each chapter fills in on the map once its citations check out against the source. The first one usually takes a few minutes."
      : "Filled chapters are ready to read. The rest fill in on the map as they pass their source checks.";
  if (phase === "outline")
    return "The model is proposing chapters from the source and its structure. They take the place of the folders on the map.";
  if (phase === "queued") return "Your run starts when a generation slot opens.";
  return "The map shows the code being read, folder by folder. Chapters replace it once they are planned.";
}

/** Elapsed seconds as the reader would say them: 42 s, 3:07, 1:02:09. */
export function clock(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value} s`;
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const rest = String(value % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}
