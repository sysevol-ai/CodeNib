// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Render the system-map film for one captured repository. Point it at a Vite
// dev server for web/ and at a checkout of the repository; it reads the call
// site's source at the indexed commit, refuses a call site whose line does
// not name the callee, then seeks /video/index.html frame by frame and
// encodes the frames with ffmpeg.
//
//   node scripts/render-repo-video.mjs --url http://127.0.0.1:3041 \
//     --repo psf__requests --source /path/to/requests --out requests.mp4
//
// --at 1000,5000 writes stills at those milliseconds instead of a film.

import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";

const args = Object.fromEntries(
  process.argv
    .slice(2)
    .reduce((pairs, token, index, list) => (token.startsWith("--") ? [...pairs, [token.slice(2), list[index + 1]]] : pairs), []),
);
const { url, repo = "psf__requests", source, out, ffmpeg = "ffmpeg", at } = args;
const fps = Number(args.fps || 30);
if (!url || !source || (!out && !at)) {
  console.error("usage: render-repo-video.mjs --url <vite-dev-url> --source <checkout> (--out <file.mp4> | --at <ms,...>) [--repo <id>] [--fps 30] [--ffmpeg <path>]");
  process.exit(2);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
await page.goto(`${url}/video/index.html?repo=${encodeURIComponent(repo)}`, { waitUntil: "networkidle" });
await page.waitForFunction(() => window.__video, null, { timeout: 30000 });
const { duration, site, repo: info } = await page.evaluate(() => {
  const { duration, site, repo } = window.__video;
  return { duration, site, repo };
});

// The call site is shown as source, so it must be the indexed commit's text
// and the anchored line must name what it calls.
const text = execFileSync(
  "git",
  ["-c", `safe.directory=${source}`, "-C", source, "show", `${info.base_commit}:${site.file}`],
  { encoding: "utf8", maxBuffer: 64 << 20 },
).split("\n");
const callee = site.to.replace(/\(\)$/, "").split(/[.:]/).pop();
const line = text[site.line - 1] ?? "";
if (!callee || !new RegExp(`\\b${callee.replace(/[$^\\.*+?()[\]{}|]/g, "\\$&")}\\b`).test(line)) {
  console.error(`refused: ${site.file}:${site.line} does not name ${site.to}: ${JSON.stringify(line.trim())}`);
  await browser.close();
  process.exit(1);
}
const start = Math.max(1, site.line - 5);
const excerpt = { start, lines: text.slice(start - 1, site.line + 1) };
await page.evaluate((value) => window.__video.setExcerpt(value), excerpt);
await page.evaluate(() => document.fonts.ready);
console.log(`call site ${site.file}:${site.line}  ${site.from} -> ${site.to}  (${info.commit_short})`);

const shoot = async (ms, path) => {
  await page.evaluate((t) => window.__video.seek(t), ms);
  await page.screenshot({ path, clip: { x: 0, y: 0, width: 1920, height: 1080 } });
};

// The poster lays out at its measured width; settle that before frame 0.
await page.evaluate(() => window.__video.seek(0));
await page.waitForTimeout(500);

if (at) {
  const dir = out || ".";
  mkdirSync(dir, { recursive: true });
  for (const ms of at.split(",").map(Number)) {
    await shoot(ms, join(dir, `still-${String(ms).padStart(5, "0")}.png`));
    console.log(`still ${ms}ms`);
  }
  await browser.close();
  process.exit(0);
}

const frames = mkdtempSync(join(tmpdir(), "repo-video-"));
const count = Math.ceil((duration / 1000) * fps);
for (let index = 0; index < count; index += 1) {
  await shoot((index * 1000) / fps, join(frames, `${String(index).padStart(5, "0")}.png`));
  if (index % fps === 0) process.stdout.write(`\rframe ${index}/${count}`);
}
process.stdout.write(`\rframe ${count}/${count}\n`);
await browser.close();

const encoded = spawnSync(
  ffmpeg,
  ["-y", "-loglevel", "error", "-framerate", String(fps), "-i", join(frames, "%05d.png"),
   "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
  { stdio: "inherit" },
);
rmSync(frames, { recursive: true, force: true });
if (encoded.status !== 0) process.exit(encoded.status ?? 1);
console.log(`wrote ${out} (${(duration / 1000).toFixed(1)}s, ${count} frames)`);
