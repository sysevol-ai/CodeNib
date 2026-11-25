// The window's state: the repositories the person added, each one's data
// from its local service, and what the window shows.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { backend, listen, message, type Progress, type RepoView, type Setup } from "./backend";
import type { MapArea } from "./areas";
import type { NotesState, RepoInfo, Tree } from "./model";

export type View =
  | { kind: "home" }
  | { kind: "inbox" }
  | { kind: "page"; repo: string; page: string }
  | { kind: "review"; repo: string; task: string }
  | { kind: "map" | "impact" | "walkthrough"; repo: string; page: string };

export interface RepoData {
  info?: RepoInfo;
  tree?: Tree;
  notes?: NotesState;
  mapAreas?: MapArea[] | null;
  error?: string;
  loadedAt?: number;
}

interface Store {
  setup: Setup | null;
  repos: RepoView[];
  data: Record<string, RepoData>;
  lines: Record<string, string[]>;
  view: View;
  go: (view: View) => void;
  open: (path: string) => void;
  reload: (path: string) => Promise<void>;
  refreshNotes: (path: string) => Promise<void>;
  call: <T>(path: string, method: string, apiPath: string, body?: unknown) => Promise<T>;
  addFolder: string | null;
  setAddFolder: (path: string | null) => void;
  chooseFolder: () => Promise<void>;
  addRepository: (path: string, analysis: boolean, generate: boolean) => Promise<void>;
  removeRepository: (path: string) => Promise<void>;
  restart: (path: string) => Promise<void>;
  palette: { open: boolean; ask?: string };
  setPalette: (palette: { open: boolean; ask?: string }) => void;
}

const StoreContext = createContext<Store | null>(null);

export function useStore(): Store {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useStore outside the store");
  return store;
}

const LAST = "codenib.last-view";
const LINES = 40;

function savedView(): View {
  try {
    const raw = localStorage.getItem(LAST);
    const view = raw ? (JSON.parse(raw) as View) : null;
    if (view && view.kind === "page") return view;
  } catch {
    // A missing or unreadable bookmark just starts at home.
  }
  return { kind: "home" };
}

