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

export type WriterId = "claude-code" | "codex";

export interface RepoView {
  path: string;
  name: string;
  analysis: boolean;
  generate: boolean;
  /** The agent that writes the pages, if one does. */
  writer: WriterId | null;
  /** The GitHub repository the folder was cloned from, as owner/repo. */
  remote: string | null;
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

export interface AgentStatus {
  id: WriterId;
  name: string;
  installed: boolean;
  signed_in: boolean;
  /** How it is signed in, in a few words. */
  detail: string | null;
  path: string | null;
  install: string;
  docs: string;
}

export type GitHubSource = "codenib" | "github-cli" | "environment" | "git";

export interface GitHubStatus {
  signed_in: boolean;
  login: string | null;
  name: string | null;
  source: GitHubSource | null;
  device_sign_in: boolean;
  problem: string | null;
}

export interface GitHubRepository {
  full_name: string;
  description: string | null;
  private: boolean;
  pushed_at: string | null;
  language: string | null;
}

export interface GitHubLookup {
  repository: GitHubRepository;
  destination: string;
  present: boolean;
}

export interface DeviceCode {
  user_code: string;
  verification_uri: string;
  device_code: string;
  interval: number;
  expires_in: number;
}

export interface CloneProgress {
  slug: string;
  line: string;
}

export const backend = {
  setup: () => invoke<Setup>("setup_status"),
  repositories: () => invoke<RepoView[]>("repositories"),
  pickFolder: () => invoke<string | null>("pick_folder"),
  add: (path: string, analysis: boolean, writer: WriterId | null) =>
    invoke<RepoView>("add_repository", { path, analysis, writer }),
  remove: (path: string) => invoke<void>("remove_repository", { path }),
  start: (path: string) => invoke<RepoView>("start_repository", { path }),
  restart: (path: string) => invoke<RepoView>("restart_repository", { path }),
  api: <T>(repo: string, method: string, path: string, body?: unknown) =>
    invoke<T>("api", { repo, method, path, body: body ?? null }),
  openExternal: (url: string) => invoke<void>("open_external", { url }),
  showInFinder: (path: string) => invoke<void>("show_in_finder", { path }),
  agents: () => invoke<AgentStatus[]>("agents_status"),
  agentSignIn: (id: WriterId) => invoke<void>("agent_sign_in", { id }),
  agentInstall: (id: WriterId) => invoke<void>("agent_install", { id }),
  github: () => invoke<GitHubStatus>("github_status"),
  githubUseToken: (token: string) => invoke<GitHubStatus>("github_use_token", { token }),
  githubSignOut: () => invoke<GitHubStatus>("github_sign_out"),
  githubDeviceStart: () => invoke<DeviceCode>("github_device_start"),
  githubDeviceWait: (code: DeviceCode) => invoke<GitHubStatus>("github_device_wait", { code }),
  githubDeviceCancel: () => invoke<void>("github_device_cancel"),
  githubRepositories: () => invoke<GitHubRepository[]>("github_repositories"),
  githubLookup: (input: string) => invoke<GitHubLookup>("github_lookup", { input }),
  cloneRepository: (input: string) => invoke<string>("clone_repository", { input }),
};

/** An error message without the transport's prefix. */
export function message(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  return text.replace(/^HTTP \d+: /, "");
}
