// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { createElement, useEffect } from "react";
import { withBasePath } from "@/lib/runtime";

/** The marketing site and Wiki share the same captured, interactive example. */
export default function FirstVisitExplorer() {
  useEffect(() => {
    void import("../../landing/assets/explore/repo-explorer.js");
  }, []);
  return createElement("codenib-explorer", {
    "wiki-href": withBasePath("/psf__requests?p=redirects"),
    "share-href": withBasePath("/#requests-auth"),
    class: "first-visit-explorer",
  }, createElement("a", { href: withBasePath("/psf__requests?p=redirects") },
    "Explore how Requests handles redirects →"));
}
