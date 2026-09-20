<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Related diagrams acceptance record

Validation date: 2026-09-20. Local branch: `vlm-wiki-facts-slice`, based on
`48c9da55` before the follow-up commits. This record covers local acceptance;
it does not claim remote CI, a GitHub deployment, or VLM accuracy certification.

## Results

| Check | Result |
| --- | --- |
| Focused backend suites listed below | 420 passed |
| `npm test` | 63 passed |
| `npm run build` | Passed; existing bundle-size warning remains |
| `CODENIB_TEST_BROWSER=chrome npm run test:visual-evidence` | Six scenarios passed |
| Screenshot / recording / personal browser profile | Not used |
| Actual local Wiki API smoke | Overview, navigation, Architecture, source, SVG and evidence returned HTTP 200 |
| Document enrichment smoke | Document context returned HTTP 200; the live payload selected both the FactBatch diagram and the multimodal Wiki preview |

Browser acceptance runs the real frontend against controlled API fixtures in
an isolated headless browser. Desktop and 390px mobile checks exercise keyboard
expansion, image loading, original-image navigation, code and document dialogs,
and page overflow. Desktop navigation additionally checks that changing the
section resets the disclosure, changes the recommendation, and hides the
section on an unrelated page. Four other scenarios cover missing, stale, empty
and failed optional evidence while the Wiki prose remains readable.

The live API smoke and the fixture browser suite are separate evidence. No new
paid VLM extraction was performed for this acceptance run, and image-summary
correctness was not manually certified.

```bash
python -m pytest \
  test/wiki/test_builder.py test/wiki/test_media_context.py \
  test/wiki/test_media_grounding.py test/wiki/test_media_vlm.py \
  test/web/test_app_runtime.py test/web/test_repo_registry.py \
  test/scripts/test_build_multimodal_knowledge.py test/test_cli.py \
  -m 'not slow and not integration and not integration_serial and not integration_serial_consumer' \
  -q --tb=short
```

## Whole-repository check limitation

`pre-commit run --all-files` was run. It found pre-existing Black/isort changes
in `codenib/wiki/media_facts.py`, `codenib/clients/guardian/artifacts.py`,
`scripts/experimental/hybrid_index/catalog.py`,
`test/scripts/hybrid_index/test_catalog.py`, and `test/test_bounded_json.py`.
It also reported B950 in `codenib/wiki/media_facts.py` and B907 in
`test/scripts/test_check_public_docs.py` and `test/wiki/test_media_storage.py`.
These files were unchanged by the intended patch before the check; unrelated
automatic formatting was restored. The full repository gate is therefore
not green. Changed-file hooks are the scoped commit gate, and this baseline
debt must remain visible to reviewers.

See [Related diagrams](related_diagrams.md) for supported behavior, reproduction
steps, snapshot consistency, display bounds, and static-export limitations.
