// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { installExperienceEvents, recordExperience } from "./experience.js";
// A host may opt in after proxying this first-party endpoint to CodeNib.
const endpoint = document.querySelector('meta[name="codenib-experience-endpoint"]')?.content;
if (endpoint) installExperienceEvents(endpoint, "landing");
const explorer = document.querySelector("codenib-explorer");
const taskButtons = document.querySelectorAll("[data-example-task]");
taskButtons.forEach(button => {
  button.addEventListener("click", () => {
    if (!explorer) return;
    explorer.setAttribute("task", button.dataset.exampleTask);
    taskButtons.forEach(item => item.setAttribute("aria-pressed", String(item === button)));
    // Keep task switching local. On a narrow screen the example is above the
    // questions; bring the explanation back into view without moving focus.
    if (matchMedia("(max-width: 860px)").matches) {
      explorer.scrollIntoView({ block: "start", behavior: "instant" });
    }
  });
});
document.querySelectorAll('a[href="#example-source"]').forEach(link => {
  link.addEventListener("click", event => {
    const selected = explorer?.shadowRoot?.querySelector('[aria-pressed="true"]');
    if (!selected || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (location.hash !== "#example-source") history.pushState(null, "", link.href);
    document.getElementById("example-source").scrollIntoView({
      block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
    });
    selected.focus({ preventScroll: true });
  });
});
document.querySelector(".repo-entry")?.addEventListener("submit", () => recordExperience("repository_submit"));
document.querySelectorAll('a[href="#get-started"]').forEach(link => {
  link.addEventListener("click", () => recordExperience("agent_setup_open"));
});
