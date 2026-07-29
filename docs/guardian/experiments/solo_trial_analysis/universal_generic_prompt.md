# Universal generic review prompt

Before declaring the change complete, pause and review your current
understanding rather than only the code you just wrote.

1. State the major externally observable claims the change is supposed to make.
2. For each claim, identify the evidence you actually executed through the
   public boundary—not merely helper behavior or code inspection.
3. Construct the smallest relevant matrices across:
   - lifecycle or operation modes;
   - option compositions, optional values, and boundary/sentinel inputs;
   - sibling public APIs and command surfaces;
   - nesting, reuse, overrides, legacy paths, and host-framework integration.
4. Identify existing ownership and control boundaries that the change crosses:
   error propagation, dependency injection, transaction commits, callbacks,
   generated schema, shared mutable objects, and caller-provided overrides.
5. Search for one plausible broken implementation that would still pass your
   current tests. Add the most discriminating test or runtime probe.
6. For a high-fan-out framework change, run the broad regression tier and
   inspect generated artifacts or collection failures, not only targeted tests.

Do not equate the number of tests with independence of evidence. Stop only when
no high-impact behavioral claim remains weakly supported.
