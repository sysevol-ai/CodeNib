<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Issue-localization cost A/B, protocol v6

Status: sealed on 2026-11-16 before any study session. Results are in
[results v6](issue_localization_cost_results_v6.md).

Rounds v1 to v5 asked whether CodeNib makes an agent's answers better, and found
no demonstrated benefit. They recorded cost only on the side, and they charged
CodeNib for building its graph inside every task. Round v6 asks the cost
question directly, in the setting an issue bot would run in. When the graph is
already built, does CodeNib let the same agent locate the code behind a real
issue with fewer model tokens and fewer steps, without locating it less often?

## Design

- **Task.** The agent reads one real GitHub issue and names the source files and
  functions that must change. It does not write the fix. Its tools are read
  only: `Read`, `Grep` and `Glob`, limited to the checkout.
- **Arms.**
  - *native*: the read-only tools alone.
  - *codenib*: the same tools plus CodeNib's `explore_context`, with no
    instruction to use it.
  - *codenib-directed*: as *codenib*, and the system prompt tells the agent to
    make `explore_context` its first tool call and then confirm in source only
    what it still needs. This is the arm an issue bot would ship, because the
    bot's author writes its prompt. It is the primary comparison.
- **Cases.** Twelve Python repositories, one issue each. Cases are chosen by a
  script, with no model and no reading of the fix. For each repository on a
  fixed candidate list, in list order, the script takes the most recently
  merged pull request that meets every rule:
  - merged on or after 2026-09-01, and it closes exactly one issue;
  - the issue was opened by a person on or after 2026-08-01, and its body has
    at least 300 characters;
  - the pull request modifies one to three existing non-test Python source
    files, with at most 100 changed lines in them, and adds, removes or renames
    none;
  - it changes no non-test source in another language;
  - the issue text names none of the changed source files.

  The first twelve repositories that yield a case and pass admission are the
  study. Dates after mid-2026 keep the fixes out of what the model could have
  seen in training.

  | Repository | Issue | Fix | Python files | Python lines | Gold files | Gold functions | Index build |
  |---|---|---|---|---|---|---|---|
  | PrefectHQ/prefect | #23878 | #23895 | 1,955 | 728k | 3 | 2 | 72 s |
  | mlflow/mlflow | #26941 | #27085 | 2,930 | 955k | 2 | 2 | 180 s |
  | pydata/xarray | #11560 | #11562 | 245 | 202k | 1 | 4 | 21 s |
  | conda/conda | #16753 | #16852 | 459 | 137k | 2 | 2 | 17 s |
  | zulip/zulip | #40239 | #40241 | 2,034 | 424k | 1 | 1 | 47 s |
  | kedro-org/kedro | #5825 | #5828 | 236 | 57k | 1 | 2 | 7 s |
  | python-poetry/poetry | #11140 | #11141 | 451 | 82k | 1 | 1 | 8 s |
  | pypa/pip | #14232 | #14233 | 700 | 209k | 1 | 1 | 21 s |
  | mitmproxy/mitmproxy | #8513 | #8516 | 583 | 90k | 1 | 1 | 14 s |
  | streamlit/streamlit | #17957 | #17960 | 1,306 | 445k | 2 | 2 | 37 s |
  | celery/celery | #10760 | #10771 | 450 | 126k | 1 | 2 | 17 s |
  | nautobot/nautobot | #9735 | #9736 | 1,264 | 331k | 2 | 3 | 33 s |

  Candidates that yielded no case under the rules: dagster, ibis, saleor,
  textual, spack and wagtail. `apache/superset` is the one reserve.
  `pantsbuild/pants` was used to shake down the harness (three sessions) and is
  excluded.
- **Snapshot.** The agent sees the tree at the fix's parent commit as a
  one-commit repository with no history. Each arm family has its own copy.
- **Gold answer.** The gold files are the source files the fix modifies. The
  gold functions are the innermost functions or classes that enclose the
  changed lines in the parent commit.
- **Agent.** Claude Agent SDK 0.2.174 with Claude Code 2.1.326,
  `claude-sonnet-5-5` at medium effort, 30 turns, a 600 s limit per session, no
  user settings, skills or other MCP servers, and no forced tool use. Sonnet is
  the model an issue bot would run at volume. Earlier rounds used Opus.
