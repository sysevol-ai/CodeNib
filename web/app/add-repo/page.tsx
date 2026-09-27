// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import Header from "@/components/Header";
import RepositoryEntry from "@/components/RepositoryEntry";
import { AppLink } from "@/lib/router";

export default function AddRepo() {
  return (
    <div className="landing">
      <Header />
      <main className="hero">
        <AppLink href="/">← Ready Wikis</AppLink>
        <h1>Start with a GitHub URL</h1>
        <p className="hero-sub">
          Explore a public repository’s files and README, then add an AI
          explanation if you want one.
        </p>
        <RepositoryEntry />
      </main>
    </div>
  );
}
