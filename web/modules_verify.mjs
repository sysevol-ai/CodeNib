// Drive the Codemap "Modules" view against a mocked backend, so the view issue
// #390 asked for is verifiable offline (no index, no LLM, no FastAPI).
//
//   node modules_verify.mjs [baseUrl] [modulemapFixture.json]
//
// The built-in payload is a hand-written preact-shaped slice. For a realistic
// check, dump a real one first and pass its path:
//
//   python -c "import json;from codenib.graph.code_graph import CodeGraph; \
//     from codenib.web.modulemap import build_modulemap; \
//     print(json.dumps(build_modulemap(CodeGraph.load_graph('<graph.pkl>'))))" > /tmp/mm.json
//   node modules_verify.mjs http://127.0.0.1:3000 /tmp/mm.json
//
// -> ../verification/modules.png + a JSON report on stdout.
import { existsSync, readFileSync } from "node:fs";
import { chromium } from "playwright";

const BASE = process.argv[2] || "http://127.0.0.1:3000";
const FIXTURE = process.argv[3];

const mod = (i, path, symbols, refs, importance, entry = 0) => ({
  id: `m${i}`,
  name: path,
  path,
  label: path,
  short: path,
  file: path,
  line: 1,
  end_line: null,
  depth: 0,
  kind: "file",
  symbol_count: symbols,
  is_root: false,
  external: false,
  importance,
  community: 0,
  ref_count: refs,
  entry_score: entry,
});

const dep = (s, t, weight, line) => ({
  source: `m${s}`,
  target: `m${t}`,
  weight,
  call_sites: weight,
  anchors: [{ file: "src/render.js", line }],
  hidden_anchors: 0,
});

const SAMPLE = {
  available: true,
  granularity: "file",
  focus: "",
  focus_label: "",
  depth: 0,
  truncated: false,
  total_modules: 6,
  excluded: { test_files: 129, derived_files: 18 },
  nodes: [
    mod(0, "src/constants.js", 19, 5, 1.0),
    mod(1, "src/diff/children.js", 4, 1, 0.8, 0.75),
    mod(2, "src/diff/index.js", 6, 2, 0.9, 0.5),
    mod(3, "src/create-element.js", 7, 3, 0.85, 0.4),
    mod(4, "src/render.js", 2, 0, 0.4, 1.0),
    mod(5, "src/component.js", 5, 2, 0.7, 0.5),
  ],
  edges: [
    dep(1, 0, 22, 12),
    dep(2, 0, 9, 30),
    dep(4, 0, 7, 14),
    dep(5, 0, 10, 21),
    dep(1, 2, 6, 40),
    dep(4, 3, 4, 18),
    dep(2, 3, 3, 55),
    dep(4, 2, 2, 20),
  ],
  hierarchy: { root: "hier::root", nodes: [], open_files: [] },
  mermaid: "",
  note: "",
};

const MODULEMAP = FIXTURE && existsSync(FIXTURE) ? JSON.parse(readFileSync(FIXTURE, "utf8")) : SAMPLE;

const REPOS = [
  {
    id: "preactjs__preact",
    name: "preactjs/preact @ 6254ee1f",
    repo: "preactjs/preact",
    base_commit: "6254ee1fe56f553705573500d97a825d4ac5abab",
    commit_short: "6254ee1f",
    language: "javascript/typescript",
    description: "Fast 3kB alternative to React with the same modern API.",
    problem_statement: "",
    languages: ["javascript", "typescript"],
    file_count: 77,
    capabilities: { sparse_search: true, codemap: true, chat: true },
  },
];
const TREE = { repo: "preactjs/preact", pages: [{ id: "overview", title: "Overview", children: [] }] };
const PAGE = { id: "overview", title: "Overview", markdown: "# preact\n\nA fast 3kB alternative to React.", citations: [], diagram: "" };
const EMPTY_GRAPH = { available: false, nodes: [], edges: [], mermaid: "" };
// The symbol codemap and the module map are built from the same graph, so
// Codemap short-circuits to its "build the graph" panel when the codemap is
// unavailable — mock it as present-but-empty or the Modules tab never renders.
const EMPTY_CODEMAP = { available: true, root: "", root_label: "", nodes: [], edges: [], mermaid: "", hierarchy: { root: "hier::root", nodes: [], open_files: [] } };

const json = (body) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const consoleErrors = [];
const pageErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
page.on("pageerror", (e) => pageErrors.push(String(e)));

await page.route("**/api/repos/*/modulemap**", (r) => r.fulfill(json(MODULEMAP)));
await page.route("**/api/repos/*/codemap**", (r) => r.fulfill(json(EMPTY_CODEMAP)));
await page.route("**/api/repos/*/commits", (r) => r.fulfill(json({ available: false, commits: [] })));
await page.route("**/api/repos/*/wiki/*/graph", (r) => r.fulfill(json(EMPTY_GRAPH)));
await page.route("**/api/repos/*/wiki/*", (r) => r.fulfill(json(PAGE)));
await page.route("**/api/repos/*/wiki", (r) => r.fulfill(json(TREE)));
await page.route("**/api/repos/*/source**", (r) =>
  r.fulfill(json({ file: "src/constants.js", start_line: 1, end_line: 3, content: "export const EMPTY_OBJ = {};\nexport const NULL = null;\n" }))
);
await page.route("**/api/repos", (r) => r.fulfill(json(REPOS)));

await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
await page.locator(".repo-card:not(.add-repo)").first().click();
await page.waitForTimeout(800);

const report = { base: BASE, fixture: MODULEMAP === SAMPLE ? "built-in sample" : FIXTURE };
const mapTab = page.locator("button", { hasText: "Dependency Map" }).first();
report.mapTab = await mapTab.count();
if (report.mapTab) {
  await mapTab.click();
  await page.waitForTimeout(1200);
}

const modulesBtn = page.locator(".codemap-views button", { hasText: "Modules" });
report.modulesButton = await modulesBtn.count();
if (report.modulesButton) {
  await modulesBtn.click();
  await page.waitForTimeout(2500); // cytoscape layout
}

report.meta = (await page.locator(".codemap-meta").first().textContent().catch(() => null))?.trim() ?? null;
report.chips = await page.locator(".codemap-chip").count();
report.smallNotes = await page.locator(".codemap p.small").allTextContents();
report.overflow = await page.evaluate(() => ({
  scrollW: document.documentElement.scrollWidth,
  clientW: document.documentElement.clientWidth,
}));
report.horizontalScroll = report.overflow.scrollW > report.overflow.clientW + 1;
report.consoleErrors = consoleErrors.slice(0, 6);
report.pageErrors = pageErrors.slice(0, 6);

await page.screenshot({ path: "../verification/modules.png", fullPage: true });
console.log(JSON.stringify(report, null, 2));
await browser.close();
