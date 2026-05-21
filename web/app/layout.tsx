import type { Metadata } from "next";
import "./globals.css";
import Shell from "@/components/Shell";
import { getWiki, relativeTime } from "@/lib/wiki";

export const metadata: Metadata = {
  title: "CodeMiner Wiki",
  description: "Auto-generated documentation for the CodeMiner repository.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const wiki = getWiki();
  const nav = {
    pages: wiki.pages.map((p) => ({ id: p.id, title: p.title })),
    modules: wiki.modules.map((m) => ({
      id: m.id,
      title: m.title,
      files: m.files.map((f) => ({
        path: f.path,
        name: f.path.startsWith(m.path + "/")
          ? f.path.slice(m.path.length + 1)
          : f.name,
      })),
    })),
  };
  const repo = {
    name: wiki.repo.name,
    languages: wiki.repo.languages,
    license: wiki.repo.license,
    commit_short: wiki.repo.commit_short,
    commit_rel: relativeTime(wiki.repo.commit_date),
  };

  return (
    <html lang="en">
      <body>
        <Shell repo={repo} nav={nav}>
          {children}
        </Shell>
      </body>
    </html>
  );
}
