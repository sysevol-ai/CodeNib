// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Isolated browser acceptance: no screenshots, recordings, user profiles or LLMs.
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

const root = fileURLToPath(new URL(".", import.meta.url));
const commit = "a".repeat(40);
const citation = (name, file) => ({ file, node_name: name, start_line: 1, end_line: 3, type: "class", score: null, content: null });
const queue = citation("QueueWorker.process", "src/queue/worker.py");
const storage = citation("StorageIndex.build", "src/storage/index.py");
const wikiPage = (id, title, text, citations = []) => ({ id, title, markdown: `# ${title}\n\n${text}`, citations, diagram: "", media_slots: [] });
const pages = {
  overview: wikiPage("overview", "Overview", "QueueWorker processes queue delivery.", [queue]),
  storage: wikiPage("storage", "Storage", "StorageIndex builds storage indexes.", [storage]),
  unrelated: wikiPage("unrelated", "Contributing", "Please discuss proposed changes with maintainers."),
};
const visual = (path, entity, source) => ({
  artifact_path: path, extractor: "acceptance-fixture",
  entities: [{ name: entity, type: "component", confidence: 0.9 }],
  relations: [{ source: entity, target: "Output", relation: "produces" }],
  claims: [{ text: `${entity} transforms its input into output.`, confidence: 0.9 }],
  context: { caption: `${entity} flow`, source_paths: [source.file], references: [{
    file: entity === "QueueWorker" ? "docs/queue.md" : "docs/storage.md",
    line: 7, title: `${entity} flow`, section: `${entity} flow`,
    excerpt: `${entity} transforms its input into output.`,
  }] },
});
const facts = [visual("queue.svg", "QueueWorker", queue), visual("queue.png", "QueueWorker", queue), visual("storage.svg", "StorageIndex", storage)];
const evidence = { state: "ready", source_commit: commit, indexed_commit: commit, artifact_count: 3, fact_count: 3, binding_count: 0, facts, bindings: [] };
const repo = { id: "acceptance", name: "Example", repo: "example/repo", base_commit: commit, commit_short: commit.slice(0, 8), language: "python", languages: ["python"], description: "Acceptance fixture", problem_statement: "", file_count: 2, capabilities: { sparse_search: true, chat: false, codemap: false } };
const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, open: false }, logLevel: "error" });
let browser;
const results = [];

