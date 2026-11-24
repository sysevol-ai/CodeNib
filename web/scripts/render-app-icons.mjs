// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Render the installed-app icons from public/codenib-icon.svg into
// public/icons/. The nib sits on the dark page colour, so the icon matches
// the app it opens. Run again after the logo changes:
//
//   node scripts/render-app-icons.mjs

import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const here = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.resolve(here, "../public");
const outDir = path.join(publicDir, "icons");
const svg = readFileSync(path.join(publicDir, "codenib-icon.svg"), "utf8");
const logo = `data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`;
// --bg-page in the dark theme (app/globals.css).
const PAGE = "#121315";

const ICONS = [
  // Launchers that show the icon as drawn: a rounded tile.
  { file: "icon-192.png", size: 192, radius: 0.22, nib: 0.62 },
  { file: "icon-512.png", size: 512, radius: 0.22, nib: 0.62 },
  // Android masks this one; keep the nib inside the 80% safe circle.
  { file: "icon-maskable-512.png", size: 512, radius: 0, nib: 0.48 },
  // iOS rounds the corners itself and wants an opaque square.
  { file: "apple-touch-icon.png", size: 180, radius: 0, nib: 0.6 },
];

mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch({ args: ["--no-sandbox"] });
try {
  for (const icon of ICONS) {
    const page = await browser.newPage({
      viewport: { width: icon.size, height: icon.size },
      deviceScaleFactor: 1,
    });
    await page.setContent(`<!doctype html>
      <html><body style="margin:0;background:transparent">
        <div style="width:${icon.size}px;height:${icon.size}px;background:${PAGE};
          border-radius:${icon.radius * 100}%;display:grid;place-items:center">
          <img src="${logo}" style="height:${icon.nib * 100}%;width:auto">
        </div>
      </body></html>`);
    await page.locator("img").evaluate((img) => img.decode());
    await page.screenshot({
      path: path.join(outDir, icon.file),
      omitBackground: icon.radius > 0,
      clip: { x: 0, y: 0, width: icon.size, height: icon.size },
    });
    await page.close();
    console.log(`wrote public/icons/${icon.file}`);
  }
} finally {
  await browser.close();
}
