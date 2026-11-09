import {
  useEffect,
  useState,
  type AnchorHTMLAttributes,
  type MouseEvent,
} from "react";
import { stripBasePath, withBasePath } from "./runtime";

export interface BrowserLocation {
  pathname: string;
  search: string;
}

function currentLocation(): BrowserLocation {
  return {
    pathname: stripBasePath(window.location.pathname),
    search: window.location.search,
  };
}

export function navigate(href: string, replace = false): void {
  if (replace) window.history.replaceState(null, "", withBasePath(href));
  else window.history.pushState(null, "", withBasePath(href));
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function useBrowserLocation(): BrowserLocation {
  const [location, setLocation] = useState<BrowserLocation>(() => currentLocation());

  useEffect(() => {
    const update = () => setLocation(currentLocation());
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  return location;
}

export function AppLink({
  href,
  onClick,
  target,
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  function follow(event: MouseEvent<HTMLAnchorElement>) {
    onClick?.(event);
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      target === "_blank"
    ) {
      return;
    }
    event.preventDefault();
    navigate(href);
  }

  return <a {...props} href={withBasePath(href)} target={target} onClick={follow} />;
}

/**
 * The address of another page of the Wiki the reader has open. A Wiki read
 * at /<repo>/page/<id>/, or any static export, moves by path so that every
 * address matches a prerendered document; the hosted Wiki keeps `?p=`.
 */
export function wikiPageLocation(pathname: string, pageId: string, pathForm: boolean): string {
  const id = encodeURIComponent(pageId);
  const page = /^(.*\/)page\/[^/]+\/?$/.exec(pathname);
  if (page) return pageId === "overview" ? page[1] : `${page[1]}page/${id}/`;
  if (pathForm) {
    const root = pathname.endsWith("/") ? pathname : `${pathname}/`;
    return pageId === "overview" ? root : `${root}page/${id}/`;
  }
  return `${pathname}?p=${id}`;
}

export function routeSegments(pathname: string): string[] {
  return pathname
    .split("/")
    .filter(Boolean)
    .map((part) => {
      try { return decodeURIComponent(part); }
      catch { return part; }
    });
}
