"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

export interface NavModule {
  id: string;
  title: string;
  files: { path: string; name: string }[];
}
export interface NavData {
  pages: { id: string; title: string }[];
  modules: NavModule[];
}

function Module({ m, activePath }: { m: NavModule; activePath: string }) {
  const moduleHref = `/modules/${m.id}`;
  const isActiveModule = activePath === moduleHref;
  const containsActiveFile = m.files.some(
    (f) => activePath === `/files/${f.path}`
  );
  const [open, setOpen] = useState(isActiveModule || containsActiveFile);

  return (
    <li className="nav-module">
      <div className={`nav-module-head ${isActiveModule ? "active" : ""}`}>
        <button
          className="twisty"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-label={`Toggle ${m.title}`}
        >
          {open ? "▾" : "▸"}
        </button>
        <Link href={moduleHref}>{m.title}</Link>
      </div>
      {open && (
        <ul className="nav-files">
          {m.files.map((f) => {
            const href = `/files/${f.path}`;
            return (
              <li key={f.path}>
                <Link
                  href={href}
                  className={activePath === href ? "active" : ""}
                >
                  {f.name}
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}

export default function Sidebar({ nav }: { nav: NavData }) {
  const pathname = usePathname() || "/";
  return (
    <nav className="sidebar-nav" aria-label="Documentation">
      <div className="nav-section-title">Wiki</div>
      <ul className="nav-list">
        <li>
          <Link href="/" className={pathname === "/" ? "active" : ""}>
            Home
          </Link>
        </li>
        {nav.pages.map((p) => {
          const href = `/wiki/${p.id}`;
          return (
            <li key={p.id}>
              <Link href={href} className={pathname === href ? "active" : ""}>
                {p.title}
              </Link>
            </li>
          );
        })}
      </ul>

      <div className="nav-section-title">Modules</div>
      <ul className="nav-list">
        {nav.modules.map((m) => (
          <Module key={m.id} m={m} activePath={pathname} />
        ))}
      </ul>
    </nav>
  );
}