/** The repository id inside its service, for API paths. */
export function repoPath(data: RepoData | undefined, rest: string): string | null {
  const id = data?.info?.id;
  return id ? `/api/repos/${encodeURIComponent(id)}${rest}` : null;
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [setup, setSetup] = useState<Setup | null>(null);
  const [repos, setRepos] = useState<RepoView[]>([]);
  const [data, setData] = useState<Record<string, RepoData>>({});
  const [lines, setLines] = useState<Record<string, string[]>>({});
  const [view, setView] = useState<View>(savedView);
  const [addFolder, setAddFolder] = useState<string | null>(null);
  const [palette, setPalette] = useState<{ open: boolean; ask?: string }>({ open: false });
  const loading = useRef(new Set<string>());
  const dataRef = useRef(data);
  dataRef.current = data;

  const go = useCallback((next: View) => {
    setView(next);
    try {
      localStorage.setItem(LAST, JSON.stringify(next));
    } catch {
      // Remembering the last page is a convenience only.
    }
  }, []);

  const call = useCallback(
    <T,>(path: string, method: string, apiPath: string, body?: unknown) =>
      backend.api<T>(path, method, apiPath, body),
    [],
  );

  const patchData = useCallback((path: string, patch: Partial<RepoData>) => {
    setData((all) => ({ ...all, [path]: { ...all[path], ...patch } }));
  }, []);

  const refreshNotes = useCallback(
    async (path: string) => {
      const notesPath = repoPath(dataRef.current[path], "/annotations");
      if (!notesPath) return;
      try {
        const notes = await backend.api<NotesState>(path, "GET", notesPath);
        patchData(path, { notes });
      } catch {
        // The next poll tries again.
      }
    },
    [patchData],
  );

  const reload = useCallback(
    async (path: string) => {
      if (loading.current.has(path)) return;
      loading.current.add(path);
      try {
        const infos = await backend.api<RepoInfo[]>(path, "GET", "/api/repos");
        const info = infos[0];
        if (!info) throw new Error("CodeNib found no repository in this folder");
        const base = `/api/repos/${encodeURIComponent(info.id)}`;
        const [tree, notes, map] = await Promise.all([
          backend.api<Tree>(path, "GET", `${base}/wiki`),
          backend.api<NotesState>(path, "GET", `${base}/annotations`).catch(() => undefined),
          backend
            .api<{ available: boolean; areas: MapArea[] }>(path, "GET", `${base}/wiki-map`)
            .catch(() => ({ available: false, areas: [] })),
        ]);
        patchData(path, {
          info,
          tree,
          notes,
          mapAreas: map.available ? map.areas : null,
          error: undefined,
          loadedAt: Date.now(),
        });
      } catch (error) {
        patchData(path, { error: message(error) });
      } finally {
        loading.current.delete(path);
      }
    },
    [patchData],
  );

  const refreshRepos = useCallback(async () => {
    const list = await backend.repositories();
    setRepos(list);
    return list;
  }, []);

  const open = useCallback(
    (path: string) => {
      const repo = repos.find((r) => r.path === path);
      if (repo?.status === "ready") {
        if (!dataRef.current[path]?.info) void reload(path);
        return;
      }
      if (repo?.status === "starting") return;
      backend
        .start(path)
        .then((started) => setRepos((all) => all.map((r) => (r.path === path ? started : r))))
        .catch((error) => toast.error(message(error)));
    },
    [repos, reload],
  );

  // Status and progress come from the app process as events.
  useEffect(() => {
    let stop = false;
    const unlisten: (() => void)[] = [];
    void listen<RepoView>("codenib://status", (repo) => {
      setRepos((all) => all.map((r) => (r.path === repo.path ? repo : r)));
      if (repo.status === "ready") void reload(repo.path);
    }).then((u) => (stop ? u() : unlisten.push(u)));
    void listen<Progress>("codenib://progress", ({ repo, line }) => {
      setLines((all) => ({ ...all, [repo]: [...(all[repo] ?? []), line].slice(-LINES) }));
    }).then((u) => (stop ? u() : unlisten.push(u)));
    return () => {
      stop = true;
      unlisten.forEach((u) => u());
    };
  }, [reload]);

  // First load: what is installed, which repositories, and open the one shown.
  useEffect(() => {
    void backend.setup().then(setSetup).catch(() => setSetup(null));
    void refreshRepos().then((list) => {
      const shown = savedView();
      const first = "repo" in shown ? list.find((r) => r.path === shown.repo) : undefined;
      const target = first ?? list[0];
      // Nothing saved, or the saved repository is gone: show the first one.
      if (!first && list[0]) setView({ kind: "page", repo: list[0].path, page: "" });
      if (target) {
        if (target.status === "ready") void reload(target.path);
        else void backend.start(target.path).then((r) => setRepos((all) => all.map((x) => (x.path === r.path ? r : x))));
      }
    });
  }, [refreshRepos, reload]);

  // Tasks move on their own: poll quickly while one runs, slowly otherwise.
  useEffect(() => {
    const timer = window.setInterval(() => {
      for (const repo of repos) {
        if (repo.status !== "ready") continue;
        const d = dataRef.current[repo.path];
        if (!d?.info) continue;
        const busy = d.notes?.tasks.some((t) => t.status === "queued" || t.status === "running");
        const age = Date.now() - (d.loadedAt ?? 0);
        if (busy || age > 15000) {
          patchData(repo.path, { loadedAt: Date.now() });
          void refreshNotes(repo.path);
        }
      }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [repos, patchData, refreshNotes]);

  const chooseFolder = useCallback(async () => {
    const folder = await backend.pickFolder();
    if (folder) setAddFolder(folder);
  }, []);

  const addRepository = useCallback(
    async (path: string, analysis: boolean, generate: boolean) => {
      const added = await backend.add(path, analysis, generate);
      setRepos((all) => [...all.filter((r) => r.path !== path), added]);
      setLines((all) => ({ ...all, [path]: [] }));
      setData((all) => {
        const next = { ...all };
        delete next[path];
        return next;
      });
      go({ kind: "page", repo: path, page: "" });
    },
    [go],
  );

  const removeRepository = useCallback(
    async (path: string) => {
      await backend.remove(path);
      setRepos((all) => all.filter((r) => r.path !== path));
      if ("repo" in view && view.repo === path) go({ kind: "home" });
      toast(`Removed from CodeNib. The folder and its .codenib/ notes stay.`);
    },
    [view, go],
  );

  const restart = useCallback(async (path: string) => {
    setLines((all) => ({ ...all, [path]: [] }));
    const started = await backend.restart(path);
    setRepos((all) => all.map((r) => (r.path === path ? started : r)));
  }, []);

  const store = useMemo<Store>(
    () => ({
      setup,
      repos,
      data,
      lines,
      view,
      go,
      open,
      reload,
      refreshNotes,
      call,
      addFolder,
      setAddFolder,
      chooseFolder,
      addRepository,
      removeRepository,
      restart,
      palette,
      setPalette,
    }),
    [setup, repos, data, lines, view, go, open, reload, refreshNotes, call, addFolder, chooseFolder, addRepository, removeRepository, restart, palette],
  );

  return <StoreContext.Provider value={store}>{children}</StoreContext.Provider>;
}
