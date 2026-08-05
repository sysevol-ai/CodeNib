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

export function navigate(href: string): void {
  window.history.pushState(null, "", withBasePath(href));
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

export function routeSegments(pathname: string): string[] {
  return pathname
    .split("/")
    .filter(Boolean)
    .map((part) => decodeURIComponent(part));
}
