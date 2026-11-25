# Frontend reading surfaces

- Choose the reading column layout from the route, before API results arrive.
  Loading an Overview map must not remove a rail and resize the prose column.
- Resolve a page's leading map or boundary card before publishing its prose.
  Unavailable visuals and failed requests must also release the loading state;
  the elapsed indicator must cover the complete visible loading period.
- Bound optional visual waits, including outline prerequisites, and ignore late
  responses after revealing prose. A stalled visual must not hide a loaded page
  indefinitely or insert itself above the reader after the fallback.
- Measure responsive map canvases before the first paint. A later
  `ResizeObserver` remains useful for actual viewport changes.
- Verify opening and refreshing while page and visual responses arrive in
  different orders. Screenshots taken after loading do not cover this behavior.
  From `web/`, run `npm run build` and `node reading_layout_verify.mjs`.
  Keep that regression provider-free with intercepted fixture responses.
- A walkthrough (`?walk=<id>`) replaces the map and the reader while the page
  list stays. Until its data answers it holds a loading state rather than
  painting the Overview, and an unknown or missing walkthrough falls back to
  the page. Side by side, the narration keeps one height for the whole walk,
  measured from its tallest step, so no step clips or moves the controls.
  After changing the view, run `npm run build` and `node walkthrough_verify.mjs`
  (provider-free fixtures, four widths).
