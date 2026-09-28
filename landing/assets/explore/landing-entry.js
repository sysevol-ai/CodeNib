// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { installExperienceEvents, recordExperience } from "./experience.js";
// A host may opt in after proxying this first-party endpoint to CodeNib.
const endpoint = document.querySelector('meta[name="codenib-experience-endpoint"]')?.content;
if (endpoint) installExperienceEvents(endpoint, "landing");
document.querySelector(".repo-entry")?.addEventListener("submit", () => recordExperience("repository_submit"));
document.querySelectorAll('a[href="#get-started"]').forEach(link => {
  link.addEventListener("click", () => recordExperience("agent_setup_open"));
});
