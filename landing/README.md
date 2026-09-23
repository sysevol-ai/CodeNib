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
- `/blogs/jev-model-grep-reranking/` compares Jev and Qwen rerankers on
  BM25 and model-planned grep candidates;
- `/blogs/local-code-intelligence-dgx-spark/` is the reproducible local
  deployment reference;
- each article lives at `landing/blogs/<slug>/index.html`.

Nested pages should reference shared assets from `/assets/...` so the same URL
works at every route depth. The local Caddy configuration falls back to the
homepage for unknown paths, so a `200` response alone does not prove that a
new route or asset exists. Check the page title and asset content type as part
of deployment verification:

```bash
curl -s https://codenib.ai/blogs/ | grep '<title>CodeNib Blog'
curl -sI https://codenib.ai/assets/blogs/fact-query-index-fact-batch.png \
  | grep -i 'content-type: image/png'
```

## Publishing an article

Public blog posts, summaries, figure labels, and accessibility metadata are
written in English, matching the rest of the site.

Markdown under `codenib/blogs/` is a repository research record. It is not
automatically rendered or published to `codenib.ai`; the MkDocs workflow
publishes the separate `docs.codenib.ai` site.

To publish a blog post, add `landing/blogs/<slug>/index.html`, add its card to
`landing/blogs/index.html` in newest-first order, refresh the homepage's
featured article, and copy its public images into `landing/assets/blogs/`.
Follow the existing article template for the canonical URL, language,
publication date, social metadata, and JSON-LD.
Use public documentation URLs and pinned source links; do not link directly
to documents excluded from the public documentation site.

Serve `landing/` locally and check the index link, article title, images,
internal anchors, tables, and desktop/mobile layout. Run `mkdocs build --strict`
and `python scripts/check_public_docs.py` for the documentation boundary.
After deployment, verify the article's actual title and asset content types
on `codenib.ai`; a merged Markdown file or a successful MkDocs deployment
does not confirm blog publication.
