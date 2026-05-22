// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
// SPDX-License-Identifier: Apache-2.0
//
// Named DOM/behavior checks for the parity loop. The loop APPENDS checks here as
// TODO items demand. Each check: async (page, base) => { pass: bool, detail: str }.
// Per gate G5, checks may be made STRICTER but never loosened to force a pass.

const goto = (page, base, route) =>
  page.goto(base + route, { waitUntil: "networkidle", timeout: 30000 });

export const checks = {
  // --- placeholder smoke check; real checks added by the loop from TODO.md ---
  app_boots: async (page, base) => {
    const resp = await goto(page, base, "/");
    const ok = resp && resp.status() < 400;
    return { pass: !!ok, detail: `status=${resp ? resp.status() : "none"}` };
  },
};