async function scenario(mode, width = 1280) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const base = `http://127.0.0.1:${server.httpServer.address().port}`;
  await context.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/visual-evidence/media") && mode === "image-failed") return json({ detail: "Image changed" }, 409);
    if (path.endsWith("/visual-evidence/media")) return route.fulfill({
      contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="80"><rect width="160" height="80" fill="#457"/></svg>',
      headers: { "Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff" },
    });
    if (path.endsWith("/visual-evidence")) {
      if (mode === "missing") return json({ detail: "No evidence" }, 404);
      if (mode === "failed") return json({ detail: "Unavailable" }, 500);
      return json({ ...evidence, state: mode === "stale" ? "stale" : "ready", facts: mode === "empty" ? [] : facts });
    }
    if (path === "/api/repos") return json([repo]);
    if (path.endsWith("/commits")) return json({ available: false, commits: [], selected: null });
    if (path.endsWith("/wiki")) return json({ repo: repo.repo, pages: Object.values(pages).map(({ id, title }) => ({ id, title, children: [] })) });
    if (path.endsWith("/source")) return json({ file: url.searchParams.get("file"), start_line: Number(url.searchParams.get("start")), end_line: Number(url.searchParams.get("end")), content: "ACCEPTANCE_SOURCE_CONTENT\nQueue delivery context\n" });
    if (path.includes("/wiki/") && pages[path.split("/").pop()]) return json(pages[path.split("/").pop()]);
    return json({ nodes: [], edges: [], available: false });
  });
  try {
    await page.goto(`${base}/acceptance`, { waitUntil: "networkidle" });
    await page.getByRole("heading", { name: "Overview", exact: true }).waitFor();
    assert.equal(await page.locator(".page-load-error").count(), 0);
    const details = page.locator(".related-visuals");
    if (mode === "image-failed") {
      await details.waitFor();
      await details.locator("summary").click();
      await details.getByRole("status").waitFor();
      assert.equal(await details.locator("img").count(), 0);
      assert.equal(await details.getByRole("link", { name: "View original" }).count(), 0);
      assert.equal(await details.locator(".related-visual-caption").count(), 0);
      await details.getByRole("button", { name: "Read in context" }).click();
      const dialog = page.getByRole("dialog", { name: "Source definition" });
      await dialog.locator(".hl-code").waitFor();
      assert.ok((await dialog.innerText()).includes("ACCEPTANCE_SOURCE_CONTENT"));
      await page.keyboard.press("Escape");
      assert.equal(await page.locator(".page-load-error").count(), 0);
    } else if (mode !== "ready") {
      assert.equal(await details.count(), 0, `${mode} evidence must not add a section`);
    } else {
      await details.waitFor();
      assert.equal(await details.getAttribute("open"), null);
      const summary = details.locator("summary");
      await summary.focus();
      await page.keyboard.press("Enter");
      assert.notEqual(await details.getAttribute("open"), null);
      assert.equal(await details.locator("article").count(), 1, "Duplicate formats must collapse");
      const img = details.locator("img").first();
      await img.waitFor({ state: "visible" });
      await page.waitForFunction(() => {
        const image = document.querySelector(".related-visuals img");
        return image?.complete && image.naturalWidth > 0;
      });
      const popupPromise = context.waitForEvent("page");
      await details.getByRole("link", { name: "View original" }).click();
      const popup = await popupPromise;
      await popup.waitForLoadState("load");
      assert.ok(popup.url().includes("/visual-evidence/media?"));
      await popup.close();
      await details.getByRole("button", { name: "Read in context" }).click();
      const dialog = page.getByRole("dialog", { name: "Source definition" });
      await dialog.locator(".hl-code").waitFor();
      assert.ok((await dialog.innerText()).includes("ACCEPTANCE_SOURCE_CONTENT"));
      assert.ok((await dialog.innerText()).includes("docs/queue.md"));
      await page.getByRole("button", { name: "Close source" }).click();
      await details.getByRole("button", { name: /QueueWorker.process/ }).click();
      await dialog.locator(".hl-code").waitFor();
      assert.ok((await dialog.innerText()).includes("src/queue/worker.py"));
      await page.keyboard.press("Escape");
      await dialog.waitFor({ state: "hidden" });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
      assert.equal(overflow, false, "No horizontal page overflow");
      if (width > 620) {
        await page.locator('.toc-link[title="Storage"]').click();
        await page.getByRole("heading", { name: "Storage", exact: true }).waitFor();
        assert.equal(await details.getAttribute("open"), null, "Section switch resets disclosure");
        await details.locator("summary").click();
        assert.ok((await details.innerText()).includes("StorageIndex"));
        assert.ok(!(await details.innerText()).includes("QueueWorker"));
        await page.locator('.toc-link[title="Contributing"]').click();
        await page.getByRole("heading", { name: "Contributing", exact: true }).waitFor();
        assert.equal(await details.count(), 0);
      }
    }
    assert.deepEqual(errors, []);
    results.push({ mode, width, passed: true });
    console.log(`PASS ${mode} ${width}px`);
  } finally {
    await context.close();
  }
}

try {
  await server.listen();
  browser = await chromium.launch({ headless: true, channel: process.env.CODENIB_TEST_BROWSER || undefined });
  await scenario("ready");
  await scenario("ready", 390);
  for (const mode of ["missing", "stale", "empty", "failed", "image-failed"]) await scenario(mode);
  console.log(JSON.stringify({ passed: results.length, screenshots: false, recordings: false, results }));
} finally {
  await browser?.close();
  await server.close();
}
