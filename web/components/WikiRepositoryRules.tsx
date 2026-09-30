// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";
import { wikiGenerationAvailable, type WikiGenerationLimits } from "@/lib/visitorWiki";
import { isStaticRuntime } from "@/lib/runtime";

export const mebibytes = (bytes: number) => `${Number((bytes / (1024 * 1024)).toFixed(2))} MiB`;

/** Read the same limits that the server enforces; no repository is downloaded. */
export default function WikiRepositoryRules({ limits }: { limits?: WikiGenerationLimits }) {
  const [loaded, setLoaded] = useState<WikiGenerationLimits | undefined>(limits);
  useEffect(() => {
    if (limits || isStaticRuntime()) return;
    let active = true;
    wikiGenerationAvailable()
      .then((value) => { if (active) setLoaded(value.limits); })
      .catch(() => {});
    return () => { active = false; };
  }, [limits]);
  const policy = limits ?? loaded;
  return (
    <div className="wiki-repository-rules">
      {policy && <p>Hosted limits: {mebibytes(policy.max_archive_bytes)} download, {mebibytes(policy.max_source_bytes)} retained files, {policy.max_files.toLocaleString()} files. Files over {mebibytes(policy.max_file_bytes)} are skipped.</p>}
      <details>
      <summary>Which repositories can generate a Wiki?</summary>
      <p>Public GitHub repositories only. We read a snapshot of the default branch.</p>
      {policy ? (
        <>
          <ul>
            <li>Download archive: up to {mebibytes(policy.max_archive_bytes)}.</li>
            <li>Retained files: up to {mebibytes(policy.max_source_bytes)} in total.</li>
            <li>Up to {policy.max_files.toLocaleString()} files, including skipped files; {policy.max_archive_entries.toLocaleString()} archive entries including directories.</li>
            <li>Files larger than {mebibytes(policy.max_file_bytes)} are skipped and listed in the result.</li>
            <li>Source analysis: up to {policy.max_chunks.toLocaleString()} code sections.</li>
          </ul>
          <p>Wiki source languages: {policy.source_languages.join(", ")}. System Map and CodeGraph currently cover {policy.graph_languages.join(", ")} source only; indexing can time out or find no relationships between chapters.</p>
        </>
      ) : (
        <p>The live service checks size and supported source languages before you connect OpenRouter. Large repositories may require a local run.</p>
      )}
      <p>Passing the source check does not guarantee completion: your model, budget and the run time limit also apply. For larger repositories, <a href="https://docs.codenib.ai/web_demo/" target="_blank" rel="noopener noreferrer">run CodeNib locally ↗</a>.</p>
      </details>
    </div>
  );
}
