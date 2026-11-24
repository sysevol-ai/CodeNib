// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Build first, then run: node web/annotations_verify.mjs [FRONTEND_URL]
// Every API response is a local fixture, including the annotation routes and
// the agent's answer; no repository, agent, model or database is used.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { chromium, webkit } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:4176";
const directory = new URL(".", import.meta.url);
const preview = process.argv[2] ? null : spawn(process.execPath, [
  "node_modules/vite/bin/vite.js", "preview", "--host", "127.0.0.1", "--port", "4176", "--strictPort",
], { cwd: directory, stdio: "ignore" });

const KEY = "local-session-key";
const commit = "c".repeat(40);
const repo = { id: "example__notes", repo: "example/notes", base_commit: commit, commit_short: "ccccccc",
  language: "python", languages: ["python"], file_count: 3, capabilities: { annotations: true } };
const tree = { repo: repo.repo, pages: [
  { id: "overview", title: "Overview", children: [] },
  { id: "runtime", title: "Agent Runtime", children: [] },
].map((page) => ({ ...page, cache_state: "ready" })) };
const QUOTE = "hands each step to the executor";
const document = (id) => ({ id, title: id === "runtime" ? "Agent Runtime" : "Overview", diagram: "",
  citations: [{ file: "src/agents.py", start_line: 10, end_line: 30, node_name: "Agent.run", type: "function", score: null, content: null }],
  generation: { mode: "offline", model: null },
  markdown: `# Agent Runtime\n\nThe runtime starts a loop.\n\n## The loop\n\nThe agent plans, then ${QUOTE} and reads the result [E1](#evidence-E1).\n\n## Limits\n\nA run stops after a fixed number of steps.` });
const ANSWER = "It hands steps over in `Agent._step()`, at `src/agents.py:40-60`.";

function server() {
  const state = { annotations: [], tasks: [], keys: [], polls: 0 };
  const now = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");
  let serial = 0;
  const id = () => `01M8${String(++serial).padStart(22, "0")}`;
  return { state, async handle(route, url, request) {
    const path = `/api/repos/${repo.id}`;
    const method = request.method();
    if (!url.pathname.startsWith(`${path}/annotations`) && !url.pathname.startsWith(`${path}/tasks`)
        && !url.pathname.startsWith(`${path}/pages/`)) return false;
    const key = request.headers()["x-codenib-session"] ?? null;
    state.keys.push(key);
    if (key !== KEY) {
      await route.fulfill({ status: 403, json: { detail: "Open the Wiki from the link `codenib wiki` printed" } });
      return true;
    }
    if (url.pathname === `${path}/pages/runtime/markdown` && method === "GET") {
      await route.fulfill({ json: { markdown: "# Agent Runtime\n\nThe runtime starts a loop.\n",
        base: "b1", kind: "page", path: ".codenib/wiki/runtime.md", edited: false } });
      return true;
    }
    if (url.pathname === `${path}/annotations` && method === "GET") {
      const task = state.tasks[0];
      if (task && task.status === "running" && ++state.polls >= 2) {
        task.status = "done";
        task.usage = { turns: 4, input_tokens: 9000, output_tokens: 400, cost_usd: 0.03, duration_ms: 6000 };
        const input = state.annotations.find((a) => a.id === task.inputs[0]);
        input.status = "answered";
        const answer = { ...input, id: id(), kind: "answer", author: "agent", status: "open", text: ANSWER,
          reply_to: input.id, task: task.id, files: [{ path: "src/agents.py", start_line: 40, end_line: 60 }], created: now() };
        state.annotations.push(answer);
        task.results = [answer.id];
      }
      const page = url.searchParams.get("page");
      await route.fulfill({ json: {
        annotations: state.annotations.filter((a) => !page || a.on.page === page),
        tasks: state.tasks,
        agents: [{ id: "claude-code", name: "Claude Code", available: true, install: "npm i -g @anthropic-ai/claude-code" }],
        kinds: ["question", "change", "note"],
      } });
      return true;
    }
    if (url.pathname === `${path}/annotations` && method === "POST") {
      const body = request.postDataJSON();
      const annotation = { id: id(), kind: body.kind, text: body.text, author: "human",
        status: body.kind === "note" ? "open" : "draft", on: body.on, reply_to: body.reply_to ?? null,
        task: null, files: [], created: now(), updated: now() };
      state.annotations.push(annotation);
      await route.fulfill({ json: annotation });
      return true;
    }
    if (url.pathname === `${path}/tasks` && method === "POST") {
      const body = request.postDataJSON();
      const task = { id: id(), inputs: body.annotations, kind: "question", agent: "claude-code", status: "running",
        created: now(), started: now(), finished: "", error: "", results: [], attempts: 1, diff: {}, usage: {},
        progress: "Claude Code is reading the code" };
      for (const annotation of state.annotations) {
        if (body.annotations.includes(annotation.id)) Object.assign(annotation, { status: "dispatched", task: task.id });
      }
      state.tasks.push(task);
      await route.fulfill({ json: task });
      return true;
    }
    await route.fulfill({ status: 404, json: { detail: "unexpected" } });
    return true;
  } };
}

