// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

(() => {
  const base = window.__CODENIB_RUNTIME__?.basePath || "/";
  const root = base.endsWith("/") ? base : base + "/";
  const route = location.pathname + location.search + location.hash;
  location.replace(root + "?__codenib_route=" + encodeURIComponent(route));
})();
