# Repository Guardian

You have access to a `query_guardian` tool. The Repository Guardian is a
separate analysis agent running in the background. It monitors the repository
for risky changes — high-churn code with low test coverage, drift between
implementations and their contracts, and functions that break when their
callers are refactored.

Guardian runs a fresh analysis cycle on every commit you make. Its findings
reflect the state of the repo as of the most recent commit.

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

- `cycle_running: true` means a cycle is in progress for your latest commit.
  The findings returned are from the previous commit. You can proceed — the
  findings will be updated shortly.
- `total_findings` is how many Guardian found overall; `returned_findings` is
  how many match your query. If they differ, Guardian filtered to what is most
  relevant to your hypothesis.
- `verdict: "confirmed"` means Guardian's LLM investigation corroborated the
  risk with code evidence. Treat these as high-priority.
- `verdict: "rejected"` means Guardian looked and found the risk is contained.
  You can proceed with less caution.
- `verdict: "inconclusive"` means the evidence is mixed. Use your own
  judgment, and consider adding a test that would make the risk observable.

## What Guardian is not

Guardian is advisory. Its findings are context, not instructions. It can be
wrong — especially on code that has been heavily refactored since Guardian
last ran a full analysis. If its findings contradict what you see in the code,
trust what you see. You do not need to call it on every commit; call it when
you have a reason to.
