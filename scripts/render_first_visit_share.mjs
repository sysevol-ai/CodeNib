// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Render the actual captured example into a 1200x630 social card. No API calls.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import path from "node:path";
const root = fileURLToPath(new URL("../landing/", import.meta.url));
const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium } = require("playwright");
const server = createServer(async (req, res) => {
  const name = new URL(req.url, "http://localhost").pathname;
  const file = path.resolve(root, `.${name}`);
  if (!file.startsWith(root)) { res.writeHead(404).end(); return; }
  try {
    const data = await readFile(file);
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Content-Type", { ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" }[path.extname(file)] || "application/octet-stream");
    res.end(data);
  } catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  await page.setContent(`<!doctype html><html><head><base href="${origin}/"><style>
    * { box-sizing: border-box; } body { margin: 0; background: #f8fafc; color: #0f172a; font-family: system-ui, sans-serif; }
    main { padding: 30px 36px; display: grid; grid-template-columns: 440px 1fr; gap: 30px; align-items: center; height: 630px; }
    .logo { width: 160px; margin-bottom: 40px; } h1 { font-size: 43px; line-height: 1.12; letter-spacing: -1.6px; margin: 0 0 26px; }
    p { font-size: 22px; line-height: 1.5; color: #475569; max-width: 350px; } .url { font-size: 18px; color: #2563eb; margin-top: 34px; }
    .example { width: 660px; height: 570px; }
  </style></head><body><main><section><img class="logo" src="assets/codenib-logo.svg" alt="CodeNib"><h1>Understand a repo.<br>Follow the code.</h1><p>A real question.<br>Clickable calls.<br>The source behind them.</p><div class="url">codenib.ai</div></section><div class="example"><codenib-explorer></codenib-explorer></div></main><script type="module" src="assets/explore/repo-explorer.js"></script></body></html>`);
  await page.locator("codenib-explorer .code-line.selected").waitFor();
  await page.waitForLoadState("networkidle");
  await page.locator("codenib-explorer").evaluate(element => {
    const height = element.getBoundingClientRect().height;
    element.style.transform = `scale(${Math.min(.9, 570 / height)})`;
    element.style.transformOrigin = "top left";
  });
  await page.screenshot({ path: path.join(root, "assets/explore/requests-card.png") });
  console.log("Rendered landing/assets/explore/requests-card.png (1200x630)");
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
