declare global {
  interface Window {
    __CODENIB_API_BASE__?: string;
    __CODENIB_RUNTIME__?: {
      mode?: "api" | "static";
      basePath?: string;
      dataBase?: string;
      trialApiBase?: string;
    };
  }
}

export const STATIC_ROUTE_PARAM = "__codenib_route";

export function normalizeRuntimeBasePath(value: string | undefined): string {
  if (!value || !value.startsWith("/") || value.includes("\\")) return "/";
  const segments = value.split("/").filter(Boolean);
  if (segments.some((segment) => segment === "." || segment === "..")) return "/";
  return segments.length ? `/${segments.join("/")}` : "/";
}

export function appBasePath(): string {
  if (typeof window === "undefined") return "/";
  return normalizeRuntimeBasePath(window.__CODENIB_RUNTIME__?.basePath);
}

export function apiBase(): string {
  if (typeof window === "undefined") return "";
  return (window.__CODENIB_API_BASE__ ?? "").replace(/\/+$/, "");
}

export function isStaticRuntime(): boolean {
  return (
    typeof window !== "undefined" && window.__CODENIB_RUNTIME__?.mode === "static"
  );
}

export function withBasePath(path: string): string {
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/|#)/i.test(path)) return path;
  const base = appBasePath();
  const absolute = path.startsWith("/") ? path : `/${path}`;
  if (base === "/") return absolute;
  return `${base}${absolute}`;
}

export function stripBasePath(pathname: string): string {
  const base = appBasePath();
  if (base === "/") return pathname || "/";
  if (pathname === base) return "/";
  if (pathname.startsWith(`${base}/`)) return pathname.slice(base.length) || "/";
  return pathname || "/";
}

export function assetUrl(path: string): string {
  return withBasePath(path);
}

export function mediaAssetUrl(path: string): string | null {
  const value = path.trim();
  if (!value || /[\\\u0000-\u001f\u007f]/.test(value)) return null;
  // Exports own every displayed media file. Never turn a cached live-server
  // URL or third-party image into a hidden request from a static preview.
  if (isStaticRuntime()) {
    if (!/^data\/repos\/[A-Za-z0-9_./%-]+$/.test(value)) return null;
    try {
      const decoded = decodeURIComponent(value);
      if (/[\\\u0000-\u001f\u007f]/.test(decoded) || decoded.split("/").some((p) => p === "." || p === "..")) return null;
    } catch { return null; }
  }
  if (/^https?:/i.test(value)) {
    try {
      const url = new URL(value);
      if (!url.hostname || url.username || url.password) return null;
      return url.protocol === "http:" || url.protocol === "https:" ? value : null;
    } catch {
      return null;
    }
  }
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/|#)/i.test(value)) return null;
  const pathname = value.split(/[?#]/, 1)[0];
  if (pathname.split("/").some((segment) => segment === "." || segment === "..")) {
    return null;
  }
  return assetUrl(value);
}

export function staticDataUrl(...segments: string[]): string {
  const configured =
    typeof window !== "undefined" ? window.__CODENIB_RUNTIME__?.dataBase : undefined;
  const root = (configured || withBasePath("/data")).replace(/\/+$/, "");
  const suffix = segments.map((segment) => encodeURIComponent(segment)).join("/");
  return suffix ? `${root}/${suffix}` : root;
}

export function restoreStaticRoute(): void {
  if (!isStaticRuntime() || typeof window === "undefined") return;
  const current = new URL(window.location.href);
  const raw = current.searchParams.get(STATIC_ROUTE_PARAM);
  if (!raw) return;

  const target = new URL(raw, window.location.origin);
  const base = appBasePath();
  const inBase =
    target.origin === window.location.origin &&
    (base === "/" || target.pathname === base || target.pathname.startsWith(`${base}/`));
  if (!inBase) {
    current.searchParams.delete(STATIC_ROUTE_PARAM);
    window.history.replaceState(null, "", `${current.pathname}${current.search}${current.hash}`);
    return;
  }
  window.history.replaceState(
    null,
    "",
    `${target.pathname}${target.search}${target.hash}`,
  );
}

export {};
