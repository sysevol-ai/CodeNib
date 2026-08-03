# CodeNib landing page

This directory is a static site with no build step.

Run it locally from the repository root:

```bash
python -m http.server 7870 --directory landing
```

Deploy `landing/` as the document root for `codenib.ai`. The product previews
embed `https://demo.codenib.ai`; when that service is unavailable, the shipped
wiki screenshot remains as the visual fallback.

## Deploy status

The live `codenib.ai` deployment was observed stale on 2026-07-25: it still
served pre-rebrand assets (`assets/logo.png`, `assets/mark.png` instead of the
current SVGs) and four `https://docs.codenib.ai` links, a hostname that does
not resolve. Redeploy this directory to replace that build. Quick check that
the live site matches the repo copy:

```bash
curl -s https://codenib.ai/ | grep codenib-logo.svg
```

No output means the stale build is still live.

Launch gate: the GitHub CTAs on this page point at
`https://github.com/sysevol-ai/CodeNib`, which returns 404 for anonymous
visitors while the repository is private. Make the repository public (or
repoint the CTAs at a public docs surface) before promoting the landing page.
