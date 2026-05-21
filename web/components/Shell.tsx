"use client";

import Link from "next/link";
import { useState } from "react";
import AskPanel from "./AskPanel";
import Sidebar, { type NavData } from "./Sidebar";

export interface ShellRepo {
  name: string;
  languages: string[];
  license: string;
  commit_short: string;
  commit_rel: string;
}

export default function Shell({
  repo,
  nav,
  children,
}: {
  repo: ShellRepo;
  nav: NavData;
  children: React.ReactNode;
}) {
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className="shell">
      <header className="topbar">
        <button
          className="hamburger"
          aria-label="Toggle navigation"
          aria-expanded={sidebarOpen}
          onClick={() => setSidebarOpen((o) => !o)}
        >
          ☰
        </button>
        <Link href="/" className="brand">
          <span className="brand-mark">◆</span>
          <span className="brand-name">{repo.name}</span>
          <span className="brand-wiki">Wiki</span>
        </Link>
        <div className="topbar-meta">
          <span className="chip">{repo.languages.join(", ")}</span>
          <span className="chip">{repo.license}</span>
          <span className="chip" title="latest commit">
            {repo.commit_short} · {repo.commit_rel}
          </span>
        </div>
        <div className="topbar-ask">
          <AskPanel />
        </div>
      </header>

      <div className="body">
        <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
          <Sidebar nav={nav} />
        </aside>
        {sidebarOpen && (
          <div
            className="scrim"
            onClick={() => setSidebarOpen(false)}
            aria-hidden
          />
        )}
        <main className="content">{children}</main>
      </div>
    </div>
  );
}
