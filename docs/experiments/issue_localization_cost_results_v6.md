<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Issue-localization cost A/B, results v6

Run on 2026-11-16 under the sealed
[protocol v6](issue_localization_cost_protocol_v6.md): 108 sessions, all with a
valid result, no retries, SDK estimate $4.75.

**Verdict: no demonstrated cost benefit, in either CodeNib arm.** With the graph
already built, telling the agent to start with `explore_context` cut turns in 7
of 12 cases but did not cut model tokens, and it located the fix as often as
native tools. Left to choose, the agent did not use the tool.

## Result

Twelve Python repositories, one real issue each, three repetitions per arm,
`claude-sonnet-5-5` with read-only tools.

| Arm | Located (of 36) | Token ratio to native, median over cases | Cases with fewer tokens | Turn ratio, median | Cases with fewer turns | Sessions that called `explore_context` |
|---|---|---|---|---|---|---|
| native | 24 | 1.00 | — | 1.00 | — | — |
| codenib | 22 | 1.10 | 4 of 12 | 1.00 | 4 of 12 | 1 |
| codenib-directed | 25 | 1.06 | 5 of 12 | 0.83 | 7 of 12 | 31 |

The rule needed a token ratio of at most 0.70 in at least 9 cases, or a turn
ratio of at most 0.80 in at least 9 cases. Neither arm met either.

## Why there was no saving

Most issues were already cheap to locate. In 7 of 12 cases the native agent
finished in two or three turns and 16,000 to 27,000 model tokens, about ten
seconds. One `explore_context` call replaces one or two searches there, and its
tool description and larger result cost about as much as they save.

| Case | native: tokens / turns / located | codenib-directed: tokens / turns / located |
|---|---|---|
| prefect | 60k / 6 / 0 | 61k / 5 / 2 |
| mlflow | 394k / 26 / 0 | 149k / 13 / 0 |
| xarray | 27k / 3 / 3 | 21k / 2 / 3 |
| conda | 26k / 5 / 3 | 50k / 6 / 2 |
| zulip | 22k / 3 / 3 | 19k / 2 / 3 |
| kedro | 19k / 2 / 3 | 21k / 2 / 3 |
| poetry | 27k / 3 / 3 | 22k / 2 / 3 |
| pip | 16k / 2 / 3 | 21k / 2 / 3 |
| mitmproxy | 113k / 11 / 3 | 132k / 9 / 3 |
| streamlit | 24k / 3 / 0 | 20k / 2 / 0 |
| celery | 16k / 2 / 3 | 19k / 2 / 3 |
| nautobot | 55k / 8 / 0 | 109k / 8 / 0 |

Values are medians of three repetitions; "located" counts repetitions.

The longer cases went both ways:

- **mlflow**: the directed arm used 38% of native's tokens and half its turns.
  No arm located the fix.
- **conda and nautobot**: the directed arm used about twice native's tokens.
- **prefect**: the directed arm located the fix in two of three repetitions;
  the other arms never did.

Summed over all 36 sessions the directed arm used 2.01 million tokens against
native's 2.30 million, 13% fewer. Almost all of that difference is the mlflow
case, so it is not evidence of a general saving.

## Secondary measures

| Measure (median per session) | native | codenib | codenib-directed |
|---|---|---|---|
| Model tokens | 26.1k | 29.7k | 21.6k |
| Turns | 3 | 3 | 2 |
| Tool calls | 2 | 2 | 1 |
| Output tokens | 868 | 996 | 890 |
| Largest context of one model call | 8.9k | 10.5k | 10.6k |
| Characters of tool results | 5.4k | 6.7k | 7.8k |
| SDK estimate | $0.026 | $0.025 | $0.031 |
| Session time | 9.7 s | 10.3 s | 9.2 s |
| First listed file correct (of 36) | 30 | 30 | 30 |
| File recall | 0.76 | 0.75 | 0.79 |
| Function recall | 0.52 | 0.51 | 0.56 |

- Session time differed by under a second at the median, inside the variation
  between repetitions. It decides nothing here, as the protocol said.
- Offering the tool without using it is not free. The *codenib* arm's largest
  context was 13% above native's at the median, from the tool description and
  server instructions.
- In the directed arm, 5 of 36 sessions skipped the tool despite the
  instruction: all three zulip sessions and two kedro sessions.

## What this does and does not show

- It answers the question asked: on issues like these, a prebuilt graph does
  not make localization cheaper for Sonnet. Building the graph was not the
  reason earlier rounds found no saving.
- It does not cover issues that take many steps to locate. Only three of
  twelve cases took native tools more than five turns, and they disagree with
  each other.
- A fix is one valid place to change the code. In four cases (prefect, mlflow,
  streamlit, nautobot) native tools never matched the fix's files, which may
  reflect other workable locations as much as wrong answers.

The harness, cases, schedule and every session trace are in
`issue_localization_cost_v6.tar.gz`.
