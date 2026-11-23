// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import Walkthrough, { WalkthroughList } from "./Walkthrough";
import WikiToc from "./WikiToc";
import type { Walk, WalkthroughSet } from "@/lib/walkthrough";

const walk: Walk = {
  id: "run",
  tab: "Running a task",
  title: "How <code>agent.run()</code> works",
  dek: "Three calls <img src=x onerror=alert(1)> deep.",
  question: "What happens when I call agent.run(task)?",
  chosen: "question and answer function chosen by hand",
  map: "Every step stays inside Agent Runtime.",
  stops: [
    ["", "agent.run(task)", null, null, true],
    ["Agent", ".run()", "agents.py:10–12", "starts the loop"],
    ["", "execute()", "executor.py:5–9", "runs the code"],
  ],
  segs: [
    ["", ""],
    ["not in the index :12", "agents.py:12", "dash", "not in the index", "named"],
  ],
  zones: [
    ["agents.py", "Agent Runtime", [1]],
    ["executor.py", "Execution", [2]],
  ],
  areas: ["agent-runtime", "execution"],
  cross: [["agent-runtime", "execution", 4, 2, "unrecorded"]],
  steps: [
    { title: "Overview", lead: "Two functions.", at: null, runs: [0], table: "stops" },
    { title: "<code>run()</code> starts", lead: "It calls.", rail: "run()", at: 1, runs: [0], area: "agent-runtime", code: ["src/agents.py", [[10, 12]], [12]] },
    { title: "<code>execute()</code> runs it", lead: "It runs.", rail: "execute()", at: 2, runs: [1], area: "execution", code: ["src/executor.py", [[5, 5]], []] },
  ],
};

const set: WalkthroughSet = {
  version: 1,
  repo: { id: "acme__agent", slug: "acme/agent", commit: "0123456789abcdef", model: "deepseek/deepseek-v4-flash" },
  areas: [
    { id: "agent-runtime", title: "Agent Runtime", name: "Agent Runtime", color: "#3987e5", symbols: 400, files: 3 },
    { id: "execution", title: "Execution", name: "Execution", color: "#d95926", symbols: 100, files: 2 },
  ],
  links: [],
  paths: { "agents.py": "src/agents.py", "executor.py": "src/executor.py" },
  wiki: {
    pages: { execution: { title: "Running code", top: "execution", depth: 0 } },
    cites: {},
    size: { execution: [100, 2] },
    calls: { "agent-runtime>execution": 4 },
  },
  walks: [walk, { ...walk, id: "tool", tab: "Calling a tool" }],
  source: {
    "src/agents.py": { "10": "def run(self):", "11": "    code = self.plan()", "12": "    return self.executor.execute(code)" },
    "src/executor.py": { "5": "def execute(code):" },
  },
};

const render = (step: number) =>
  renderToStaticMarkup(<Walkthrough set={set} walk={walk} step={step} onGo={vi.fn()} onPage={vi.fn()} />);

describe("Walkthrough", () => {
  it("shows generated code markup and escapes any other tag", () => {
    const html = render(0);
    expect(html).toContain("How <code>agent.run()</code> works");
    expect(html).toContain("&lt;img src=x");
    expect(html).not.toContain("<img");
  });

  it("indexes the path's functions on the overview, each a way into its step", () => {
    const html = render(0);
    expect(html).toContain("2 functions in 2 files");
    expect(html).toContain('aria-label="Go to step 2: Agent.run()"');
    expect(html).toContain("https://github.com/acme/agent/blob/0123456789abcdef/src/agents.py#L10-L12");
  });

  it("shows a step's lines with the noted one marked, and names the next step", () => {
    const html = render(1);
    expect(html).toContain('<span class="ln m"><span class="no">12</span>');
    expect(html).toContain("agents.py 10–12");
    expect(html).toContain('<span class="walk-btn-to">: execute()</span>');
  });

  it("says how a hop the index does not record was followed, and where it crossed in", () => {
    const html = render(2);
    expect(html).toContain("calls a method by this name on a value whose type it does not know");
    expect(html).toContain("crosses in from Agent Runtime; the map records 4 other calls in that direction");
    expect(html).toContain("The Wiki does not cite <code>execute()</code>");
    expect(html).toContain('class="walk-page-link">Running code</button>');
  });

  it("goes on to the next walkthrough from the last step", () => {
    expect(render(2)).toContain('<span class="walk-btn-to">: Calling a tool</span>');
  });

  it("lists the walkthroughs on the Overview by their questions", () => {
    const html = renderToStaticMarkup(<WalkthroughList set={set} onOpen={vi.fn()} />);
    expect(html).toContain("What happens when I call agent.run(task)?");
    expect(html).toContain("3 steps through 2 functions");
  });

  it("lists the walkthroughs in the page list, the open one current", () => {
    const html = renderToStaticMarkup(
      <WikiToc
        pages={[{ id: "overview", title: "Overview", children: [] }]}
        activeId=""
        onPick={vi.fn()}
        walks={[{ id: "run", label: "Running a task" }, { id: "tool", label: "Calling a tool" }]}
        activeWalk="tool"
        onWalk={vi.fn()}
      />,
    );
    expect(html).toContain("Walkthroughs");
    expect(html).toMatch(/class="toc-link active" aria-current="page" title="Calling a tool"/);
    expect(html).not.toMatch(/class="toc-link active"[^>]*title="Overview"/);
  });
});
