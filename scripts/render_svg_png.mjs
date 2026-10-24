// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Render a self-contained SVG (fonts embedded) to a PNG at its own size, for
// blog sharing images. Usage: node scripts/render_svg_png.mjs <in.svg> <out.png>
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium } = require("playwright");

const [input, output] = process.argv.slice(2);
if (!input || !output) {
  console.error("usage: node scripts/render_svg_png.mjs <in.svg> <out.png>");
  process.exit(2);
}
const svg = await readFile(input, "utf8");
const width = Number(svg.match(/<svg[^>]*\bwidth="(\d+)"/)[1]);
const height = Number(svg.match(/<svg[^>]*\bheight="(\d+)"/)[1]);
const src = `data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`;
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
  await page.setContent(`<body style="margin:0"><img id="figure" src="${src}" width="${width}" height="${height}"></body>`);
  await page.locator("#figure").evaluate(img => img.decode());
  await page.locator("#figure").screenshot({ path: output });
  console.log(`${output} ${width}x${height}`);
} finally {
  await browser.close();
}
