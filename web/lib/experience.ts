// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { installExperienceEvents, recordExperience } from "../../landing/assets/explore/experience.js";
import { isStaticRuntime } from "./runtime";

export function startExperienceEvents() {
  if (!isStaticRuntime() && window.__CODENIB_RUNTIME__?.experienceEvents === true) {
    installExperienceEvents("/api/experience-events", "wiki");
  }
}
export { recordExperience };
