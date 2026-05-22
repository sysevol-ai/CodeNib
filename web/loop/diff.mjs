// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
// SPDX-License-Identifier: Apache-2.0
//
// Pixel-diff two PNGs and append a mismatch ratio to diff-report.json.
// Images are cropped to their common dimensions so size mismatch alone does not
// dominate the metric (layout/chrome diff, per LOOP.md DIFF SEMANTICS).
//
// Usage:
//   node loop/diff.mjs <baselinePng> <appPng> <outName> [threshold]
// Exit code 0 if mismatch <= threshold (default 0.15), else 1.
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";

const baseP = process.argv[2];
const appP = process.argv[3];
const outName = process.argv[4] ?? "diff";
const threshold = parseFloat(process.argv[5] ?? "0.15");

const DIFF_DIR = new URL("./diffs/", import.meta.url).pathname;
const REPORT = `${DIFF_DIR}diff-report.json`;

const a = PNG.sync.read(readFileSync(baseP));
const b = PNG.sync.read(readFileSync(appP));
const w = Math.min(a.width, b.width);
const h = Math.min(a.height, b.height);

const crop = (img) => {
  const out = new PNG({ width: w, height: h });
  for (let y = 0; y < h; y++)
    for (let x = 0; x < w; x++) {
      const si = (img.width * y + x) << 2;
      const di = (w * y + x) << 2;
      out.data[di] = img.data[si];
      out.data[di + 1] = img.data[si + 1];
      out.data[di + 2] = img.data[si + 2];
      out.data[di + 3] = img.data[si + 3];
    }
  return out;
};

const ca = crop(a);
const cb = crop(b);
const diff = new PNG({ width: w, height: h });
const mism = pixelmatch(ca.data, cb.data, diff.data, w, h, { threshold: 0.1 });
const ratio = mism / (w * h);
writeFileSync(`${DIFF_DIR}${outName}.png`, PNG.sync.write(diff));

const pass = ratio <= threshold;
const entry = { outName, baseline: baseP, app: appP, dims: { w, h }, mismatchPixels: mism, mismatchRatio: +ratio.toFixed(4), threshold, pass, ts: new Date().toISOString() };

let report = [];
if (existsSync(REPORT)) {
  try {
    report = JSON.parse(readFileSync(REPORT, "utf8"));
  } catch {}
}
report.push(entry);
writeFileSync(REPORT, JSON.stringify(report, null, 2));
console.log(JSON.stringify(entry, null, 2));
process.exit(pass ? 0 : 1);
