// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import {
  breakName,
  citingPages,
  codeRows,
  miniAreas,
  placeBadge,
  placeLabels,
  routeLayout,
  stepForStop,
  stepStates,
  stopArea,
  type Walk,
  type WalkthroughSet,
} from "./walkthrough";

const walk: Walk = {
  id: "run",
  tab: "Running a task",
  title: "How <code>run()</code> works",
  dek: "",
  question: "What happens when I call agent.run(task)?",
  chosen: "chosen by hand",
  map: "",
  stops: [
    ["", "agent.run(task)", null, null, true],
    ["Agent", ".run()", "agents.py:10–20", "starts"],
    ["Agent", "._step()", "agents.py:30–40", "steps"],
    ["", "execute()", "executor.py:5–9", "runs code"],
  ],
  segs: [
    ["", ""],
    [":12", "agents.py:12", "", ""],
    ["not in the index :33", "agents.py:33", "dash", "not in the index", "named"],
  ],
  zones: [
    ["agents.py", "Agent Runtime Layer", [1, 2]],
    ["executor.py", "Execution", [3]],
  ],
  areas: ["agent-runtime", "execution"],
  cross: [["agent-runtime", "execution", 4, 3, "unrecorded"]],
  steps: [
    { title: "Overview", lead: "", at: null, runs: [0], table: "stops" },
    { title: "run()", lead: "", at: 1, runs: [0], code: ["src/agents.py", [[10, 12]], [11]] },
    { title: "_step()", lead: "", at: 2, runs: [1] },
    { title: "execute()", lead: "", at: 3, runs: [2] },
    { title: "Recap", lead: "", at: null, runs: [], recap: true },
  ],
};

const set: WalkthroughSet = {
  version: 1,
  repo: { id: "acme__agent", slug: "acme/agent", commit: "0123456789abcdef" },
  areas: [
    { id: "agent-runtime", title: "Agent Runtime Layer", name: "Agent Runtime Layer", color: "#3987e5", symbols: 400, files: 3 },
    { id: "execution", title: "Execution", name: "Execution", color: "#d95926", symbols: 100, files: 2 },
    { id: "tools", title: "Tools", name: "Tools", color: "#199e70", symbols: 900, files: 4 },
  ],
  links: [],
  paths: { "agents.py": "src/agents.py" },
  wiki: {
    pages: {
      "agent-runtime": { title: "Agent Runtime", top: "agent-runtime", depth: 0 },
      "step-loop": { title: "The step loop", top: "agent-runtime", depth: 1 },
      tools: { title: "Tools", top: "tools", depth: 0 },
    },
    cites: { "Agent.run()": ["tools", "agent-runtime", "step-loop"] },
    size: {},
    calls: {},
  },
  walks: [walk],
  source: {
    "src/agents.py": { "10": "    def run(self):", "11": '        """Run."""', "12": "        return self._step()" },
  },
};

describe("walkthrough state", () => {
  it("marks the step's stop active, earlier stops done and later ones ahead", () => {
    const states = stepStates(walk, 2);
    expect(states.stops).toEqual(["is-done", "is-done", "is-active", "is-future"]);
    expect(states.segs).toEqual(["is-done", "is-active", "is-future"]);
    expect(states.zone).toBe(0);
  });

  it("shows the whole path as done on the overview and the recap", () => {
    for (const index of [0, 4]) {
      const states = stepStates(walk, index);
      expect(states.whole).toBe(true);
      expect(states.stops.every((state) => state === "is-done")).toBe(true);
      expect(states.zone).toBe(-1);
    }
  });

  it("opens each stop's own step, and the overview for the user's call", () => {
    expect(stepForStop(walk, 2)).toBe(2);
    expect(stepForStop(walk, 0)).toBe(0);
  });

  it("reads a stop's area from its file's zone, whatever suffix the name carries", () => {
    expect(stopArea(set, walk, 1)).toBe("agent-runtime");
    expect(stopArea(set, walk, 3)).toBe("execution");
    expect(stopArea(set, walk, 0)).toBeNull();
  });
});

describe("walkthrough layout", () => {
  it("leaves room for a file heading where a stop opens a new file", () => {
    const { ys } = routeLayout(walk, 300, 0);
    expect(ys[1] - ys[0]).toBe(104);
    expect(ys[2] - ys[1]).toBe(60);
    expect(ys[3] - ys[2]).toBe(104);
  });

  it("spreads a short route toward the narration's height, at most 36px a hop", () => {
    const tight = routeLayout(walk, 300, 0);
    const spread = routeLayout(walk, 300, 2000);
    expect(spread.ys[3] - tight.ys[3]).toBe(36 * 3);
  });

  it("breaks a long free function at the underscore nearest its middle", () => {
    expect(breakName("_fit_transform_one_with_callbacks()")).toEqual(["_fit_transform_one", "_with_callbacks()"]);
    expect(breakName("evaluate()")).toBeNull();
  });

  it("puts every area a walk passes through on the small map, then the largest of the rest", () => {
    const areas = miniAreas(set);
    expect(areas.map((area) => area.id)).toEqual(["tools", "agent-runtime", "execution"]);
    expect(areas[1].name).toBe("Agent Runtime");
  });

  it("moves a label that would sit on another to the other side of its area", () => {
    const areas = miniAreas(set).map((area) => ({ ...area, x: 100, y: 100, r: 10, pos: "above" as const, anchor: "middle" as const }));
    const labels = placeLabels(areas.slice(0, 2), () => 60);
    expect(labels[0].y).toBe(82);
    expect(labels[1].y).toBe(128);
  });

  it("places a count where it covers nothing", () => {
    const at = (t: number): [number, number] => [t * 100, 0];
    const [x] = placeBadge(at, [{ x: 30, y: -20, width: 40, height: 40 }]);
    expect(x < 30 - 11 || x > 70 + 11).toBe(true);
  });
});

describe("walkthrough code", () => {
  it("drops the indentation the shown lines share and marks the noted line", () => {
    const rows = codeRows(set, ["src/agents.py", [[10, 12]], [11]]);
    expect(rows.map((row) => row.number)).toEqual([10, 11, 12]);
    expect(rows[0].html).toContain("def");
    expect(rows[0].hang).toBe(4);
    expect(rows[2].hang).toBe(8);
    expect(rows.map((row) => row.marked)).toEqual([false, true, false]);
  });

  it("says how many lines a gap hides", () => {
    const rows = codeRows(set, ["src/agents.py", [[10, 10], [12, 12]], []]);
    expect(rows.map((row) => row.kind)).toEqual(["line", "gap", "line"]);
    expect(rows[1].hidden).toBe(1);
  });

  it("lists the pages that cite a function, its own area's first", () => {
    expect(citingPages(set, "Agent.run()", "agent-runtime")).toEqual({ cited: true, ids: ["step-loop", "agent-runtime"] });
    expect(citingPages(set, "Agent._step()", "agent-runtime")).toEqual({ cited: false, ids: ["agent-runtime"] });
  });
});
