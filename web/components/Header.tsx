"use client";

import { useEffect, useState, type ReactNode } from "react";
import { AppLink } from "@/lib/router";

function ThemeToggle() {
  const [theme, setTheme] = useState<"light" | "dark">("light");
  useEffect(() => {
    const t = document.documentElement.dataset.theme;
    setTheme(t === "dark" ? "dark" : "light");
  }, []);
  function toggle() {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("cm-theme", next);
    } catch {}
    setTheme(next);
  }
  return (
    <button
      className="icon-btn theme-toggle"
      onClick={toggle}
      aria-label="Toggle color theme"
      title="Toggle theme"
    >
      {theme === "dark" ? (
        // sun
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5" />
        </svg>
      ) : (
        // moon
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  );
}

function ShareButton() {
  const [done, setDone] = useState(false);
  return (
    <button
      className="btn-primary"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(window.location.href);
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        } catch {}
      }}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="18" cy="5" r="3" />
        <circle cx="6" cy="12" r="3" />
        <circle cx="18" cy="19" r="3" />
        <path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4" />
      </svg>
      {done ? "Copied" : "Share"}
    </button>
  );
}

export default function Header({
  center,
  actions,
}: {
  center?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="site-header">
      <AppLink href="/" className="brand">
        <img className="brand-mark" src="/codenib-icon.svg" alt="" /> CodeNib Wiki
      </AppLink>
      {center}
      <div className="header-right">
        <span className="header-note">Source-linked repository docs</span>
        {actions}
        <ShareButton />
        <ThemeToggle />
      </div>
    </header>
  );
}
