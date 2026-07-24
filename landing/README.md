# CodeNib landing page

This directory is a static site with no build step.

Run it locally from the repository root:

```bash
python -m http.server 7870 --directory landing
```

Deploy `landing/` as the document root for `codenib.ai`. The product previews
embed `https://demo.codenib.ai`; when that service is unavailable, the shipped
wiki screenshot remains as the visual fallback.
