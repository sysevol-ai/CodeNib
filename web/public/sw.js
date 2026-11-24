// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// The installed app's service worker. It caches nothing: every request goes
// to the network as before, so a deploy is live on the next load. Only when
// opening a page fails because the device is offline does it answer, with a
// short page saying so.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

const OFFLINE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#121315">
<title>CodeNib Wiki · Offline</title>
<style>
  html { color-scheme: dark; }
  body {
    margin: 0; min-height: 100vh; box-sizing: border-box;
    display: grid; place-items: center;
    background: #121315; color: #e8eaed;
    font: 16px/1.5 Geist, system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: env(safe-area-inset-top) 24px env(safe-area-inset-bottom);
  }
  main { max-width: 26rem; }
  h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.5rem; }
  p { margin: 0 0 1.25rem; color: #bdc1c6; }
  button {
    font: inherit; font-weight: 500; color: #121315; background: #e8eaed;
    border: 0; border-radius: 6px; padding: 0.5rem 1rem; cursor: pointer;
  }
  button:focus-visible { outline: none; box-shadow: 0 0 0 3px rgba(138, 180, 248, 0.5); }
</style>
</head>
<body>
<main>
  <h1>You are offline</h1>
  <p>CodeNib Wiki reads each page from its server, so it needs a connection. Try again once you are back online.</p>
  <button type="button" onclick="location.reload()">Try again</button>
</main>
</body>
</html>`;

self.addEventListener("fetch", (event) => {
  // Only page loads get a fallback; everything else is left to the browser.
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    fetch(event.request).catch(
      () =>
        new Response(OFFLINE, {
          status: 503,
          headers: {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
          },
        }),
    ),
  );
});
