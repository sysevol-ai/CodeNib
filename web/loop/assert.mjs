// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
// SPDX-License-Identifier: Apache-2.0
//
// Run a named DOM/behavior assertion against the running app.
// Checks live in checks.mjs as { name: async (page, base) => ({pass, detail}) }.
//
// Usage:  node loop/assert.mjs <checkName>          # run one
//         node loop/assert.mjs --all                # run all (regression sweep)
import { chromium } from "playwright";
import { checks } from "./checks.mjs";

const APP_BASE = process.env.APP_BASE ?? "http://127.0.0.1:3000";
const arg = process.argv[2];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

async function run(name) {
  const fn = checks[name];
  if (!fn) return { name, pass: false, detail: "UNKNOWN_CHECK" };
  try {
    const r = await fn(page, APP_BASE);
    return { name, ...r };
  } catch (e) {
    return { name, pass: false, detail: "THREW: " + e.message };
  }
}

let results = [];
if (arg === "--all") {
  for (const name of Object.keys(checks)) results.push(await run(name));
} else {
  results.push(await run(arg));
}
await browser.close();

const allPass = results.every((r) => r.pass);
console.log(JSON.stringify({ allPass, results }, null, 2));
process.exit(allPass ? 0 : 1);
