// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { allWikiPages, wikiStages, type SavedWiki } from "./visitorWiki";

const work: Record<string, { description: string; next: string }> = {
  queued: {
    description:
      "Waiting for a generation slot before processing your repository.",
    next: "Read the repository source.",
  },
  connecting: {
    description: "Preparing this generation run and its model connection.",
    next: "Download the repository at the saved commit.",
  },
  downloading: {
    description: "Fetching the pinned repository and skipping oversized files.",
    next: "Identify the source files and symbols.",
  },
  analyzing: {
    description: "Identifying source files and symbols to ground the Wiki.",
    next: "Plan a chapter list from the repository's structure.",
  },
  outline: {
    description:
      "Reading repository context and asking the model to propose the chapter list.",
    next: "Find the source evidence for the first chapter.",
  },
  retrieving: {
    description: "Searching and ranking source evidence for this chapter.",
    next: "Organize that evidence into a chapter plan.",
  },
  planning_page: {
    description:
      "Asking the model to compose this chapter from source evidence and fix unsupported claims.",
    next: "Turn the supported content into a readable chapter.",
  },
  writing: {
    description: "Assembling the chapter's explanation and supporting visuals.",
    next: "Check its source citations, then save the chapter for reading.",
  },
  editing: {
    description:
      "Reviewing whether each section explains its topic clearly, with supported examples and design reasons.",
    next: "Assemble the reviewed chapter and check its source citations.",
  },
  reviewing: {
    description:
      "Reading the finished chapter to assess whether it explains the problem, path, decisions and boundaries.",
    next: "Save the chapter and its reading assessment.",
  },
  checking: {
    description:
      "Checking the chapter against its source before publishing it.",
    next: "Save the chapter if it passes, then move to the next one.",
  },
};

export function formatWait(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value}s`;
  return `${Math.floor(value / 60)}m ${value % 60}s`;
}

export function wikiActivity(wiki: SavedWiki, now: number, connected = true) {
  const running = wiki.status === "running" || wiki.status === "queued";
  const pages = allWikiPages(wiki.pages);
  const chapter = pages.find((page) => page.id === wiki.active_page)?.title;
  // Provider accounting also updates updated_at. Only a matching stage event
  // can establish how long this step has been running.
  const stageStarted = [...wiki.history]
    .reverse()
    .find(
      (event) => event.stage === wiki.stage && event.page === wiki.active_page,
    )?.at;
  const progressAge = Math.max(0, now - wiki.updated_at);
  const delayed = running && (wiki.stalled || progressAge >= 180);
  const waiting = running && progressAge >= 45;
  const current = work[wiki.stage] || {
    description: "Processing the current Wiki step.",
    next: "Save each chapter as it completes its source checks.",
  };
  const recent: SavedWiki["history"] = [];
  for (const event of wiki.history) {
    const last = recent.at(-1);
    if (!last || last.stage !== event.stage || last.page !== event.page)
      recent.push(event);
  }
  return {
    running,
    chapter,
    description: current.description,
    next: current.next,
    stageAge:
      stageStarted === undefined ? null : Math.max(0, now - stageStarted),
    progressAge,
    delayed,
    animate: running && connected && !delayed,
    tone: !connected || delayed || waiting ? "waiting" : "active",
    label: !connected
      ? "Connection interrupted"
      : delayed
        ? "No recent progress reported"
        : waiting
          ? "Waiting for the next update"
          : "Generation in progress",
    explanation: !connected
      ? "The last status could not be refreshed. The server may still be working; reconnecting does not start another run."
      : delayed
        ? "The server is responding, but the worker has not reported progress for a while. This does not confirm that it has stopped. Saved chapters remain available."
        : waiting
          ? "This operation has not reported a new result yet. Model requests can take a minute or longer; the page will update when the operation returns."
          : "A step can involve several model requests. The next update appears when an operation finishes.",
    recent: recent.slice(-3).map((event) => ({
      ...event,
      label: wikiStages[event.stage] || event.stage,
      chapter: pages.find((page) => page.id === event.page)?.title,
    })),
  };
}
