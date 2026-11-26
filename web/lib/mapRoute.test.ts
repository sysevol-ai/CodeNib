// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { callLabel, plainTitle, routeFromJourney, routeHeading, stopPlace } from "@/lib/mapRoute";
import { extractJourney } from "@/lib/wikiPresentation";

const OVERVIEW = `# Overview

## From \`get()\` to \`HTTPAdapter.send()\`

1. **\`get()\`** · [Request Lifecycle](?p=request-lifecycle): Passes the URL on. [E2](#evidence-E2)
2. **\`serve()\`**: Picks a mode. [E3](#evidence-E3)
3. **\`src/requests/sessions.py:Session.send()\`** · [Session Orchestration](?p=session-orchestration): Picks an adapter. [E1](#evidence-E1)
4. **\`HTTPAdapter.send()\`** · [Request Lifecycle](?p=request-lifecycle): Sends it. [E4](#evidence-E4)
`;

const CITATIONS = [
  { file: "src/requests/sessions.py", start_line: 752 },
  { file: "src/requests/api.py", start_line: 74 },
  { file: "src/requests/cli.py", start_line: 9 },
  null,
];

describe("routeFromJourney", () => {
  const journey = extractJourney(OVERVIEW).journey;

  it("places each stage in its area with the cited file and line", () => {
    const route = routeFromJourney(journey, CITATIONS, ["request-lifecycle", "session-orchestration"]);
    expect(route?.total).toBe(4);
    expect(route?.stops.map((stop) => [stop.index, stop.areaId, stop.file, stop.line])).toEqual([
      [1, "request-lifecycle", "src/requests/api.py", 74],
      [3, "session-orchestration", "src/requests/sessions.py", 752],
      [4, "request-lifecycle", null, null],
    ]);
  });

  it("shows short names, not file-qualified symbols", () => {
    const route = routeFromJourney(journey, CITATIONS, ["request-lifecycle", "session-orchestration"]);
    expect(route?.stops[1].label).toBe("Session.send()");
  });

  it("leaves out stages in no area or an area the map does not draw", () => {
    const route = routeFromJourney(journey, CITATIONS, ["request-lifecycle", "session-orchestration"]);
    expect(route?.stops.some((stop) => stop.symbol === "serve()")).toBe(false);
  });

  it("names the shown steps when some are off the map", () => {
    const route = routeFromJourney(journey, CITATIONS, ["request-lifecycle", "session-orchestration"])!;
    expect(routeHeading(route)).toBe("From `get()` to `HTTPAdapter.send()`");
    expect(routeHeading({ ...route, total: route.stops.length, title: "From `a.b.get()` to `x.y.send()`" })).toBe(
      "From `get()` to `HTTPAdapter.send()`",
    );
  });

  it("returns nothing for a path that never leaves one area", () => {
    expect(routeFromJourney(journey, CITATIONS, ["request-lifecycle"])).toBeNull();
    expect(routeFromJourney(null, CITATIONS, ["request-lifecycle"])).toBeNull();
  });
});

describe("route text", () => {
  it("writes every stop as a call", () => {
    expect(callLabel("RouterGroup.handle")).toBe("RouterGroup.handle()");
    expect(callLabel("Session.send()")).toBe("Session.send()");
  });

  it("names a stop by file name and line", () => {
    expect(
      stopPlace({ index: 1, symbol: "get()", label: "get()", areaId: "a", file: "src/requests/api.py", line: 74 }),
    ).toBe("api.py:74");
    expect(stopPlace({ index: 1, symbol: "get()", label: "get()", areaId: "a", file: null, line: null })).toBeNull();
  });

  it("drops code marks from the heading", () => {
    expect(plainTitle("From `get()` to `HTTPAdapter.send()`")).toBe("From get() to HTTPAdapter.send()");
  });
});
