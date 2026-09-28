// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Explicitly enabled, first-party funnel counts. No URL, referrer, repository,
// prompt, credential, or persistent cross-visit identifier enters the payload.
const events = new Set([
  "page_view", "example_view", "example_source_open", "example_github_open",
  "example_wiki_open", "example_share", "repository_submit",
  "generation_form_view", "generation_start", "first_chapter_read", "agent_setup_open",
]);
let send;

export function recordExperience(event) {
  if (events.has(event)) send?.(event);
}

export function installExperienceEvents(endpoint, surface) {
  if (send || navigator.doNotTrack === "1" || navigator.globalPrivacyControl || !["landing", "wiki"].includes(surface)) return;
  let url;
  try { url = new URL(endpoint, location.href); } catch { return; }
  if (url.origin !== location.origin || !["http:", "https:"].includes(url.protocol)) return;
  if (typeof globalThis.crypto?.randomUUID !== "function") return;
  let visit = crypto.randomUUID().replaceAll("-", "");
  let seen = new Set();
  try {
    const prior = JSON.parse(sessionStorage.getItem("codenib-experience") || "null");
    if (/^[a-f0-9]{32}$/.test(prior?.visit)) {
      visit = prior.visit;
      seen = new Set((prior.events || []).filter(item => events.has(item)));
    }
  } catch { /* Storage restrictions do not affect the product. */ }
  send = (event) => {
    if (seen.has(event)) return;
    seen.add(event);
    try { sessionStorage.setItem("codenib-experience", JSON.stringify({ visit, events: [...seen] })); } catch {}
    void fetch(url.href, {
      method: "POST", credentials: "omit", referrerPolicy: "no-referrer", keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event, surface, visit }),
    }).catch(() => {});
  };
  document.addEventListener("codenib:experience", (event) => recordExperience(event.detail?.event));
  recordExperience("page_view");
}
