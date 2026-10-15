# Frontend reading surfaces

- Choose the reading column layout from the route, before API results arrive.
  Loading an Overview map must not remove a rail and resize the prose column.
- Resolve a page's leading map or boundary card before publishing its prose.
  Unavailable visuals and failed requests must also release the loading state;
  the elapsed indicator must cover the complete visible loading period.
- Measure responsive map canvases before the first paint. A later
  `ResizeObserver` remains useful for actual viewport changes.
- Verify opening and refreshing while page and visual responses arrive in
  different orders. Screenshots taken after loading do not cover this behavior.
  From `web/`, run `npm run build` and `node reading_layout_verify.mjs`.
  Keep that regression provider-free with intercepted fixture responses.
