<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Related diagrams in the local Wiki

Related diagrams connects repository images to the Wiki page being read. It is
an optional enhancement to the live Wiki: ordinary page content, navigation,
and source citations remain usable when no visual evidence is available.

## Reader behavior

The section is collapsed initially and shows at most two relevant images.
Expand it to see each original image, a model-produced summary, the reason it
was selected, and links to related sources. **Read in context** opens the
document excerpt that references the image, when available.

Selection considers component names, document captions and section context,
explicit source paths recognized by the indexed source reader, and shared
source modules. It excludes common branding filenames and deduplicates image
formats with the same path stem. Generic words alone do not establish relevance.
Changing Wiki sections recomputes the selection and closes the disclosure.
Pages without relevant diagrams show no section.

These are heuristic recommendations, not a proof that a visual claim is
implemented in code. Source buttons use the current Wiki page's citations;
visual extraction confidence alone does not authorize a code link.

## Generate visual evidence

Use a repository containing local images and Markdown references. Configure an
OpenAI-compatible vision endpoint through environment variables; keep secrets
out of command arguments and committed files:

```bash
export VLM_BASE_URL=https://your-provider.example/v1
export VLM_MODEL=your-vision-model
export VLM_API_KEY=your-key

codenib wiki /path/to/repository --preset fast \
  --visual-facts-model "$VLM_MODEL" \
  --visual-facts-api-base "$VLM_BASE_URL" \
  --visual-facts-api-key-env VLM_API_KEY \
  --visual-facts-max-artifacts 16 \
  --no-open
```

This makes provider requests and publishes a validated bundle to
`<repository>/.codenib/multimodal-knowledge.json`. The artifact limit bounds how
many images the provider inspects (default: 16). Before each request, the CLI
prints the current image number, total, and path. Retries can make more than
one request per image. If extraction fails, no replacement bundle is published;
the command exits with a recovery hint. To open the Wiki without generation,
rerun the original command after removing all `--visual-facts-*` options and
their values. Wiki prose generation is configured
separately with `--generate` and the Wiki model options.

For an offline pipeline smoke check from a source checkout:

```bash
python scripts/build_multimodal_knowledge.py /path/to/repository \
  --output /tmp/visual-knowledge.json \
  --exclude-root .git --exclude-root .codenib \
  --max-artifacts 16 --publish-to-wiki
codenib wiki /path/to/repository --preset fast --no-open
```

Without a VLM, extraction uses deterministic repository metadata. This checks
the pipeline but does not establish image-understanding quality, and may not
produce any recommendations for a given page.

## Snapshot consistency

Visual evidence and the Wiki index must refer to the same commit. Stale
evidence is withheld, and its media endpoint rejects stale requests. The
media response also verifies the image hash against its fact pack. Image URLs
carry that hash, and responses use `no-store` so reindexing at the same commit
cannot reuse an immutable cached image. A rejected or failed image hides its
summary and original-image links while retaining source navigation. The
source reader also checks the captured checkout, so edits made after indexing
can invalidate source reads even if the Git commit has not changed.

After source changes, rebuild the index, regenerate evidence as needed, and
restart the Wiki. For development, use a separate checkout as the indexed
preview target so frontend builds, tests, and edits do not mutate that target.
Do not edit the manifest or its fingerprints to bypass consistency checks.

## Scope and limits

- Supported surface: the dynamic local Wiki. Static exports do not yet include
  this section.
- The API exposes at most 64 fact packs; the reader sees at most two selected
  diagrams per page. Repository totals can exceed these display limits.
- Document enrichment inspects at most eight distinct documents and four
  references per visual. Documents exceeding 128 KiB are skipped; excerpts
  and headings are bounded.
- Context currently comes from recorded local Markdown/MDX references. Remote
  images, missing references, and HTML-only document layouts can reduce recall.
- Entity names and document terms use lexical matching. There is no semantic
  embedding ranker or independently verified visual-claim evaluator here.
- Missing, stale, empty, or failed optional evidence does not replace the Wiki
  page with an error panel.

## Verification

```bash
python -m pytest test/wiki/test_media_context.py \
  test/wiki/test_media_grounding.py test/wiki/test_builder.py \
  test/web/test_app_runtime.py test/web/test_repo_registry.py -q
cd web
npm test
npm run build
```

Run browser acceptance independently of the live backend:

```bash
cd web
npx playwright install chromium
npm run test:visual-evidence
```

Alternatively, use an already-installed Chrome with
`CODENIB_TEST_BROWSER=chrome npm run test:visual-evidence`.
The script starts a temporary Vite server and an isolated headless browser
with fixture API responses. It checks keyboard expansion, original images,
source and document navigation, section changes, mobile overflow, and absent,
stale, empty or failed optional evidence, plus failed image loading. It creates no screenshots or
recordings and does not use the user's browser profile or call a model.
This verifies frontend interactions; backend route tests and a real local
repository smoke check remain separate evidence.
