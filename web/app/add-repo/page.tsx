// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import Header from "@/components/Header";
import RepositoryEntry from "@/components/RepositoryEntry";
import { AppLink } from "@/lib/router";
import "@/components/WikiRun.css";

export default function AddRepo() {
  return (
    <>
      <Header />
      <main className="repository-route own-add">
        <h1>Start with a GitHub URL</h1>
        <p>
          Generate a source-linked Repo Wiki from a public repository. Its size
          and languages are checked, and its folders drawn as a map, before you
          connect your OpenRouter account.
        </p>
        <RepositoryEntry />
        <AppLink className="own-back" href="/wikis">← Ready Wikis</AppLink>
      </main>
    </>
  );
}
