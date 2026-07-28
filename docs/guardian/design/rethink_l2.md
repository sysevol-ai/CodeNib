# Rethinking L2: From Risk Localization to Autonomous Review

## Motivation

Our recent Guardian experiments suggest that the primary limitation is **not** L3 investigation or code localization. Instead, the bottleneck lies in **how L2 explores and reasons about a change**.

Current L2 is effective at discovering localized implementation issues. It can identify suspicious code regions, missing integrations, or inconsistent behavior near modified files. However, it struggles with larger questions such as:

* Is the feature actually complete?
* Does the implementation satisfy the original task?
* Which assumptions have never been validated?
* What important scenarios have not yet been challenged?

These failures are fundamentally about **exploration policy**, not retrieval or localization.

---

# Failure Analysis

## 1. Premature convergence

After one finding is resolved, L2 tends to conclude that the review is complete.

Observed behavior:

```
Find issue A
    ↓
Solver fixes A
    ↓
Confirm fix
    ↓
Stop
```

A human reviewer typically behaves differently:

```
Find issue A
    ↓
Solver fixes A
    ↓
Good.
What else could still be wrong?
```

The issue is not insufficient context—it is **premature termination of exploration**.

---

## 2. No re-grounding after repository evolution

Guardian currently carries previous findings forward across cycles, but it rarely reconstructs its understanding of the repository after new commits.

Instead of:

```
Old understanding
        ↓
Carry forward
        ↓
Continue investigating
```

we want:

```
New commit
        ↓
Re-understand the feature
        ↓
Update repository understanding
        ↓
Continue review
```

Each solver commit changes the repository state. The review process should therefore restart from the updated implementation rather than simply confirming previous findings.

---

## 3. Implementation-biased reasoning

One particularly interesting failure was that synthesized tests mirrored the implementation's assumptions.

For example, Guardian generated tests that exercised the parser exactly as implemented, but never challenged whether the implementation matched the task specification (e.g., shifted-key aliases).

Current behavior:

```
Implementation
        ↓
Generate tests
        ↓
Verify implementation
```

Desired behavior:

```
Task
        ↓
Expected behavior
        ↓
Challenge implementation
```

The objective of test synthesis should be to falsify the current implementation, not reinforce it.

---

## 4. Memory stores findings instead of understanding

Current memory is largely organized around findings:

```
Finding A
Finding B
Finding C
```

This helps avoid duplicate reports but does not improve future reasoning.

Instead, memory should capture Guardian's evolving understanding of the repository.

For example:

```
Current understanding:

✓ parser implemented
✓ protocol bindings updated
? shifted aliases
? metadata emission
? end-to-end shortcut behavior
```

Future commits update this understanding rather than simply resolving individual findings.

Memory becomes a persistent model of repository behavior instead of a collection of historical bug reports.

---

# A More Agentic L2

Rather than introducing additional structured objects or rule-based workflows, we propose making L2 itself more autonomous.

Instead of asking:

> Find implementation risks.

L2 continuously asks three questions.

### Q1. What do I currently believe this change accomplishes?

L2 constructs the strongest coherent explanation of the feature using the task description, code changes, documentation, tests, and repository context.

---

### Q2. What evidence supports each belief?

For every major behavioral claim, L2 gathers supporting evidence.

Examples include:

* implementation paths
* callers
* tests
* runtime behavior
* documentation
* historical commits

The goal is not to prove correctness, but to understand why the current explanation appears plausible.

---

### Q3. Which important beliefs still lack convincing evidence?

This is the critical step.

Instead of searching directly for bugs, L2 identifies **unsupported assumptions** in its own explanation.

Example:

```
Belief

The parser now supports shifted aliases.

Evidence

✓ parser handles '+' token
✓ unit tests pass

Missing evidence

• no Ctrl+Shift+= execution path
• no end-to-end shortcut example
• no runtime validation
```

Rather than immediately reporting a bug, L2 asks:

> What investigation would most effectively reduce this uncertainty?

This naturally produces investigation objectives without relying on manually designed risk taxonomies.

---

# Explain → Challenge → Investigate

Conceptually, the L2 loop becomes:

```
Observe
    ↓
Explain
    ↓
Challenge
    ↓
Investigate
    ↓
Update understanding
```

The key difference is that "Challenge" is directed toward Guardian's own understanding, not merely the implementation.

This resembles how experienced human reviewers work:

1. Build an understanding of the change.
2. Look for weak assumptions.
3. Search for contradictory evidence.
4. Revise the understanding.
5. Repeat until no high-value uncertainty remains.

---

# A Better Stopping Criterion

Current stopping criterion:

```
All findings resolved
        ↓
Stop
```

Proposed stopping criterion:

```
No high-impact belief remains weakly supported
        ↓
Stop
```

This avoids premature convergence while naturally encouraging broader review after each solver update.

---

# Implications for Test Synthesis

Under this view, tests are not generated to increase coverage.

They are generated to challenge specific unsupported beliefs.

Instead of:

```
Implementation
        ↓
Generate tests
```

we move toward:

```
Belief
        ↓
Missing evidence
        ↓
Targeted adversarial experiment
```

Test synthesis becomes one possible investigation strategy rather than the central objective.

---

# Research Hypothesis

Rather than framing Guardian as a better bug detector, we believe the more interesting research question is:

> **How can an autonomous reviewer continuously construct, challenge, and update its understanding of repository behavior without prematurely converging on its initial interpretation?**

This shifts the focus from localization to **autonomous repository understanding**.

The central hypothesis is that persistent memory should preserve **understanding**, not merely findings. L2 should continuously revise that understanding as the repository evolves, actively searching for unsupported assumptions instead of simply confirming previously identified issues.

If successful, this approach should enable Guardian to discover not only localized implementation defects, but also incomplete features, missing integration paths, and higher-level behavioral gaps that are difficult to detect through traditional risk localization alone.
