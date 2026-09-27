// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Offline browser acceptance: npm run build; npm run preview -- --port 4179
// node preview_verify.mjs http://127.0.0.1:4179 /path/to/artifacts
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import { chromium } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:4179";
const output = process.argv[3];
const commit = "a".repeat(40),
  tree = "b".repeat(40);
const content = {
  "README.md":
    "# Example repository\n\nA tiny HTTP service.\n\n![tracker](https://tracker.invalid/pixel)\n\n[unsafe](javascript:alert(1))\n\n<script>window.previewInjected=true</script>\n",
  "src/main.py": "from service import run\n\ndef main():\n    return run()\n",
};
const blobs = Object.fromEntries(
  Object.entries(content).map(([path, text]) => [
    path,
    {
      sha: createHash("sha1")
        .update(`blob ${Buffer.byteLength(text)}\0${text}`)
        .digest("hex"),
      size: Buffer.byteLength(text),
      content: Buffer.from(text).toString("base64"),
      encoding: "base64",
    },
  ]),
);
const ready = [
  {
    id: "ready-wiki",
    repo: "owner/ready",
    name: "ready",
    base_commit: commit,
    commit_short: commit.slice(0, 8),
    language: "Python",
    summary: "A prepared Wiki.",
    description: "",
    file_count: 2,
    capabilities: {},
  },
];
const browser = await chromium.launch({ headless: true });
const reports = [];
try {
  for (const mobile of [false, true]) {
    const context = await browser.newContext({
      viewport: mobile
        ? { width: 390, height: 844 }
        : { width: 1440, height: 1000 },
    });
    const page = await context.newPage();
    const errors = [],
      requests = [];
    let modelCalls = 0;
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const request = route.request(),
        url = new URL(request.url());
      requests.push({
        origin: url.origin,
        path: url.pathname,
        method: request.method(),
      });
      const json = (value) => route.fulfill({ json: value });
      if (url.origin === new URL(base).origin) {
        if (url.pathname.endsWith("/api/repos")) return json(ready);
        if (url.pathname.includes("/api/"))
          return route.fulfill({ status: 404, json: {} });
        return route.continue();
      }
      if (url.origin === "https://api.github.com") {
        assert.equal(request.headers().authorization, undefined);
        if (url.pathname.includes("/limited/"))
          return route.fulfill({
            status: 403,
            json: { message: "rate limit" },
          });
        if (url.pathname.endsWith("/owner/new"))
          return json({
            full_name: "owner/new",
            private: false,
            default_branch: "main",
            description: "A tiny HTTP service.",
            language: "Python",
          });
        if (url.pathname.endsWith("/branches/main"))
          return json({
            commit: { sha: commit, commit: { tree: { sha: tree } } },
          });
        if (url.pathname.includes("/git/trees/"))
          return json({
            sha: tree,
            truncated: false,
            tree: [
              {
                path: "src",
                type: "tree",
                mode: "040000",
                sha: "c".repeat(40),
              },
              ...Object.entries(blobs).map(([path, blob]) => ({
                path,
                type: "blob",
                mode: "100644",
                sha: blob.sha,
                size: blob.size,
              })),
            ],
          });
        const blob = Object.values(blobs).find((item) =>
          url.pathname.endsWith(`/git/blobs/${item.sha}`),
        );
        if (blob) return json(blob);
      }
      if (url.origin === "https://openrouter.ai") {
        assert.equal(
          request.headers().authorization,
          "Bearer fake-browser-key",
        );
        if (url.pathname === "/api/v1/key")
          return json({
            data: { is_management_key: false, limit_remaining: 1 },
          });
        if (url.pathname === "/api/v1/chat/completions") {
          modelCalls++;
          const body = request.postDataJSON();
          assert.equal(body.model, "anthropic/claude-sonnet-4.6");
          assert.equal(
            JSON.stringify(body).includes("fake-browser-key"),
            false,
          );
          const input = JSON.parse(body.messages[1].content);
          assert.equal(input.commit, commit);
          const source = input.sources.find(
            (item) => item.path === "src/main.py",
          ).id;
          return json({
            model: body.model,
            usage: { cost: 0.002 },
            choices: [
              {
                finish_reason: "stop",
                message: {
                  content: JSON.stringify({
                    summary: "A small service entry point.",
                    components: [
                      {
                        title: "Service entry",
                        description: "Calls the service runner.",
                        evidence: { source, start_line: 1, end_line: 4 },
                      },
                    ],
                    connections: [],
                  }),
                },
              },
            ],
          });
        }
      }
      throw new Error(
        `Unexpected external request: ${url.origin}${url.pathname}`,
      );
    });
    await page.goto(base);
    await page
      .getByRole("link", { name: "owner/ready", exact: true })
      .waitFor();
    const before = Date.now();
    await page
      .getByLabel("Public GitHub repository")
      .fill("https://github.com/owner/new");
    await page.getByLabel("Public GitHub repository").press("Enter");
    await page
      .getByRole("heading", { name: "Example repository", exact: true })
      .waitFor();
    const firstResultMs = Date.now() - before;
    assert.equal(modelCalls, 0);
    assert.equal(await page.locator(".preview-readme img").count(), 0);
    assert.equal(await page.locator('a[href^="javascript:"]').count(), 0);
    assert.equal(
      await page.evaluate(() => window.previewInjected === true),
      false,
    );
    await page.getByRole("button", { name: "src/", exact: true }).click();
    await page.getByRole("button", { name: "main.py", exact: true }).click();
    await page
      .locator(".preview-code")
      .getByText("return run()", { exact: false })
      .waitFor();
    assert.equal(
      await page
        .getByRole("link", { name: "View on GitHub" })
        .getAttribute("href"),
      `https://github.com/owner/new/blob/${commit}/src/main.py`,
    );
    await page
      .getByText("Use your OpenRouter key for an AI explanation", {
        exact: true,
      })
      .click();
    await page.getByLabel("OpenRouter inference key").fill("fake-browser-key");
    await page
      .getByRole("button", { name: "Connect key", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Explain repository", exact: true })
      .waitFor();
    assert.equal(await page.locator('input[type="password"]').count(), 0);
    assert.equal(modelCalls, 0);
    await page
      .getByRole("button", { name: "Explain repository", exact: true })
      .click();
    await page
      .getByRole("heading", { name: "Service entry", exact: false })
      .waitFor();
    assert.equal(modelCalls, 1);
    assert.equal(
      await page
        .getByRole("link", { name: "src/main.py:1–4" })
        .getAttribute("href"),
      `https://github.com/owner/new/blob/${commit}/src/main.py#L1-L4`,
    );
    const storage = await page.evaluate(() =>
      JSON.stringify([localStorage, sessionStorage]),
    );
    assert.equal(storage.includes("fake-browser-key"), false);
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
    );
    if (output) {
      await fs.mkdir(output, { recursive: true });
      await page.screenshot({
        path: `${output}/wiki-quickstart-${mobile ? "mobile" : "desktop"}.png`,
        fullPage: true,
      });
    }
    await page.getByRole("button", { name: "Disconnect", exact: true }).click();
    await page
      .getByText("Use your OpenRouter key for an AI explanation", {
        exact: true,
      })
      .click();
    await page.getByLabel("OpenRouter inference key").waitFor();
    assert.equal(
      await page.getByLabel("OpenRouter inference key").inputValue(),
      "",
    );
    await page.reload();
    await page
      .getByRole("heading", { name: "Example repository", exact: true })
      .waitFor();
    assert.equal(modelCalls, 1);
    await page.goto(`${base}/preview/limited/repo`);
    await page
      .getByRole("alert")
      .getByText(/GitHub is limiting/)
      .waitFor();
    await page.getByRole("button", { name: "Retry repository" }).waitFor();
    assert.equal(modelCalls, 1);
    assert.deepEqual(errors, []);
    assert.equal(
      requests.some(
        (r) =>
          r.path.includes("/api/") &&
          r.origin === new URL(base).origin &&
          r.path !== "/api/repos",
      ),
      false,
    );
    reports.push({
      viewport: mobile ? "mobile" : "desktop",
      firstResultMs,
      modelCalls,
      pageErrors: errors,
      noOperatorModelCalls: true,
      noCredentialStorage: true,
      sourceLinksPinned: true,
    });
    await context.close();
  }
} finally {
  await browser.close();
}
if (output)
  await fs.writeFile(
    `${output}/wiki-quickstart-browser.json`,
    JSON.stringify(reports, null, 2) + "\n",
  );
console.log(JSON.stringify(reports, null, 2));
