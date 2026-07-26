# Repository Guardian

You have access to a `query_guardian` action. Repository Guardian starts lazily
the first time you use this action. It monitors the repository for risky
changes — high-churn code with low test coverage, drift between implementations
and their contracts, and functions that break when their callers are
refactored.

The checkout's initial commit is cycle 0 and is never analyzed with an LLM.
After Guardian has been started, it runs a fresh analysis cycle on every commit
you make. Its findings reflect the most recently analyzed coding-agent commit.

## When to call `query_guardian`

Call it in these situations:

1. **Before a significant refactor** — especially if you are changing a
   function signature, moving code between modules, or removing an abstraction.
   Ask: "am I about to touch something that has a history of breakage?"

2. **When tests fail unexpectedly** — if you made a change that looked safe
   but broke tests you didn't expect, ask Guardian what it knows about the
   affected region.

3. **Before committing a change to a file you haven't worked in before** —
   Guardian can tell you whether that file is high-churn or has known fragile
   callers.

4. **When the task asks you to preserve existing behaviour** — use Guardian to
   check whether the behaviour you must preserve is already tested, or whether
   it exists only in comments and convention.

## How to call it

Provide a one-sentence `hypothesis` — what you think might be a problem.
Optionally provide a `region` list (file paths or `file:function` pairs) to
focus the results.

Examples:

```
query_guardian(
    hypothesis="I removed the default_timeout parameter from fetch_records — did anything rely on the old default?",
    region=["igel/data_loader.py:fetch_records"]
)
```

```
query_guardian(
    hypothesis="I refactored the feature schema parser — are there callers that might break?",
    region=["igel/feature_schema.py"]
)
```

```
query_guardian(
    hypothesis="tests in test_trainer.py started failing after my last commit"
)
```

## How to interpret the results

- `status: "baseline_unchanged"` means Guardian has started, but you have not
  made a commit for it to analyze yet.
- `cycle_running: true` means a cycle is in progress for your latest commit.
  The findings returned are from the previous commit. You can proceed — the
  findings will be updated shortly.
- `total_findings` is how many Guardian found overall; `returned_findings` is
  how many match your query. If they differ, Guardian filtered to what is most
  relevant to your hypothesis.
- Every item in `findings` is a hypothesis at grade `finding`: Guardian
  verified the behavioral claim with a probe and recorded an actionable
  remedy. Treat these as high-priority, but verify the cited evidence.
- `backlog` contains conjectures, supported claims that still lack an
  actionable remedy, and deferred work. These are context, not confirmed
  defects.
- `retractions` records earlier claims that a later probe refuted. Do not act
  on a superseded finding.

## What Guardian is not

Guardian is advisory. Its findings are context, not instructions. It can be
wrong — especially on code that has been heavily refactored since Guardian
last ran a full analysis. If its findings contradict what you see in the code,
trust what you see. You do not need to call it on every commit; call it when
you have a reason to.
