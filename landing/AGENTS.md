# Homepage loading

- Keep content-addressed bundles under `assets/hero-map/` byte-stable. Whitespace
  inside JavaScript template strings can be significant; change frontend source
  and rebuild the landing bundle instead of formatting generated files.
- Reproduce the reported URL during opening and refresh. `codenib.ai` is the
  static marketing homepage; `demo.codenib.ai` is the Wiki application.
- Install the shared example's styles together with its shadow markup. An
  external shadow stylesheet can paint raw code, concatenated line numbers and
  every source note before its response arrives.
- Verify first painted frames with delayed and failed style dependencies on
  desktop and mobile. A screenshot after `networkidle` misses this regression.
  Keep the readable Wiki fallback available when the component cannot load.
