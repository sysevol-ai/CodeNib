import wikiData from "@/data/wiki.json";

export interface RepoMeta {
  name: string;
  description: string;
  languages: string[];
  supports: string[];
  license: string;
  commit: string;
  commit_short: string;
  commit_date: string;
  commit_subject: string;
  file_count: number;
  loc: number;
  github: string;
}

export interface SymbolMethod {
  name: string;
  signature: string;
  lineno: number;
  docstring: string;
}

export interface SymbolEntry {
  kind: "class" | "function";
  name: string;
  signature: string;
  lineno: number;
  end_lineno: number;
  docstring: string;
  methods: SymbolMethod[];
}

export interface FileEntry {
  path: string;
  name: string;
  language: string;
  lines: number;
  docstring: string;
  symbols: SymbolEntry[];
  source_url: string;
  parse_error?: string;
}

export interface ModuleEntry {
  id: string;
  name: string;
  title: string;
  description: string;
  path: string;
  file_count: number;
  symbol_count: number;
  loc: number;
  files: FileEntry[];
}

export interface WikiPage {
  id: string;
  title: string;
  markdown: string;
}

export interface Wiki {
  repo: RepoMeta;
  architecture_mermaid: string;
  pages: WikiPage[];
  modules: ModuleEntry[];
}

const wiki = wikiData as unknown as Wiki;

export function getWiki(): Wiki {
  return wiki;
}

export function getModule(id: string): ModuleEntry | undefined {
  return wiki.modules.find((m) => m.id === id);
}

export function getPage(id: string): WikiPage | undefined {
  return wiki.pages.find((p) => p.id === id);
}

export function getFile(path: string): {
  module: ModuleEntry;
  file: FileEntry;
} | undefined {
  for (const m of wiki.modules) {
    const f = m.files.find((x) => x.path === path);
    if (f) return { module: m, file: f };
  }
  return undefined;
}

export function relativeTime(iso: string): string {
  if (!iso) return "unknown";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diff = Date.now() - then;
  const day = 86400000;
  if (diff < day) return "today";
  const days = Math.floor(diff / day);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(months / 12)}y ago`;
}
