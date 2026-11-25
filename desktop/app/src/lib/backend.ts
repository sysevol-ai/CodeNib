// The bridge to the app process. The window never reaches a repository's
// service itself: `api` asks the app to forward the request, and the app adds
// the session key.

type TauriGlobal = {
  core: { invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T> };
  event: {
    listen: <T>(event: string, handler: (event: { payload: T }) => void) => Promise<() => void>;
  };
  window?: { getCurrentWindow?: () => { startDragging: () => Promise<void> } };
};

function tauri(): TauriGlobal {
  const found = (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__;
  if (!found) throw new Error("CodeNib's app bridge is unavailable");
  return found;
}

export function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  return tauri().core.invoke<T>(cmd, args);
}

export function listen<T>(event: string, handler: (payload: T) => void): Promise<() => void> {
  return tauri().event.listen<T>(event, (e) => handler(e.payload));
}

export type RepoStatus = "stopped" | "starting" | "ready" | "failed";

export interface RepoView {
  path: string;
  name: string;
  analysis: boolean;
  generate: boolean;
  status: RepoStatus;
  error: string;
  line: string;
}

export interface Setup {
  codenib: string | null;
  version: string | null;
  bundled: boolean;
}

export interface Progress {
  repo: string;
  line: string;
}

export const backend = {
  setup: () => invoke<Setup>("setup_status"),
  repositories: () => invoke<RepoView[]>("repositories"),
  pickFolder: () => invoke<string | null>("pick_folder"),
  add: (path: string, analysis: boolean, generate: boolean) =>
    invoke<RepoView>("add_repository", { path, analysis, generate }),
  remove: (path: string) => invoke<void>("remove_repository", { path }),
  start: (path: string) => invoke<RepoView>("start_repository", { path }),
  restart: (path: string) => invoke<RepoView>("restart_repository", { path }),
  api: <T>(repo: string, method: string, path: string, body?: unknown) =>
    invoke<T>("api", { repo, method, path, body: body ?? null }),
  openExternal: (url: string) => invoke<void>("open_external", { url }),
  showInFinder: (path: string) => invoke<void>("show_in_finder", { path }),
};

/** An error message without the transport's prefix. */
export function message(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  return text.replace(/^HTTP \d+: /, "");
}