async function selectQuote(page) {
  await page.evaluate((quote) => {
    const root = window.document.querySelector(".wiki-story");
    const walker = window.document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const at = node.data.indexOf(quote);
      if (at === -1) continue;
      const range = window.document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + quote.length);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      node.parentElement.scrollIntoView({ block: "center" });
      window.document.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
      return;
    }
    throw new Error("quote not on the page");
  }, QUOTE);
}

let browser;
try {
  for (let attempt = 0; attempt < 100; attempt++) {
    try { if ((await fetch(base)).ok) break; } catch { /* Preview is starting. */ }
    if (preview?.exitCode !== null && preview?.exitCode !== undefined) throw new Error("Preview exited before becoming ready.");
    if (attempt === 99) throw new Error("Frontend did not become ready.");
    await delay(100);
  }
  for (const [engineName, engine] of [["chromium", chromium], ["webkit", webkit]]) {
    browser = await engine.launch();
    for (const width of [1440, 1280, 1024, 390]) {
      // The app's service worker (pwa_verify.mjs) is off here: WebKit sends a
      // controlled page's requests past Playwright's routes, around the fake API.
      const context = await browser.newContext({
        viewport: { width, height: 900 }, reducedMotion: "reduce", serviceWorkers: "block",
      });
      context.setDefaultTimeout(10000);
      const page = await context.newPage(), errors = [], unexpected = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const fake = server();
      const path = `/api/repos/${repo.id}`;
      await context.route("**/api/**", async (route) => {
        const url = new URL(route.request().url());
        if (await fake.handle(route, url, route.request())) return;
        let data;
        if (url.pathname === "/api/repos") data = [repo];
        else if (url.pathname === `${path}/commits`) data = { available: false, commits: [] };
        else if ([`${path}/visual-evidence`, `${path}/threads`, `${path}/walkthroughs`].includes(url.pathname)) {
          return route.fulfill({ status: 404, json: {} });
        } else if (url.pathname === `${path}/wiki`) data = tree;
        else if (url.pathname === `${path}/wiki-map`) data = { available: false, reason: "graph_unavailable", areas: [], links: [] };
        else if (url.pathname.endsWith("/boundary")) data = { available: false, inbound: [], outbound: [], truncated: false };
        else if (url.pathname.startsWith(`${path}/wiki/`)) data = document(url.pathname.split("/").at(-1));
        else { unexpected.push(url.pathname); data = {}; }
        return route.fulfill({ json: data });
      });
      const label = `${engineName} ${width}px`;

      // The terminal link carries the session key; the address drops it at once.
      await page.goto(`${base}/${repo.id}?p=runtime&session=${KEY}`, { waitUntil: "domcontentloaded" });
      await page.locator('.wiki-main[aria-busy="false"] .wiki-story').waitFor();
      assert.equal(new URL(page.url()).searchParams.get("session"), null, `${label}: key left in the address`);

      // Select words, ask about them, save a draft.
      await selectQuote(page);
      await page.locator(".cn-picker").waitFor();
      await page.keyboard.press("1");
      await page.locator(".cn-composer textarea").waitFor();
      await page.keyboard.type("Where does the step go?");
      await page.keyboard.press("Enter");
      await page.locator(".cn-drawer .cn-thread .cn-status.is-draft").waitFor();
      assert.equal(fake.state.annotations.length, 1);
      const saved = fake.state.annotations[0];
      assert.equal(saved.text, "Where does the step go?", `${label}: the first keystroke was lost`);
      assert.equal(saved.on.quote, QUOTE);
      assert.equal(saved.on.section, "The loop");
      assert.match(saved.on.prefix, /The agent plans, then $/);
      assert.match(saved.on.suffix, /^ and reads the result/);

      // The words are marked without changing the page's DOM.
      const marked = await page.evaluate(() =>
        typeof CSS !== "undefined" && "highlights" in CSS
          ? (CSS.highlights.get("codenib-annotation-active")?.size ?? 0) + (CSS.highlights.get("codenib-annotation")?.size ?? 0)
          : -1);
      assert.ok(marked === 1 || marked === -1, `${label}: ${marked} marked ranges`);

      // The drawer makes room on a wide screen and becomes a bottom sheet on a phone.
      const drawer = await page.locator(".cn-drawer").boundingBox();
      const content = await page.locator(".wiki-content").boundingBox();
      if (width >= 1100) {
        assert.ok(content.x + content.width <= drawer.x + 1, `${label}: the drawer covers the page text`);
      } else if (width <= 820) {
        assert.ok(drawer.y > 200 && Math.round(drawer.width) === width, `${label}: no bottom sheet`);
      }
      assert.equal(await page.evaluate(() => window.document.documentElement.scrollWidth > innerWidth), false, `${label}: sideways scroll`);

      // Send it; the answer comes back under the question with its code linked.
      await page.getByRole("button", { name: "Send to Claude Code" }).click();
      await page.locator(".cn-thread .cn-status.is-answered").waitFor({ timeout: 15000 });
      const answer = page.locator(".cn-item.is-agent .cn-answer");
      await answer.waitFor();
      assert.match(await answer.innerText(), /Agent\._step\(\)/);
      assert.equal(await answer.locator("button.cite-src").count(), 1, `${label}: code reference not linked`);
      assert.match(await page.locator(".cn-usage").innerText(), /4 turns · 9\.4k tokens · \$0\.030 · 6 s/);

      // Edit page opens the page's Markdown, and Esc leaves the page as it was.
      await page.getByRole("button", { name: "Edit page" }).click();
      await page.locator(".cn-editor-text").waitFor();
      assert.match(await page.locator(".cn-editor-text").inputValue(), /The runtime starts a loop/);
      assert.match(await page.locator(".cn-editor-bar").innerText(), /\.codenib\/wiki\/runtime\.md/);
      await page.locator(".cn-editor-text").press("Escape");
      await page.locator(".wiki-story").waitFor();

      // A reload keeps the key; a fresh browser without it is told where to get it.
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.locator('.wiki-main[aria-busy="false"] .wiki-story').waitFor();
      await page.getByRole("button", { name: /^Notes/ }).click();
      await page.locator(".cn-drawer .cn-item.is-agent").waitFor();
      const stranger = await (await browser.newContext({
        viewport: { width, height: 900 }, serviceWorkers: "block",
      })).newPage();
      await stranger.context().route("**/api/**", async (route) => {
        const url = new URL(route.request().url());
        if (await fake.handle(route, url, route.request())) return;
        if (url.pathname === "/api/repos") return route.fulfill({ json: [repo] });
        if (url.pathname === `${path}/wiki`) return route.fulfill({ json: tree });
        if (url.pathname.startsWith(`${path}/wiki/`) && !url.pathname.endsWith("/boundary")) {
          return route.fulfill({ json: document("runtime") });
        }
        return route.fulfill({ status: 404, json: {} });
      });
      await stranger.goto(`${base}/${repo.id}?p=runtime`, { waitUntil: "domcontentloaded" });
      await stranger.locator(".wiki-story").waitFor();
      await stranger.getByRole("button", { name: /^Notes/ }).click();
      assert.match(await stranger.locator(".cn-drawer").innerText(), /printed in your terminal/);
      await stranger.context().close();

      assert.ok(fake.state.keys.includes(KEY) && fake.state.keys.includes(null));
      assert.deepEqual(errors, []);
      assert.deepEqual(unexpected, []);
      await context.close();
      console.log(`Annotations: ${label} passed.`);
    }
    await browser.close();
    browser = undefined;
  }
} finally {
  await browser?.close();
  preview?.kill();
}
