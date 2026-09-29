// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Render selected, recorded CLI fields. This never fabricates an agent response
// or makes a model call. Install web dev dependencies and Chromium first.
import { readFile, mkdtemp, copyFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(join(root, "web/package.json"));
const { chromium } = require("playwright");
const output = join(root, "landing/assets/demos");
const record = JSON.parse(await readFile(join(output, "codegraph-claude.json"), "utf8"));
const font = (await readFile(join(root, "landing/assets/fonts/GeistMono-Variable.woff2"))).toString("base64");
const frames = await mkdtemp(join(tmpdir(), "codenib-agent-demo-"));
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1080, height: 608 }, deviceScaleFactor: 1 });
  await page.route("**/*", route => route.abort());
  await page.setContent(`<!doctype html><html lang="en"><meta charset="utf-8"><style>
    @font-face{font-family:Mono;src:url(data:font/woff2;base64,${font})}
    *{box-sizing:border-box}body{margin:0;background:#0b1523;color:#e6edf5;font:20px Mono,monospace}
    main{height:608px;padding:36px 42px;display:flex;flex-direction:column;gap:22px}
    header{display:flex;justify-content:space-between;font:16px Mono;color:#8aaccd}
    h1{font:500 29px Mono;margin:0;color:#fff}pre{font:20px/1.58 Mono;margin:0;white-space:pre-wrap;flex:1}
    footer{border-top:1px solid #26374b;padding-top:17px;font:14px Mono;color:#9bb0c7}
  </style><main><header><span>CodeNib → Claude Code</span><span>Requests · Python</span></header>
  <h1></h1><pre></pre><footer>Recorded CLI excerpts · waits condensed · psf/requests ${record.source_commit.slice(0,8)} · CodeNib main ${record.codenib_commit.slice(0,8)}</footer></main></html>`);
  await page.evaluate(() => document.fonts.ready);
  const short = name => name.split(":").pop();
  // Keep Class.method() when it fits; otherwise show the method alone.
  const label = name => short(name).length <= 24 ? short(name) : short(name).split(".").pop();
  const tool = call => call.tool_name.split("__").pop();
  const [explore, trace] = record.tool_calls;
  const anchors = record.source_anchors.slice().sort((a,b) => b.start_line-a.start_line);
  const graph = record.dependency_result;
  // The root is the queried symbol, located by the explore_context anchors.
  const lines = new Map([
    ...anchors.map(a => [a.symbol, `${a.file.split("/").pop()}:${a.start_line}`]),
    ...graph.nodes.map(node => [node.name, `${node.file.split("/").pop()}:${node.line}`]),
  ]);
  const callers = name => graph.edges.filter(edge => edge.target === name).map(edge => edge.source);
  // Walk the recorded impact edges while each symbol has a single caller.
  const chain = [graph.root];
  while (callers(chain.at(-1)).length === 1) chain.push(callers(chain.at(-1))[0]);
  const entries = callers(chain.at(-1)).map(label);
  const verbs = entries.filter(name => name.startsWith("Session."));
  const others = callers(chain.at(-1)).filter(name => !verbs.includes(label(name)));
  const entryLine = `${others.map(name => `${lines.get(name).padEnd(16)}${label(name)}`).join("\n  ")}\n  ${"sessions.py".padEnd(16)}${verbs.length} Session verbs: ${verbs.slice(0, 2).map(v => v.split(".").pop()).join(", ")}, …`;
  const rows = chain.slice().reverse().map((name, depth) =>
    `  ${lines.get(name).padEnd(16)}${" ".repeat(depth * 2)}└ ${label(name)}`);
  const scenes = [
    {title:"1 / 3   Connect your repository", text:`$ ${record.setup_command}\n\n${record.setup_output_excerpt.join("\n")}`},
    {title:"2 / 3   Ask in plain language", text:`Recorded prompt (excerpt):\n${record.prompt.split(" Cite files")[0]}\n\nRecorded MCP calls:\n${tool(explore)}(${JSON.stringify(explore.tool_input.query)})\n${tool(trace)}(${JSON.stringify(trace.tool_input.symbol)}, ${trace.tool_input.direction}, depth ${trace.tool_input.depth})`},
    {title:"3 / 3   Where it happens, and who reaches it", text:`${tool(explore)} → source anchors\n${anchors.map(a => `  ${`${lines.get(a.symbol)}–${a.end_line}`.padEnd(20)}${label(a.symbol)}`).join("\n")}\n\n${tool(trace)} → ${chain.length}-hop caller chain, one call\n  ${entryLine}\n${rows.join("\n")}`},
  ];
  for (const [index, scene] of scenes.entries()) {
    await page.evaluate(scene => {
      document.querySelector("h1").textContent = scene.title;
      document.querySelector("pre").textContent = scene.text;
    }, scene);
    const overflow = await page.evaluate(() => document.documentElement.scrollHeight > 608 || document.querySelector("pre").scrollWidth > document.querySelector("pre").clientWidth);
    if (overflow) throw new Error(`Scene ${index} overflows`);
    await page.screenshot({ path: join(frames, `${index}.png`) });
  }
  await copyFile(join(frames, "2.png"), join(output, "codegraph-claude.png"));
  const common = ["-y", "-loglevel", "error", "-framerate", "1/5", "-i", join(frames, "%d.png"), "-t", "15"];
  execFileSync("ffmpeg", [...common, "-filter_complex", "[0:v]split[a][b];[a]palettegen=stats_mode=diff:max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3", "-loop", "0", join(output, "codegraph-claude.gif")]);
  execFileSync("ffmpeg", [...common, "-r", "24", "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", join(output, "codegraph-claude.mp4")]);
  execFileSync("ffmpeg", [...common, "-r", "12", "-c:v", "libvpx-vp9", "-crf", "35", "-b:v", "0", "-pix_fmt", "yuv420p", join(output, "codegraph-claude.webm")]);
  console.log("Rendered 15-second replay from the recorded transcript.");
} finally {
  await browser.close();
  await rm(frames, { recursive: true, force: true });
}
