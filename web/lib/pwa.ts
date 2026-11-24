// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

/// <reference types="vite/client" />

/** The installed app: its title bar colour and its service worker. */

import { assetUrl } from "@/lib/runtime";

const THEME_COLORS = { light: "#ffffff", dark: "#121315" } as const;

/** Paint the installed app's title bar in the page colour of a theme. */
export function syncThemeColor(theme: "light" | "dark"): void {
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", THEME_COLORS[theme]);
}

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/** Whether this page may register the service worker. */
export function serviceWorkerAllowed(
  location: Pick<Location, "protocol" | "hostname">,
  production: boolean,
  supported: boolean,
): boolean {
  if (!production || !supported) return false;
  return location.protocol === "https:" || LOOPBACK.has(location.hostname);
}

/**
 * Register sw.js, which caches nothing and only answers offline page loads.
 * Registration waits for the page to load, so it never delays the first view.
 */
export function registerServiceWorker(): void {
  if (
    !serviceWorkerAllowed(
      window.location,
      import.meta.env.PROD,
      "serviceWorker" in navigator,
    )
  )
    return;
  const register = () => {
    navigator.serviceWorker.register(assetUrl("/sw.js")).catch(() => {
      // The Wiki works the same without it; only installing as an app needs it.
    });
  };
  if (document.readyState === "complete") register();
  else window.addEventListener("load", register, { once: true });
}
