# CodeNib landing page

This directory is a static site with no build step.

Run it locally from the repository root:

```bash
python -m http.server 7870 --directory landing
```

Deploy `landing/` as the document root for `codenib.ai`. The product previews
embed `https://demo.codenib.ai`; when that service is unavailable, the shipped
wiki screenshot remains as the visual fallback.

## Static routes

The site uses real directory indexes for nested routes:

- `/` is `landing/index.html`;
- `/blogs/` is `landing/blogs/index.html`;
- each article lives at `landing/blogs/<slug>/index.html`.

Nested pages should reference shared assets from `/assets/...` so the same URL
works at every route depth. The local Caddy configuration falls back to the
homepage for unknown paths, so a `200` response alone does not prove that a
new route or asset exists. Check the page title and asset content type as part
of deployment verification:

```bash
curl -s https://codenib.ai/blogs/ | grep '<title>CodeNib Blog'
curl -sI https://codenib.ai/assets/blogs/codegraph-agent-ready.png \
  | grep -i 'content-type: image/png'
```
