# Source-rubric correction for the matched pilot

The original pytest impact criterion names `collect_one_node` as a
`CallInfo.from_call` call site. The direct collection invocation is in
`pytest_make_collect_report`; `collect_one_node` dispatches that report hook and
consumes its result. This wording was not checked precisely enough before the
first run.

The [versioned correction](codegraph_source_protocol_v2.json) is recorded after
inference. It is a separate retrospective diagnostic protocol. The original
tasks, answers, source captures, native repair outcomes and frozen policy remain
unchanged. No model task is rerun.

## Original item and source

The original item says: “Identify call_runtest_hook and collect_one_node as
production CallInfo.from_call call sites.” Read as direct invocation sites, its
collection premise is wrong. Read as an affected collection path, it does not
specify the distinction it expects an answer to make.

The pinned pytest source shows:

| Function | Source | Role |
| --- | --- | --- |
| `call_runtest_hook` | `src/_pytest/runner.py:191-199` | Direct factory call with configured re-raising |
| `pytest_make_collect_report` | `src/_pytest/runner.py:252-275` | Direct collection factory call, with no `reraise` argument |
| `collect_one_node` | `src/_pytest/runner.py:368-375` | Calls the collection-report hook, removes `rep.call`, and may dispatch interactive handling |
| nose `pytest_runtest_makereport` | `src/_pytest/nose.py:27-33` | Additional direct factory call used to substitute skip exception information |

The [pytest assessments](codegraph_source_assessment_v1_pytest.json) retain
version-bound source excerpts. All four answers identify the actual direct
collection factory and explain the upstream entry/consumer correctly. The
original ambiguous item is therefore unassessable for those four observations.
It is retained in the denominator and receives neither zero nor a model-error
label.

## Separate corrected diagnostic

The corrected item requires the runtest and collection direct callers and
explicitly distinguishes the upstream `collect_one_node` entry/consumer. All
other source criteria, native grading, task membership, attempts, timing and
usage formulas stay unchanged.

All four answers meet that corrected item. Their retrospective diagnostic
acceptance is recorded separately from the original unassessable status. It
cannot establish a prospective 20% gain. Any future inference cohort must freeze
its own complete protocol after source-checking every expected call site and
exercising each arm's exact setup and first query on every selected repository.
