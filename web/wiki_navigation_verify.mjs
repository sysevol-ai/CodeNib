// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Build first, then run: node web/wiki_navigation_verify.mjs [FRONTEND_URL]
// Every API response is a local fixture; no repository, model or database is used.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:4174";
const directory = new URL(".", import.meta.url);
const preview = process.argv[2] ? null : spawn(process.execPath, [
  "node_modules/vite/bin/vite.js", "preview", "--host", "127.0.0.1", "--port", "4174", "--strictPort",
], { cwd: directory, stdio: "ignore" });

const repo = { id: "example__nav", repo: "example/nav", base_commit: "a".repeat(40),
  commit_short: "aaaaaaa", language: "python", languages: ["python"], file_count: 4, capabilities: {} };
const leaf = (id, title, tier) => ({ id, title, tier, cache_state: "ready", children: [] });
const tree = { repo: repo.repo, pages: [
  leaf("overview", "Overview", "primary"),
  { ...leaf("sessions", "Sessions", "primary"), children: [
    leaf("redirects", "Redirects", "key"), leaf("state", "Session state", "detail"),
  ] },
  ...["One", "Two", "Three", "Four"].map(name => leaf(name.toLowerCase(), name, "primary")),
  leaf("compat", "Compatibility", "more"),
] };
const callPath = {
  id: "sessions-call-path", kind: "diagram", placement: "aside",
  title: "Selected call path: get() → send()", purpose: "", source_citations: [],
  render_contract: { schema_version: 1, adapter: "flow", provenance: "relations", evidence: ["R1", "R2"],
    data: {
      nodes: ["get()", "Session.request()", "send()"].map((label, index) => (
        { id: `n${index}`, label, detail: "", evidence: ["R1"] })),
      edges: [
        { source: "n0", target: "n1", label: "", evidence: ["R1"] },
        { source: "n1", target: "n2", label: "", evidence: ["R2"] },
      ],
    } },
};
const document = id => ({ id, title: id, diagram: "", citations: [],
  generation: { mode: "generated", model: "fixture", renderer: "fact_plan" },
  grounding: { valid: true }, quality: { valid: true },
  media_slots: id === "sessions" ? [callPath] : [],
  markdown: `# ${id}\n\nThe opening sentence says what this code does for a caller.\n\n## Details\n\nA body paragraph.`,
});
const boundary = { available: true, truncated: false, inbound: [], outbound: [] };

let browser;
try {
  for (let attempt = 0; attempt < 100; attempt++) {
    try { if ((await fetch(base)).ok) break; } catch { /* Preview is starting. */ }
    if (attempt === 99) throw new Error("Frontend did not become ready.");
    await delay(100);
  }
  browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let data = {};
    if (path.endsWith("/api/repos")) data = [repo];
    else if (path.endsWith(`/repos/${repo.id}`)) data = repo;
    else if (path.endsWith("/boundary")) data = boundary;
    else if (path.endsWith("/wiki-map")) data = { available: false, reason: "graph_unavailable", areas: [], links: [] };
    else if (path.endsWith("/wiki")) data = tree;
    else if (path.includes("/wiki/")) data = document(path.split("/").pop());
    await route.fulfill({ json: data });
  });
  const labels = () => page.locator(".wiki-toc .toc-label").allInnerTexts();
  const open = async id => {
    await page.goto(`${base}/${repo.id}?p=${id}`);
    await page.locator(".wiki-toc .toc-link").first().waitFor();
    await page.getByText("The opening sentence").first().waitFor();
  };

  await open("overview");
  const first = await labels();
  assert.deepEqual(first, ["Overview", "Sessions", "One", "Two", "Three", "Four"],
    "the first view lists only primary pages");

  const toggle = page.getByRole("button", { name: "Show 2 detail pages under Sessions" });
  await toggle.click();
  assert.ok((await labels()).includes("Redirects"), "a parent opens on request");
  await page.getByRole("button", { name: "Hide 2 detail pages under Sessions" }).click();
  assert.ok(!(await labels()).includes("Redirects"), "a parent closes on request");

  await page.getByRole("button", { name: "More topics (1)" }).click();
  assert.ok((await labels()).includes("Compatibility"), "lower-ranked topics open on request");

  await open("redirects");
  assert.ok((await labels()).includes("Redirects"), "the current page stays visible");
  assert.equal(await page.locator(".wiki-toc .toc-link.active").innerText(), "Redirects");

  await open("compat");
  assert.ok((await labels()).includes("Compatibility"), "a page under More topics stays visible");

  await open("sessions");
  const steps = await page.locator(".call-path-symbol").allInnerTexts();
  assert.deepEqual(steps, ["get()", "Session.request()", "send()"], "steps follow the recorded path");
  assert.match(await page.locator(".call-path-caption").innerText(), /selected/i,
    "the figure says it is one selected path");
  const deeper = await page.locator(".wiki-read-next button").allInnerTexts();
  assert.deepEqual(deeper, ["Redirects", "Session state"], "a parent links to its detail pages");
  await page.locator(".wiki-read-next button").first().click();
  await page.waitForURL(/p=redirects/);

  await open("sessions");
  const graph = page.locator("details.wiki-disclosure").first();
  assert.equal(await graph.evaluate(node => node.open), false, "callers and calls start closed");
  console.log("wiki navigation verified");
} finally {
  await browser?.close();
  preview?.kill();
}
