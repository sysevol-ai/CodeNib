# Cross-task synthesis: 60 solo trials

## Result

Across five tasks, 27 of 60 solo trials passed.

| Model | Passes | igel | Textual | IPython | FastAPI | sqlite-utils |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Luna | 5/20 | 0/4 | 1/4 | 1/4 | 0/4 | 3/4 |
| Terra | 5/20 | 1/4 | 0/4 | 3/4 | 0/4 | 1/4 |
| Sol | 17/20 | 2/4 | 3/4 | 4/4 | 4/4 | 4/4 |
| **All** | **27/60** | **3/12** | **4/12** | **8/12** | **4/12** | **8/12** |

Sol's advantage is large, but its three failures are important: one optional
value, one parser sentinel, and one integration-override provenance rule were
still missed despite extensive tests. Better generation alone does not remove
the need for adversarial review.

## What failures have in common

The failures are not primarily localization failures. Agents generally edited
the correct files and implemented the central happy path. They failed when a
belief about that path was generalized beyond its evidence:

- “the helper works” became “all public operations preserve its error”;
- “these sequences parse” became “the public event semantics are correct”;
- “the common route works” became “the option propagates through the whole
  framework”;
- “SAVEPOINT was issued” became “existing helpers cannot commit it away”;
- “the serializer works” became “the host lifecycle captures every channel.”

This supports the proposed Explain → Challenge → Investigate direction. The
valuable context is not a list of suspicious files. It is a compact model of
the feature plus the important beliefs that still lack independent evidence.

## Repeated capability gaps

### 1. Failure to construct the right matrix

Four kinds of matrix repeatedly mattered:

- lifecycle/operation: fit, evaluate, predict; record and replay; create,
  rollback, and commit;
- input grammar/options: separators, optional values, sentinels, compositions;
- public surface: constructors, helpers, decorators, CLI commands;
- integration mode: nesting, reuse, overrides, legacy paths, real host
  execution.

Agents often wrote several tests along one row while leaving another row
untested. Test count therefore has weak causal meaning without test
independence.

### 2. Implementation-derived tests

Several failed suites encoded the implementation's assumptions:

- igel tests accepted invented option restrictions or path provenance;
- Textual tests sampled only grammar forms already handled;
- FastAPI tests covered simple GET routes but not shared-router ownership;
- sqlite-utils tests exercised direct methods but not committing helpers or
  reopened CLI state.

A useful reviewer must derive expected behavior from public contracts,
repository conventions, sibling APIs, and callers—not merely mirror changed
branches.

### 3. Missing boundary semantics

Errors were swallowed at igel's public operations; IPython cell failure was
confused with a Python exception; FastAPI dispatch was bypassed; sqlite CLI
status diverged from library success. A helper-level assertion cannot validate
a boundary contract.

### 4. No high-fan-out invariant check

FastAPI made this most visible: targeted feature checks passed while OpenAPI or
test collection failed broadly. Similar invariants elsewhere were artifact
path overrides, event fallback cardinality, and transaction commit ownership.
The agent needs to ask which unchanged global property the new mechanism could
silently perturb.

### 5. Premature stopping after green local evidence

Many traces stopped after compilation, a narrow suite, or hand-picked probes.
The missing action was usually one final challenge:

> What important claim am I making that my executed evidence does not actually
> distinguish from a plausible broken implementation?

## Proposed hardcoded-context experiment

Use `universal_generic_prompt.md` unchanged across all five tasks. It contains
no task names, oracle cases, or expected patches. This measures whether a
generic autonomous-review procedure improves solo performance.

For diagnosis only, each task also has an `oracle_ceiling_prompt.md`. Those
prompts deliberately reveal post-hoc failure information and must be labeled
contaminated. They answer whether targeted context can rescue the run, not
whether Guardian can discover that context.

Recommended comparison:

1. solo baseline;
2. solo + universal generic prompt;
3. solo + per-task oracle ceiling prompt;
4. later, solo + Guardian-generated custom message.

Analyze changes trial-by-trial, not only by average score. In particular:

- If the generic prompt closes much of the gap, the prototype should emphasize
  review policy and stopping criteria.
- If only oracle prompts help, task-specific repository understanding is the
  bottleneck.
- If even oracle prompts do not help, the bottleneck is execution/implementation
  ability rather than missing context.

## Implication for the Guardian prototype

The evidence favors a small interface rather than a heavy new object model:

1. accept a custom message from the solver containing its current
   understanding, uncertainties, and claimed verification;
2. re-ground that message against the current repository state;
3. return a short set of unsupported high-impact beliefs and the best
   discriminating investigations;
4. after a solver commit, rebuild the understanding rather than only resolving
   old findings;
5. stop when no high-impact belief remains weakly supported, not merely when
   the findings list is empty.

The task-specific reports provide examples of the message Guardian should learn
to produce. The generic prompt is the initial hardcoded approximation.

