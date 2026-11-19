# ContextBench results

CodeNib's ContextBench agent searches with two Claude Haiku 5.5 workers: a first
search on the issue, then a scout told to look for what the first one missed. It
keeps the union of what both read. Claude Opus 5.5 then repairs. This page reports
how much annotated code the search finds (line Recall), what it costs, and how
many issues the complete agent resolves. [ContextBench setup and scoring](contextbench.md)
explains the benchmark and metrics.

## Results at a glance

Line Recall of the search stage on three populations, compared with a single
Opus search on the same tasks:

| Tasks | Used in development? | Haiku pair | Opus | Pair − Opus, 95% CI | Search usage, pair vs Opus |
| --- | --- | ---: | ---: | ---: | ---: |
| [500 submitted](#submitted-system-500-tasks) | About 105 of them | 77.07% | 72.83% | +4.24 [+1.59, +6.41] | $5.68 vs $91.16 |
| [32 unseen](#independent32-complementary-source-search) | No | 40.00% | 37.91% | +2.09 [−1.91, +6.48] | $0.50 vs $6.43 |
| [559 unseen, all remaining](#held-out559-every-remaining-task-search-only) | No | 56.59% | 55.10% | +1.49 [−0.16, +3.34] | $6.93 vs $103.08 |

- On unseen tasks the pair matches Opus Recall at under 8% of its search cost.
  Only on the 500 tasks used in development is the gap clearly above zero.
- The pair's Precision and F1 are lower than Opus in all three, because the
  union of two searches reads more code.
- Absolute scores differ by population. The 500 tasks are ContextBench's curated
  verified split; the others come from the harder, uncurated full release.

## Submitted system, 500 tasks

The complete agent ranks #1 in line Recall (77.90%) and Pass@1 (77.60%) among
Agent systems on the [official leaderboard](https://contextbench.github.io/) as of
October 23, 2026 (UTC).

| Stage, same 500 tasks | Line Recall | Line Precision | Line F1 |
| --- | ---: | ---: | ---: |
| Search pair, before repair | 77.07% | 20.52% | 28.16% |
| Final selected context, after repair | 77.90% | 20.39% | 28.11% |

Repair resolved **388 of 500** tasks under native grading, with all failures
counted. Recorded model usage was $338.88: $5.68 for search and $333.20 (98%)
for repair.

This is not a held-out result. About 105 of the 500 tasks were the development
set used to choose the configuration, and earlier validation sets were drawn from
them. Models never saw annotations; those were loaded only after outputs were
sealed.

## 559 unseen tasks {#held-out559-every-remaining-task-search-only}

After the configuration was frozen, its search stage ran once on all 559 remaining
default ContextBench tasks that had never been used, with no sampling. Code,
prompts, models, budgets and evaluator were unchanged. Annotations were opened
only after every output was sealed.

| Search, all 559 tasks | Line Recall | Line Precision | Line F1 | Search usage |
| --- | ---: | ---: | ---: | ---: |
| One Haiku search | 51.54% | 34.26% | 34.41% | $3.49 |
| Haiku pair | 56.59% | 28.78% | 32.18% | $6.93 |
| Opus search | 55.10% | 32.58% | 34.51% | $103.08 |

The pair is 1.49 points above Opus [−0.16, +3.34] and 5.06 points above one Haiku
[+4.04, +6.22]. Line F1 is 2.32 points below Opus [−4.63, −0.38]. The scout adds
about 182 lines per task, and 13% of them are annotated, against 34% for the
first search. That explains the lower Precision.

Four tasks could not run and count as zero for every configuration: one has no
issue text in the dataset, and three have submodules that can no longer be
fetched. The [per-task rows](../assets/contextbench_search_pair_heldout_559.csv)
and [summary](../assets/contextbench_search_pair_heldout_summary.json) include the
per-language results.

## 32 unseen tasks {#independent32-complementary-source-search}

The first unseen check: 32 tasks from outside the 500, four per language, selected
after the configuration was frozen.

| Search, 32 tasks | Line Recall | Line Precision | Line F1 | Usage |
| --- | ---: | ---: | ---: | ---: |
| One Haiku search | 33.98% | 27.44% | 26.53% | $0.25 |
| Two independent Haiku searches | 38.82% | 26.19% | 27.82% | $0.48 |
| Haiku pair (first search + scout) | 40.00% | 24.14% | 27.18% | $0.50 |
| Opus search | 37.91% | 33.08% | 31.15% | $6.43 |

The pair beats one Haiku search by 6.02 points [+3.08, +9.56], so the second
search helps. Its gains over two plain searches (+1.17 [−2.41, +5.12]) and over
Opus are not significant. See the
[per-task rows](../assets/contextbench_search_pair_independent_32.csv) and
[summary](../assets/contextbench_search_pair_independent_summary.json).

## 32 tasks inside the 500 not used in development {#independent-32-task-validation}

Four per language; none were used to develop or choose the configuration. In the 500-task
run the pair scored 69.54% line Recall on them and Opus 69.11%. Opus drops on
unseen full-release tasks as much as the pair does, so the lower unseen scores
mostly reflect a harder population, not tuning.

The same set was first used to test function completion and a graph tool on Opus.
Together they changed Recall by −0.88 points [−5.73, +3.59] and lowered F1;
neither is part of the submitted system ([rows](../assets/contextbench_independent_32.csv)).

## Earlier study: function completion, 24 tasks {#historical-post-run-24-task-results}

An earlier development study used 24 tasks, three per language, and an Opus agent
with CodeNib source tools. Reading the whole function around each observed line
raised Recall at a 16K context budget:

| Final context, 24 development tasks | Line Recall | Line Precision | Line F1 |
| --- | ---: | ---: | ---: |
| Opus agent, 16K delivery | 56.68% | 27.12% | 30.81% |
| Same search + function completion, 16K | 64.97% | 23.12% | 29.06% |
| Published JiuwenSwarm, same task IDs | 64.46% | 22.13% | 27.45% |

These tasks were used for development, and the interval against JiuwenSwarm
spans zero ([rows](../assets/contextbench_development_24.csv),
[summary](../assets/contextbench_development_summary.json)).

## Evidence

- [Public submission](https://github.com/ContextBench/contextbench.github.io/tree/5fddce90e136f73a36ad0e16a5e15205cc18a375/submissions/2026-10-19_codenib-agent-haiku55-opus55):
  configuration, task list, raw trajectories, usage receipts and reproduction
  guide for the 500-task result.
- 500-task search comparison: [per-task rows](../assets/contextbench_search_pair_verified_500.csv),
  [summary](../assets/contextbench_search_pair_verified_summary.json); final
  context: [summary](../assets/contextbench_final_context_500_summary.json).
- [Full earlier version of this page](https://github.com/sysevol-ai/CodeNib/blob/65a6058c3d0cfe144c95c5da07c2592eddf4af54/docs/evaluation/contextbench-results.md):
  every cohort, gate and negative result in full, including routes that were
  tried and rejected.

??? note "Scoring and provenance"

    Scores are task-macro averages of the pinned official evaluator
    (`1436c28a`) on dataset revision `c2855792`; failed tasks count as zero.
    Intervals are paired percentile bootstraps over repository-alias clusters,
    10,000 resamples, unadjusted. Usage is SDK-reported model usage, not an
    invoice; it excludes compute and native-test containers. On the 500 tasks the
    Opus figure is a frozen native control with one unknown fee, and its cost is
    nominal subscription value. The search runtime is `71e489e5` for all three
    populations.
