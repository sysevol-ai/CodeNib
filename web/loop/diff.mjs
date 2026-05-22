// diff.mjs — structural pixel diff between a reference region and our app shot.
//
// Usage:
//   node loop/diff.mjs <referencePng> <appPng> <outDiffPng> [threshold]
//
// Images rarely share dimensions (content differs, by design — see LOOP.md
// DIFF SEMANTICS). We compare the overlapping top-left region at a common size
// so the metric reflects layout/chrome/typography skeleton, not literal
// content. Prints { diffRatio, width, height, pass } as JSON; exits non-zero
// when diffRatio exceeds the threshold (default 0.15) so the loop can branch.
import { readFileSync, writeFileSync } from "node:fs";
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";

const [refPath, appPath, outPath, thrArg] = process.argv.slice(2);
const threshold = parseFloat(thrArg ?? "0.15");

if (!refPath || !appPath || !outPath) {
  console.error("usage: node loop/diff.mjs <ref.png> <app.png> <out.png> [threshold]");
  process.exit(2);
}

const ref = PNG.sync.read(readFileSync(refPath));
const app = PNG.sync.read(readFileSync(appPath));

const w = Math.min(ref.width, app.width);
const h = Math.min(ref.height, app.height);

function crop(src, w, h) {
  const dst = new PNG({ width: w, height: h });
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const si = (src.width * y + x) << 2;
      const di = (w * y + x) << 2;
      dst.data[di] = src.data[si];
      dst.data[di + 1] = src.data[si + 1];
      dst.data[di + 2] = src.data[si + 2];
      dst.data[di + 3] = src.data[si + 3];
    }
  }
  return dst;
}

const a = crop(ref, w, h);
const b = crop(app, w, h);
const out = new PNG({ width: w, height: h });

const mismatched = pixelmatch(a.data, b.data, out.data, w, h, {
  threshold: 0.1,
  includeAA: false,
});
writeFileSync(outPath, PNG.sync.write(out));

const diffRatio = mismatched / (w * h);
const pass = diffRatio <= threshold;
console.log(
  JSON.stringify({ diffRatio: +diffRatio.toFixed(4), width: w, height: h, threshold, pass }, null, 2)
);
process.exit(pass ? 0 : 1);