- **CodeNib build.** Commit `4bc87ffd`, served with
  `codenib mcp <checkout> --tool-surface explore` in prebuilt mode. BM25 and the
  symbol graph are built once per repository with
  `codenib index --preset graph --language python` before any session. Build
  time is reported and is not part of a session.
- **Admission.** Model-free, per repository: both views are fresh, and one
  `explore_context` call on the issue title returns source with dependency
  expansion enabled and no graph-unavailable diagnostic. All fourteen prepared
  repositories passed.
- **Repetitions and order.** Each arm runs each case three times in fresh
  sessions: 108 sessions. A seeded shuffle orders the 36 case-repetition groups
  and the three arms inside each group. The three sessions of a group run back
  to back, so their wall times share one time window. Three groups run at once.
- **Retries.** A session that ends without a result, or with an execution
  error, is rerun once. Reaching the turn limit is a result and is not rerun.

## Measures

Wall time depends on API load, so it does not decide this round. The deciding
measures follow from what the agent did:

- **Model tokens**: all input tokens, cached or not, plus output tokens, summed
  over the session's model calls.
- **Turns**: the session's turn count as the SDK reports it.
- **Located**: every gold file is among the files the answer lists (at most
  five).

Per case and arm, the three repetitions are reduced to their median. Each
CodeNib arm is then compared with native tools as a per-case ratio.

## Decision rule

For each CodeNib arm against native tools, in this order:

1. **Cost benefit** if the median over cases of the token ratio is at most
   0.70, the ratio is below 1 in at least 9 of 12 cases, and the arm locates
   the fix in no fewer than native's count minus 2, out of 36 sessions.
2. Otherwise **fewer steps without a demonstrated token saving** if the same
   three conditions hold for turns with a median ratio of at most 0.80.
3. Otherwise **costs more** if the median token ratio is at least 1.30 and the
   ratio is below 1 in at most 3 cases.
4. Otherwise **no demonstrated cost benefit**.

The *codenib-directed* verdict is the headline. The *codenib* verdict is
reported next to it together with how often the agent chose the tool.

Secondary measures, reported without a threshold:

- output tokens, the SDK cost estimate and the largest context of any one model
  call;
- tool calls, `explore_context` calls and the first tool used;
- characters of tool results returned to the model;
- first-listed file correct, file recall and function recall;
- session wall time and API time, as medians and per-case ratios.

The planned SDK estimate is about $7. No new group starts once the observed
estimate reaches $25.

## Commitments

All hashes are SHA-256.

| Input | Hash |
|---|---|
| Policy (`policy.json`) | `8439e3db799344b1aec0e0db5332c3ee0340b28500008bde036039a4efd9f478` |
| Case selection (`select_cases.py`) | `f599a5a87067c0be7eff473465e04169e6352b0dc0e02c589d040eb9a7ab9205` |
| Selected pull requests (`candidates.json`) | `e4cca9a32f4328a95b36d304b561ea212bd08affec6be91876c9d0f17ad8b93d` |
| Snapshots and gold answers (`prepare.py`) | `2212a160f30c89c2e0e341814148b47582338c2306c166def031c731698ace22` |
| The twelve cases (their `case.json` files, in table order) | `51a845f8117da940bd7fc31d74186497475b87a73b47e0736f18b80c6847d67d` |
| Session runner and prompts (`run.py`) | `52212d7479a9cf5a811bf3d25a4e46ec2ef761e93ebb262ae6d0b3b48fa5bdf4` |
| Schedule (`execute.py`) | `a0db35dc818aa832bbf3302ac9bc5e7886730122a682245486e10c0fd3408590` |
| Scoring and decision rule (`score.py`) | `b71f490d74373053fcf45264f21b84e284f27f20355d10dc6c1650574e7b6b82` |
| Admission (`admit.py`, `admission.json`) | `843fba8bbf4a83948ed780ce65f03916f2b0336c1839fdd88e2ae1512ea6d3b2`, `c1a2d4e95b5de23ee637028a212b92f8a64d86bfe1a3ca67288aa59c5474c65a` |

The harness, the cases and every session trace are published with the results.

## Limits known before the run

- One issue per repository, and twelve repositories. The round can show a
  large, consistent difference and cannot resolve a small one.
- A fix is one valid place to change the code, not the only one. An answer that
  names another workable location counts as not located.
- Issues whose text names the changed file are excluded, so the cases are
  harder to locate than a random issue.
- All repositories are Python.
