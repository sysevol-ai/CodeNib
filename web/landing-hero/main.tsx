// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Entry for the codenib.ai hero map, built by vite.landing.config.ts into
// landing/assets/hero-map/. It replaces the static preview inside
// #codenib-hero-map once it loads.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import LandingMap, { type CapturedMap } from "./LandingMap";
import captured from "./maps";

const mount = document.getElementById("codenib-hero-map");
if (mount) {
  const demo = mount.dataset.demo || "https://demo.codenib.ai";
  createRoot(mount).render(
    <StrictMode>
      <LandingMap maps={captured as CapturedMap[]} demo={demo} />
    </StrictMode>,
  );
}
