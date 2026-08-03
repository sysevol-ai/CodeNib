# Behavioral-obligation prompt investigation

## Conclusion

The first prompt set was delivered correctly, but it was not consistently good
enough. All 60 jobs completed, every metadata record contains the expected
task-specific prompt hash, and every recovered agent log is non-empty. The
performance change therefore cannot be explained by a missing or malformed
context injection.

The intervention supports a narrower hypothesis than “task-specific context
helps”:

> Explicit obligations help when they are correct, repository-grounded, and
> concrete about the affected execution paths. Abstract, ambiguous, or invented
> obligations can redirect the agent toward a coherent but wrong solution.

This explains both extremes in the matrix. IPython improved from 8/12 to 12/12
because its prompt named the actual host lifecycle and the exact channel and
control-flow distinctions. IGEL fell from 3/12 to 0/12 because its prompt added
an unsupported relocation requirement and ambiguously described ownership of
artifact paths.

## Aggregate result

| Task | Original reward | Obligation reward | Original F2P | Obligation F2P | Diagnosis |
| --- | ---: | ---: | ---: | ---: | --- |
| IGEL | 3/12 | 0/12 | 196/288 | 106/288 | Prompt-induced artifact-path model |
| Textual | 4/12 | 3/12 | 247/276 | 248/276 | Missing non-`u` Kitty grammar family |
| IPython | 8/12 | 12/12 | 197/204 | 204/204 | Concrete obligations matched host seams |
| FastAPI | 4/12 | 4/12 | 493/516 | 462/516 | Contract still underspecified; P2P improved |
| sqlite-utils | 8/12 | 6/12 | 702/720 | 687/720 | Upsert and invariant result semantics omitted |
| **Total** | **27/60** | **25/60** | **1835/2004** | **1707/2004** | Net negative despite a perfect IPython result |

The obligation runs preserved 51,105/51,120 P2P checks. FastAPI accounts for
10 of the 15 P2P failures and sqlite-utils for the other 5; IGEL, Textual, and
IPython preserved all P2P checks.

## Task diagnoses

### IGEL: the prompt actively supplied the wrong path model

All 12 trials failed, in two highly convergent clusters:

- Ten trials scored 6/24. Their fit path attempted to write
  `model_results/model.joblib` or `description.json` without creating the
  challenge fixture's configured result directory.
- Sol 1 and Sol 4 scored 23/24. They wrote
  `feature_schema_path: "feature_schema.joblib"`, but the consumer loads
  `Path(description["feature_schema_path"])` directly, so the value was not
  resolvable from the process working directory.

The traces repeatedly cite the injected requirements about “moving a result
bundle” and not replacing caller-owned paths. Those requirements encouraged
agents to normalize paths relative to a bundle or to rewrite the repository's
default-path initialization. The public task did not require movable bundles.

The repository's actual seam is more specific: tests and clients override
`Igel.results_path`, `Igel.default_model_path`, `Igel.description_file`, and
related configuration before construction. Fit must honor those values, create
the configured parent directory, and record a schema path that the later
consumer can load directly. The v2 prompt removes relocation entirely and
states this concrete convention.

### Textual: semantic coverage omitted one wire-format family

The obligation prompt held aggregate F2P roughly flat, but nine trials failed.
The dominant failures were:

- arrow release for `\x1b[1;1:3A`;
- modified functional repeat for `\x1b[1;3:2D`.

Most failing traces claimed that repeat/release handling had been verified.
Their probes, however, exercised CSI-`u` forms. The prompt described the
`modifier[:phase]` grammar but did not say explicitly that it applies to
functional final-byte sequences (`A`, `B`, `C`, `D`, `~`, and related
terminators) as well as `u`.

This is an applicability failure in the prompt itself. The v2 prompt names both
families and gives falsifiable arrow and modified-functional examples. It also
keeps the legacy ESC-prefixed cases separate so a broad parser rewrite does not
silently change their public identities.

### IPython: the prompt reached the right level of specificity

All 12 trials passed. Agent traces consistently used the stated obligations to
select:

- real `InteractiveShell` pre/post-cell lifecycle hooks;
- separate explicit stdout, stderr, display, expression-result, and failure
  channels;
- IPython's non-raising `ExecutionResult` failure behavior;
- real shell tokenization for quoted paths and repeated redactions;
- deferred finalization when `%session_bundle stop` runs inside a recorded
  cell.

These were not implementation recipes. They were specific behavioral
distinctions tied to real repository seams. The v2 version mostly preserves
this prompt, adding explicit end-to-end probes rather than changing its model.

### FastAPI: broad obligations allowed several incompatible contracts

The result stayed at 4/12, but the failure distribution changed. Many trials
implemented HEAD correctly and preserved most of the base suite, then diverged
on OPTIONS and inheritance.

Common failed obligations were:

- `auto_options=True` on POST or another operation did not enable one
  path-wide OPTIONS response;
- the OPTIONS `operations` object was synthesized from route metadata instead
  of matching the OpenAPI path item after excluding `head` and `options`;
- explicit HEAD appeared incorrectly in `operations`, or disabled implicit
  HEAD still appeared in `methods`/`Allow`;
- defaults and nearest-value inheritance were collapsed too early;
- some public helpers accepted the parameters but did not forward them;
- custom route classes or existing OpenAPI snapshots regressed.

The v1 prompt named these areas but not their exact expected values. The v2
prompt states `auto_head=True`, `auto_options=False`, the nearest explicit
precedence chain, path-wide enablement by any operation, canonical method
ordering, and exact OPTIONS payload derivation. This removes multiple plausible
interpretations without prescribing a routing implementation.

### sqlite-utils: architecture was understood, but API semantics were not

Six trials passed. Five of the six failed Luna/Terra trials missed
`test_cli_upsert_safe_mode`; Terra 3 also missed three library upsert checks.
Luna 1 and Luna 2 failed 13 checks each because their invariant evaluator did
not implement the expected expression forms and their failure results did not
match the required schema.

The v1 prompt successfully focused agents on the hard transaction obligation:
normal helper contexts and commits must not escape an active checkpoint. It did
not state several observable API obligations:

- non-`SELECT` row predicates mean every row must satisfy the predicate;
- aggregate expressions are evaluated once for the table;
- `SELECT` invariants are executed as supplied and their first scalar result is
  the truth value;
- non-strict failures return `success`, `checkpoint_id`, non-empty `failures`,
  and `error_report`, while strict failures raise only after rollback;
- CLI upsert must pass stdin records, primary-key options, and safe-mode
  behavior through the existing command path.

The v2 prompt adds these contracts and an exact real-CLI success/failure probe.

## Prompt-design rule for the next experiment

A useful injected obligation should contain four parts:

1. the triggering condition or concrete entry point;
2. the exact observable behavior or invariant;
3. the repository-wide paths to which it applies;
4. a discriminating probe that would fail under the most plausible wrong
   interpretation.

Avoid adding attractive properties that are not demanded by the task, such as
IGEL bundle relocation. Also avoid substituting broad nouns such as “all
helpers,” “ordered operations,” or “real CLI” for an explicit list when the
task's difficulty is precisely the missing member of that list.

The files under `prompts_v2/` apply this rule. They are a proposed second
intervention and must be evaluated separately; they do not retroactively
describe the prompt used by the completed 60-run matrix.
