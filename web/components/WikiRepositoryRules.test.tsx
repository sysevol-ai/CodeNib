// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import WikiRepositoryRules from "./WikiRepositoryRules";

it("explains the enforced limits, excluded files and separate graph coverage", () => {
  const html = renderToStaticMarkup(<WikiRepositoryRules limits={{
    max_archive_bytes: 20 * 1024 * 1024,
    max_source_bytes: 40 * 1024 * 1024,
    max_file_bytes: 4 * 1024 * 1024,
    max_files: 4000,
    max_archive_entries: 8000,
    max_chunks: 50000,
    source_languages: ["python", "typescript"],
    graph_languages: ["python"],
  }} />);
  expect(html).toContain("20 MiB");
  expect(html).toContain("40 MiB");
  expect(html).toContain("4,000 files");
  expect(html).toContain("4 MiB");
  expect(html).toContain("python, typescript");
  expect(html).toContain("cover python source only");
  expect(html).toContain("does not guarantee completion");
});
