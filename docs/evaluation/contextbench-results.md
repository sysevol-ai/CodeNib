# ContextBench retrieval experiments

The complementary Haiku search pair has completed all **500 canonical tasks**
under execution protocol `3e20eb64`, with 1,000 accepted worker sessions and no
retries or repairs. These are context-search results on an already-exposed
population, not a fresh independent validation or an official leaderboard entry.

| Search configuration | Line Recall | Line Precision | Line F1 | Recorded search usage |
| --- | ---: | ---: | ---: | --- |
| CodeNib complementary Haiku pair | 77.07% | 20.52% | 28.16% | $5.680771745; all 1,000 sessions known |
| Frozen native Opus control | 72.83% | 27.97% | 33.95% | $91.1558898 nominal subscription value on 499 tasks; one fee unknown |

The paired Recall difference is **+4.24 percentage points**, with an unadjusted
repository-alias bootstrap 95% interval of [+1.59,+6.41] points; F1 falls 5.78
points, interval [-7.21,-4.22]. The comparison uses 57 alias clusters and 10,000
resamples. The original failed-zero control position is retained. Different
models and billing routes, the unknown control fee and prior annotation exposure
prevent a matched-model efficiency or independent generalization claim.

The [1,000 per-task comparison rows](../assets/contextbench_search_pair_verified_500.csv)
and [summary with hashes, all metric levels and limitations](../assets/contextbench_search_pair_verified_summary.json)
preserve the original results. Search usage averages $0.01136 per candidate task;
it excludes repair, native grading and compute. The search batch is settled once;
its summary retains the budget snapshot at that settlement. These are recorded
model usage values, not reconciled cash invoices or a provider credit balance.

The pinned official evaluator now scores the final selected contexts for all
**231 completed first attempts**, including all43 unresolved repairs. Per-task
Recall, Precision and F1 are macro averaged, with F1 computed before averaging:

| Final selected context, partial231 | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| File | 91.44% | 20.38% | 30.28% |
| Block / symbol | 86.98% | 14.48% | 22.87% |
| Line | 80.65% | 21.87% | 31.47% |

The [partial231 score summary](../assets/contextbench_final_context_partial231_summary.json)
retains exact metrics and evaluator/input hashes. The same231 IDs in the original
search table have80.12% line Recall and31.55% F1. Final selection adds about0.53
Recall points while F1 decreases about0.08 points; the observed gain is modest.
The same-ID descriptive audit finds15 tasks with higher line Recall and216
unchanged, with none lower. Line F1 improves on7 tasks, falls on44 and remains
unchanged on180. Adding confirmed source can increase Recall while reducing
Precision; a higher final F1 is not guaranteed.
This is not a strict causal ablation: three reused development repairs have
different original search inputs and retrospective selections. Source attribution
for arbitrary shell output remains partial. The231 source mix differs from
full500; do not compare these partial values directly with the full500 search
table or present them as the final leaderboard score. Native outcomes remain
188/231 resolved, or81.39%, with$141.76314628 allocated search-plus-repair model
usage, about$0.61369 per task. This scoring invokes no model or native test.

The canonical500 batches with completed grading and settled fees retain
**231 unique first candidate attempts:188 natively resolved and43 unresolved**.
They include the three original development repairs,174 Verified tasks and54 Pro
tasks in canonical metadata order. All231 raw patch/native/selection/fee bindings
authenticate, including2,521 unique metered search/repair sessions. The remaining269
are outside these settled cohorts. The verified native transport is now installed,
and the unchanged dispatcher has started the first32 remaining tasks with two
concurrent solver workers. The full269 continuation uses nine fixed batches;
new outcomes join the total only after native grading and fee closure. The
latest54 Pro first attempts finish with
**34 resolved,20 unresolved**, across675 metered repair sessions and
**$70.9951234** actual API usage. Their$270 reservation closes once and is released;
it is not an additional charge. The preceding55 Verified attempts retain their
45 resolved,10 unresolved outcomes and$21.4079353 usage.
The earlier58 selected-context exports pass the unchanged official evaluator as
a format compatibility check. A zero-model provenance replay now reauthenticates
all354 original search workers and confirmed repair inputs for the completed177,
then recompiles every selected context and receipt. Each matches its original
record and exact prepared metric input; each selected patch matches the natively
tested first patch. All177 have confirmed model-input receipts. This replays
input provenance without rerunning official scoring. Arbitrary-shell attribution
remains partial, and the original three selections remain disclosed as retrospective.
These exposed partial results do not establish full500 Pass@1 or complete-system
Recall/F1. Full500 input provenance and publication replay remain open.

The latest64 fixed tasks complete with **58 resolved,6 unresolved** and
**$25.4390047** across504 fully metered repair sessions. Every first patch,
native result and source selection authenticates, and all128 owned solver/native
containers are independently confirmed absent. The$320 reservation closes once;
reused search is not debited again. This includes the disclosed native
baseline-positive django-7530 task; a native pass alone does not prove its issue fixed.

The preceding32 fixed tasks retain **29 resolved,3 unresolved** and
**$11.9111416** across264 fully metered repair sessions. Their64 separate native
neutral/reference controls pass before inference. Exact model/patch/selection
hashes authenticate the completed results, and all64 owned solver/native
containers are independently confirmed absent. Fees close once; reused search
is not debited again.

The preceding16 tasks retain their **15 resolved,1 unresolved** outcomes and
**$4.9256369** across124 repair sessions. Three model-complete cases encounter
Docker cleanup acknowledgement timeouts. Preserve the original errors and
solver/config/patch bytes; exact-owner absence checks and zero-model
reconstruction recover observations/usage and grade the same first patches.
Only four cases never attempted by the failed controller continue after the
cleanup fix, with unchanged model, prompts, context policy, guards and native
controls. No model retry, task replacement or search rerun occurs. The cleanup
helper bounds removal/absence verification and checkpoints ownership while
preserving model outputs; dispatch still stops on unconfirmed cleanup.

Across all231 repairs, new API usage totals **$139.3904282**. Their allocated
original search usage is$2.37271808, for **$141.76314628** modeled search-plus-repair
usage, without charging reused search again. At this closure, the new$500
authorization has **$360.6095718 remaining**, with no active reservation; cumulative
program API usage including prior work is$220.40193318. These are recorded SDK
usage values; native compute and reconciled invoices remain separate.
These values describe the settled231 prefix. The first remaining32-task batch
is now running under a$160 reservation, leaving$200.6095718 unreserved at launch;
reservation is a ceiling, not an additional charge. Its actual receipts are not
yet settled. All116 Poly and153 Multi source/native admissions and all269
actual model/native command checks precede this launch.

The Poly preparation encounters native environment failures before any of the
remaining269 model requests. A VSCode diagnostic compiles but cannot download
Electron during the network-isolated test, producing no parsed tests. Preserve
that failure. Seven native-only cache profiles now preload the public Electron
version pinned by each task's base revision. Live image checks bind each profile
to its original task image, unchanged tracked source/tests and execution settings;
the separate source-only agent images remain unchanged. Four preparation commits
initially inherit the prewarming container's `sleep` entrypoint and are rejected.
Separate configuration-only images restore the original empty entrypoint, with
the failed records retained. One actual cached neutral/reference diagnostic
records all four required test IDs in both runs and yields False/True as expected.
This diagnostic does not replace the full232 canonical controls or any candidate
attempt. This diagnostic preceded the completed full admission and current paid
continuation; it adds no candidate repair.

All116 Poly source environments are now prepared, including two code-server LFS
recoveries and three Transformers cleanliness recoveries. The code-server
snapshots preserve the full
canonical LFS asset through a local object cache; one also retains its exact
canonical submodule with one commit and no remotes or reflogs. The Transformers
snapshots remove untracked nested repositories inside the isolated solver
container. The original native images and failed evidence remain unchanged.
Whole-cohort source reconciliation completes only after the original source
writer exits; the five original failures remain available separately.
The updated native adapter preserves the pinned scorer's unresolved result for
an empty parser output instead of raising a `KeyError`; raw output and missing
test IDs remain explicit. Empty output cannot satisfy the separate required-test
execution gate.

Native grading records explicit container limits and supports an opt-in8GiB
memory ceiling while retaining the3GiB default. One Svelte reference run exits137
under3GiB; a fresh neutral/reference pair under8GiB records all876 required test
IDs and qualifies. No independent kernel OOM event or peak-memory measurement is
claimed. Eight Prettier environments hit the256-process limit because Jest sizes
its worker pool from the host CPU count. An opt-in `--native-test-workers 2`
inserts only `--maxWorkers=2` into their original test command, preserving test
selectors and the JSON reporter. All eight fresh control pairs qualify, with
every required test ID observed. Worker overrides accept only the original
Prettier and code-server Jest commands; unsupported runners and preexisting
worker overrides are rejected. All nine original failed pairs remain available.
These native resource profiles change neither model inputs nor scoring rules.
All76 focused unit tests and pinned hooks pass. Canonical controls run as each
source becomes ready; complete Poly and Multi execution qualification and
final500 evaluation remain open. These fixes add no candidate attempts or repair
score claims.

Further original controls expose Transformers public-model download failures and
CPU-thread exhaustion. A read-only diagnostic loads all six BERT, RoBERTa and
GPT-2 config/tokenizer combinations when networking and bounded threads are
available. The pinned upstream manager uses Docker's default networking; the
earlier adapter adds an offline restriction. An explicit `--native-network bridge`
now restores upstream networking only for Transformers and code-server integration
tests, while the default stays `none`. An optional `--native-thread-workers 2`
bounds Transformers' and Keras' CPU pools without changing their test commands. Both effective
settings are recorded and tested separately from inference. Three fresh
Transformers pairs initially qualify with all8,87 and10 required IDs observed in
each run; twelve further pairs also record every required ID and qualify. The
latest Whisper reference originally reaches the900-second deadline; its fresh
bounded-thread pair records all236 required IDs within the unchanged deadline.
These prospective native-only profiles differ from the earlier offline policy;
all original failures remain retained. The agent environment, model and context
policy stay frozen.

Six fresh Keras pairs qualify with bounded CPU pools. A seventh progresses beyond
the TensorFlow abort but initially aborts while creating JAX's CPU backend. A native-only
`--native-cpu-count 2` option exposes two allowed host CPUs while retaining the
two-CPU quota and original backend, test command and test selection. Its fresh
full neutral/reference pair now qualifies with all2,398 required IDs recorded in
both runs. These seven recoveries leave the original failures available.

Two code-server worker2 pairs progress beyond process exhaustion but remain
unqualified. Bridge-network diagnostics expose remaining test errors and native
annotation names that differ from actual Jest titles/paths. One Transformers
task also has missing annotated identifiers and dependency errors. The pinned
native scorer reports the reference patches resolved despite186,269 and19
missing required identifiers respectively. The adapter retains their unresolved
outcomes and original raw scorer output; no identifier remapping or claim of
complete execution is made.

A prospective conservative policy binds these three known P2P identifier defects
to their exact task, dataset, source and reference-log hashes. Completed matching
runs may close as unresolved first attempts; the policy cannot create a positive
outcome or hide a deadline/cleanup failure. Fresh neutral/reference executions
under this policy all close unresolved, while retaining their failed positive
control checks. All500 tasks still require actual candidate attempts and remain
in the denominator. This is explicit conservative failure accounting, not positive
environment qualification. Remaining paid admission must consume this separate
policy and its actual receipts before launch; none of these three cases has a
candidate repair yet.

All153 Multi source environments are prepared, including eight reused sources.
A closure audit reauthenticates every source proof, builder and canonical
commit/tree binding, all34 source resource profiles and307 required
solver/source/native image IDs. Snapshots exclude reference artifacts and retain
the single-commit boundary. The jq sources preserve their exact oniguruma Gitlink
and use a checksum-verified public Python runtime; native images stay unchanged.
Original source failures remain retained. The source owner has exited, and the
unchanged four-worker controller continues its canonical control sweep. The306
planned neutral/reference runs are not a completed-execution count: an early
neutral error can prevent its reference from starting. Source closure adds no
candidate attempt or provider usage.

Six observed Zstd native-script deadlines now have separately closed neutral and
reference diagnostics at3,600 seconds, with the same adapter, images, scripts,
scoring and container resources. They produce the expected unresolved/resolved
outcomes and pass current-result/source/cleanup reaudits. Their aggregate reports
and pure NONE transitions still need actual base-only executions before admission.
Three modern Ponyc images instead stop before
tests because the offline runner lacks their pinned LLVM dependency. An audit of
all12 unattempted Ponyc source recipes isolates these three configurations.
Two caches are compiled and independently source-checked. Their original image
commit requests exceeded a90-second client read guard but actually completed;
those observation errors remain retained. Metadata restoration preserves every
registered filesystem layer and restores the full original execution Config.
Its small manifest requires those existing layers and is not a portable image.
The original default-cache controls retain a broadcast failure and compiler
signal9/deadline failures. Fresh900-second pairs now satisfy the required native
criterion for all three cached Ponyc cases after explicit networking and bounded
worker/memory corrections. The third dependency preparation completes with its
separate3,600-second commit guard and verified cleanup. No patch, native test or
model call runs during cache construction; construction alone is not admission.
Other closed corrections cover Simdjson, GitHub CLI, Fastjson, Logstash and three
Svelte cases. Native process and CPU-affinity options remain separate from model
execution; their latest adapter passes142 related unit tests and pinned hooks.
The remaining resource controls and full-cohort qualification remain open.

Canonical source restoration now covers all eight affected Jackson images and
seven Dayjs images. Their original build manifests differ from their Git
commits; derived native images restore only the canonical manifest bytes and
preserve reference/test scripts and source-only model images. Dayjs also needs
exact Config restoration after Docker normalizes null labels; that original
error stays retained, and its metadata archive requires existing layers.
Actual readback authenticates all15 derived source images. Fresh canonical
controls expose unavailable old Jackson parent POMs and Dayjs worker exhaustion.
All eight Jackson images now cache authentic parent POMs, unchanged from exact
public snapshot Git objects, including the
[pinned upstream snapshot](https://github.com/FasterXML/jackson-bom/tree/c26c2b0fab0edff84590ef05febd1866242dd3a9).
No target build or test runs during this metadata installation. All eight Jackson
and seven Dayjs corrected pairs have completed strict readback. A Mockito observation records
a256-process peak,17 denied allocations and current memory use below2GiB.
Its higher-process-limit pair then exposes a500-millisecond timing assertion
failure while several native containers share CPUs0/1. A separate pair uses
CPUs4/5 with the same two-CPU quota, scripts and scoring; its reference records
all104 required IDs as passed. The original process, deadline and timing failures
remain retained. This is native environment evidence, not a candidate score.

Two other original Ponyc neutral runs abort inside added regression tests, after
635 and874 completed test results respectively. Their references record all
declared targets. The prospective source-bound admission keeps170 and36 absent
non-NONE targets uncredited and requires independent base-only executions covering
the original test IDs. Independent local bases now record all1,083 and1,042 tests
as passed and cover their historical expected IDs. A new adoption reader
reauthenticates all28 completed baselines, recomputes inverse-test-patch
cancellation with a private Git index and retains their actual current-result
evidence without rerunning tests. The full selected cohort retains26 early executions plus two fresh baselines
at positions376 and486. All28 now authenticate against actual test results. A separate original reference records a cleanup failure
after its required tests pass. A fresh unchanged pair now satisfies the criterion
with all749 targets recorded as passed in its reference. The neutral records
704 passes and10 failures;35 explicit NONE targets remain absent and uncredited.
The original raw error remains unchanged and all containers are independently
absent. Native exit codes2/2 remain visible; this is not a fully green suite.

Actual current-result reaudits now cover all153 selected Multi pairs with
no audit errors, including all ten Vue memory corrections and all seven MUI
runner restorations. Their corrective batch owners have exited. The full
selected-admission proof now binds all306 control records,28 actual baseline
executions and the source/native identities after the qualifier exits. Seven MUI native images restore only
extra trailing blank lines in the official run/test scripts; actual readback
binds the exact pinned bytes, unchanged Git tree, reference patches and Config.
The source-only agent images remain unchanged. MUI481 retains its original
2,000-millisecond Mocha timeout failures. A finite different-CPU pair now
qualifies with the same two-CPU quota,8GiB memory, internal timeout and tests;
its reference passes all5,794 required labels. This does not establish a
specific cause for the earlier timing variation.

The Logstash reference log contains two passed records joined with stderr
messages, which the unchanged native parser cannot recognize. An explicit
separate-stream capture mode retains byte-exact stdout and stderr and records
their sizes, hashes and stdout-then-stderr parser-log composition. It preserves
partial timed-out output and cleanup; cross-stream chronology is unavailable.
The default remains merged. All142 related unit checks pass with one skip, and a
fixture using the pinned official parser reproduces both lost labels and checks
the separated records. A fresh actual neutral/reference pair now qualifies: the reference passes
all1,060 required labels, and both raw-stream bindings authenticate. Unrelated
failures and native exits1/1 remain visible; this is not a fully green suite.
The fixture, reference controls and original failed runs contribute no candidate
credit. This change is
[stacked draft PR842](https://github.com/sysevol-ai/CodeNib/pull/842), after
[PR841](https://github.com/sysevol-ai/CodeNib/pull/841).

The uninstalled native transport archives five approved native adapter versions
in19 files while preserving all nine generated model-dispatch, reservation and
settlement programs byte-for-byte from the prior verified draft. Actual config
boundaries reject changed images, adapters, deadlines, missing native files and
unknown manifest entries. Raw-log checks reject altered metadata, stdout, stderr
and composed logs. After exact pidfd retirement of the childless old collector and archive
waiters, the new collector is live for the nine remaining cohorts. It rereads
all231 original first attempts and reproduces the same188 resolved/43 unresolved
results,2,521 sessions and fees. Original observer/source files remain retained.
The actual draft gate rejects incomplete Multi qualification before a paid plan
or reservation. After the complete153 proof and owner exit, the native contract
is frozen and all269 actual native/model command lists parse against the approved
runtimes; changed physical-CPU binding, outer timeout and log mode are rejected.
An initial Poly field-reader mismatch stops before output or a paid request;
its original error stays retained, and the corrected metadata reader leaves
command-validation functions unchanged. Exact pidfd retirement of the obsolete
release waiter and six stopped, childless paid parents now precedes atomic
installation. An initial installer fails before retirement because its Conda
Python lacks pidfd support; the failed files and unchanged-ledger evidence remain
retained. The corrected installer delegates only process retirement to system
Python. Installed native admission passes again after retirement, before the
unchanged dispatcher acquires its fresh intent and starts the first32 tasks.

A separate early diagnostic on one prepared MUI environment records all 8,548
declared required test identifiers with PASS/FAIL statuses in both actual local
runs. The neutral patch remains unresolved and the reference satisfies the pinned
native criterion. Raw native exit codes 3 and 1 remain recorded; this does not
claim a fully passing test suite. Both owned containers are independently absent.
This diagnostic changes no canonical control or candidate record and does not
establish the complete Multi execution qualification.

Read-only validation of all82 original nominally passing Poly pairs finds one
VSCode neutral run with an empty parse after a compile exit137. Its reference
records all required IDs and passes, but the pair does not satisfy the separate
execution gate. A fresh8GiB pair now records all306 required IDs in both runs,
with the authenticated Electron cache and unchanged selectors/scoring. Its
original remains retained. A separate read-only selection now binds all116 Poly
cases:81 original strict pairs,32 corrected strict pairs and three explicitly
conservative unresolved pairs. The113 positive-reference cases record all
required IDs in both controls; the other three receive no execution credit or
positive-reference claim. Six changed/incomplete selection fixtures are rejected.
Canonical original cohorts, source records and every failed native log remain
unchanged. This admission proof is not a candidate repair or a full500 score.

The installed remaining269 paid preparer transports the selected Poly
admissions, including bridge/thread/CPU-affinity resources and the three known
negative descriptors. All116 actual Poly argument bindings authenticate; the
34 prepared Go profiles also bind to the current source builder. Every option
reaches only the native grading CLI. Generated model-dispatch, reservation and
settlement sources match the preceding nine variants exactly. Changed selected
admission/control hashes are rejected. Before full qualification, the real global
gate rejects an incomplete Multi execution proof before any plan or reservation;
the completed153-case proof now satisfies that gate.

Separate nine-phase lifecycle fixtures inject case admissions and the global
gate into a temporary root. They exercise all269 fixed IDs, two disjoint shards,
duplicate-plan rejection, a live preceding owner, insufficient budget and
idempotent settlement. They do not qualify Multi environments. Copied native
snapshots import their current CLI in isolated Python; no provider, native test,
Docker build or real financial mutation occurs in these checks.

The installation requires both actual cohort admissions, current source
finalization and actual source/native qualifier exits. All32 readiness
gate combinations are checked. Only after these conditions hold may it retire
six exact stopped, childless obsolete owners through pidfd, retain their original
intent and installation backups, install the verified transport and start the
unchanged nine-phase dispatcher under a fresh exclusive intent. Three malformed
retirement requests are rejected before any signal. All six actual obsolete
parents and the childless obsolete release waiter have now exited through exact
pidfd acknowledgement. Installation retains their original intent and alias
backups. The new original dispatcher owns the$160 first-batch reservation and
two solver shards; interrupted execution still requires root recovery without
automatic model retries.

To make room for remaining environments,104 closed Pro source/native image caches
are removed with normal Docker reference checks and no forced removal or global
prune. Their original raw archive parts, source Git seeds and preparation inputs
remain hash verified. Repeating native tests requires re-pulling pinned public
bases and rebuilding private derived images, whose IDs can change; exact private
Docker exports are not retained. A further221 caches from119 closed Verified
tasks are removed, while17 remain protected by Docker references. All24 original
archive parts and121 source Git seeds for those tasks authenticate after cleanup.
Existing context/receipt replay uses the retained raw material and does not
require those image caches.

A further108 source/native cache images from54 early closed Verified tasks are
removed with the same normal Docker reference checks. The remaining early case
lacks this admission metadata and stays excluded. Three original archive parts
authenticate, and all54 retained Git seed TAR hashes match their archived source
preparation proofs before and after removal. Anonymous HEAD checks confirm the54
pinned public base digests are still available. Current/future case references,
all existing containers and foreign tags remain protected; no forced image,
container or raw-file deletion occurs. During parallel source preparation the
observed free-space change is+19.4GiB, which is not an isolated reclaim estimate.
Exact private image exports remain unavailable; repetition needs pinned base
pulls and private rebuilds. These cache operations add no model calls or fees.

The54 early source Git seeds also have a separate private rebuild supplement:
326 original members in two actual decoded parts totaling90,352,595 bytes,
each below50MiB. All54 rehydrated Git repositories retain the canonical commit,
tree and one-commit history without remotes or reflogs. This overlaps the original
58-case trajectory archive and adds no candidate attempts. It proves source Git
rehydration, not native-image or test reproduction; publication review remains open.

Another42 superseded source-profile and native-base caches from21 closed cases
are removed using normal Docker reference checks. All29 original archive parts
and84 preparation inputs authenticate before and after removal. Multiple known
owned tags are removed individually; other tags, current/future case references
and container references remain protected. Concurrent environment pulls prevent
an isolated reclaimed-space estimate. All original Git seeds and evidence remain.

All 449 qualifying temporary host clones have now been rehydrated from their
retained Git seeds, compared against the restored metadata and every source-file
or symlink byte, and removed after a separate consumer rechecks the full manifest.
Another 25 clones remain: ten involve submodules, two retain original failed
preparations and thirteen belong to superseded build directories whose prepared
proofs bind retained recovery seeds. Those thirteen effective recovery-seed hashes
also authenticate. Original benchmark checkouts, seeds, source proofs, prepared
images, model contexts and canonical native results remain unchanged.

A retention audit authenticates all 32 original repair archive parts and the two
overlapping source-supplement parts. All 35,182 indexed loose-member path
occurrences remain present; five archive-only support members are decoded and
hashed from the actual earlier58 parts. This verifies original compressed-part
hashes and loose-file presence, without claiming every loose member was rehashed.
These zero-model material and cache checks establish no additional candidate score.

The closed64 metadata-order cohort completes source/native admission before any
request. Preserve two original Docker create timeouts and their source-only
recoveries. The timed-out creates leave two containers that appear after an
initial absence check, so the old cleanup claims are not accepted. Exact-owner,
image and state checks identify both residual containers; explicit removal and
subsequent absence checks complete their cleanup. Future source builders keep
cleanup unconfirmed when creation is not acknowledged. Four deterministic
regression checks cover that late-create race and removal outcomes. Both retried
source images retain the original canonical commit/tree and native image. No
repair-model or search request is repeated. The model runtime stays frozen at
`25d4580c`; source-preparation cleanup and the new Poly cache/aggregation utilities
have not changed its model, prompts, context policy or budget guards.

One fixed task, `django__django-7530`, already resolves under the pinned
native criterion with a neutral patch that changes no business code. Both the
original official image and the clean-source image produce that result; both
reference-patch runs also resolve. The issue-specific router regression errors
before the reference patch and passes afterward, but it is absent from the
[pinned dataset's required-test lists](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/blob/c104f840cc67f8b6eec6f759ebc8b2693d585d4a/data/test-00000-of-00001.parquet).
Its recorded `FAIL_TO_PASS` instead names the already-passing
`test_squashmigrations_initial_attribute`. All four runs use identical official
evaluation scripts, dataset, harness, parser, scorer and output transport; their
owned containers are independently confirmed absent. Retain the original failed
neutral-false admission check. A documented environment-admission exception
requires this four-fixture original-image concordance, with ten local regression
checks rejecting missing, changed or ungraded evidence. The native criterion
and task population stay unchanged, and the first candidate patch still requires
ordinary native grading. Disclose this baseline-positive task separately;
a passing native outcome alone does not demonstrate repair of its reported issue.

A read-only execution audit parses359 original logs: all119 completed Verified
candidate patches and their240 admitted/control-concordance runs. All107 resolved
Verified candidate logs record every required test status. Fourteen Django log
records have official parser identifier gaps; their missing names appear in the
actual test-output spans, which also record executed unittest suites. Keep the
original parser and native outcomes unchanged.
One original boolean admission, `pylint-dev__pylint-4604`, is collection-blocked:
the reference test patch imports `pylint.constants.IS_PYPY`, which the reference
production patch adds. The original base and candidate patch lack that symbol;
both collect zero tests, while the reference runs and passes21. Its candidate
remains unresolved. This is recorded separately from a behavioral test failure;
the neutralFalse/referenceTrue pair alone did not establish executable tests.
No production reference code, grading change or model retry is introduced.

The next55 complete the additional prospective test-execution admission before
inference. Their110 control logs record every required test identifier, and every
`FAIL_TO_PASS` target has a PASS/FAIL/ERROR execution status. Conditional
`PASS_TO_PASS` XFAIL/SKIPPED statuses match between both controls and remain
explicitly disclosed; they are not described as executed.
The original waiting paid owner is held before preparation/reservation until
the complete proof is available, then resumes without restart. The two first-attempt
shards retain the same frozen model/context runtime, native criteria, population
and budget policy.
Three actual-log regressions reject the collection-blocked Pylint fixture, accept
the61-required-test pytest fixture, and preserve the matching scikit-learn XFAIL.
A separate owned-child test verifies STOP/CONT acknowledgement and rejection of
a mismatched process identity through Linux pidfd. Only a complete110-log proof
releases the same existing paid owner; failures retain it for root recovery.
The final110-log proof passes before its existing$275 reservation and first
repair requests. Native qualification itself makes no provider call or financial
mutation.

Twelve Sphinx environments initially omit per-test pass records even when a
reference patch passes its required tests. Each original native image adds
`pytest -rA` in its tox configuration, while the canonical source checkout omits
that reporting-only change. A `PYTEST_ADDOPTS=-rA` source-image profile restores
the report without changing filesystem layers, code, tests, command, parser or
scorer. Two older tox files override that variable; a narrowly scoped append
override restores the same reporting flag while preserving the actual
materialized `PYTHONWARNINGS` and other variables. The newer tox key alias drops
that warning setting in a real metadata probe and is rejected.
These image environment settings also affect any agent pytest reporting;
that observation change is disclosed. The model/context code and guards remain
unchanged. All12 corrected neutralFalse/referenceTrue pairs qualify, with every
required test identifier authenticated. The55-case cohort retains136 actual
native fixtures:110 admitted runs and26 superseded or rejected reporting runs.
All original fixture outcomes remain available. Canonical records are backed up
byte-for-byte and replaced only after the original writer exits; an atomic
last-written reconciliation marker gates the held owner. Deterministic file
fixtures verify idempotence, staged recovery, rejection of unrelated writes and
missing/failed/changed release evidence. No candidate model attempt repeats.

A read-only inventory now authenticates all3,471 source/control input files,
including55 original Git seed TARs and all136 actual control records. Each of the
110 selected control hashes matches its admission. After the actual177-case
index,55-case native/fee closure and original controller exit, a separate
zero-model archiver retains8,191 original members in nine decoded, byte-verified
Zstandard parts totaling418,339,937 bytes. Together with the unchanged earlier58
and64 archives, their task coverage is exactly177. Actual partial122 input is
rejected before archive creation. This55-case original increment remains private
and unredacted. Its separate publication preview now finishes:8,193 members in
nine parts totaling420,100,144 bytes, each below50 MiB. A55-case semantic fixture
verifies all417 exact requests and2,528 SDK UUID pseudonyms with unchanged
observation receipts, selected contexts, patches, native outcomes and fees.
The actual-part review recomputes93,771 decompressed Git-object IDs and retains
128 private-key-header findings. All128 reconstruct the exact public Sphinx
localhost test certificate, authenticated against its pinned upstream Git blob;
none is silently erased from the original scan. Changed certificate bodies are
rejected by the classifier. This scoped classification does not establish that
arbitrary credentials or personal data are absent.
Composition authenticates the disjoint231 cases across32 repair parts totaling
1,526,302,235 bytes. All shared extraction paths have identical bytes, and13,277
SDK digest links have no conflicts. All344 findings from the55 Verified and54 Pro
previews remain retained with exact public-source classifications bound; no
declared finding remains unclassified. Full500 raw coverage and publication
gates remain open.

The closed64 cohort's final three pytest environments initially fail even with
reference patches:
their history-free source snapshots cause editable installation to infer
`pytest==0.1.dev1`, below the unchanged test configuration's minimum. Preserve all
six original failed fixtures. A pytest-specific
[setuptools-scm version override](https://github.com/pypa/setuptools-scm/blob/main/docs/config.md#environment-variables)
uses each original native image's installed distribution metadata. The derived
source images retain identical filesystem layers, canonical source/tree and
reference-artifact hygiene; only that package metadata environment setting is
added. Empty Docker entrypoints normalize from `[]` to `null`; five local checks
accept that equivalence while rejecting other command, environment, entrypoint
or filesystem changes. All three corrected environments pass
neutralFalse/referenceTrue before inference. Future pytest source admission uses
the same metadata preparation. The64-case gate retains136 actual fixture runs:
130 admitted fixtures, plus the six original failed pytest fixtures. Native data,
required tests, parser, scorer, model, prompts and context policy are unchanged.

A separate private original-byte archive retains the earlier58 completed first
attempts, including the seven unresolved cases, failed native wrapper logs and
original cleanup failures/recovery siblings. All7,700 members round-trip with
matching hashes; its compressed parts are each below50 MiB. Exact runtime source,
original metric inputs, outcomes, session usage and selection records are
retained. This unredacted material is not published. Full500 raw coverage,
publication redaction and portable replay remain open submission gates.

A separate private increment retains all next64 first attempts, including their
six unresolved outcomes, original source TARs, admission failures and resource
amendments. Its9,068 original members round-trip byte-for-byte through15 bounded
zstd parts totaling740,483,203 bytes. An independent decoder reads the actual
split parts, verifies every member and the complete stream checksum, rechecks
the unchanged earlier58 parts, and confirms the union covers exactly122 first
attempts. It explicitly retains all six failed pytest fixtures, six corrected
fixtures, four baseline-positive concordance fixtures and both original
source-create timeouts with their owned cleanup. No model request, native test,
official scoring or financial settlement is repeated. This original increment
remains unredacted and private; separate publication previews are described below.

A private redacted preview retains all7,700 original members and verifies7,703
archive members including its support manifests. It substitutes known workstation
labels and2,602 SDK session/event UUIDs consistently. All463 actual repair model
requests and their request hashes remain identical, as do patches, recomputed
observation receipts, final source selections, native outcomes and system usage.
A public digest linkage can reconcile pseudonymous SDK identities to the original
usage commitments without exposing raw identity values. The exact authorized
credential is absent. Three bounded archive parts total142,348,276 bytes. These
checks cover the declared substitutions. A separate pattern review scans all7,703
archive members and1,703 nested runtime files, finding no declared credential
patterns, sensitive JSON fields or known workstation/Git-operator identifiers.
This establishes only the named checks on this58-case snapshot; it does not prove
the absence of all possible personal data. Full500 privacy coverage, portable
replay and publication approval remain open. The1000 search-session
digest links are also independently reconstructed from the unchanged original
and redacted search artifacts, preserving all recorded fees.

A matching private next64 preview retains all9,068 original members and verifies
9,070 archive members across15 bounded parts totaling745,084,879 bytes. Known
workstation labels and3,011 SDK UUIDs are substituted consistently. All504 exact
repair requests/request hashes, patches, recomputed observations, final selected
contexts, native outcomes and usage remain unchanged. All66 nested Git seed TARs
remain present with normalized owner/time headers; their Git object-storage and
index bytes remain identical. This preserves the source/admission failures and
all64 first outcomes. The earlier58 runtime/publication index remains an explicit,
unchanged dependency. An independent decoder verifies all9,070 members from the
actual split parts. Its declared-pattern review covers all1,584 nested Git files
and374,187 decompressed Git objects across65 distinct object-storage sets; all
object IDs recompute. Archived Git configuration, hooks and alternates are never
used. The exact authorized credential, declared credential shapes and known
workstation/Git-operator identifiers yield zero findings. Sensitive named-field
checks on outer JSON also find none. These are scoped checks on the122 settled
attempts, not proof that arbitrary personal data or credentials are absent.
Full500 coverage, broader privacy, full500 portable replay, publication and
comparability approval remain open.

A separate relocated-directory replay now reads only all four private
publication-preview sets, the search-preview parts and their public digest links.
It hashes all43,510
archive-member occurrences, extracts the required records and1,703 bundled runtime
files, then imports CodeNib modules from that frozen runtime. All231 selections
and observation receipts recompile identically;2,059 repair calls and462 actual
search SDK fee receipts reproduce2,521 unique sessions and$141.76314628 modeled
usage. Every native result remains bound to its original first patch. The worker
uses the installed Python dependencies, with Python opens restricted to the new
directory and dependencies; networking and external processes are denied.
Optional Git/font/config probes remain blocked. This is a Python audit guard,
not an OS sandbox. It makes no model calls or new official scoring. Fresh-clone
source-footprint replay, native-test reproduction, broader privacy and full500
publication/comparability approval remain open.

A separate **three-case candidate-only end-to-end pilot** now completes the
original metadata-selected fmt, Gson and ponyc development tasks. The existing
mini SWE-agent loop uses Opus 5.5, medium effort, 20 turns and a $4 per-case
search-plus-repair SDK usage guard. It reuses the Haiku search receipts and
receives version-bound navigation, then pulls source through CodeNib read/search
commands. Initial source bodies are empty; the 2,048-token navigation guard does
not cap later source reads.

| Native task | Native outcome | New repair API usage | Search plus repair usage |
| --- | --- | ---: | ---: |
| fmtlib/fmt-2394 | Resolved | $0.34861 | $0.35695 |
| google/gson-1391 | Resolved | $0.21937 | $0.23041 |
| ponylang/ponyc-2205 | Unresolved | $1.36353 | $1.38093 |

All three native evaluations complete: **2 resolved, 1 unresolved**. There are
24 new metered repair sessions, **$1.9315108** new API usage and $0.03678121 of
reused search usage, for $1.96829201 modeled search-plus-repair usage. Reused
search is included in system cost without charging experiment spending twice.
Every solver/native container is removed. There are no model retries, replacement
tasks or repeated searches. The first native wrapper fails at startup because
the isolated native environment lacks model-only `tiktoken`; that failure is
retained, and the exact same first patch is graded with the existing native-only
`scripts/contextbench_multiswe.py` entrypoint. Its result is joined through the
existing receipt helper in the solver environment. No scorer changes occur.

The [three per-task records](../assets/contextbench_search_pair_native_pilot_3.csv)
and [summary with hashes and scope](../assets/contextbench_search_pair_native_pilot_summary.json)
retain the failed C outcome and all fees. Confirmed CodeNib reads account for
291, 91 and 827 original-source lines respectively; arbitrary shell output and
modified-file coordinates remain outside that partial attribution. This is a
feasibility/cost pilot on already-exposed tasks, without a no-context control.
It establishes neither comparative repair gains nor independent or full500
Pass@1. Full-system ContextBench Recall/F1 remains unevaluated.

Mechanical arithmetic from this three-task mean gives about **$322** for
500 repairs, or **$328** including the completed500 search usage. This is not a
representative forecast: five languages are absent, task costs vary, native
compute is excluded and no full500 repair budget is reserved. Broader native
environment coverage and review of task/protocol comparability remain open
before formal submission. A final-context selection contract is now implemented
before further candidate repair requests, as described below.

A subsequent zero-model inventory matches the original native task record and
base commit for **all500** search tasks. Anonymous registry HEAD requests resolve
all500 candidate image tags to immutable manifest digests:

| Native source | Matched task records and commits | Accessible image manifests |
| --- | ---: | ---: |
| SWE-bench Verified | 174/174 | 174/174 |
| SWE-bench Pro v1 | 54/54 | 54/54 |
| Multi-SWE-bench | 156/156 | 156/156 |
| SWE-PolyBench | 116/116 | 116/116 |

The native harness can construct all174 Verified test specifications and all156
Multi instances/scripts. Poly has parser mappings for all116 tasks, and Pro has
the required runner, parser and Dockerfile sources for all54. This is metadata
and availability coverage, not500 validated environments or native outcomes;
registry HEAD checks do not download layers or verify their platform. The34
Multi repository aliases require explicit mapping to their original native
repository names. Pro uses dataset revision `7ab51149`, which matches these54
tasks; the upstream default now selects V2, so it must not silently replace v1.
A separate transport warmup now verifies cached local images for all54 fixed Pro
tasks: one existing image is reused and53 are downloaded by frozen manifest and
platform/config identity. Sequential downloads check actual Docker-data headroom
and defer during a paid phase. No container, source-image commit, native test,
provider request or financial mutation occurs. This removes future layer-download
waiting; source hygiene and108 actual native controls remain required before Pro
inference.
The [official Pro source](https://github.com/scaleapi/SWE-bench_Pro-os/tree/66f92766bba642462d4bbe5479e83f91f9211862)
retains the v1 grading pipeline. No additional model requests occur in this
inventory. The native adapter now resolves the six known Multi alias mappings
and pins all31 native repository classes. Poly chooses the official parser for
each of its11 repositories, rather than applying the Svelte JavaScript parser
to other languages. The Pro v1 native-only CLI authenticates its original runner,
per-task scripts and Dockerfiles against a pinned manifest, preserves the
official required-test subset criterion and bounds its owned local container.
All54 Pro task records and their test identities bind to the frozen source
metadata; loading the original runner verifies six pinned source files. This
extends metadata and adapter coverage. The first original Pro v1 NodeBB task
now passes neutralFalse/referenceTrue with unchanged pinned tests/parser/scorer.
Retain its original uncached npm and localhost IPv6 failures. A separate local
native image adds only public cached npm dependencies, offline flags and
IPv4-first DNS; all12 recorded grader/probe containers are absent. These are
zero-model fixtures, without additional candidate outcomes. Per-dispatch source
preparation and native controls remain open for the other unexecuted tasks. The repair prompt, model loop, budgets and navigation
delivery are unchanged; only Pro gains the existing source-only preflight.
A separate54-case Pro first-attempt controller was held before any
reservation or model request. Its source preparation produces46 prepared
environments and eight OpenLibrary failures: replacing the parent Git metadata
leaves submodule Git pointers referring to the removed metadata. Original failures
remain retained. Source-only recovery now prepares all54 environments, retaining
each of the16 submodule gitlinks with one commit and unchanged tracked source.
One native submodule has an untracked file; retain its rejected recovery and
remove that untracked artifact before the successful clean-source check. The
original control owner exits without dispatch when the source gate first fails;
a new fixture owner reuses its unchanged per-case dispatch. All54 original
neutral/reference pairs now finish:39 pass the native Boolean gate and15 fail.
Jest, Go and Ansible workers can see64 host CPUs despite the two-CPU quota;
their failed fixtures capture missing tests and process/thread exhaustion.
A separate native-only `--cpu-count 2` profile binds two allowed CPUs and sets
`PYTEST_XDIST_AUTO_NUM_WORKERS=2`, keeping the existing CPU quota, memory, process
limit and network isolation. Older Jest still reads64 CPUs after affinity is
bounded, so Element-Web additionally receives only `--maxWorkers=2` in its pinned
run script. Archive both original/effective scripts and their hashes; parser,
test selection and native subset scoring remain unchanged. This concurrency
argument is a disclosed native command change. Both resource runtime snapshots
remain separate from the frozen21 model/context implementation files.
The three real Jest/Go/Ansible pilots now pass neutralFalse/referenceTrue.
Ansible executes every one of its30 required IDs in both controls. Jest and Go
still omit required neutral IDs because of test collection or compile conflicts;
their Boolean success is not full execution qualification. All15 original
resource/dependency failures now have corrected neutralFalse/referenceTrue pairs
in separate output directories. Two Flipt reference failures need public Go
module caches: only native preparation has network access, reference changes
touch only dependency manifests, and canonical source commit/tree/worktree are
restored before committing separate native cache images. Model source images
remain unchanged. Both actual corrected native controls pass with networking
disabled. No original fixture is overwritten.
The Go pilot's supplementary original-base fixture omits only the evaluation
test-file checkout, retaining both entryscripts. It records seven actual test IDs,
including all six required IDs, while the evaluation test overlay fails to compile
against the old production interface. Its native resolved flag is excluded from
candidate outcomes and it does not amend readiness or release the paid owner.
All54 selected references execute and pass every required ID. The pinned parser
records full neutral execution in34 cases; exact raw Jest failure rows establish
full execution in two more. The remaining18 have explicit base-source conflicts:
ten imports, six Go compilations, one missing configuration option and one suite
registration failure. Missing IDs remain unexecuted. A separate prospective
admission protocol records these conflicts and replaces the additional strict
empty-control execution gate; it preserves original tests, parser, PASSED-subset
scoring, all first attempts, failures and the500 denominator. The strict
qualifier and all earlier failed fixtures remain retained. The earlier request
for another user confirmation was unnecessary under the existing task and
budget authority and is withdrawn; no unanswered question is treated as approval.
These diagnostic fixtures add no model requests, API usage or candidate outcomes.
Six Go solver-source profiles additionally set only `GOMAXPROCS=2` and
`GOFLAGS=-p=2`. Their filesystem layers and other execution settings are
identical; actual probes authenticate clean canonical Git history and effective
Go settings. These settings affect agent Go command concurrency and are separate
from native-only dependency caches. Model/context code and scoring stay frozen.
The source-aware protocol still requires actual required-ID reference passes.
An existing real NodeBB neutral control is gradedFalse while omitting six required
tests; the qualifier rejects it. Its corrected neutral/reference controls execute
all291 required tests and qualify. This fixture creates no new candidate result.
Isolated file fixtures exercise all fixed task identities, two
disjoint27-case shards, admitted native-image overrides, stale/failed-control
rejection, exact session accounting and duplicate-owner rejection. The complete
solver dispatch AST remains unchanged apart from cohort accounting and Pro native
grading transport and per-case four-file runtime archiving. The actual54-source
fixture authenticates all selected controls, preserves the exact15 measured CPU
profiles, exercises two disjoint27-case shards and rejects changed runtime copies.
Reservation/settlement source bytes and idempotence remain unchanged; real ledgers
are not mutated by the fixture. After all54 source-aware admissions, prior
Verified fee closure and at least$270 of available authorized usage are verified,
the same original pidfd-identified controller resumes. Its two disjoint shards
now complete all54 first attempts, retaining34 resolved and20 unresolved outcomes.
The$270 reservation closes once against$70.9951234 in actual SDK usage; the original
controller exits after updating both financial ledgers.
At the Pro closure checkpoint, the remaining269 owner was still held for its
source and test-execution gates; those gates now precede the active continuation.
The Pro source/control inventory now freezes4,656 files and722,239,538 bytes,
including63 original/recovered Git seed TARs and147 distinct native control/probe
attempts. All108 original controls and108 selected control hashes remain bound.
Candidate outputs are inventoried only after their own native/fee closure.
A separate read-only check authenticates all54 completed Pro patches:
their four-file grading snapshots, selected source/native images, original
harness, CPU/script settings, first patches and unchanged subset scores match.
Their captured60-file model manifests match the preceding closed cohort; these
are separate from the21 dispatch-frozen hashes and the four native files.
Actual54 native/fee closure and original-owner exit are verified; this check adds
no model call, native rerun or financial mutation. The resulting231-case index
retains all188 resolved and43 unresolved first attempts.

A reusable private closed-increment preview now passes the actual55-case,
417-request regression with all8,191 original members and8,193 round-trip members.
Exact requests, observations, patches, selections, fees and outcomes remain
unchanged. Actual partial122 input and the then-unclosed Pro batch are rejected
before output creation. The corrected producer additionally recognizes16 actual
submodule Git indexes across the63 Pro seed archives as binary storage; every
other member classification and the rest of the producer AST are identical.
Earlier177 publication previews remain unchanged. After actual54 native/fee
closure, the231-case index and original-owner exit, the Pro original-byte archive
retains9,897 byte-identical members in five parts totaling217,914,029 bytes.
Its private publication preview now round-trips9,899 members in five parts
totaling218,768,936 bytes, each below50 MiB. All675 exact requests, observations,
patches, selections, fees and native outcomes pass semantic replay, with5,136 SDK
UUID pseudonyms and63 rebuilt Git seed archives. Actual-part review verifies
all79 independent Git roots, including16 submodule roots, and recomputes263,738
decompressed object IDs. It retains216 private-key-header findings without
printing matched values. Each occurrence binds to its exact seed commit/path and
one of53 complete files anonymously fetched at pinned public upstream commits
in Ansible, Element Web and qutebrowser. All full-file bytes compare identically;
changed bodies with the same paths and headers are rejected. This classifies the
declared markers without removing them or asserting absence of arbitrary secrets
or personal data. Broader publication review and full500 submission remain open.

A separate private43-member review-support part retains the later provenance and
portable tools in994,887 bytes. Its copied-input source verifier reproduces all216
classifications with system Python, Git, Zstandard and anonymous upstream HTTP,
without model credentials or Python reads of the original checkout/experiment.
The standard-library offline part checker binds all344 finding classifications
and rejects an altered prior classification even with coherently rebuilt archive
and manifest checksums. These supplementary checks preserve the complete raw
benchmark parts separately and add no model call, native rerun or new score.

The private submission draft now binds all500 IDs to the pinned
`contextbench_verified` metadata and the frozen search population. It distinguishes
HF dataset revision `c2855792b006af41c67202d33883fb9d46362853` from official
evaluator commit `1436c28a8eb95496da4ea69ad458b9f8a8eb7d61`; all29 frozen
evaluator source hashes and the actual issue/Gold table hash reauthenticate.
The draft contains no final results file or submission claim.

The remaining269 first attempts now have a live owner for nine fixed,
sequential batches. Isolated synthetic admissions exercise all269 identities,
source-resource overrides, native controls and known-fee error records. Every
batch partitions each task once; reservation and settlement variants are
idempotent, and insufficient budget, stale metadata, changed controls and duplicate
plans are rejected. The complete inference-dispatch AST is preserved apart from
accounting and explicit native-runtime archiving/transport/status checks. Repair
code stays at`25d4580c`; each case separately archives the pinned four-file native
runtime, whose CLI loads under isolated Python without a provider call.
The prior Pro owner has exited after actual fee closure. Later batches
start only after the preceding phase has closed fees and updated both ledgers
and its handoff; a completion marker is written last. This prevents overlapping
reservations and late metadata writes. The owner waits for actual full-cohort
source/native admission, reserves only existing authorized usage and never
retries a model or replaces a task. The actual first32-task phase has started
under a$160 reservation; the synthetic checks add no native environment or
candidate score. The obsolete original269 controller has exited before this
new exclusive paid intent.
Admission prepares both remaining source cohorts before inference. Actual
Poly116 and Multi153 admission proofs and source/native owner exits now precede
obsolete-owner retirement and the unchanged nine paid phases under a fresh intent.
Poly checks required identifiers against actual parsed test results. Multi needs
transition-aware qualification: its report's base/test-patch results are historical
dataset fields; only the current fix-patch result records the local control run.
The153-case audit identifies21 pure NONE-to-PASS cases and five aggregate-only
test reports. Two observed regression-triggered aborts also require base-only
evidence; their missing IDs receive no execution credit. Expected NONE-to-PASS
absence and a regression abort must remain distinct from a broken test runner.
The original qualifier cannot admit the observed failed original controls. Its
prospective replacement binds completed original or separately corrected pairs
to current test results, original failures and the actual source images. Extra
local base-source executions remain outside candidate metrics and must finish
before the selected153-case proof can close. A file fixture authenticates the
existing fmt18-test result, rejects a synthetic empty current reference despite
its18 historical passes, and verifies that the original test patch plus its
inverse restore the exact base tree in a private Git index. This adds no model or
native test call. Actual ambiguous cases now have local base-only execution
using the unchanged native adapter and test command; their native resolved flag
is excluded from candidate outcomes. All28 actual baselines authenticate before
paid continuation and add no candidate outcome.
A separate zero-model result owner adopts the unchanged231-case index and waits
for nine successive closed cohorts, requiring exactly500 first attempts for the
final result. It authenticates patch, native outcome, final context and unique
search/repair fees for every case, including failures. New Poly/Multi records bind
their archived native-runtime files to the selected authenticated adapters.
The current collector supports five native adapter versions in19 files, plus
separate-stream byte and composition checks. Existing population, failure-
retention and fee functions remain AST-identical. Regression fixtures reproduce
the old digest's rejection of the real v9 runtime and reject changed, missing,
symlinked, old-version and misbound snapshots. Future-record fixtures add no
native-execution or candidate credit.
A separate actual replay reauthenticates all231 original records, preserving the
188/43 outcomes,2,521 unique metered sessions and$141.76314628 allocated usage.
The continuation requires a hash-bound receipt for the previous read-only owner's
exit and unchanged prefix files before acquiring its new exclusive intent.
The original full500 provenance observer remains compatible through its unchanged
index-readiness function. All58 earlier native adapter hashes also match their
already-archived source bytes. Mismatched index/join/evaluation populations remain
rejected. Only actual500-case closure enables the unchanged official final-context
evaluator and complete-system cost aggregator. No full500 evaluation or result
exists yet; raw publication and comparability remain open.
A separate private raw-archive owner waits for the actual500 index, all nine
native/fee phase closures and the paid/result-collection owners' exits. Its prior
231 cases remain four disjoint archive dependencies; the overlapping54-case
source-rebuild supplement adds no candidate attempts. A boundary fixture rejects
the actual231 prefix as500 and a synthetic complete index with a live paid owner.
Failed-output and binary-seed bytes roundtrip through actual split zstd parts;
symlinked inputs and corrupted parts are rejected. Archive completion requires
decoding every actual part and hashing every member. The remaining269 archive
has not completed, and private archival does not approve publication. The new
read-only archive waiter starts after exact retirement of the old waiter and
requires the installed, frozen selected-native transport before acquiring output.
It retains97 actual constructor/probe/recovery scripts, including failed
installation evidence. Actual full153 admission, source inventory and partial231
rejection checks pass; the codec, prior-part and nine-phase native/fee checks
remain unchanged. No archive compression starts until actual full500 closure.
A separate read-only owner waits for that actual full500 index and its closed
official metric record, then replays all1,000 original search workers and confirmed
repair inputs through the unchanged selection compiler. It requires the exact
canonical500 IDs and binds unchanged evaluation-input, first-patch and metric
hashes. The existing readiness predicate rejects the actual122 records when
asked for500; a partial population cannot start the replay. No model request,
native test, official scoring rerun or financial mutation occurs in this owner.
Its full500 verification has not executed yet, and it does not approve raw
publication or comparability.
A separate resource-reuse admission rechecks the existing AutoGPT and Go377
solver images through two actual networkless probes. Both retain the exact
canonical source tree, single-commit history and reference hygiene; the pinned
public tokenizer and bounded Go environment remain available, and both probe
containers are removed with acknowledged creation and verified absence. Eight
configuration mutations are rejected. No image is rebuilt or candidate retried.
The other33 Go environments still require their own resource/native admission.
An actual frozen-image metadata check also reproduces a Multi startup mismatch:
the native image starts at`/home`, while its project is`/home/cli`. A separate
Multi-only builder now accepts`/home` or the exact project startup directory,
while source commands, committed solver working directories and tree/history
checks still use the exact project path; owned-container cleanup is unchanged. All153 generated builder
ASTs differ only in that startup admission. ENV-only Go profiles preserve every
filesystem layer and reject existing nontrivial compiler flags. These corrected
Multi paths are staged; no additional Multi environment or candidate is qualified.
A first additional Verified environment restores the exact canonical source over
the image's single-file `pyproject.toml` setup overlay and verifies clean,
history-free source. Its native neutral control is unresolved and its reference
control resolves, as expected. The original strict UTF-8 decoding failure is
retained; a stream adapter now preserves exact raw bytes and an incremental UTF-8
text view before calling the unchanged pinned executor, parser and scorer.
Timeout flags and duration still come from the official executor. A separate
`scripts/contextbench_verified.py` entrypoint also completes the native reference
fixture without importing repair-model dependencies. It authenticates ten native
source files and binds the task, source, patch, pinned image and owned container;
the solver archives both the grader and transport implementations. The new
transport/grader checks cover split Unicode, invalid bytes, timeout drainage,
cleanup failure and the native-only CLI. These are zero-model environment
fixtures, without additional candidate attempts, API usage or Pass@1 results.

A fixed Go/Multi fixture for `cli__cli-10048` also passes
neutralFalse/referenceTrue after bounding compiler parallelism to its container
CPU quota: `GOMAXPROCS=2` and `GOFLAGS=-p=2`. The derived native image retains
exactly the original filesystem layers, entrypoints, patches, scripts and native
scorer. Its original process-limit failure is preserved. Both fixture containers
are removed. This validates one Go environment, without a candidate repair or a
general claim about all Go tasks.

The Poly native-only CLI optionally accepts `--cl100k-cache-file` for the public
`cl100k_base.tiktoken` resource. It requires SHA-256
`223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`, reads the
bytes once, and copies only that fixed resource into the owned container's
isolated tokenizer-cache directory. The original native image digest and source,
test command, parser and scorer are unchanged; network access stays disabled.
The recorded AutoGPT reference fixture previously fails because this public
cache is unavailable. That original failure remains recorded. With the cache,
`Significant-Gravitas__AutoGPT-4652` passes neutralFalse/referenceTrue using the
same native image; both containers are removed. This option does not download
resources, invoke a provider or add a source/test overlay.

Separate source-only images prepare the same resource conditions for these two
future solver tasks: bounded Go compiler concurrency and the pinned public
tokenizer cache. Real isolated probes verify the exact canonical commit/tree,
clean history-free source and absence of native reference artifacts. The Python
probe loads `cl100k_base` with network disabled. Both probe containers are
removed. No candidate model request has run for either case, and no existing
model prompt, navigation, selection policy, guard or search receipt changes.

The [upstream unified format](https://github.com/EuniAI/ContextBench/blob/1436c28a8eb95496da4ea69ad458b9f8a8eb7d61/docs/process_trajectories.md#unified-format)
separates explored `pred_steps` from final `pred_files`/`pred_spans`. The pinned
mini extractor uses a final `<PATCH_CONTEXT>` block; it does not equate every
shell observation with that final selection. CodeNib's custom unified exporter
uses `retained-search-plus-confirmed-base-source-v1`: retain every frozen search
span, then union authenticated original-source ranges actually delivered through
controlled solver reads/searches. Merge overlapping or adjacent inclusive ranges
without a body-token cap. Repair additions exclude unconfirmed, elided and
modified-file coordinates; arbitrary-shell attribution remains partial. Original
search VCS observations are preserved with their existing disclosure. Selection
reads no annotations and makes no model requests. All500 search-only projections
retain identical selected line sets. The three prior native cases receive
retrospective postprocessing; subsequent repairs use the fixed rule.
The existing77.07% remains a search-policy result. Full-cohort final-selection
metrics and protocol comparability still need verification before submission;
neither custom-format support nor partial observation coverage establishes them.

The checkout-only `scripts/contextbench_submission_results.py` aggregates the
canonical500 final-context report, all500 joined native outcomes and their
search-plus-repair session usage. It requires the exact declared instance set,
retains unresolved cases in the Pass@1 denominator, rejects unknown or duplicate
metering, and computes F1 per instance before averaging. First run the unchanged
official evaluator on the500 selected-context exports; use that report as the
aggregation input. Input provenance, raw-artifact replay and maintainer review
remain separate submission gates. The tool does not rerun native tests or claim
publication approval. Its macro file/symbol/line arithmetic matches all500
frozen search scores. An actual26-case context report with16 joined native
records is rejected without producing score files; it cannot stand in for the
complete500 run.

A final boundary audit found mutable `.git` observations in six candidate tasks,
adding seven predicted non-source lines. None of the Gold paths is Git metadata,
so those seven lines add no direct line-Recall hits. This does not rule out an
indirect effect on worker behavior. Original predictions, metrics and raw bytes
are unchanged. Git metadata is outside the source fingerprint; complete trace
replay from fresh public clones and raw-log publication remain unproven. The
future benchmark boundary rejects explicit VCS metadata paths, including symlink
aliases, and recursive grep exclusions take precedence over caller globs. This
fix does not change the frozen runtime or retroactively clean this run.

A separate local publication copy now redacts workstation labels, opaque SDK
identifiers and local Git identities while preserving all1,000 worker records.
All500 reconstructed predictions, statuses and fees match the originals.
Its ten bounded archive parts round-trip8,645 members; the original fourteen
parts remain unchanged. A before/after hash manifest distinguishes original
receipt commitments from publication bytes. The historical native-control raw
archive is excluded with an explicit manifest disclosure; candidate tasks are
not filtered. No raw logs are published by this PR.

The redacted copy also recomputes every official per-case file/symbol/line metric
on all500 exact, clean source snapshots, with zero model calls. The archived
evaluator has no Git checkout: authenticate its pinned source files and invoke
the unchanged scoring bridge. The checkout-only reproduction entrypoint now
uses that dispatch, covered by12 focused tests including real subprocess
regressions. Complete filesystem-fingerprint replay and fresh-clone entire-trace
portability are separate checks. The source phase has now verified all500
fingerprints and reconstructed all1,000 worker footprints on the existing
checkouts. Its old archived-evaluator dispatch then fails at the Git precondition;
that failed invocation is retained. Recovery proves its decoded evaluation input
exactly equals the already-successful official scoring input, including all Gold,
prediction and source-path fields, and reuses that report without rerunning
source or models. Every case metric matches within an absolute tolerance of
1e-12. This completes existing-checkout source/metric replay with a disclosed
scoring recovery; fresh-clone entire-trace byte portability remains unproven.

The preceding **independent32 complementary-search comparison passes its frozen
Recall/cost gate**. All128 worker sessions and128 configuration positions are
valid, with every source outcome sealed before selected annotations open.
The two-Haiku primary scores **40.00% line Recall/27.18% F1 for$0.499195665**,
versus fresh native Opus **37.91%/31.15% for$6.427971300** on the same32 tasks.
It uses7.77% of Opus's nominal search-stage cost while losing3.96 F1 points. The Recall
difference is+2.09 points, paired repository-alias95% interval[-1.91,+6.48];
stable superiority to Opus is not established. This gate authorized the unchanged
primary's canonical500 run. The default-only, language-balanced population differs from
verified500;40.00% is not a leaderboard score or directly comparable to the
72.83% native verified500 control.

**The latest search-pair fees cover context search only.** Both sides of the
7.77% comparison include their recorded search-session usage and SDK auxiliaries;
neither includes a downstream repair solver. The completed500 batch has the same
search-only scope. These are model-reported usage values, not complete task
resolution costs or reconciled invoices; compute costs remain separate.

The completed search batch authenticates and unions source intervals actually
observed by its two workers; it has no repair stage. The checkout-only
`scripts/benchmark_context_solver.py --search-pair` adapter now connects those
receipts to the existing mini SWE-agent loop through version-bound navigation.
Initial source is empty: the solver receives filenames, line ranges and handles,
then pulls bodies through the existing controlled read/search commands. The
navigation token limit does not cap source bodies read later.

`--total-meter-usd` is required for this adapter. Actual search-session usage is
subtracted before setting the repair SDK guard. The `--initial-context none`
control receives neither source bodies nor navigation and excludes the unused
search stage from its modeled system cost. Reused search receipts are labeled;
their usage must not be added a second time to cumulative experiment spending.
The guard is not an account-level cash cap. Saved handoff hashes, confirmed
navigation delivery and a whole-run session/fee report preserve unknown usage,
reject repeated SDK sessions and include stopped repair attempts. Per-session
billing routes distinguish API usage from subscription valuation. Native grading
also exports a joined result only after task, patch, solver and fee receipts
match; its native outcome remains separate from the partial source-attribution
coverage.

Subscription CLI runs also require a subscription init and an allowed quota
event explicitly confirming included Max usage with paid overage disabled.
Missing or unsafe receipts prevent release of the next shell action and stop
further inference; a terminal fee remains recorded even if a later event rejects
the route. The API pilot above uses its separately confirmed API-key route.

Local verification exercises the real pinned mini loop and local shell with
scripted SDK replies: navigate, read source, edit, read the changed source and
execute a fixture assertion before submission. Navigation adds no source credit;
modified-file reads add no guessed original-coordinate credit. The original
solver path also retains its metered-budget-stop behavior. A read-only handoff
check reconstructs both original workers on one completed500 case without new
models or annotations. These checks establish wiring, not repair quality.

Native grading remains separate. Validated environments include the admitted
Verified tasks, Multi fmt/Gson/ponyc and one Go fixture, the Poly Svelte and
AutoGPT fixtures, and one Pro v1 NodeBB fixture. This is not full500 native environment coverage.
Other task records still require their source and native controls before
candidate dispatch. Source attribution is still partial for
arbitrary shell output. The three-case pilot above adds real candidate repair
outcomes; complete-system Recall/F1, independent/full500 Pass@1 and an end-to-end
cost advantage remain unestablished.

The Poly adapter authenticates the complete native CSV at dataset revision
`d56445f9940eae4e9d2974ec66820c2f1d7754e6`, then selects the exact original
task/repository/base commit. Its separate grader uses the
[pinned official harness](https://github.com/amazon-science/SWE-PolyBench/tree/9c836c5d7f3cb991934132b77d29e6941d912a07)
patch application, parser and scorer, with an already-local image digest and
bounded container ownership. Native patches and tests never enter solver inputs.
The `grade` subcommand takes the official native `test.csv` as `--dataset`, plus
`--native-upstream` and `--native-image`; it does not pull images or fall back to
building a different environment.

Fresh real-container controls observe both required F2P tests and all598 P2P
tests. The test-only control fails the two F2P tests; the reference passes them.
Four other browser-mock tests fail identically on the untouched base and the
reference, outside the native F2P/P2P sets. The initial stricter zero-exit
readiness gate remains failed. The official scorer nevertheless resolves the
reference and rejects the test-only control; the adapter preserves both the
official judgment and raw exit codes instead of treating a nonzero process exit
as an automatic resolution failure. Positive resolution also requires a terminal
native log, all required test identities and completion inside the declared
native-call wall guard. Timeout/partial-log cases retain the raw scorer output
but remain incomplete errors; they cannot supply a positive Pass@1 outcome.
Actual CLI environment fixtures reproduce
those positive/negative judgments with exact patch and cleanup receipts. These
are reference controls, not model-produced repairs or candidate Pass@1.

A separate local solver image restores the exact clean source tree, retains one
source commit and removes source reflogs, remotes and known reference artifacts.
The completed search pair reconstructs into four metadata-only navigation entries
for this task. The real pinned mini Docker environment also executes CodeNib
read/search commands there: read bytes match the original commit, and container
removal is confirmed. No model receives these probe outputs. A portable source
image builder and broader Poly environment validation remain open; this local
image is not an available image for every Poly task. The Poly readiness change
passes147 checks, including four pinned mini-loop fixtures, with no provider
requests. The later three-case repair pilot is accounted separately above.

An end-to-end comparison must join search, source handoff, repair and native
grading by the same task, base commit and frozen configuration. Record source
actually delivered to model requests, subsequent reads and edits, the final
patch and native test outcomes. Report search-only retrieval metrics separately
from retrieval metrics reconstructed from the complete system trajectory, plus
real Pass@1. Count every participating model session once in total cost,
including search workers and repair auxiliaries; missing usage remains unknown.
Use common declared model and budget limits for the paired system comparison,
and retain failed or stopped attempts in their fixed task denominators.

The earlier model-only router remains rejected:44.11% Recall versus native
Haiku45.17% and Opus49.86% on its separate closed32. The exposed-eight
complementary-search result,71.62% Recall versus Opus70.56%, remains development
evidence. Neither historical result is replaced or relabeled by the new cohort.

CodeNib is the system under test: native source tools, agent execution, source
delivery and model routing are CodeNib system configurations. Embedding and graph
are optional mechanisms, not the definition of the system. The native agent
results below therefore belong to CodeNib. Distinguish system-level Recall,
F1, cost and latency from claims that one particular index, graph or context
projection caused an improvement; the latter require matched component ablations.

The latest **cheap-search callable-tail guard fails its Recall screen**:
67.14% Recall/41.03% F1 versus native Haiku68.07%/36.66%, on the same eight
exposed tasks. Its five interventions deliver59 new tail lines but add only
0.025 Recall points within those executed trajectories. It is not expanded.

The **eight-task file/function-outline development policy fails**:
line Recall/F1 are **69.40%/37.35%**, versus **69.91%/39.66%** for the exact
original native controls. Seven agents query CodeNib outlines, one fetches a
body, and mean per-task maximum provider input rises15.58%. This policy is not
promoted or expanded to500. All arms retain all eight tasks and score8/8 valid.

The earlier indexed surface-first policy retains its separate **four-task
positive development screen**,74.02% Recall/42.40% F1 versus original native
71.61%/33.68% and one native repeat71.37%/31.76%. These are different policies
and exposed task sets; neither establishes an independent or stable indexed
context gain. The separate eight-language dense-index study fails preparation
with7/8 ready before any agent call. Ordinary agent source tools remain the
working baseline; embedding/graph are not required by the current scoreboard
path. Further enhancement experiments need a concrete retrieval failure and a
measured benefit, rather than component use alone. Named-symbol/source-discovery/
document-admission production fixes remain separately verified dependencies.

A subsequent **native Haiku5.5 model baseline** on the exact same eight tasks
scores68.07% Recall/33.80% precision/36.66% F1, versus native Opus5.5's
69.91%/35.47%/39.66%, with8/8 valid in both arms. All eight source outcomes seal
before current known annotations reopen. This uses the same ordinary source
tools, prompt,20turn limit, medium effort and4096 output setting, without indexed
context or a router. The Haiku SDK guard is$0.08 per case, with observed acceptance
up to$0.15 and a$2 batch guard; every case completes below that guard.

| Same eight exposed tasks, ordinary source tools | Line Recall | F1 | All-model API/list-price usage estimate |
| --- | ---: | ---: | ---: |
| Opus5.5 | 69.91% | 39.66% | $1.4261087 |
| Haiku5.5 | 68.07% | 36.66% | $0.043406125 |

Haiku loses1.84 Recall points and2.99 F1 points while using96.96% less nominal
API/list-price value. This is a candidate low-cost endpoint on this exposed
sample: a **CodeNib low-cost native agent configuration**, not evidence of an
incremental indexed-context/router gain, independent result or500
score. Haiku observes4,250 source lines and65,606 tool-reply cl100k tokens versus
3,981/57,004 for Opus; lower model cost does not imply fewer source tokens. The
[case-level CSV](../assets/contextbench_haiku_native_development_8.csv) retains
all16 outcomes. Haiku is lower in TypeScript/Go, higher in Python/JavaScript/C
and equal in C++/Java/Rust; these retrospective labels cannot be runtime routing
inputs or justify language-specific outcome selection.

Both cost estimates retain all terminal model usage, including auxiliaries,
and their model subtotals match terminal totals. The original Opus route is Max,
while the new Haiku route is API-key usage: compare captured API/list-price
valuation, not subscription cash-zero against an API invoice. Account cash/credit
balance remains unknown. Official Haiku pricing changes above100,000 prompt
tokens; [published rates and constraints](https://platform.claude.com/docs/en/models/haiku-5-5/overview)
are checked before the first new inference, and the account's model-metadata API
confirms the exact model ID without an inference call. SDK loop mean/median is
32.85/31.68seconds for Opus and21.86/21.26seconds for Haiku. This excludes the fresh
source-fingerprint preparation and is not a matched cold-start latency study.

Haiku's terminal same-model usage also contains CLI auxiliary work; all eight
fail the main-response-only input reconciliation check. Preserve that incomplete
attribution and make no complete main-context maximum claim. The all-model
terminal usage/costs remain known. No missing counters become zero.

The native Haiku eight API sessions use$0.043406125 in recorded SDK/API usage value, with
zero unknown terminal fees. The unused$1.956593875 of the prospective$2 reservation
is released. This is a CodeNib model-configuration cost candidate; incremental
router value is not yet established.

A subsequent **bounded callable-tail Stop guard fails its Recall screen** on
these same eight exposed tasks. Runtime `d998fa25`, protocol `e39ffad3`, registers
one explicit SDK Stop intervention. It requires a confirmed native read with a
continuation footer and an already-read callable signature, then returns only
the remaining location, at most80 lines. It parses at most16 authenticated
files with a2MiB per-file parse cap; oversized files are explicit coverage gaps.
The existing reader still authenticates complete file bytes. Native
tools/prompt, Haiku model, medium effort,20turn/4096output and case guards stay
fixed. There is no index, embedding, graph, Gold/language routing or repair call.
All eight source outcomes seal before current known annotations reopen; all
eight execute and score valid, with no retry or replacement.

| Same eight exposed tasks | Line Recall | F1 | All-model API/list-price usage estimate |
| --- | ---: | ---: | ---: |
| Opus native, original control | 69.91% | 39.66% | $1.4261087 |
| Haiku native, original control | 68.07% | 36.66% | $0.043406125 |
| Haiku + callable-tail guard | 67.14% | 41.03% | $0.048852250 |

The guard arm loses0.94 Recall points versus Haiku native, while F1 rises4.37
points and nominal usage rises12.55%. Recall remains2.77 points below Opus.
It fails the declared Recall-priority screen and the target of matching Opus
Recall at half its nominal usage; it is not promoted to independent32 or500.
The [24-row case CSV](../assets/contextbench_continuation_development_8.csv)
preserves all three arms, including unchanged controls.

Five cases receive a single metadata intervention and actually read all59
requested new tail lines:30 Python,15 JavaScript,1 TypeScript,3 C and10 Java.
The TypeScript tail is only a closing delimiter; structural completeness is
not task relevance. A post-hoc official evaluation of the same executed
trajectories before their first intervention versus final source shows only
0.025 Recall points added, entirely in C. This is observed within-trajectory
coverage, not a causal comparison against a no-hook run. Other between-run
score changes, including the F1 increase, cannot be attributed to the guard.
The CLI emits `stop-hook-error` notifications for the deliberate block; all
five are followed by actual requested reads, and guard source-failure flags
are false. The metadata itself receives zero unread-source credit.

Guard-arm source lines/reply estimates total3,600/57,508, versus4,250/65,606 for
Haiku native; this descriptive difference does not establish a compression
effect. SDK loop mean/median is30.33/23.86seconds versus21.86/21.26. These exclude
fresh source preparation and are not matched cold-start timings. All terminal
model subtotals reconcile with fees, while all eight Haiku main-response input
attributions remain incomplete due to same-model auxiliary usage. No complete
main-context maximum or physical KV saving is inferred.

The eight new sessions use$0.048852250, with zero unknown terminal fees; the
unused$1.951147750 of their$2 reservation is released. Known program API usage
is now$59.186247105 under$100, leaving$40.813752895 with no active reservation.
No repair or fresh default-only task is opened. The next hypothesis is
task-relevant coverage review and selective strong-model search, evaluated
against **both** ordinary model baselines with all parent/child costs. It
is now implemented in a separate experiment; no router benefit, independent validation
or full500 expansion is established.

An engineering follow-up fixes a separate long-anchor delivery defect in
`explore_context`. Previously, a correctly ranked long callable was clipped to
its first40/80/120 lines before query projection. The independent main-based
[#838](https://github.com/sysevol-ai/CodeNib/pull/838) selects the strongest
exact-token line from authenticated live source inside that anchor and returns
at most the original profile's line/character budget. Sequential `read_source`
is unchanged. Scope, inspected end and omitted-line metadata remain explicit;
stale source cannot turn indexed fallback text into verified evidence.

Runtime `33e48384` passes66 focused experiment-consumer checks. Its independent
main-based copy passes298 MCP/projection checks with7 skips. A zero-model replay
holds the ranked hit fixed on unchanged synthetic300-line and actual MUI source.
For `groupBy groupedOptions reduce`, MUI fast mode changes from80–119 to785–824;
all three budgets now include the reducer terms. This demonstrates source
delivery after localization, not a ranking or agent-quality gain. The prior
native Haiku arm does not call this operation, so this defect does not explain
its scores. Original failed observations remain intact; no Gold, evaluator,
model call, new prospective task or API usage is added. The source repair is
separate from the still-unmeasured cheap-search/evidence-reviewer policy.

The source repair's required unit CI now succeeds at the exact independent
head `f5f02935`; [#838](https://github.com/sysevol-ai/CodeNib/pull/838) remains
unmerged. A subsequent **same-session evidence reviewer is implemented and
locally verified; its first live batch stops at an integration failure**.
Runtime `93d5c79a` adds an
explicit experiment-only SDK argument. At the first eligible Stop, CodeNib
deduplicates complete source lines already confirmed in the agent's inputs,
reauthenticates them through the retained source reader, and supplies stable
snapshot-bound handles together with the issue, handoff and search metadata.
It never fills in unread source. Both view/reader locks are released before
network work. One tool-free Opus review may suggest at most two grounded
search questions; a Stop block asks the same native search agent to continue
in its existing history. Only subsequent actual tool delivery adds source credit.

The one-attempt latch precedes any await and the hook session must match the
SDK init receipt. A free provider token count admits at most one paid review
under a request forecast, with no retry or redirect. Packet/budget/no-evidence
deferrals retain the native outcome explicitly. A capped review is prospectively
a metered no-action outcome, even if its text parses. Malformed replies, unknown
handles/models/usage and stale source reject the attempt after the main SDK
terminal is drained; known fees remain and unknown fees never become zero.
Main/auxiliary SDK usage and direct reviewer usage stay separate, with a combined
estimate only when both are known. Source is checked again after continuation.

All23 new mocked HTTP/source/SDK tests and116 focused checks pass, including
concurrent callbacks, source mutation and known/unknown terminal-fee retention;
tests make no billed calls. The subsequent separately frozen same-eight live
batch makes two search/review attempts, then stops: Python returns a single
Markdown-fenced JSON decision which the strict adapter rejects; JavaScript
validly stops without continuation. Six tasks remain unclaimed. Preserve the
one failure, one valid attempt and six unclaimed zeros in the selected-eight
denominator; this is not a usable strategy-quality estimate. All outcomes seal
before existing labels reopen. Main/auxiliary SDK usage is$0.009603920 and direct
Opus reviews are$0.176708, totaling **$0.186311920**, all known. The$3 reservation
closes and releases$2.813688080. No original outcome is retried or reinterpreted.
Offline presentation normalization validates the two cited Python gaps but adds
zero source or quality credit.

The first **CodeNib Haiku-router** integration batch at `f40a2f31` also stops
after two attempts, retaining six unclaimed tasks. Python's native observations
include a file outside the retained source inventory, so packet authentication
fails before a reviewer request. JavaScript pays only for its Haiku gate; the
full-packet Opus count exceeds the prospective$0.15 review forecast and defers.
Known main/auxiliary/gate usage totals **$0.013565555**, with zero Opus reviews
and no unknown fees. Its$3 reservation closes with$2.986434445 released. This is
another failed integration screen, not evidence against the agent strategy.

The local successor keeps uncaptured files as explicit coverage gaps, never
verified bodies, without widening source authority. A Haiku gap gate selects
already-seen source by existing handles for the optional Opus review; other
seen ranges remain labeled locators. Subsequent search stays in the original
native agent session. A single whole-response JSON fence is accepted, while
prose extraction, malformed objects, duplicate keys, unknown handles and capped
decisions remain rejected. All48 review/router cases and141 focused checks pass
without billed calls. The old failures remain immutable. Both native controls
remain required; specific context-selection attribution needs a matched ablation,
whereas the whole router policy is a CodeNib system configuration. Independent32/
500 and leaderboard gates remain open.

A separate successor at `b0256f8f`, protocol `2963114d`, now completes and seals
all eight source outcomes before existing annotations reopen. All eight execute
and score valid, without retry or replacement. Before dispatch, a zero-model
replay authenticates all eight cached native histories plus the two stopped
router histories; missing authority is explicit, never restored as source.

| CodeNib agent configuration, same eight exposed tasks | Line Recall | F1 | All-model nominal usage |
| --- | ---: | ---: | ---: |
| Opus native control | 69.91% | 39.66% | $1.4261087 |
| Haiku native control | 68.07% | 36.66% | $0.043406125 |
| Haiku + selected-evidence late router | 68.90% | 41.14% | $0.113543820 |

Seven cases stay Haiku-only; TypeScript pays for one Opus review of seven
selected prior source entries with42 other seen ranges retained as locators.
The strong review also says stop. **No case receives a Stop block or reads
additional source after review.** The observed0.83 Recall-point/4.48 F1-point
difference from Haiku native cannot be assigned to a route that caused no
continuation. The policy costs161.58% more than Haiku native and remains1.01
Recall points below Opus, although nominal usage is92.04% lower than Opus.
Both the actual-intervention screen and the target of matching Opus Recall at
half its nominal usage fail. Do not expand this late-critic policy to32/500.

All fees reconcile: main/auxiliary SDK usage totals$0.038414820, and direct
gate/strong reviews add$0.075129000. All eight main-response-only input audits
remain incomplete due to same-model CLI auxiliary usage; do not infer complete
main-context savings. Observed source/reply estimates are3,542 lines/56,303
cl100k tokens versus native Haiku4,250/65,606, with no compression attribution.
SDK-loop mean/median is30.18/24.11seconds versus native Haiku21.86/21.26; this is
not a matched cold-start study. C additionally takes37.27seconds for source
preparation and115.86seconds total, including terminal source checks.

The [40-row case CSV](../assets/contextbench_agent_router_development_8.csv)
retains both native controls, both stopped integration cohorts, all failed/
unclaimed zeros and the complete successor. This is a CodeNib system experiment;
it does not require an embedding/graph gain to belong to CodeNib. It also supplies
no evidence that late self-review is better than simpler native search. The
next agent hypothesis should route work to an actual search worker, rather than
paying to judge only the evidence already found. A new prospective protocol is
required; no new independent task or repair batch starts here.

The successor's$3 reservation closes with$2.886456180 released. Including both
preserved failed integration attempts, this iteration adds$0.313421295 known
API usage. Program usage is **$59.499668400/$100**, leaving$40.500331600, with
no active reservation or unknown fee from these three batches at their closure.

### Before-search worker routing and independent system comparison

The successor at `0fc02a85` routes before source search. A single tool-free
Haiku request sees only the original issue and chooses one actual Haiku or
Opus native search worker. The selected worker receives the unchanged native
prompt, read/grep/glob tools, medium effort,20turn limit and4096 output cap.
Each model keeps its native SDK guard: Haiku$0.08 or Opus$0.75. The router uses
low effort,768 output tokens and a$0.01 request forecast. Known-fee capped or
malformed decisions default to Haiku without a retry; unknown identity, usage
or source integrity rejects the attempt. Router reasons are metadata, do not
enter the worker prompt and receive zero source credit.

Protocol `712569fe` seals all eight new source outcomes before reopening these
already-exposed annotations. All8 execute and score valid, with no replacement.

| CodeNib system configuration, same exposed eight | Line Recall | F1 | All-model nominal usage |
| --- | ---: | ---: | ---: |
| Opus native control | 69.91% | 39.66% | $1.4261087 |
| Haiku native control | 68.07% | 36.66% | $0.043406125 |
| Before-search router, all eight workers Haiku | 71.04% | 40.17% | $0.044898795 |

The new SDK main/auxiliary usage is$0.043736595 and routing adds$0.001162200.
No Opus worker is selected. The2.96 Recall-point difference from the original
Haiku run is a stochastic between-run difference; it cannot demonstrate
selection of a better model for difficult issues. The prospective mixed-worker
screen fails. Never force an Opus quota after seeing these outcomes. The cheap
CodeNib agent configuration remains worth independent evaluation. These are
system results even when no indexed context, embedding or graph is exercised.

All24 rows in the [worker-router case CSV](../assets/contextbench_worker_router_development_8.csv)
match immutable per-case metrics and all-model fees. SDK-loop mean/median is
22.59/20.84seconds, versus native Haiku21.86/21.26. Source observations total
3,867 lines and62,090 cl100k tool-reply tokens. Complete main-input attribution
remains unknown in8/8 because of same-model CLI auxiliaries; lower input or
compression costs are not established. The$4 reservation closes, releasing
$3.955101205. Known cumulative API usage is **$59.544567195/$100**; these are
captured nominal usage values, not a reconciled cash bill or credit balance.

Independent design `cab3bc5a` freezes native Haiku, native Opus and the unchanged
before-search router before opening new issues. Metadata-only selection
`059fec72` chooses32 default-only tasks, four per language across23 repository
alias clusters, at most two per cluster. IDs, original issue identities and
exact source commits exclude known exposures. This is outside verified500 and
has different language weighting; it is not a new official leaderboard sample.
All32 exact sources are prepared before inference. Execution protocol `63d57a66`
completes96/96 valid worker runs with no retries or replacement, plus32 issue-only
classifier calls. Source seal `64b5eac1` precedes selected annotation access.
Actual routing is29 Haiku/3 Opus, with model identity, execution and all-model
fees reconciled. Compare all three on the fixed32 denominator:

| CodeNib system configuration, independent32 | Line Recall | Precision | F1 | All-model nominal usage |
| --- | ---: | ---: | ---: | ---: |
| Native Haiku | 45.17% | 27.66% | 28.63% | $0.252892905 |
| Native Opus | 49.86% | 32.15% | 33.58% | $6.218777900 |
| Before-search router,29 Haiku/3 Opus | 44.11% | 26.94% | 28.40% | $0.898562665 |

The [96-row case CSV](../assets/contextbench_system_agent_independent_32.csv)
and [machine-readable summary](../assets/contextbench_system_agent_independent_summary.json)
reproduce all individual and aggregate line metrics and fees. Paired
repository-alias-cluster bootstrap intervals use23 clusters,10,000 draws and
seed20261018. Router minus Haiku Recall is **-1.06 points**, with an unadjusted
95% interval **[-3.49,+1.71]**; Haiku minus Opus is **-4.68 points**, interval
**[-9.67,-0.42]**. The router has no demonstrated gain; its point estimate is
dominated by Haiku on Recall and cost. Haiku and Opus are nondominated among
these three configurations only. Every predeclared quality gate fails, including
Haiku within two Recall points of Opus at at most25% of its usage. Do not expand
this failed router policy or reinterpret the gates.

A retrospective decomposition of the same frozen outcomes separates model
choice from same-model repeats. The29 routed Haiku searches contribute
-1.5012 Recall points relative to the native Haiku controls. The three routed
Opus searches contribute -0.9006 points relative to their native Opus controls;
choosing those three native Opus controls instead of Haiku would contribute
+1.3419 points. These sum to the observed -1.0599 points. This algebra uses
separate stochastic searches and is not causal identification or evidence that
a live router can realize the offline composition. It does not justify saying
that all loss is misrouting or that randomness proves a benefit.

Post-seal source diagnostics retain the official normalized paths, all32 tasks
and every original denominator. Across the cohort only140 of22,359 annotated
lines are unavailable at the base source; mean per-task source reachability is
99.06%. Those boundary issues cannot explain the low Recall. Partitioning every
Gold line also reconciles all96 observed-source intersections with the official
reports. Native Haiku's task-macro Gold fractions are **45.17% observed,
37.16% unread in files with some observed source,16.73% in unlocated files,
and0.94% unavailable**. A file may have only one grep line observed. This is a
descriptive retrieval gap, not an achievable-gain estimate or permission to
inject whole files. It motivates testing candidate-block selection and on-demand
reading; closed32 tasks stay excluded from future independent validation.

SDK-loop mean/median is31.82/30.51seconds for Haiku,39.22/41.30 for Opus and
32.14/28.85 for the router. These exclude Python imports and initial CLI/auth
checks, and do not establish a controlled cold-start comparison. Main-input
reconciliation remains incomplete in32 Haiku and29 routed Haiku runs because
of same-model auxiliaries; all terminal main/auxiliary fees are known. Lower
fees do not establish context compression. New nominal API usage totals
**$7.370233470**; the$16 reservation is closed, releasing$8.629766530 of budget.
Known cumulative usage is **$66.914800665/$100**, leaving **$33.085199335**
with no active reservation or model job. This is not an invoice or credit-balance
reconciliation. No repair batch starts.

An offline complementarity audit reuses the two completed Haiku searches on
the same eight exposed development tasks. The first reads4,250 unique lines,
the second3,867;2,624 overlap and their union contains5,493. The second contributes
1,243 lines absent from the first, so repeated cheap search can find different
source. The exact interval union is verified against both actual observed sets;
no new source is invented. The pinned evaluator scores this hypothetical union
at **73.54% Recall/34.48% F1**, versus68.07%/36.66% and71.04%/40.17% separately.
It adds2.50 Recall points over the stronger run while reducing F1 by5.68 points.
Both historical runs together record$0.088304920, including the second run's
router overhead. This audit makes zero new model calls and opens no new labels.

The [eight-row opportunity CSV](../assets/contextbench_haiku_pair_opportunity_8.csv)
reproduces every union score, source overlap and recorded fee. **This is not an
executed multi-worker policy:** it combines separate historical runs, without
a joint orchestration/assembly run or independent validation. Source actually
viewed by CodeNib workers counts as system retrieval; a parent model is not a
prerequisite for that metric. Concatenated steps here are an offline representation,
with no efficiency or latency claim. A future policy needs a live execution,
complete worker/source receipts, a declared return contract and all-worker costs.
Claims about a repair consumer need its actual handoff and native outcomes.
This offline eight-task audit does not change the completed32 results or gates.

### Live two-worker source search on the exposed eight

Runtime `71e489e5` and protocol `f2bba747` turn the earlier complementarity
opportunity into actual execution on the same eight exposed development tasks.
Every task runs one common first Haiku search, an independent Haiku repeat,
a complementary Haiku search and a fresh native Opus control. Secondary worker
order is fixed by a pre-run hash. Both two-worker configurations use the same
first receipt, source assembler, native read/grep/glob tools and per-worker
medium effort,20-turn/4096-output limits. Native Haiku/Opus SDK guards remain
$0.08/$0.75. All32 actual workers and32 configuration positions are valid;
every source outcome seals before these eight known annotations reopen.

The complementary worker receives the unchanged original issue plus at most
2,048 cl100k tokens of source-version-bound navigation metadata: filenames,
observed line intervals and stable handles. **This is a metadata cap, not a
source-body budget.** The first worker's bodies and generated explanation are
not copied into the second model window. The worker reads required source with
the ordinary tools; navigation itself adds zero source credit. The shared
assembler verifies one source identity and unions only authenticated observed
intervals. A parent model does not reread the union. This is a live CodeNib
system policy, without a new embedding, graph, Jev request or repair batch.

| CodeNib system configuration, same exposed eight | Line Recall | Precision | F1 | All-model nominal usage |
| --- | ---: | ---: | ---: | ---: |
| One native Haiku search, common first worker | 68.83% | 25.48% | 32.26% | $0.046606145 |
| Two independent Haiku searches | 70.79% | 23.03% | 30.17% | $0.085153860 |
| Haiku plus metadata-seeded complementary search | 71.62% | 22.10% | 29.29% | $0.100641385 |
| Fresh native Opus | 70.56% | 35.75% | 39.69% | $1.119668300 |

The [32-row case CSV](../assets/contextbench_search_pair_development_8.csv)
and [machine-readable summary](../assets/contextbench_search_pair_development_summary.json)
reproduce every per-case metric and configuration fee. Both pair configurations
pass the prospective development target: Recall within two points of Opus and
usage at most25% of Opus. The complementary pair costs8.99% of Opus and its
Recall is1.06 points higher on these eight, while F1 is10.40 points lower.
This is a measured Recall/cost tradeoff, not evidence of better repair quality.

Summed per-task unique observed source is4,992 lines for the first Haiku worker,
5,839 for the independent pair,6,548 for the complementary pair and3,686 for
Opus. Counting each worker's own reads before cross-worker dedup gives8,980
lines for the independent pair and10,527 for the complementary pair, including
3,141 versus3,979 repeated lines. The full cohort therefore does not establish
fewer source reads, better context density or less repetition from metadata.
The demonstrated opportunity is affordable extra coverage; improved selection
quality still needs evidence.

The complementary-search policy increment over the independent pair is **+0.83 Recall
points**, exploratory paired-repository95% interval **[-3.69,+5.26]**. Against
Opus it is **+1.06 points**, interval **[-4.51,+6.65]**. Intervals use eight
repositories,10,000 draws and seed20261018. Do not claim stable metadata routing
or global Pareto optimality from these exposed tasks. The declared development
point-score screens pass. The previous failed model-only router stays rejected.

### Independent32 complementary source search

Independent design `bcaf5253` freezes these unchanged policies before
metadata selection `34023146` chooses32 new default-only tasks: four per language
across22 repository-alias clusters, at most two per cluster. Selection excludes
verified500 and all closed cohorts/issue/source aliases. The metadata-only pool
has591 eligible tasks before selection and559 after conservatively reserving
these32. All32 exact-source preparations pass with empty issue and Gold fields
before selected issue-only projection. Execution protocol `914a4a4f` completes
all128 SDK searches with the unchanged development parameters. Four synthetic
postprocessor fixtures verify source-before-Gold ordering and reject unaccepted
source, unknown fees and duplicate positions before annotation projection.
All128 source outcomes seal before the selected32 Gold projection. All32 tasks
remain in each denominator; there are no retries, replacements or repairs.

| CodeNib system configuration, same independent32 | Line Recall | Precision | F1 | All-model nominal usage |
| --- | ---: | ---: | ---: | ---: |
| One native Haiku search | 33.98% | 27.44% | 26.53% | $0.250863650 |
| Two independent Haiku searches | 38.82% | 26.19% | 27.82% | $0.480771855 |
| Primary: metadata-seeded complementary Haiku pair | 40.00% | 24.14% | 27.18% | $0.499195665 |
| Fresh native Opus | 37.91% | 33.08% | 31.15% | $6.427971300 |

The [128-row case CSV](../assets/contextbench_search_pair_independent_32.csv)
and [machine-readable summary](../assets/contextbench_search_pair_independent_summary.json)
reconcile all per-case and aggregate file/symbol/line metrics, validity and fees.
The primary passes both the predeclared Recall-within-two-points/at-most25%-cost
gate and the stronger point target of at least Opus Recall for lower cost.
Recall improves6.02 points over the common native Haiku,95% interval[+3.08,+9.56].
Against the independent pair it improves1.17 points, interval[-2.41,+5.12];
against Opus2.09 points, interval[-1.91,+6.48]. Intervals resample22 repository-alias
clusters10,000 times with seed20261019, without multiplicity adjustment.
The latter two intervals cross zero: neither stable Opus superiority nor stable
complementary-policy gain over a simple repeat is established. Only the
predeclared primary is eligible for500; a secondary is not substituted after scoring.

The primary observes30,640 unique lines, versus28,117 for the independent pair,
21,362 for one Haiku and24,341 for Opus, summed per task. Cross-worker repetition
is16,234 versus14,322 lines. This remains an affordable-coverage result, not fewer
reads, better context density or context compression. Every one of128 source
intersections reconciles with the official reports. Base-source reachability is
98.15% on average;290 of22,306 annotated lines are unavailable. These diagnostic
lines remain in the original denominators and do not change scores or gates.

The actual128 distinct sessions add **$7.157075170** to the program ledger, counting
the common first worker once. Close the$12 reservation, releasing$4.842924830 of
budget. Known cumulative API usage is **$75.330733235/$100**, leaving
**$24.669266765** with no active reservation at this closure. Weighting four cost
samples per language to verified500 gives an arithmetic primary search estimate
of **$5.905410835**; using each language's observed maximum gives$8.702784340.
Neither is a guaranteed cap or includes repairs/compute. A500 run needs its own
frozen guard and complete live source validation. Existing500 source/control
readiness verifies1,499 original files and500 matching Git commits; this does not
replace complete live source authentication or launch inference.

All terminal main/auxiliary fees are known. Main-input attribution remains
incomplete for all96 Haiku sessions; all32 Opus sessions reconcile. SDK worker
durations are retained without a global ensemble turn order/AUC or controlled
cold-cache latency claim. Usage is not a reconciled invoice or credit balance.
Native issue-resolution outcomes and official submission remain open.

The scout-versus-repeat comparison changes both the navigation handoff and the
instruction to seek complementary evidence. It evaluates that whole policy;
it does not isolate the causal contribution of metadata alone.

### Canonical500 search-pair execution and reproduction

Design `aabbd7e3` freezes the passing primary for the same canonical500 IDs,
order and source commits as the native control. This is an already-exposed
population, not a fresh independent500. The policy and runtime remain unchanged:
one first Haiku worker followed by one metadata-seeded complementary worker,
eight concurrent cases, at most1,000 SDK sessions, no retries, replacement tasks
or repairs. The execution guard is$12 within the program's$100 API allowance;
the guard is a dispatch limit rather than an account-level cash cap. Every
source snapshot must authenticate before issue-only inputs and execution freeze.
All500 configuration positions and1,000 worker positions must seal with known
fees before the same500 annotations reopen. All500 source preparations passed
with zero errors. Execution protocol `3e20eb64` has finished: all500 cases score
valid, all1,000 actual SDK fees are known, and all source positions sealed before
the same500 annotations reopened. Line Recall is77.07%, Precision20.52% and
F1 28.16%. Actual search usage is$5.680771745; the$12 reservation is now closed
and both existing budget views reconcile to$81.01150498 of known program API
usage. Original native-control failures, unknown fees and annotation-access
timing remain disclosed. No new-pair native repair outcome is measured.

`scripts/contextbench_reproduce_search_pair.py` replays archived observations
and the pinned official evaluator without model requests. It verifies each
extracted member and all1,677 frozen runtime source files, audits clean pinned
repositories and their full source fingerprints, reconstructs both workers'
actual observed intervals, and checks every candidate file/symbol/line metric.
Only derived path labels change when repositories move; source text and original
evidence stay unchanged. `--repo-map` accepts existing task-to-repository paths;
`--repos --prepare-repos` instead fetches exact public commits and declared
submodules. The latter needs Git/network access, but makes no model requests.
The six Git-metadata cases described above are an explicit limitation: Git
internals are not pinned source, so source fingerprints alone cannot establish
their byte-identical replay on fresh clones. Strict replay must report a mismatch
rather than silently drop those observations or replace the original score.

After the local archive is complete and reviewed for publication, extract its
bounded parts in filename order into an empty directory. The original archive
index and verification receipt must accompany the extracted members:

```bash
mkdir /path/to/extracted-bundle
gzip -dc /path/to/archive/logs.tar.part*.gz | tar -xf - -C /path/to/extracted-bundle
gzip -dc /path/to/archive/trajectories.tar.part*.gz | tar -xf - -C /path/to/extracted-bundle
cp /path/to/archive/archive-index.json /path/to/archive/verification.json /path/to/extracted-bundle/
python scripts/contextbench_reproduce_search_pair.py \
  --bundle /path/to/extracted-bundle \
  --repo-map /path/to/task-repositories.json \
  --jobs 4 \
  --evaluator-python /path/to/pinned-evaluator-env/bin/python \
  --output /path/to/new-reproduction-output
```

`--jobs` selects1-16 independent source-replay workers and defaults to1. Results
retain protocol order even when checks finish out of order. A failed check
cancels queued work; official scoring runs once after every source replay passes.

The bundle records original Python/package inventories, pinned replay/evaluator
requirements, source-preparation code and evaluator sources. Fresh Python3.11.5
environments built from the pinned requirements, without system-site packages,
provider SDKs or credential variables, reproduce both cached Python/JavaScript
source footprints and pass three nonempty synthetic evaluator cases. These
include the official hidden-directory normalization/missing-source diagnostic,
retained without remapping its annotation. This is a portable setup check;
the full inventory alone is not a container image. The replayer checks source
footprints and candidate metrics, not solver repairs or new model behavior.
Final fee settlement is complete. The full1,000-trajectory credential scan finds
no hits for the current key or the five checked credential shapes; this is not a
complete privacy review. A separate provenance check identifies local reflog
email material in six worker records, alongside workstation paths and SDK
identifiers. A separate publication copy now redacts those known values, retains
all1,000 candidate workers and verifies unchanged predictions, statuses and fees.
The original completed local archive
round-trips 8,653 original members in 14 gzip parts, all below50MiB; its index
SHA-256 is `070148a0f0cbced59984f0429a44aad2a82508056c2b831cc27dec431036c4a1`.
All500 source fingerprints and1,000 worker footprints now replay on the existing
checkouts. The original invocation fails only at the archived-evaluator Git
precondition, with that failure retained. A separate recovery verifies its decoded
input is exactly identical to the completed official metric job, reuses that
report and matches every case metric within1e-12. It restarts neither source nor
models. Fresh-clone entire-trace portability, final publication review, complete
native issue-resolution outcomes and formal submission remain open. Original raw
archives stay local.

### Fixed500 native control

The canonical **500-task native baseline** is now sealed and scored. With the
original empty-source failure counted as zero in the fixed500 denominator,
line task-macro Recall/precision/F1 are **72.83%/27.97%/33.95%**. File Recall/F1
are **84.83%/45.58%**; block (official symbol) Recall/F1 are **81.30%/31.47%**.
The official evaluator returns **499 valid, one failed** across eight languages
and59 repository clusters. This uses CodeNib read/grep/glob and the trajectory
bridge, without indexed graph/vector/BM25/Jev enhancement. It is a known/exposed
context-only baseline, not an enhancement gain, independent validation, repair
result or leaderboard rank. Earlier122 annotation access is disclosed; no native
case is retried or replaced. Scoring adds zero model calls and zero API usage.

A separate local archive now preserves all500 original control attempts,
499 qualified source packets, exact configurations/frozen implementations and
the completed control evaluation. All2,070 members pass byte-for-byte round-trip
verification and every case reference resolves. Eight gzip parts total
28,479,415bytes; the largest is5,219,150bytes, below50MiB. The original empty
failure and prior qualification/exposure records remain. This is an internal
publication candidate; candidate-context results, native resolution outcomes
and submission review remain separate open gates. Historical partial archives
are unchanged; cancelled443 consumers require no new outputs or archive.

A read-only input audit reconciles499 original native records against their
terminal SDK counters:3,396 distinct main-model responses and4,202 repeated
content blocks counted once. The original empty-source case's terminal input
usage and cost remain unavailable. Among the known499, each task's maximum
single-response input averages17,540.45tokens, with median14,066, nearest-rank
P90 31,993 and maximum69,859. These baseline-only statistics use a different
population from the four-task policy comparison; its input reduction must not
be extrapolated to all500. Known selected SDK nominal usage remains$91.1558898
plus one unknown terminal, not a new cash charge or a complete cash bill.

A portable verifier reproduces all500 raw input-accounting records directly
from the unchanged compressed control archive using Python's standard library.
It needs no extraction, SDK, GPU, source repository, credentials or labels;
all499 input summaries and known fees match, with unknown values preserved.
The supplementary eight-file gzip bundle is323,530bytes and passes round-trip
hash verification. This completes control usage reproducibility, with candidate
strategy validation, native resolution outcomes and publication review open.

Re-scoring the pinned September12 public submission on these exact500 Gold/source
inputs and the same official environment gives:

| Same500 tasks, line task macro, failed cases counted as zero | Native Opus5.5 + CodeNib source tools | openJiuwen-JiuwenSwarm + DeepSeek-V4-Pro |
| --- | ---: | ---: |
| Recall | 72.83% | 75.30% |
| Precision | 27.97% | 19.43% |
| F1 | 33.95% | 25.70% |

Native Recall is2.46points lower and F1 is8.25points higher. Local validity is
499/500 versus500/500; the public submission's original499-valid parser result is
retained separately. Different model/harness and context-only versus repair
workflows prevent causal, matched-cost or resolution claims. This compares one
pinned submission, without asserting a current leaderboard rank; it adds no
model/API calls.

The historical late4K policy and all three sealed32 cohorts remain below for
reproducibility. The unstarted443 additional consumers stay cancelled.

The earlier interim **native-only baseline** scores **122/122 valid cases** with the
pinned official evaluator. Line Recall is **76.81%**, precision **30.75%**, and
task-macro F1 **39.00%**. File Recall/F1 is **80.32%/44.33%**; block
(official symbol) Recall/F1 is **81.78%/35.07%**. This is every completed case in
one frozen continuation snapshot: 58 Python, 34 JavaScript, 26 TypeScript and
4 Go tasks across 16 repository clusters. Completion order is not random; this
is neither all500 nor an enhanced-versus-native comparison, and it cannot be
compared directly with the earlier32 or used to claim a leaderboard rank.
Scoring makes **zero model calls and $0 new API usage**.

The published leader's original predictions cover **all the same122 IDs**.
Re-scoring those predictions with our exact Gold/source inputs and the same
pinned evaluator/environment gives122 valid cases on both sides:

| Same122 tasks, line-level task macro | Native Opus 5.5 + CodeNib source tools | openJiuwen-JiuwenSwarm + DeepSeek-V4-Pro |
| --- | ---: | ---: |
| Recall | 76.81% | 79.24% |
| Precision | 30.75% | 21.12% |
| F1 | 39.00% | 29.65% |

Native Recall is **2.43 points lower**, while F1 is **9.35 points higher**.
The public reported scores have one parser failure in this subset; the common
environment scores all122 instead of silently dropping that case. Original
public artifacts and hashes remain. This is a same-task comparison between
different models/harnesses, not a CodeNib enhancement ablation or all500 rank.
The [official submission](https://github.com/ContextBench/contextbench.github.io/tree/a40dfa6b16f14cafd2b1cc46faf0969b38ef03b9/submissions/2026-09-12_openjiuwen-jiuwenswarm)
includes issue resolution, while this native run is context-only; cost and
resolution results are not matched. Local re-scoring adds no model/API calls.

The current baseline uses CodeNib's bounded `read`/`grep`/`glob` tools,
repository-bound adapters and consumed-source trajectory/evaluator bridge.
It uses no semantic graph, vector retrieval, BM25 or Jev reranking. High native
scores therefore do not establish a gain from CodeNib's indexed retrieval.
The unstarted443 late4K consumer plan is cancelled and its $40 API reservation
is released; historical consumed calls and their costs remain unchanged.

Current development connects the existing `explore_context_impl` directly to
the SDK agent as an explicit on-demand tool. The compiler builds vector and
symbol-graph artifacts; no BM25 or Jev backend is selected. An adapter numbers
only complete projected lines verified against authenticated source bytes, so
omitted, partial and index-only fallback text receives no source credit. The
first pilot selects the first previously exposed122 task in each of Python,
JavaScript, TypeScript and Go, without selecting by quality. It uses isolated
source checkouts and cached local Jina embeddings, retains the same main Opus
model/turn/$0.75 guard, and makes no repair calls. The first four-task development pilot is now source-sealed and scored with the
same official evaluator and native controls: **4/4 valid per arm**. Native versus
optional indexed-tool line Recall is **71.61% → 73.69%**, F1 **33.68% → 40.92%**;
unique observed source lines are **3,209 → 2,927** and Max nominal SDK usage is
**$1.1760282 → $1.1217879**, with **$0 new API** and no repairs. This aggregate
is **not evidence of CodeNib core benefit**: Python, TypeScript and Go never
call the core tool. Only JavaScript calls it once, and that case declines in
Recall **74.95% → 74.15%**, F1 **40.28% → 39.15%**. All four remain in the report;
no favorable subset is promoted. They are exposed development cases, not an
independent gate or leaderboard result.

JavaScript and TypeScript have actual compiled graphs; Python indexing exceeds
its600-second limit and Go lacks `scip-go`. Those missing backends stay explicit,
with dense retrieval available. The measured cold preparation times remain
costs of the indexed method even when a subsequent iteration reuses the cache.
There is no explicit Haiku controller; SDK auxiliary usage is not a context policy.

The same real query exposes a source-delivery bug: a named symbol can be lost in
generic routing or displaced by dense hits. Independent main-based
[PR #835](https://github.com/sysevol-ai/CodeNib/pull/835) preserves exact indexed
query symbols and prioritizes named routes. **105 focused tests** and a
same-query/index/source/four-window replay verify the requested definition is
returned first, without model or Gold calls. This is a concrete fix, not a
benchmark gain. Its source changes are included in the next development policy;
the original four trajectories stay unchanged. The PR is ready for review and its
new required unit CI run passes; it remains unmerged.

The completed source-only metadata policy appends at most256 estimated tokens
of compiler-reference metadata after a successful read, then lets the agent
request bodies through the actual context tool. It uses stable symbol/source
identity, bounded edge inspection, history-based repeat suppression and source
authentication. Reference counts claim neither runtime calls nor test coverage;
metadata locations receive no unread-source credit. Source-only replay adds14
hints to15 historical JS/TS reads with identical source predictions; those replay
outputs were never historical model inputs.

The real four-task iteration is frozen at `7db07141`, combining this metadata
policy with the named-symbol fix. These are the same exposed development cases,
after v0 scoring, with unchanged Opus5.5/medium/20turn/$0.75guard and all outcomes
retained. Each arm scores4/4 valid:

| Same four development tasks, line task macro | Recall | F1 | Unique source lines | Max nominal SDK estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 71.61% | 33.68% | 3,209 | $1.1760282 |
| Optional core tool, v0 | 73.69% | 40.92% | 2,927 | $1.1217879 |
| Reference hints plus named routing, v1 | 68.24% | 40.60% | 2,693 | $1.0819325 |

Reject v1 for the Recall-first objective; do not expand this policy. Actual agents
consume13 graph hints (JS10, TS3) and two graph-unavailable notices. Only Go calls
`explore_context`, once, using dense retrieval because its graph is unavailable.
Thus the named-route fix is not exercised by that call. The bundling and small,
exposed sample do not establish a causal effect of either component. Consumed
tool-reply estimates are46,257/43,327/43,602cl100k tokens across the three arms;
these estimates are not Claude input usage. New API usage and repairs are zero.

Reusing exact-case authenticated indexes takes9.30/8.94/18.29/4.99seconds for
Python/JS/TS/Go. Original cold preparations remain818.05/131.15/617.66/17.94seconds,
including the failed Python graph build; warm reuse does not erase those costs.
An initial private launcher omits embedding fields and fails before constructing
any child process or model intent; its evidence is retained, and a corrected
protocol is frozen before the four actual sessions. No model retry occurs.
The latest89 relevant tests, pinned hooks, strict MkDocs and public-docs boundary
checks pass. Further context work must improve useful discovery and demand-driven
source selection, rather than expand a failed metadata policy.

A separate surface-first adapter at `95cbc503` asks the agent to formulate a
focused indexed query before broad source investigation. Surface replies contain
authenticated declarations, locations and snapshot-bound handles. Optional body
fetches page at200lines, reference confirmed prior tool calls instead of repeating
source, and reject changed source/manifest identities. Partial oversized lines
are explicitly unavailable and receive no source credit. This is a source-checkout
experiment using existing exploration/source APIs, with no new product route or
persistence. The composed explorer still performs bounded local source reads
internally; metadata-only model delivery does not establish a source-I/O saving.

The same four exposed development cases are frozen before four fresh sessions
under protocol `ede991e4`. All4/4 score valid. Each agent makes one actual surface
query, receiving22 candidates in total; subsequent source observations overlap15
locations (14 through native `read`, one through grep only). This is a later-read
proxy, not proof that a candidate changed reasoning
or a patch. All four choose native source tools afterward, with **zero handle body
fetches**. Body pagination/deduplication therefore have local contract tests, not a
measured runtime benefit.

A post-hoc source audit distinguishes handle adoption from complete-function
reading. Of22 surface entries,17 exactly match unsplit AST function/method
ranges. Subsequent confirmed native `read` replies cover12 of those completely;
five remain unread and none is partially read. Two other non-callable ranges
are also fully read. Thus zero body-handle calls does not mean no complete
function bodies reached the agent; it still does not prove a causal benefit.

Reopening only these four already-exposed official normalized annotations
finds zero additional annotation lines inside any returned surface range.
Reading every surfaced body would therefore add zero Recall on these specific
traces; that is a diagnostic upper bound, not an executed candidate score.
None of the five unread callable ranges overlaps those annotations. The238
remaining annotation lines include104 in already observed files and121 in
files without dense L2 documents, principally Markdown/JSON/YAML. These counts
overlap and are not task-macro scores. One Go annotation line is outside the
pinned source; its original denominator/source issue remains unchanged. Dense
file coverage is not overall graph/native-tool availability, and annotation
overlap is not semantic usefulness. This directs later development toward
discovering missing local scopes and non-code context, rather than forcing
body-handle use or dumping every candidate. The pending eight-case policy,
source predictions, budgets and scoring remain unchanged; no new task labels,
models or API calls are added by this four-task diagnosis.

One post-hoc native-only repeat is frozen under `884a5f9b` before its four new
sessions. It repeats all four once, keeps all original controls/results and never
replaces a native500 record or retries a failed attempt. It is a disclosed
stability check, not an independent or prospectively paired primary experiment:

| Same four exposed tasks, line task macro | Recall | F1 | Unique source lines | Consumed tool-reply cl100k estimate | Max nominal SDK estimate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original native | 71.61% | 33.68% | 3,209 | 46,257 | $1.1760282 |
| One native repeat | 71.37% | 31.76% | 3,236 | 44,480 | $1.1046278 |
| Surface-first | 74.02% | 42.40% | 2,610 | 37,376 | $1.0091893 |

The frozen development expansion screen passes because surface Recall exceeds
both native means. Gains remain small-sample and concentrated in Python; the
optional v0 run already scores73.69% Recall without core use in that case. Do not
promote this to a repeatable population or causal gain. The candidate uses the
same Opus5.5/medium/20turn/$0.75guard; all metering and included-Max receipts pass.
New API usage and repair calls are zero. Source-only preflight validates23
candidate declarations/2,356metadata tokens over four real indexes with zero
unread-source credit; those local candidates are never model inputs.

Reported main-model cache counters remain separate from tool-reply estimates.
Surface versus repeated native cache-creation counters are81,984 versus88,935,
cache-read counters381,309 versus478,849, and output counters13,795 versus14,812.
These are cumulative SDK counters, not peak context, physical KV residency or
converted API bills. Replies append to history; there is no compaction/eviction
implementation or measured invalidation benefit here.

A supplementary post-hoc audit now measures provider-reported input per unique
main-model response. Assistant content blocks sharing a response ID are counted
once, and uncached, cache-write and cache-read inputs are added using the
[documented input accounting](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
All93 distinct responses across the12 original/surface/repeated-native streams
reconcile exactly with terminal SDK input counters;152 repeated content blocks
are not added twice. The mean of each task's maximum single-response input is
25,980tokens for original native,24,965.75 for repeated native and22,048 for
surface-first. Surface is11.69% below the repeat on this four-task measure;
Python increases while the other three decrease. This is provider-reported
input size, not physical KV memory, an HTTP request count, converted subscription
charges or an independent/causal result. Existing quality scores and cumulative
cost records stay unchanged. The source-checkout accounting helper rejects
missing/conflicting usage and incomplete terminal reconciliation;62 relevant
usage/surface/SDK tests pass.

Latest99 relevant surface/cache/SDK/source/navigation tests and pinned hooks pass.
Prepared cache reuse requires unchanged before/after source snapshots, the exact
case/local embedding route, an explicitly non-live producer, no model intent and
zero SDK events before existing native artifact/source authorization. A new
`--vector-only` labs option supports a separately declared dense-only ablation;
it skips graph compilation/loading, without claiming a backend failure. The next
eight development IDs are frozen under `0d598174` by fixed metadata ordering,
one per language/repository, excluding the four pilot IDs and all three sealed32
cohorts/aliases. This arm is separate from the mixed-view four-task pilot, with
fresh preparation costs retained; it is neither fresh independent validation
nor another full500.

The initial source-only gate completes6/8 preparations and fails on two before
any SDK/model dispatch. Ansible has two symlink aliases read by discovery and
then rejected by the native schema-8 canonical-source guard. C's dataset label
is passed directly to a factory that deliberately accepts `cpp` for C sources.
Keep that original failed gate, all eight IDs and all preparation costs.

Independent main-based [PR #836](https://github.com/sysevol-ai/CodeNib/pull/836)
adds an opt-in lexical regular-file discovery filter for schema-8 producers,
preserving strict final validation and existing chunker defaults. The real
Ansible replay retains486 regular L0 files and both alias targets;228 independent
main-based builder/chunker/compiler tests, strict MkDocs and public-docs checks
pass, as does a fresh required unit CI run. The pilot separately translates dataset labels through the registry's
`chunker_language`, preserving the direct factory's rejection of `c`. The broader
feature passes179 relevant and190 compiler tests. Source-only correction protocol
`4ddf8f93`, runtime `85b7f0d2`, rebuilds the two failed preparations and retains
six already-verified artifacts. No case/model retry, quality-based replacement or
model/tool/prompt/budget change occurs. The corrected source-only gate also
fails: Python is ready, but the C checkout includes LLVM and its396,364 L2
documents exceed the existing256MiB canonical-document limit during save.
Its16,536 L0 and396,364 L2 embeddings have already been computed; total C
preparation takes2,545.82seconds (2,473seconds in the vector build). The audit
stops before live-protocol freeze or any agent dispatch. All eight IDs, both
failed source gates and all preparation costs stay; there is no eight-task
agent quality result, corpus narrowing or case replacement.

The late-admission fix streams the exact document metadata/canonical encoding
for every requested schema-8 level before any level initializes an embedding
model. It preserves the256MiB limit, existing save-time guard, native format,
legacy behavior and publication boundary; it does not make the C artifact fit.
Three focused regressions,93 index and448 compiler tests pass. Independent
main-based [PR #837](https://github.com/sysevol-ai/CodeNib/pull/837) passes539
index/compiler tests and its fresh required unit CI. The same-C admission replay
rejects the unchanged oversized payload in243.37seconds with zero embedding-model
initializations. This verifies earlier rejection, not a successful vector build
or a quality gain. All three production fixes remain unmerged dependencies.

A distinct source-checkout policy now uses CodeNib's existing authenticated
source reader and tree-sitter chunkers without building repository indexes.
Native grep/glob discovers a file; `explore_context` returns a bounded definition
outline, optionally focused near a hit line. Snapshot-bound handles retrieve
selected bodies with pagination and confirmed-prior-read deduplication. Non-code
files receive file handles. Metadata receives zero unread-source credit; only
actual delivered, verified source lines count. No graph counts are invented.
The requested file is parsed locally; this does not eliminate local source I/O.
66 relevant source/surface/SDK/input-accounting tests pass.

Protocol `b8df97c5`, clean runtime `bc92d34c`, retains the exact same eight tasks,
commits, full source fingerprints and original native controls. All8/8 source-only
preparations pass before dispatch. The C/LLVM source retains107,570 files and
prepares in45.81seconds on the existing filesystem. This is not a matched
cold-machine timing; preserve the failed dense2,545.82seconds, its243.37second
zero-embedding admission probe and the earlier unfrozen46.53second local probe.

The first two SDK sessions stop before assistant/tool output because the local
quota parser requires a legacy `overageEnabled` field omitted by the receipt.
The actual events explicitly allow subscription usage, set `isUsingOverage=false`
and agree on typed/raw organization-disabled overage fields. Six positions remain
unclaimed; preserve all eight original outcomes with zero accepted executions
and unknown terminal costs for both attempted sessions. A narrow schema fix
accepts this explicit disabled state while rejecting paid, absent or conflicting
evidence.75 relevant tests and both actual-event replays pass.

Receipt-only continuation `b51f7dc1`, runtime `a2b5e74c`, completes8/8 under the
same source strategy, source scope, Opus5.5/medium/20turn/$0.75guard and included
Max route. **None of its eight agents uses the advertised context tool.**
A further primary-handle policy `aa32ebf0`, runtime `5b79410a`, is frozen from
this non-adoption evidence before reopening current-eight annotations. It directs
new relevant files through outlines and selected body handles, retaining native
read for precise gaps/errors. Two SDK guidance/delivery checks pass. The optional
arm finishes unchanged and source-seals before primary dispatch; both complete
source arms seal before current-eight annotations are reopened. Neither uses
API keys, repair calls, graph, vector retrieval, BM25 or Jev.

All three arms score8/8 valid on the same eight tasks, source snapshots and
pinned official evaluator. Failed source/SDK attempts remain separate, without
replacement or retuning. The [case-level CSV](../assets/contextbench_file_surface_development_8.csv)
includes exact per-case metrics, actual context calls, source/reply counts,
reconciled input maxima and SDK nominal usage.

| Same eight exposed tasks, line task macro | Recall | Precision | F1 | Surface/body adopter cases | Source lines | Tool-reply cl100k estimate | Max nominal SDK estimate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Original native | 69.91% | 35.47% | 39.66% | 0/0 | 3,981 | 57,004 | $1.4261087 |
| Optional file outline | 70.06% | 34.03% | 38.72% | 0/0 | 3,666 | 54,623 | $1.5609549 |
| Primary outline/body flow | 69.40% | 34.26% | 37.35% | 7/1 | 4,090 | 66,461 | $1.6787390 |

The primary policy loses0.51 Recall points and2.30 F1 points against original
native. It passes the declared6/8 surface-adoption threshold but fails the4/8
body-adoption threshold and Recall comparisons against both controls. Its seven
surface calls and one body call have zero tool errors. The body fetch covers
Svelte's `ElementWrapper.addBindings`, lines385–549:164 newly delivered lines
plus one prior grep line omitted, without partial-line credit. This verifies
actual CodeNib source delivery and deduplication, not an overall benefit.

All24 selected raw streams reconcile provider input counters with terminal SDK
usage. Mean per-task maximum single-response input is16,940.75tokens for native,
17,523.875 for optional and19,580.25 for primary. Primary is15.58% above native;
known cumulative cache-write/read inputs are130,635/659,158 versus113,670/432,899.
These are provider counters, not physical KV residency or cash charges; no
compaction, eviction or measured invalidation saving is implemented.

The16 completed new sessions add$3.2396939 in known Max SDK nominal estimates;
the two earlier pre-assistant sessions retain unknown terminal costs. New API-key
usage and repairs remain zero. The optional policy's0.15point Recall movement
has zero actual CodeNib adoption and cannot establish component gain. The primary
file-outline policy is not promoted to independent validation or500. Existing
ordinary-source controls, all preparation failures/costs and the earlier indexed
four-task screen stay intact. Subsequent retrieval experiments must identify a
concrete missing-context opportunity and demonstrate a benefit; embedding/graph
are optional hypotheses. Fresh default-only32 validation, a frozen candidate500,
native outcomes and submission review remain open.

A separate all-eight reporter passes source-only replay against the eight native controls
and four earlier surface trajectories. Its contract retains all selected
positions, gives metadata zero source credit and reports execution/evaluator
validity separately before reopening the already-exposed labels.

A separate local integration replay checks both first surfaced handles on each
of the seven currently ready indexes. All14 location bodies match authenticated
source, metadata receives zero source credit, repeated reads are omitted and
all native artifact bytes remain unchanged. These local synthetic receipts are
never model inputs or quality results; they add zero actual agent body fetches.
All selected locations fit one body page, so real-index multi-page behavior
remains unexercised beyond the existing deterministic large-function test. C
remains in the original eight-case cohort and is not replaced by this check.

A source-only ranking diagnostic on the four actual agent queries compares raw
inner product, cosine reranking of128 candidates and full cosine without changing
persisted artifact bytes or original scores. Cached Jina vector norms vary from
approximately10.93 to14.76; focused-query top candidates are mostly unchanged.
The [current author usage examples](https://huggingface.co/jinaai/jina-embeddings-v2-base-code)
normalize vectors or compute cosine. This is a hypothesis to check separately,
not evidence that every raw-IP configuration is a product defect or a measured
quality improvement. No additional model/Gold/API calls occur in that diagnostic.

The user-requested interim report prospectively changes the annotation schedule:
these122 actual-source predictions were sealed before their selected Gold was
read, while remaining native investigations were
still incomplete at that time. The later443 consumer plan has since been cancelled. Preserve the original full500-before-first-new-Gold schedule
as **not followed**. Runtime gold stays empty; the frozen model, tools, compiler,
population and budgets do not change or use these results. Final full500 results
must disclose this prior annotation access and keep the three32 cohorts sealed.
Earlier untouched-pool counts predate this interim; refresh exposure metadata
before any future independent selection.

The cheaper Max-to-Batch execution route now **passes a four-task development
engineering check**: four normal Max investigations and eight normal Batch packet
inputs cost **$0.430218** in new API usage. The **$1.2485557** Max SDK estimate
is separate nominal metering. This is route/source delivery verification, not
another quality test or a full500 result.

The unchanged first-read source-window policy fails its complete 24-task development
stability gate. Recall 58.39% → 63.91%, F1 31.97% → 27.94%; repository-cluster 95%
Recall interval [-2.76, +12.52] points across 24 repository clusters. All 48 selected
SDK sessions are retained for $9.86110630, with no repairs. That first-read policy
has no established independent or official500 gain.

The grep-neighborhood policy does **not** pass its frozen
independent Recall gate. Across 32 new tasks in seven languages, Recall improves
65.52% → 68.93% while F1 falls 26.12% → 23.30%. The +3.42-point Recall interval
is [-2.27, +10.08], which includes zero. All 64 SDK sessions complete for
$12.63056860, with no repair calls. This is not an established leaderboard gain.

The grep-neighborhood policy earlier passed its eight-task development screen and replication with
Recall 35.24% → 38.91% and F1 31.26% → 28.20%. Preserve those positive screening
points, but do not promote this unchanged policy into repairs, official500 or a
product default. Both earlier validation cohorts remain sealed against tuning.

Both eight-task online grep/Jev policies **fail their development gates**.
Replacing grep replies with source blocks reduces Recall/F1 from
42.36%/36.80% to 40.97%/31.94%. The subsequent policy preserves every original
grep match and suggests read locations instead, but still loses Recall/F1,
43.29%/37.08% to 38.73%/34.17%. These disclosed development tasks establish
no leaderboard advantage. The source-block repair check resolves the same C++
task in both arms and loses both C predictions on budget exceptions.

The October 16 independent validation also rejects the earlier function-plus-graph
candidate. All 32 selected tasks remain in the scores and sealed against tuning.
The first-source graph and later relationship-check policies fail their gates
as documented below. None advances to the official 500 or a product default.

Earlier development experiments found useful missing source by completing
functions after search. That offline gain has not transferred to the tested
online agent. These are retrieval experiments, not a leaderboard submission.

Reranking has positive fixed-pool development evidence.
The fixed 100-task, 4K ContextBench control below improves Opus Recall/F1 from
39.88%/22.60% to 45.70%/27.84% using the Perplexity decision scorer. The separate
[grep/Jev evaluation](grep_jev.md) improves code-block Recall@5 from 58.62% to
71.40% in research and from 48.37% to 65.57% in a fresh product run. Those use
CodeNib Base, not ContextBench, and neither establishes an online-agent gain.
The online control below tests source ranking before delivery; graph expansion
is not a prerequisite. Both grep/Jev controls fail their retrieval gates; positive
fixed-pool rankings did not transfer to these adaptive source-delivery policies.

## First-read same-file source: Recall-first development

The new hypothesis changes actual source delivery after a file has been located.
On a read with no prior observed successful read of that file, preserve the
complete native reply and append up to 2,048 cl100k tokens of previously unread
source from the same file. Physical windows contain at most 64 lines and 3,000
characters, ordered by distance from the current read and then line number.
Keep at most 200 candidates and ten used windows; skip files above 2 MiB and
explicitly report overlong lines. The candidate's total read-reply character
ceiling is **32,000**, versus native 16,000. This larger delivery allowance and
its downstream token costs are part of the treatment.

This source-checkout feature is default-off (`--file-source-tokens 2048`). It
adds no BM25, graph, AST expansion, file map, planner or runtime ranker. Existing
read/grep/glob, system prompt, actual model, turns, output-token allowance,
timeout and dollar guards stay matched. Concurrent reads may repeat source
until replies enter observations; their exact originals, source hashes and
returned additions all have receipts. Only complete source delivered before a
later assistant response receives coverage. Scoring projections recompute
trajectory and served payloads without changing raw attempts.

Before implementation, a zero-new-API replay freezes source choices on the
historical eight native trajectories. Calls and real prior observations stay
fixed, so these figures describe source opportunity rather than new agent
behavior:

| Fixed historical calls, eight development tasks | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| Original native trajectory | 43.29% | 38.51% | 37.08% |
| Nearby unread windows, bounded 2K addition | 56.16% | 27.72% | 34.96% |
| Same windows and budget, new Jev ordering | 56.83% | 27.59% | 34.92% |

Jev-only ranking costs **$0.02435412** for 71 decisions, with known usage for
every request. Its +0.66-point Recall change over nearby order has exploratory
95% interval [-2.05, +3.35] points and fails the frozen practical screen of at
least one point. Do not add a runtime ranker from this result. The complete
capped window pool can address 73.50% Recall, but that unbounded union is neither
budget-achievable delivery nor an agent score.

A wider, separately frozen replay retains all 24 earlier native development
trajectories. Nearby windows increase fixed-history Recall **53.35% → 62.32%**,
while F1 falls **31.18% → 27.59%**, with additions in all 24 tasks and no new
model calls. It passes its source-opportunity screen of five Recall points and
18 eligible tasks. These opportunities do not replace actual online comparisons.
Historical reports lack normalized annotation metadata; the evaluation-only
bridge uses the pinned official `Gold.line_spans_init()`, preserves original
counts and the failed bridge logs, and changes no source selection or gate.
The runtime matches all 46 eight-task historical read renderings exactly and
passes 102 focused retrieval tests and pinned hooks.

Earlier cheap ranking probes on the historical **read-hint** trajectories use a
different baseline and candidate pool. Preserving grep hits and appending bounded
source gives 42.09% Recall without ranking versus 42.18% with cached Jev scores,
a +0.09-point weak point screen. It technically passes that frozen point screen,
but is too small to justify buying main-agent sessions. Scoring only unread
fragments gives 42.33% with edit relevance or 41.95% with marginal-information
criteria. The selected mode adds only +0.24 points over unranked source, below
the separately frozen one-point practical screen. Those 102 new decisions cost
**$0.020709528**, with no main-agent or repair sessions. Cached scores add no new
charges; all variants, intervals, gates and costs remain in the sanitized export.
Native grep output caps occur in only one of 41 historical calls, so the separate
cap diagnostic does not justify another search-policy implementation.

Both independent32 cohorts stay sealed. Development uses the original 105
exposed tasks only. A metadata-only inventory finds 636 extra default-config
ContextBench rows outside verified500 by ID, original ID and repository/commit.
A documentation viewer unexpectedly rendered reference previews for ten task
IDs; all are quarantined from future unseen validation. This initially leaves **630**
additional default-config candidates and **329** remaining verified candidates.
A broader source-identity audit also covers the complete 100-task CodeNib Base
test split used by Jev research/product controls. It excludes previously seen
base commits across repository aliases, leaving **623** additional default
candidates and **324** verified candidates. The raw full Parquet matches the
pinned dataset revision and SHA-256; schema checks on only the ten quarantined
examples pass the existing loader. No new candidate annotations are read.
The extra population includes C++ and Java, but is not verified500 and still
needs a broader exposure audit, annotation-format/source readiness and a new
sampling protocol. No historical completed cohort or outcome is retroactively
changed. Future documentation checks use raw README/schema metadata to avoid
additional preview exposure.


The separately frozen online pilot uses the first four metadata positions of the
original eight-task development cohort: Python, TypeScript, C and C++. Every
selected task remains; no paid attempt is retried or replaced. Both arms use
actual Opus 5.5, medium effort, configured `max_turns=20`, 600 seconds, 4,096 maximum output tokens
and $0.75 SDK guards under a $5 paired reservation. Runtime is `4a6992cb`;
protocol is `90acdd335dc7215c8909f029a5a5fe7f24cf4f1a05e779c864191c5a431e5b99`.

| Actual agent, all four selected tasks | Recall | Precision | F1 | API SDK estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 42.87% | 36.97% | 38.64% | $0.771637 |
| First-read source windows | 49.31% | 29.01% | 35.47% | $0.871572 |

First-read same-file source windows pass the four-task pure-context development screen: Recall 42.87% → 49.31%, F1 38.64% → 35.47%. API SDK usage is $1.64320910, with no repairs. The exploratory Recall interval is [-0.73, +13.61] points; this is not independent or leaderboard evidence. Both prior validation cohorts stay sealed.
All eight sessions are normal, known-usage completions, with exact original-read,
source-version, model/route and confirmed-delivery receipts. All four tasks
receive additional source. Secondary final16K/32K metrics remain in the export.

The unchanged-policy replication on the remaining four metadata positions (Go, Java, JavaScript, Rust) passes its frozen development gate. Recall 38.71% → 54.57%, F1 29.14% → 27.68%; Recall difference +15.85 points, exploratory 95 interval [+4.29, +27.42]. All selected outcomes and failures remain. API SDK usage $2.16424280; no repairs.

First-read same-file source windows pass both four-task pure-context development screens. Across all eight tasks, Recall 40.79% → 51.94%, F1 33.89% → 31.57%. The exploratory repository-cluster 95% Recall interval is [+3.61, +19.32] points. The 16 SDK sessions cost $3.80745190, with no repairs. This is development evidence; both prior independent cohorts remain sealed.

The wider continuation retains every one of the original24 development tasks
absent from the original eight; there are 16 fixed positions and 32 SDK sessions,
under a $12 reservation. Source policy, runtime 4a6992, model and per-case budgets
stay unchanged; at most four sessions run concurrently. Every failure remains.
Its protocol is `6a71770c44645adb172fff2d437dcd53db146c7a5bb4efb0367667a691fda8c7`.

The unchanged first-read source-window policy fails its complete 24-task development stability gate. Recall 58.39% → 63.91%, F1 31.97% → 27.94%; repository-cluster 95% Recall interval [-2.76, +12.52] points across 24 repository clusters. All 48 selected SDK sessions are retained for $9.86110630, with no repairs. Independent and official500 gains remain unestablished.

| Actual agent, all 24 development tasks | Recall | Precision | F1 | API SDK estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 58.39% | 29.90% | 31.97% | $4.431075 |
| First-read source windows | 63.91% | 22.07% | 27.94% | $5.430032 |

All 48 sessions finish normally, with known usage and valid official scores for
all 24 tasks in each arm; no trailing source is excluded. SDK 0.2.165 and CLI 2.1.301
match across all 48 configurations. The SDK-reported turn totals are retained
separately and are not inferred HTTP request counts. All three staged positive
Recall point screens pass, but the frozen complete 24 Recall stability gate fails.
Do not launch another independent cohort from this unchanged policy. F1 is
reported without a veto; the failed gate is Recall uncertainty. Both old 32 cohorts
stay sealed. No repair/native-harness or official500 batch follows this result.

A zero-call descriptive projection strips only captured source additions from the
candidate replies, keeping its adaptive calls fixed. Recall is 58.39% for native,
53.27% for those unaugmented candidate calls and 63.91% for the actual candidate.
The exact source decomposition is +10.64 points of mechanical added coverage and
-5.12 points of unaugmented-call difference, with 15 wins/four ties/five losses.
The latter mixes model randomness and policy-associated behavior; it is not a
causal estimate. Every source choice is frozen before annotation counts. No
actual alternate unaugmented agent was run. A new zero-API hypothesis tests one
bounded late source envelope after a preserved native investigation, before any
paid continuation. Its opportunity must not be labeled real model delivery.

The separately frozen late-envelope source replay retains the actual native
investigation and appends one 2K source packet total, rather than 2K per early
read. It uses unread physical windows in already located files, with final-handoff
file mentions and round-robin ordering fixed before annotation counts. Recall
opportunity is 58.39% → 62.19%, F1 31.97% → 29.17%; the unbounded capped pool
addresses 82.75% Recall. All 24 cases and the source-opportunity gate pass for zero
new model calls. That replay alone establishes no model-input delivery. The later
Jev-only ranking screen below fails its separate one-point gate; the physical 4K
control and actual-input study use the unranked policy. Prior first-read gates stay
failed.

The completed late-source Jev-only probe retains all 24 tasks and 558 decisions
for **$0.271715010**, with known usage and exact source/model
receipts. Near-order Recall/F1 is 62.19%/29.17%; edit-relevance ranking is 62.97%/30.17%,
and missing-evidence ranking is 61.48%/29.04%. The selected edit criterion adds only
+0.78 Recall points over near, exploratory 95% interval [-1.80, +3.33].
It fails the predeclared one-point screen. Do not buy a main-model ranking pilot
or change the failed gate. This adds no real main-agent source delivery.

A separate zero-call source-envelope control compares touched-callable source
with physical windows at matched 2K and 4K budgets, keeping the completed native
prefix fixed. The callable helper is parser-derived, not a compiler call graph;
only actual previously unseen lines are packed. A final callable may be partial.

| Hypothetical late source, all 24 cases | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| Original native prefix | 58.39% | 29.90% | 31.97% |
| Physical 2K | 62.19% | 23.94% | 29.17% |
| Callable 2K | 61.97% | 26.80% | 31.39% |
| Physical 4K | 65.23% | 19.90% | 26.23% |
| Callable 4K | 63.34% | 25.17% | 30.51% |

The 4K physical source opportunity passes its separately frozen five-point
coverage/eight-positive-task gate. Callable 4K fails its structural gate: it
does not beat matched physical 4K by one Recall point and its native-relative
change is under five points. Preserve both decisions and the earlier failed
Jev gate; the selected physical 4K delivery hypothesis uses no ranker. The zero-call
preflight fixes only a tuple/list comparison, with original logs retained.
The prospective four-task actual-input study uses two 32K packet arms with the
same completed native prefix; the treatment packs its novel 4K envelope before
that prefix. A packet receives source credit only after an actual main-model
assistant response confirms its exact prompt, complete physical lines, source
version, model and first-party API route. The consumer disables repository tools
and rejects compaction, unexpected tools, missing assistants or changed sources.
Terminal usage survives SDK errors; prepared packet geometry alone gets no credit.

The first attempt uses runtime `539e6a6b`, protocol
`cfe51d9c65987fc13aa1d5613cb3f0d7f118d9821d663471d928d8112d53cce0`.
It retains two normal Python sessions, two TypeScript budget errors and four
unstarted C/C++ sessions, then stops as declared. All four attempted sessions
have known usage totaling **$0.85013280**. The output allowance is consumed by
thinking and SDK continuations; configured `max_turns=1` does not imply one
reported turn or one underlying request. Its mechanism gate fails. Historical
prefixes stay in the four-case denominator; failed/unstarted new packets add no
new source credit. No session is retried or replaced within this attempt.

A separate followup protocol `4e1b0193accf1d176e2938ad419ba3cd2a8682b3cba96a6d46c55f8a2311e3ef`,
runtime `c141a2b7`, keeps identical tasks, model, source packets and $0.25 case
guards, requests thinking disabled and limits review to 120 words/512 output tokens.
Installed-SDK command construction verifies the requested flag before dispatch.
All eight sessions finish normally with exact source-input receipts for
**$0.91064290**. Actual thinking blocks still appear in seven sessions, with one
to three SDK-reported turns. Preserve this mode-conformance failure and the
original strict analyzer failure. A separately recorded delivery analysis scores
the confirmed exact source without claiming the mode worked; the source-delivery
receipt gate passes, while mode conformance and the overall mechanism gate stay
**false**. No thought contents are inspected or published.

| Confirmed cumulative source, four development tasks | Recall | Precision | F1 | New review SDK usage | Allocated prefix + review |
| --- | ---: | ---: | ---: | ---: | ---: |
| Shared native prefix + native packet | 42.87% | 36.97% | 38.64% | $0.38721930 | $1.15885590 |
| Shared native prefix + late physical 4K packet | 49.41% | 23.91% | 31.53% | $0.52342360 | $1.29506020 |

The +6.54-point Recall result is component evidence on disclosed development
cases. It cannot establish fresh adaptive-agent behavior, repair success or
independent quality. The already incurred prefix costs $0.77163660; allocate it
fully to each logical pipeline arm, but do not count those allocations as new
spending. Final 32K packet-only F1 is 38.66%/31.54%; cumulative source above retains
all historical actual observations. Both attempts cost **$1.76077570** in total
against the original **$3** reservation. All new usage is known. Earlier failed
gates and both sealed 32 cohorts remain unchanged.

The reusable source compiler `scripts/contextbench_late_packet.py` builds this
policy from actual native messages without gold, reference patches, search or
model calls. Only observed files qualify; final-handoff mentions affect file
order, never source facts. Exact source hashes bracket construction. Its zero-call
parity gate passes: all 24 frozen extra/native packets and all four actual-input
pairs are byte-identical. The producer is committed as `1aa5bac1`; 96 focused
retrieval tests and pinned hooks pass. A future source-only protocol must report actual
SDK mode behavior rather than claim effective no-thinking. There is no product
route or default change.


The separately frozen remaining 20 source-delivery study uses runtime `1aa5bac1`,
protocol `13c82732a9cc6b3c7ff30e7d2b98fe88c99c997829472c72b771f63244d1f4a3`.
It retains the same physical 4K compiler and review controls, fixes all remaining
positions before dispatch and reserves **$8** within the existing $100 authorization.
Its new engineering gate measures source delivery, with actual thinking and
reported SDK turns descriptive; it does not redefine the original four-task
mode or overall mechanism gates. It makes **24** SDK sessions for **$2.87066330**,
with **0** unknown usage records. All 20 selected cases are retained; 23 sessions are normal, 1 error and 16
not run. The source-confirmed budget-error packet still receives no new source
credit under this frozen normal-terminal protocol; never change that decision
after scoring.
The earlier opportunity was already evaluated, so these known development cases
cannot provide a new confirmatory quality test.

| Confirmed cumulative source, all 24 development cases | Recall | Precision | F1 | Allocated prefix + attempted review |
| --- | ---: | ---: | ---: | ---: |
| Shared native prefix + native packet | 58.39% | 29.90% | 31.97% | $6.04475590 |
| Shared native prefix + late physical 4K packet | 62.16% | 24.13% | 28.64% | $6.59869990 |

The complete development source-delivery gate **fails**. The 24-case table
retains common historical prefixes for every error/unstarted position, crediting
new packets only on normal confirmed source inputs. Repository bootstrap
uses 5,000 resamples, seed 20261017, with 24 canonical repository clusters;
Recall changes by **+3.77 points**. F1 is reported without a veto.
The full historical prefix costs **$4.43107480** and is allocated
once to each logical pipeline, not charged again. The followup and remaining 20 review attempts, including the new budget error,
cost **$3.78130620**; the first failed attempt adds
**$0.85013280**. All actual packet-input attempts total
**$4.63143900** inside the separate $3 and $8 reservations.
No effective no-thinking guarantee is claimed: actual thinking appears in
21/24 remaining 20 sessions, with reported SDK turns
1–3 among attempted sessions. These are SDK sessions/turns, not underlying HTTP request counts.

The source envelope increases used source/model tokens; both final review packets
have the same 32K ceiling, but used tokens are not equalized. This is a context
strategy with extra source volume and associated cost, not proof of reranking or
better autonomous discovery. Do not advance this failed delivery attempt to independent inference. The next
source-consumer protocol must handle known terminal budget outcomes explicitly
before more calls; preserve this closed attempt without replacement or retries.
Any later independent protocol needs untouched IDs and exact source snapshots;
neither sealed 32 cohort can be reused. No repair or official500 batch follows automatically.


The root cause is now verified against [official Opus 5.5 capability documentation](https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5):
adaptive thinking cannot be disabled. The earlier requested disabled setting was
not a valid guarantee for this model. The 512-token allowance includes thinking
and text, and [a too-small output allowance can truncate the response](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting).
Actual SDK usage also includes auxiliary Haiku calls: **$0.06910240** across the
36 packet SDK sessions, already included in the existing costs. This audit changes
no source score, failed gate, status or cost total. Requested main-model identities
remain separate from all billed auxiliary models.

Runtime `d2bf1136` replaces the packet consumer's CLI loop with one direct
Messages request. The source compiler and all 24 packet bytes remain unchanged.
The new protocol is `7f97e9a0cc118f8d5a535e9a7256927d05e2a084e50f593c7d72e84ce0f33727`. Before payment, the
[free token-count endpoint](https://platform.claude.com/docs/en/build-with-claude/token-counting)
checks all 48 exact inputs; capped output,2%/64-token margin and per-request
headroom reserve **$6.557596** under an **$8** study ceiling. The consumer
recounts before each paid request, binds source versions before/after, uses
adaptive thinking, medium effort and 2,048 output tokens, and disables HTTP retries,
redirects, tools and continuation. There is no CLI or billed auxiliary model.
The model's [standard/global uncached prices](https://platform.claude.com/docs/en/models/opus-5-5/overview)
are frozen at $4/$20 per million input/output tokens for usage estimates.

A normal `end_turn` or `max_tokens` Message confirms actual input under this new
prospective source-only protocol. A capped response is labeled incomplete and
never treated as answer or repair success. This does not change the previous
SDK normal-terminal criteria or retroactively credit its budget-error packets.
The known failing C packet, largest and smallest source packets occur in the first
engineering positions; all 24 remain without replacement or score filtering.

| Direct confirmed cumulative source, all 24 development cases | Recall | Precision | F1 | New Messages usage | Allocated native prefix + new review |
| --- | ---: | ---: | ---: | ---: | ---: |
| Native packet | 58.39% | 29.90% | 31.97% | $1.82993200 | $6.26100680 |
| Late physical 4K packet | 65.18% | 19.94% | 26.26% | $2.42481200 | $6.85588680 |

The complete 24 engineering gate **passes**: 48 paid Messages requests,
96 free count requests including whole-cohort preflight, **0**
unknown usage records and **48** completed text reviews.
Stop reasons are `{'end_turn': 48}`. SDK session count is zero.
Cumulative and final 32K source scores remain distinct. Final packet Recall/F1 is
58.38%/32.04% for native and
65.18%/26.41% for late source.
Reuse the **$4.43107480** actual historical native prefix in both logical
pipelines without billing it again. Total packet-delivery usage including all
failed SDK studies is **$8.88618300**. Source geometry,
model, input/version/HTTP/usage receipts and 104 focused local tests are recorded;
no graph or runtime ranker is added. Both old 32 cohorts remain sealed.

A zero-call source audit explains the small difference from the earlier 65.23%
opportunity: intermediate repacking omits 63 boundary blank source lines. There
is no additional source loss in prediction parsing. Actual scores credit only
delivered lines; keep both original opportunity and actual scores unchanged.

Only the complete engineering result enables a new frozen independent protocol
on untouched task IDs. This already known development coverage is not a fresh
quality test. Extra source/model tokens and their cost remain part of treatment;
Recall is primary and F1 is reported without a veto. No repairs/full500 run
follows automatically; verified500 still needs independently useful quality and
a separately measured budget decision.


A new independent 32 fixes metadata IDs from 324 previously untouched verified
candidates, excluding known source/issue exposures, both old cohorts, the original
105 development tasks and exact known source commits. It has 12 Python tasks and
four each in C, Go, JavaScript, Rust and TypeScript, spanning 23 repository names.
There is no untouched C++/Java coverage, and this is not repository-held-out or a
representative full500 distribution. Issue text is read only after ID selection;
runtime gold remains explicitly empty. All 32 exact source commits pass readiness.

Original protocol `0b84a4a4e39a20449366e079277a3658bb99e0b0da02f907a6c7e346905b632e`
uses parent runtime `d2bf1136` and a $24 ceiling. Its execution fails locally before
labels: eight native investigations and 14 packet consumers cost **$2.56634860**,
with seven complete pairs. A literal tokenizer marker in a source candidate
raises a packing error before that case's consumers. Preserve the failed original
operational gate, raw attempts and source-only terminal audit without quality scoring.

Runtime `8d5e0b87` treats tokenizer marker literals as ordinary source text in
packing and validation. Seven completed packet pairs replay byte for byte; 129
focused tests pass, including three marker regressions. Before any selected
annotation access or new model call, an explicit correctness amendment freezes
protocol `75a7579f4f0f379e23a556e869d7132de2113cb29c5e033a0f85e28ad7ce09ad`.
It reuses all eight completed native investigations and 14 consumers, running
only the 24 native investigations and 50 consumers not previously attempted.
No model call is retried or replaced. Tasks, source selection, model, query,
token/turn/output caps and quality gates stay unchanged. This is the amended
pre-label validation, not a passing reclassification of the original execution.

Each task shares one fresh native investigation between two actual packet
consumers. Native SDK requests use Opus 5.5, medium effort, 20 configured turns,
4,096 requested output tokens, a $0.75 guard and 600-second timeout. The direct
Messages consumers use the same model, adaptive thinking, medium effort, 2,048
output tokens, $0.50 guards and no retry, redirect, tools or continuation.
Raw expected native budget errors remain visible; the prospectively defined
source-only terminal policy permits confirmed observations without claiming a
completed investigation. Source selection uses only already located files and
the unchanged physical 4K compiler; BM25, graph and Jev remain absent.

Only after terminal execution, immutable receipts and frozen source predictions
are the selected annotations materialized in a separate evaluation cohort.
The original empty-gold runtime is unchanged. Primary quality requires a positive
lower endpoint of the repository-cluster 95% Recall interval, 5,000 resamples,
seed 20261017, plus all 32 native receipts accepted, all 64 confirmed packet inputs,
32 valid cumulative and final-packet official scores per arm, known usage and
whole-study cost within $24. Precision and case-macro F1 are reported without a veto.

| Confirmed cumulative source, all 32 independent tasks | Recall | Precision | F1 | Allocated investigation + consumer |
| --- | ---: | ---: | ---: | ---: |
| Native packet | 74.94% | 22.73% | 30.13% | $8.07216980 |
| Late physical 4K | 79.09% | 14.83% | 22.32% | $8.82854980 |

The amended independent gate **passes**. Recall changes **+4.15 points**,
repository-cluster 95% **[+1.99, +6.85] points** across
22 canonical repository clusters. F1 changes
**-7.82 points**. The packet-only Recall/F1 is
74.94%/30.15% for native and
79.09%/22.32% for late source;
it remains secondary to cumulative source. Extra delivered source, input tokens
and cost are part of treatment; this does not establish ranking, autonomous
investigation behavior or downstream repair improvement.

Actual whole-study usage is **$11.18372580**: **$5.71699380** for 32 native SDK sessions
and **$5.46673200** for 64 paid Messages, with 64 free token-count requests.
The observed candidate allocation exceeds control by **$0.75638000** across 32
tasks, or **$0.02364 per task**. This includes the observed input/output usage
difference, not only a deterministic input-token charge.
Both phases are included once; reused prefix allocations are not a second bill.
The amended phase adds $8.61737720 after the original
$2.56634860. There are 0 missing-usage cases and zero repair calls.
Raw native statuses are `{'ok': 32}`; consumer stop reasons
are `{'end_turn': 64}`. There are
64/64 complete review responses; a capped response
could confirm source input without claiming complete reasoning or repair success.

The new 32 is sealed alongside both earlier cohorts. The passing amended gate permits a separately measured verified500 quality/cost/readiness plan.
No repair, native-harness batch, full500 run or product default starts automatically.
The original local failure and all earlier failed policies remain unchanged.

## Verified500 budget and submission gates

The current [official submission guide](https://github.com/ContextBench/contextbench.github.io/blob/a40dfa6b16f14cafd2b1cc46faf0969b38ef03b9/submissions/README.md)
requires real issue-resolution outcomes for Pass@1 alongside file/block/line
metrics. Metadata, results, README, raw logs and trajectories must be committed;
external links or LFS pointers are insufficient. Archives may be split/compressed
with checksums and an index; each committed file is capped at 50 MiB. Maintainer
review, not format validation alone, establishes a published result. A context-only
study cannot supply a repair score or a complete leaderboard submission.

Pinned verified500 IDs match the official selected500 metadata. Its language
counts are Python 266, JavaScript 60, Go 40, TypeScript 66, C 23, C++ 10, Java 15 and Rust 20.
The independent 32 does not cover 25 C++/Java tasks. Cost projections below are
arithmetic from this small six-language sample, **not representative forecasts or
whole-run reservations**. Repairs, native grading, retries, compute, quota delays
and uncovered-language uncertainty are excluded.

| Candidate-only context500 route | Gross 500-task sample arithmetic before reuse |
| --- | ---: |
| API native investigation + standard packet review | $137.95 |
| API native investigation + Batch packet review | $113.64 |
| Max native investigation + standard API packet review | $48.62 |
| Max native investigation + Batch packet review | $24.31 |

The authorized API ceiling stays **$100**, with **$40.90601127** remaining in
known usage-value accounting. A two-arm all-API context500 comparison projects
to $174.75 before repairs and is not the selected route. The separate frozen
context-only protocol below uses Max for native investigation, reuses every
qualified existing result and reserves **$40** for all **443** eligible candidate
Batch inputs, retaining one interrupted zero-source position in all500. Prior 444-position small-sample arithmetic was **$21.59** before exact counts,
output caps, margin and headroom; it is not a forecast or reservation for the
current443 inputs. The
earlier 32-only reuse scenario ($22.75 for 468 positions) remains historical.
Never choose reuse by a favorable score.
[Batch processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
offers 50% standard-price savings ($2/$10 per million Opus 5.5 input/output tokens)
and may take up to 24 hours. Only the independent final packet requests are directly
batchable; interactive SDK/tool turns do not automatically receive that discount.
The four-task route check below passes, but no new full500 Batch has been submitted.
All 443 eligible exact provider counts and the complete reservation must pass before one
paid create. No reduced subset or extra repair batch is scheduled.

Live supported Agent SDK rate events verify Max included usage on completed
ordinary500 investigations, with paid extra usage disabled. This is a current
receipt, not a reservation of whole-run quota or elapsed-time capacity. Max SDK
dollar estimates are not subscription cash charges. The existing independent32
retains its frozen API route. Context metrics can be evaluated before repairs;
real native resolution outcomes and complete raw artifact packaging remain
separate formal submission gates.

## Max and Batch: four-task route verification

Runtime `4143a045` adds the source-checkout-only
`scripts/contextbench_packet_batch.py` consumer. Its frozen protocol is
`4cb3ebd851d30369e90b57d94d7d825d8ff5328ffb6f5cb202b87e236e71d2b7`.
Four fixed known development tasks in C, Rust, Python and TypeScript first run
fresh native Max investigations,then use the unchanged late physical 4K compiler
and two final-source arms. Runtime inputs contain metadata, the already known
issue and explicit empty gold; no new annotation columns or quality scores are
loaded. All three independent 32 cohorts remain sealed.

The Max profile keeps Opus 5.5, medium effort, 20 configured turns, 4,096 requested
output tokens, $0.75 guards and 600-second timeouts. All four finish normally,
with actual main Opus 5.5,`apiKeySource=none`,verified Max subscription login and
API/provider variables removed. No API-key fallback occurs. Max SDK nominal
usage is **$1.2485557**,included once in the mixed SDK ledger; actual subscription
or extra-usage billing is unverified.

Eight exact packet prompts pass free input counts and source checks. The
input/output-cap forecast with margin/headroom is **$0.59034360**, inside a frozen
**$2** API reservation. One acknowledged Batch create is followed only by
read-only status/results calls. All eight items finish at `end_turn`, with exact
prompt, source, model, runtime and known usage receipts. Batch API usage is
**$0.430218**; the standard-price equivalent for these same actual tokens is
**$0.860436**. There are zero
unknown-usage items, retries, replacements, repairs or native tests. This measures
the cheaper route on known cases, not independent quality or Max quota for 500.

The submitter records an exclusive durable intent before the one paid create.
That file is the linearization point: a concurrent or resumed submitter cannot
create a second batch. A lost acknowledgement remains billing-unknown and never
resubmits. A saved acknowledgement can recover through read-only requests.
Result order is irrelevant: every custom ID and assistant message identity must
match the frozen request set and provider counts. Queue acceptance or free
counts get no source credit; canceled, expired and error items remain failed.
Raw results and terminal metering are durable before source/runtime validation.
Keep complete/capped response semantics separate from source input confirmation.

All **52 focused local tests**, including 29 new Batch tests, and pinned hooks pass.
Mocked direct/Batch comparisons send equal requests for the same packet and
produce equal source predictions at the declared half-price tariff. Real Max
investigations produce fresh observations; their packet bytes are not claimed
to equal older investigations. No existing quality gate or failed attempt is
reclassified by this engineering result.

The initial source-only inventory verified **119/500** exact, clean checkouts
from existing native-source manifests. A separately frozen source-only preparation
now records the terminal coverage below. No 500-task model run has started.

The public metric bridge is also verified against the
[paper's definition-level block alignment](https://arxiv.org/html/2602.05892v3)
and the [official example aggregator](https://github.com/ContextBench/contextbench.github.io/blob/a40dfa6b16f14cafd2b1cc46faf0969b38ef03b9/submissions/2026-09-12_openjiuwen-jiuwenswarm/aggregate.py):
website `block` maps to evaluator `symbol`; `span` is character-byte coverage.
Average per-task metrics, including each task's F1, rather than deriving F1 from
mean Recall/Precision. A posthoc check of the sealed32 raw reports reproduces
the frozen primary values exactly; it changes no policy or gate. File Recall
stays 85.42%; block Recall changes 82.84% → 83.60%,while line Recall changes
74.94% → 79.09%. The measured gain is primarily missing lines inside located files.

Context-only500 execution still needs source-gap handling, exact provider counts,
Max quota/time and complete budget headroom. Formal leaderboard submission also
needs real native resolution outcomes and complete artifact archives. Retrieval
quality can be evaluated before repairs; the small engineering pass starts no
automatic full500 or repair batch.

## Verified500 source preparation and existing-output reuse

Source-only runtime `50519a3a`, protocol `87b5c023`,
now verifies **500/500 exact parent snapshots**, with **zero preparation errors
or unstarted positions**. The parent-snapshot gate passes. **3 snapshots**
explicitly retain an empty Git pointer without a submodule path/URL mapping, so
**complete child-source availability is not established**. This is **zero model
calls and $0 new API usage**; it is preparation rather than a leaderboard score.

The helper prepares declared modules at their pinned commits. For an unmapped
gitlink, it records the path/SHA as `unavailable_gitlinks`, keeps it empty and
rejects injected files. It invents no child repository or contents. A missing
input never removes a task or changes a future official gold denominator.
Both cold-clone paths now audit source before publishing a new directory.

The initial strict source attempt remains failed: runtime `ccb49d53`, protocol
`38c3127c`, ended with 452 ready, 3 source errors and 45 unstarted at its 12 GiB
admission floor. A separately frozen continuation keeps all 500 positions and
records unavailable children explicitly. It uses two workers and a prospective
**8 GiB free-space floor** after the C/C++/Go/Java/Rust sources have finished;
this is admission control, not a disk quota. Terminal cache free space is
**9.01 GiB**.

Standard local Git repacking of **16 newly owned, non-donor borrowers** reclaims
**1.82 GiB** of duplicate object storage.
Every before/after pinned-source, reference, configuration, alternate and donor
check matches; native Git connectivity checks pass. The historical 119 trees, donor
objects, source files and original attempt receipts remain. Git object
representation changes do not constitute new source or model observations.

The source helper retains separate working trees and borrowed local objects;
donors must remain available throughout borrowers' lifetime. **13 local Git
tests** pass first, followed by **122 relevant context/runtime unit tests** with
eight integration cases deselected. Coverage includes shallow references,
independent files, missing mappings, nested/dirty/unprepared modules, injected
opaque contents and cold-clone source drift before publication. Pinned hooks
pass; these tests need no network/model calls.

A separate read-only audit verifies **all 56 existing packet pairs**: 24 disclosed
development positions plus all 32 sealed independent positions. The current
compiler reproduces packet bytes and source versions; full Batch request params
equal each already consumed standard Messages request. Raw native/result hashes,
actual models, known usage and confirmed input match. Native SDK/CLI and active
configuration match. The initial whole-file check accepts 32 and rejects 24,
retaining that strict failed gate. A separate complete-module AST comparison
proves the only implementation difference is two ordinary-text tokenizer keywords
in post-investigation packing. The native SDK loop does not call that packer;
all actual packets reproduce byte-identically. This documented semantic audit
accepts all 56 without changing the original strict result.
Stored native `grep_tokens` differs (2K/4K), but
the native branch constructs no grep wrapper, so this knob is inactive; the
configuration difference remains explicit. Four fresh Max engineering tasks
overlap the development 24 and add no unique reusable positions.

A further profile/source audit qualifies **32 additional original native
prefixes**, without new calls, quality scoring or annotation access. The original
strict empty advertised-catalog check fails and remains recorded; a separate
qualification proves the actual built-in catalogs equal those of the fixed
validated reference, with the same three permitted source tools. Full active
SDK/query code-path and source/profile parity match. None of the sealed cohort's
original quality conclusions changes. Thus **88 native prefixes** are reused:
56 complete context pairs plus 32 additional prefixes, leaving **412** fresh
investigations and **444** candidate consumers. Reuse includes every matching
case independent of its score.

## Canonical500 context-only baseline and execution receipts

The frozen canonical500 native protocol `cf2f2e47`, runtime `e8f6b675`, stops
after **37 attempts: 36 accepted and one interrupted**. The optional quota guard
assumed a rate event must precede assistant output; one assistant message arrived
first and the guard interrupted before terminal metering. The original execution
gate **fails and remains failed**. Its **$3.85129110** known Max nominal usage
excludes that one unknown terminal usage. Partial assistant token metadata is not
substituted for a terminal bill. The failed case called no source tools and
receives **zero source in both arms**, retained in all500 denominators without
retry, replacement or a new packet request.

The pre-label quota-order correction uses protocol `21456878`, immutable runtime
`1b0ad5ac`, with unchanged SDK options/query/source/compiler. It completes
**65 additional accepted investigations**, then stops **before another dispatch**
at the original **4 GiB artifact-space floor**. All65 have known terminal usage;
all intents reconcile and owned children are terminal. This capacity stop adds
**zero failed model cases** and does not replace the original interrupted case.

Capacity protocol `ac478f89` uses a large output volume and short `TMPDIR`, with
the same4GiB artifact floor and unchanged `1b0ad5ac` model/source/compiler/consumer
profile. It completes53 known-usage investigations;52 pass its strict ban on
unavailable-tool attempts. One normal terminal investigation also attempts
`mcp__source:read`; the SDK rejects it with an explicit `No such tool available`
error, and it returns no source. The original strict gate **fails and remains
failed**; its complete raw attempted action/error and all receipts are retained.

Prospective source-receipt protocol `4daea3d3` distinguishes this SDK-proven
rejection from executed capabilities. It keeps the exact registered read/grep/glob
toolset, requires the matching SDK error identity and `is_error=true`, and gives
the rejected action **zero source credit**. It adds no tool alias or execution.
The original confirmed reads survive: native source geometry and compiled packet
bytes are identical when only the rejected action/reply are removed. Five
adversarial boundary replays reject success, extra source, missing/duplicate
results and broader registered tools. All53 streams pass this source-only audit,
without labels, quality scoring, a new model call or a retry. No model, prompt,
search, compiler, budget or primary statistical gate changes. This operational
qualification does not turn the original strict attempt gate green.

The new run reuses **242 qualified source prefixes**, including56 complete pairs,
and continues **only257 original unclaimed IDs**. All500 IDs/source commits and
denominators remain, with one original zero-source case and three unavailable
child-source cases. Runtime gold stays empty and all three32 cohorts stay sealed.
This is the canonical population, **not fresh independent500**.

The default-off `--max-included-only` guard still requires first-party Max auth,
init `apiKeySource=none` and rate events explicitly reporting
`overageEnabled=false` and `isUsingOverage=false` before terminal case acceptance.
It retains pending raw messages and drains to terminal usage when event order
differs; a missing event rejects after metering. An explicitly unsafe event still
stops immediately. It changes no SDK query options or account settings and enables
no API fallback. The first request can precede its event, so it is not a provider
billing cap. **36 mocked SDK boundary tests**, then **142 relevant local tests**
pass, including late quota events and missing-event terminal metering. Pinned
hooks pass; no billed test tier runs.

All500 native records are now terminal. The original214-case continuation
stops after a recorded budget result is followed by the SDK's typed
`ResultError`; preserve its original strict failure. The adapter now accepts only
an exact matching budget terminal, including session/UUID/type/cost, then completes
existing tool/auth/quota/meter checks without a retry. A source-only replay with
the pinned SDK reconstructs the historical exception from the saved terminal
frame; the original exception data was not saved and this inferential provenance
is explicit. Normalized messages, source prediction and cost remain identical.

Protocol `1cda4a24`, immutable runtime `fdb6b9ee`, continues only the43 never-claimed
IDs. All43 are accepted, all terminal usage is known, and Max nominal usage is
$8.5949313 with no API charge. A real new budget terminal also passes the typed
exception path. The population is456 qualified retained source prefixes plus43
new prefixes plus the one original empty-source failure. No failed case is
retried, removed or replaced. The source receipt amendment does not turn earlier
strict failures green.

Native-only seal `f7057ac6` freezes all500 actual consumed-source predictions
before this final population's label read, disclosing the prior122 labels.
The evaluator returns499 valid and the original empty-source case as failed.
Fixed500 line task-macro Recall/precision/F1 are72.83%/27.97%/33.95%; valid499-only
line Recall is72.98%. The unchanged CLI summary is a different, micro-averaged
quantity: line Recall55.90%, precision24.24%. Both forms are retained, with the
fixed-population task macro used for comparisons. The predictions SHA-256 is
`b69e8d7a85ef3cf0f2c0de7b560477b69b5d7afd2146c0e3a8dda19d796236f9`;
metrics SHA-256 is
`6f2f02002aeae2455e171aa74d3d88a1a558c147708eab186cfe91f9d09a2eb4`.
Known selected native Max nominal usage totals$91.1558898, excluding the one
original unknown terminal usage; it is not an API cash bill. Scoring makes zero
model/API calls. These known500 cases are not an independent validation set,
indexed-context ablation or formal rank; runtime Gold remains empty.

The cumulative baseline uses source already consumed by the investigator.
All56 historical native-packet source unions equal those prefixes; existing
standard-request costs remain actual costs without rebilling or retrospective
Batch discounts. The unstarted443 extra consumers are cancelled. Known API usage
stays **$59.09398873** under the **$100** ceiling, leaving **$40.90601127**;
the previous $40 reservation is released. No new Batch/count/repair call starts.

The completed all500 seal/scoring retains earlier122 annotation access and the
failed case, with official validity separate from fixed-population accounting.
The original no-interim-Gold timing gate must not be reported as passed.
Actual indexed-context usage, a small matched development comparison and later
validation are separate gates; the plain native baseline cannot satisfy them.
Formal submission separately needs real native outcomes and complete raw
archives. Draft [PR #834](https://github.com/sysevol-ai/CodeNib/pull/834) stays
unmerged; context research proceeds before repair batches.

Raw archival preparation now covers **243 ended native attempts**, including the
original interrupted one, and **112 already consumed standard Messages**. The
125/65/53-case groups have disjoint task IDs and retain the capacity phase's
original rejected-action strict gate. Across them,
**796 file members** (**713 distinct member paths**) round-trip byte-identically;
overlapping implementation/configuration names have identical hashes. There are
**7 gzip tar-stream parts**, **11,568,526 compressed bytes** total; the largest
part is **4,258,086 bytes**, below the official50MiB cap. This is a partial local
archive, not complete500 evidence or new quality scoring. The remaining257 native
outputs and completed baseline evaluation still need archival inclusion; native
outcomes and publication review remain open. No new443 Batch archive is expected after cancellation. A source-only audit verifies all500 evaluator target paths match the
recorded snapshots, including119 aliases to retained trees; no duplicate source
checkout or source-path rewrite is needed. Archival work makes zero model/API
calls and reads no new annotations.

## Fresh independent32: seven-language retrieval-only validation

The unchanged `0a80e3bf` neighborhood policy advances to a separately frozen
32-task validation under protocol
`b4b92a3e2bd02a7fde3a86b6f86edeb83f23c2492e896ac5f3c6d4e3e0ccf038`.
Selection uses only IDs, language and repository metadata from the pinned
verified dataset, excluding 105 development IDs and all 32 earlier validation
IDs. Fixed seeded SHA ordering and repository round-robin select eight Python
tasks and four each in C, Go, Java, JavaScript, Rust and TypeScript, spanning
21 repository names. The extra four Python slots replace unavailable C++ slots;
this does **not** reproduce eight-language coverage or estimate full500 quality.
The earlier independent32 stays sealed and is never recycled.

All 32 source snapshots prepare successfully at their exact commits. Runtime
inputs contain metadata, issue text and explicit empty `gold_context`; no gold,
reference patches or native test labels are materialized before inference. After
terminal execution and frozen output hashes, only the selected gold annotations
will be loaded into a separate evaluation cohort. Preserve raw SDK attempts;
source from trailing tool results without a later assistant response is excluded
from a labeled scoring projection, with every exclusion reported. A zero-call
regression finds that message filtering alone leaves cached trajectory/served
predictions stale; a separate pre-scoring correction recomputes those fields
from the confirmed messages. It is frozen while inference is running and before
annotation access. The original analyzer is preserved; policy, tasks, budget and
promotion gates remain unchanged.

Both arms use fresh Opus 5.5 sessions, medium effort, 20 turns, 600 seconds and
4,096 maximum output tokens, with the same native read/glob and system prompt.
The candidate retains the frozen 2,048-token, 20-line physical grep neighborhood
policy. There are 64 planned SDK sessions, at most four concurrent. The batch
reserves **$22**, with $0.75 session guards and $0.25 dispatch headroom; development
usage forecasts **$14.58** from the eight-task controls. These are estimates,
not a provider hard cap or reconciled credit deduction. The existing $100
additional API authorization still applies; no repairs or native test calls run.

The primary requires a positive lower endpoint of the paired, repository-cluster
95% Recall interval across **all 32 selected tasks**, with complete source,
model/route, usage and delivery receipts. Resample canonical repository URLs
5,000 times with seed 20261017. Report Precision, per-task macro F1, failures and
cost without an F1 nondecrease veto. Equal-language macro and final 16K/32K
served contexts are secondary. No interim scoring, outcome-based replacements,
experiment retries or provider/model fallback is allowed.

The independent run is terminal: 64/64 SDK sessions are recorded, and all 32 selected
tasks remain in each arm. All source, configuration, model, route,
usage and delivery checks pass; both arms have 32 normal sessions and 32 valid
official scores. No paid attempt was retried or replaced. Seven tasks have
canonical annotation/source issues; keep those original paths and denominators unchanged
in both arms, with all 32 tasks retained. These evaluation-only diagnostics never enter
runtime inputs. Gold annotations were first loaded after immutable terminal output
receipts; reference patches and native test labels remain unused.

| Cumulative confirmed-source trajectory, all 32 tasks | Recall | Precision | F1 | API SDK estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 65.52% | 21.69% | 26.12% | $6.108574 |
| Hit-preserving nearby source | 68.93% | 18.20% | 23.30% | $6.521994 |

The paired Recall change is **+3.42 points**, repository-cluster 95% interval **[-2.27,
+10.08]** across 21 canonical repository clusters. F1 changes **-2.82 points**. The
frozen independent Recall gate fails. Do not promote this unchanged policy into repairs,
official500 or a product default. Positive development screening is preserved, but has
not established a stable independent gain.

Total API SDK usage is **$12.63056860**, against the $22 reservation, with **zero repair
calls** and 0 missing-usage sessions. Unconfirmed trailing tool results excluded from
scoring are `{"native": 0, "neighbors": 0}`; recomputed trajectory and served
predictions use only confirmed exact source. Original SDK attempts and the pre-scoring
correction remain immutable. Final served16K/32K and equal-language macros remain
secondary in the sanitized export and never replace the primary.

This new 32 is now sealed against policy tuning, alongside the old 32. At that sealing point, the remaining
untouched pool had 331 tasks and no C++ or Java cases; subsequent preview quarantine
leaves 329 as detailed above. Any later validation must
explicitly revise coverage rather than recycle these tasks. The [official submission
guide](https://github.com/ContextBench/contextbench.github.io/blob/main/submissions/README.md)
requires real issue-resolution outcomes in addition to retrieval metrics; this
context-only run is not a complete formal leaderboard submission.

## File-structure navigation: pure retrieval priority

The user-selected priority for subsequent policies is higher cumulative line
**Recall**, with Precision, F1 and cost reported alongside it. Historical frozen
gates remain unchanged; F1 nondecrease is no longer a veto on future pilots.
Repair through mini SWE-agent is downstream validation, after a context strategy
passes retrieval gates. No new repair or native-harness batch is part of this
iteration. Historical native test-collection issues do not affect the source-line
retrieval evaluator.

A read-only diagnosis reproduces the official totals on the eight disclosed
native trajectories. Of 4,408 physically available annotated lines, 2,541 are
missing: **1,768 in already observed files** and **773 in unobserved files**.
Another 125 annotated lines are unavailable at their canonical paths/ranges;
those remain in the official denominator. The 1,768/2,541 split motivates
file-local navigation, rather than establishing a policy gain.

Runtime `57387c83` adds an optional callable map to first successful file reads.
It preserves the original source response and appends at most 2,048 cl100k tokens
of parser-derived function names and 1-based ranges. Bodies are absent. Ranges
longer than 500 lines or lacking a named declaration prefix are omitted to avoid
known macro-wrapped-header ambiguities. Prior read receipts suppress repeated
maps; concurrent first reads may conservatively repeat metadata. There is no
retained file cache. Only later actual source reads can increase retrieval scores.
The same native read/grep/glob tools and system prompt remain in both arms.

Fixed-trace map construction freezes every map before annotation comparison.
It adds zero source credit and exposes callable addresses containing 1,102
previously missing annotated lines. This is an addressability diagnostic, not a
predicted Recall improvement or an adaptive-agent result. The implementation
passes 91 retrieval/source/SDK tests and pinned hooks.

A fresh pure retrieval comparison is frozen under protocol
`55896c5d1602b10bf3711ce00ba7192a0edadc40a595449666ca4693ec1bfbf9`:
first four tasks in the existing eight-task metadata order, native versus file
map, actual Opus 5.5, the explicit Anthropic API route, 20 turns and 600 seconds.
The batch reserves **$5**, with $0.75 per-session SDK guards and $0.25 dispatch
headroom. Independent32 stays sealed. The primary requires higher macro
trajectory Recall and nondecreasing F1 across all four selected tasks; final
16K/32K served metrics remain secondary. All eight sessions must complete with
verified model/route/source/usage, maps in at least three tasks and zero metadata
source credit. All reliability checks pass, but both primary metrics decline.

| Cumulative trajectory, all four selected tasks | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| Native source tools | 40.75% | 42.00% | 39.77% |
| First-read file-symbol maps | 39.37% | 36.89% | 36.83% |

The Recall difference is **-1.38 points**, exploratory paired 95% interval
**[-12.09, +6.51]**, and F1 changes **-2.95 points**, **[-8.70, +2.67]**.
All eight sessions complete with known usage for **$1.496507** in API SDK
estimates and zero repair calls. Actual maps appear in every candidate task;
metadata adds no source credit. Python and TypeScript improve Recall, C declines
and C++ ties. Keep the failed whole-cohort gate; do not expand this policy or
select its positive subset. The new Recall-first preference does not change
this result. A future positive pilot still requires larger retrieval replication
before independent validation or a small repair check.

## Hit-preserving nearby source: Recall-first pilot

Runtime `0a80e3bf` preserves every raw grep reply and appends at most 2,048
cl100k tokens of exact nearby source. It visits all verified hit anchors at each
distance, up to 20 physical lines, and omits current matches and lines already
observed in the conversation. The total reply stays within 15,900 characters;
an oversized unmodified original is preserved. Files over 2 MiB are explicitly
skipped. Exact source hashes, original replies, prior observations, delivered
lines and budget omissions are recorded. Read/glob and the system prompt remain
unchanged. There is no graph, parser expansion, BM25, Jev or second model.

The fixed-trace source replay freezes every reply before reading annotations.
It preserves all original source credit across the same eight development
trajectories. Macro Recall changes **43.29% → 48.50%**, Precision **38.51% →
29.54%** and F1 **37.08% → 33.67%**. The added coverage motivates a pilot under
the user's prospective Recall-first preference; the extra noise remains visible.
This is an opportunity replay with fixed historical calls, not an online result.

A fresh four-task comparison uses protocol
`e3c9d9d51e74967925857d4889823cdfed5ae8d80120bd8511bba4a69fe6d5f8`.
It fixes the same first four metadata positions, Opus 5.5, first-party Anthropic
API, 20 turns, 600 seconds and $0.75 SDK guard plus $0.25 dispatch headroom per
session, with a $5 batch reservation. Both arms start fresh. Independent32 stays
sealed. The primary requires higher cumulative line Recall across all four
selected tasks, plus successful exact source/model/route/usage/delivery audits;
Precision, F1 and costs are reported without an F1 veto. Final served 16K/32K
contexts are secondary. No repairs or native tests are run. All eight sessions
complete and all source/model/route/usage/delivery checks pass.

| Cumulative trajectory, all four selected tasks | Recall | Precision | F1 | API SDK estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 33.10% | 31.42% | 32.06% | $0.750282 |
| Hit-preserving nearby source | 35.83% | 26.92% | 28.93% | $1.010220 |

The Recall-first pilot screen passes with **+2.73 points**, exploratory paired
95% interval **[-18.00, +23.46]**. F1 changes **-3.13 points**, **[-17.34,
+11.08]**. Python and TypeScript improve Recall; C and C++ decline. The candidate
uses 34.6% more SDK-estimated dollars. This is a noisy development screen, not
an established strategy gain or a leaderboard advantage. Total API SDK usage is
**$1.76050270**, with zero repairs and all selected outcomes retained.

The original auxiliary audit attempted to re-execute grep and compare output
verbatim. It fails because ripgrep traversal can reorder matches and alter a
capped subset, a behavior already documented in the pinned runner. A separate
read-only follow-up keeps original attempts and official scores unchanged and
verifies captured raw hashes, their complete presence in actual model inputs,
source versions, prior observations and deterministic appended lines. The failed
audit and follow-up remain recorded; no paid attempt or evaluator result is replaced.

An unchanged-policy replication selects all remaining four disclosed development tasks
(Go, Java, JavaScript, Rust) under protocol
`d9834073c3f0021cdb71cdc0158b644ccc0247278b2d7c4a3da4480d9ca8ba3f`. It reserves another
$5 with the same model, runtime, turns, timeout and session guards. Require positive
Recall on this new four-task cohort and the combined eight, while retaining F1/cost and
every failure. It is development replication, not independent validation. No repair work
or official500 follows automatically; replication is complete: all eight additional
sessions finish, source/model/route/usage/delivery audits pass, and API SDK usage is
**$1.88522710** with zero repairs.

| Cumulative trajectory, remaining four development tasks | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| Native source tools | 37.39% | 35.58% | 30.47% |
| Hit-preserving nearby source | 41.99% | 24.07% | 27.46% |

The replication Recall difference is **+4.60 points**, exploratory paired 95% interval
**[-1.56, +11.91]**. F1 changes **-3.00 points**.

| Cumulative trajectory, all eight development tasks | Recall | Precision | F1 | API SDK estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 35.24% | 33.50% | 31.26% | $1.549202 |
| Hit-preserving nearby source | 38.91% | 25.49% | 28.20% | $2.096528 |

The combined Recall difference is **+3.66 points**, exploratory paired 95% interval
**[-8.05, +14.66]**; F1 changes **-3.07 points**. The Recall-first replication gate
passes on both the new four and combined eight. This remains development evidence
requiring fresh independent retrieval validation. The extension was conditional on the
first pilot, so the combined interval is descriptive development uncertainty, not
independent validation. Total nearby-source API SDK usage is **$3.64572980**; every
selected outcome remains counted.

## Preserved grep with read-location hints

Runtime `10d1a439` tests the same eight disclosed tasks in three fresh arms,
with actual Opus 5.5 and the same explicit Anthropic API route, source snapshots,
20 turns and 600-second limit. All 24 sessions complete and score validly.
Native retains its ordinary tools. Both hint arms preserve original grep replies
verbatim and may append at most three source ranges with 512 additional tokens.
One retains candidate order; the other uses Jev against the issue and current
grep arguments. Ranges are metadata, with no unread-source credit. Read and glob
are unchanged. No graph, BM25 index or separate planner is added.

| Cumulative trajectory, all eight selected tasks | Recall | Precision | F1 | Main-model API SDK estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 43.29% | 38.51% | 37.08% | $1.607556 |
| Original-order read suggestions | 43.21% | 37.69% | 36.34% | $1.621346 |
| Jev read suggestions | 38.73% | 36.37% | 34.17% | $1.609930 |

The primary Jev/native difference is **-4.56 Recall points**, exploratory paired
95% interval **[-10.08, -0.64]**, and **-2.91 F1 points**, **[-10.30, +2.89]**.
The prespecified unranked secondary also misses the point-score gate, with
-0.09 Recall and -0.74 F1 points against native. Against unranked hints, Jev
changes Recall/F1 by -4.48/-2.18 points. Intervals use 5,000 paired repository
resamples and seed 20261017; this is another development trial, not new held-out
evidence.

Jev makes 75 successful decisions for **$0.009593262**. Main-model API SDK usage
is **$4.83883150**; the combined **$4.848424762** is within the $10 reservation.
Every reply has a later assistant response; raw originals, candidate construction,
score order, delivered source and runtime/input hashes verify. Suggestions add
zero source credit. The 41 Jev views contain 36 suggestions matching actual later
read output, an adoption diagnostic rather than proof that hints caused a read.
Unranked hints have 42 such matches. Neither adoption nor verbatim preservation
establishes a quality gain. Do not expand this policy or continue output-format
variants on these tasks.

The motivating frozen-candidate audit finds whole hit-bearing candidates omitted
in 12 of the prior 38 Jev source-block views, including seven views losing entire
candidate files. Three have more than ten candidates but use fewer than 1,024
reply tokens. Original raw replies were stored only as hashes; unsorted ripgrep
traversal prevents exact reconstruction, so the failed raw replay is retained
and no exact historical hit-loss count is claimed. New receipts keep raw replies.

All four preselected native/Jev-hint repair attempts submit patches, costing
**$6.03557620** in Claude Max SDK estimates. Both C++ repairs use three calls,
cost $0.340808/$0.340607 and pass the same 18 native tests. The C repairs cost
$2.497070/$2.857091 and both fail the native resolution criteria. Each arm
resolves one of the two selected tasks; all failures remain counted. Source,
prompt, usage and owned-container absence audits pass. This second C++ pair does not
reproduce the earlier large single-task cost difference. The selected tasks and
all outcomes remain in the [development export](../assets/contextbench_online_development_summary.json).

A separate, frozen repair-only ablation now compares identical native 16K source
with and without up to 2K tokens of the retrieval agent's existing final answer.
It marks those notes as unverified and gives them no source credit. Notes add
input tokens; both arms share source and SDK-meter limits, rather than identical
total input-token counts. Six fresh attempts cover the already disclosed Pylint,
ponyc and fmt tasks; they reuse existing retrieval, with no new API-key inference.
The Max SDK reservation is $17. Pylint's pinned source tree matches, and its
separate empty/reference reports mark all 21 required labels unresolved/resolved;
that native task has no preservation-test list. The collection-error diagnostic
below qualifies this readiness check. No reference patch or grading feedback
enters a solver. All six attempts and native grades are now complete:

| Repeated development task | Source only | Source plus notes | Source SDK estimate | Notes SDK estimate |
| --- | --- | --- | ---: | ---: |
| Pylint, Python | Collection error | Collection error | $0.448441 | $0.867652 |
| ponyc, C | Budget error; captured patch fails tests | Budget error; patch conflicts with native tests | $3.207922 | $3.192637 |
| fmt, C++ | Resolved | Patch conflicts with native tests | $0.386881 | $0.954843 |
| All three selected tasks | **1/3** | **0/3** | **$4.043244** | **$5.015131** |

Notes cost 24.0% more in repair SDK estimates and do not improve resolution.
Stop this handoff policy. The completed ablation costs **$9.058375** in Max SDK
estimates, with zero new API-key inference calls and zero missing usage. All
frozen hashes, identical source receipts, initial prompts differing only by the
notes/advisory block, native outcomes and owned-container absence verify. Both C
budget errors remain errors; their executed patches were captured and graded.
These are repeated development tasks, not independent evidence.

A read-only failure audit limits what those unresolved reports establish. Both
Pylint attempts collect zero tests: the native test patch imports `IS_PYPY`,
which is absent at the base and introduced by the official reference production
patch. The earlier empty/reference false/true check therefore did not establish
an executable behavioral baseline. Both Multi notes patches conflict with the
native test patches before tests execute. The source-only ponyc patch does run
1,017 tests, with 1,006 passing and 11 failing. Keep all original outcomes and
selected denominators; do not label collection or patch-application failures as
proof that the production fix is wrong. Native fixture readiness and patch
application need a separate prospective protocol before broader quality claims.

## Low-cost Haiku repair control

The next screen uses the explicitly selected `claude-haiku-5-5` through the
first-party API, with a **$1 total SDK reservation**, $0.10 per-attempt guards
and $0.06 per-attempt dispatch headroom. Model availability is verified without
inference. The API route cannot fall back to Max or another model.

Six fresh attempts compare the same three disclosed tasks with an empty initial
source list or the frozen native 16K source. Issue, prompt templates, images,
20 turns, medium effort and 4,096 output-token ceiling match. Both arms may
inspect the checkout. Notes, graph and reranking are absent; independent32 stays
sealed. Frozen upstream retrieval used Opus, so repair cost and allocated
pipeline cost must be reported separately. All six repair attempts and native
reports complete for **$0.26029216** across 55 SDK queries, with no missing usage.
Actual model, first-party route, frozen hashes, initial prompts differing only
by source availability and owned-container absence all verify.

| Haiku control, all three selected tasks | Native resolved | Repair SDK queries | New repair API SDK estimate | Allocated pipeline usage estimate |
| --- | ---: | ---: | ---: | ---: |
| Issue only | 1/3 | 35 | $0.130250 | $0.130250 |
| Frozen native source | 1/3 | 20 | $0.130042 | $0.683119 |

Both C++ patches pass native resolution. In C, issue-only passes 1,006 of 1,017
tests and fails 11; source passes 1,010 and fails seven. Both remain unresolved;
the source arm retains its metered budget error and captured patch. Both Python
attempts hit the same `IS_PYPY` collection error diagnosed above. All selected
positions stay counted; the reports do not establish three valid behavioral
comparisons.

The resolution gate fails. Fewer repair queries and four fewer C test failures
are narrow development diagnostics, not independent or stable gains. The reused
Opus retrieval costs $0.55307720 across these tasks and is allocated only to the
source pipeline arm; it is not charged again in program totals. Including it
removes any pipeline-cost saving. Haiku makes the screen cheap, but a cheap
full retrieval/repair pipeline and qualified native fixtures still need evidence.
Do not advance this policy to independent32, official500 or a product default.

For prompts of at most 100,000 tokens, Haiku 5.5's standard input/output prices
are $0.10/$0.50 per million tokens, one-fortieth of Opus 5.5's $4/$20. Prompts
above that threshold use Haiku's higher $0.50/$2.50 rates. Token prices do not
establish cost per resolved issue. See the [official Haiku specifications](https://platform.claude.com/docs/en/models/haiku-5-5/overview)
and [pricing](https://platform.claude.com/docs/en/about-claude/pricing).

## Online grep/Jev on eight development tasks

Runtime `cf4bc60b` runs three fresh arms on the first task per language in the
existing disclosed 24-task order. All 24 sessions finish with actual Opus 5.5,
the explicitly authorized first-party Anthropic API route, unchanged source
snapshots, 20 turns and a 600-second limit. The independent 32 remains sealed.
No graph, BM25 index or additional search planner enters this comparison.

Native grep keeps its ordinary replies. The two source-block arms use the same
candidate construction: actual grep hits select at most 40 bounded chunks,
then at most ten blocks fit a 4,096-token reply. One keeps original order; the
other applies Jev before the next model turn. Read/glob tools are unchanged.
The primary comparison measures that complete source-delivery policy; the
Jev/chunk contrast isolates ordering within identical tools and packing, while
allowing subsequent agent queries to diverge.

| Cumulative trajectory, all eight selected tasks | Recall | Precision | F1 | Main-model API SDK estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 42.36% | 37.88% | 36.80% | $1.644856 |
| Grep source blocks, original order | 47.29% | 34.49% | 35.02% | $2.056302 |
| Grep source blocks, Jev order | 40.97% | 29.46% | 31.94% | $1.789692 |

The declared development gate fails. Against native, Jev changes Recall by
**-1.39 percentage points**, exploratory paired 95% interval **[-8.25, +3.80]**,
and F1 by **-4.86 points**, **[-9.05, -1.48]**. Against the unranked source-block
arm, the changes are -6.32 Recall points and -3.07 F1 points. All eight tasks
are distinct repositories; intervals use 5,000 paired repository resamples and
seed 20261017. The 16K handoff also loses F1, 37.44% to 32.02%.

Jev makes 55 successful decisions for **$0.00552048**. Main-model SDK usage is
**$5.49084920**, so total reported API usage value is **$5.49636968**, within the
shared $10 reservation. SDK values are separate from account billing and credit
balance. Every source reply is followed by an assistant response; source lines,
rankings, runtime/input hashes and cost receipts verify without missing usage.
A retrospective audit of the 38 live Jev views finds 31 reordered pools, but
only 12 change which source lines fit. This describes the fixed observations,
not what an unreranked agent would subsequently do.

This source-block policy is not promoted or expanded. End-to-end validation
uses the already supported fmt C++ and ponyc C tasks, selected for environment
compatibility before inspecting these scores. Each compares the fresh native
and Jev 16K handoffs through the same mature solver and separate native tests.
All four repair attempts are now complete, using Opus 5.5 through the existing
Claude Max route, common prompts, the same pinned source-only image per task and
20 turns. fmt has a $2 SDK guard per arm; ponyc has $3. Repair metering is an
SDK estimate, separate from retrieval API usage and account billing.

| Task | Native outcome | Jev outcome | Repair calls, native / Jev | Repair SDK estimate, native / Jev |
| --- | --- | --- | ---: | ---: |
| fmt, C++ | Native tests pass | Native tests pass | 6 / 3 | $0.771102 / $0.361354 |
| ponyc, C | SDK budget stop; no prediction | SDK budget stop; no prediction | 9 / 13 | $3.081903 / $3.028282 |

The two C stops remain failures in the selected-task denominator: each arm
resolves one of two tasks. Neither C attempt reaches native grading, so this
does not show that their in-container edits failed the native tests. The Jev C
attempt confirms four reads and three searches, adding 130 original-source
lines beyond its handoff; adoption alone does not establish repair quality.
Both C++ repairs pass the same 18 required tests. Their retrieval-plus-repair
meter estimates are $0.878777 and $0.473425. That single paired efficiency
observation cannot establish a stable saving or a comparative Pass@1 advantage.

The original driver stopped after the fully metered native C budget error.
An explicit execution amendment completes only the already selected, unrun Jev
arm with unchanged inputs, model, limits and the original $10 repair reservation.
There is no baseline retry. The original stop and grading receipts are retained.
All four attempts total **$7.24264130** in repair SDK estimates, with no missing
usage; frozen input/runtime hashes, common prompts and container removal verify.

The budget errors also expose an evaluation defect: the SDK exception bypasses
patch collection before container cleanup. The prospective solver fix `f607ea7c` preserves
already-executed edits after a fully metered budget stop, keeps the original
error and usage, and makes no extra model call. Unknown-usage and unrelated
errors do not use this path. Exported terminal patches receive no source credit.
The historical C failures remain unchanged; a future comparison must freeze
this collection policy before inference. These two repeated development tasks
are not an independent Pass@1 estimate, and arbitrary shell-source attribution
remains partial.
The [development summary](../assets/contextbench_online_development_summary.json)
retains every task, protocol and failed gate.

## Independent 32-task validation

The frozen selection contains four tasks per language and 22 repositories,
excluding all 105 earlier development IDs. It is independent by task, not by
repository, and is not a population-weighted estimate of the verified 500. Its
source mix is 23 Multi, five Poly, three Pro and one SWE-bench Verified task.

All three arms use actual Opus 5.5, SDK 0.2.165 / CLI 2.1.301, the same source
snapshots, 20-turn limit, 600-second timeout, $3/case and $20/arm SDK-meter guards.
The policy is frozen at `3f04993f`; runtime `4152c23d` fixes preparation-failure
accounting before any held-out inference without changing the model-facing policy.
Each arm has 31 completed and officially scored tasks, with one Git submodule
preparation failure retained as zero. No selected task is substituted or dropped.

| Cumulative observed trajectory, all 32 tasks | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 69.64% | 29.01% | 29.01% | $6.740282 |
| Online function completion | 68.56% | 24.50% | 28.22% | $8.055923 |
| Function completion + targeted graph tool | 68.76% | 23.58% | 26.16% | $8.876974 |

Scores are macro means of per-task line metrics, including per-task F1 and the
failed preparation's zero. The prespecified primary contrast is the complete
graph-enabled route against native tools; it changes both completion and graph
navigation. Its Recall difference is **-0.88 percentage points**, paired 95%
bootstrap interval **[-5.73, +3.59]**. F1 changes **-2.84 points**, interval
**[-4.78, -1.02]**. The frozen gate requires a positive lower Recall interval and
non-decreasing F1, so it fails. Intervals use 10,000 paired task resamples with
seed `20261016`.

The exploratory ablations also fail to establish an improvement. Completion
alone changes Recall by -1.08 points, interval [-5.78, +2.79], and F1 by -0.79,
interval [-2.17, +0.69]. Adding graph navigation to completion changes Recall by
+0.19 points, interval [-1.41, +1.62], and F1 by -2.05, interval [-3.81, -0.48].
These secondary intervals have no multiple-comparison adjustment.

Eleven tasks have available graphs. The candidate calls the graph tool on all
31 prepared tasks, including all eleven with graphs: 35 calls yield source on
six tasks, with 14 returned source records. Four calls receive invalid-argument
errors; other responses include unavailable graphs and definitions that cannot
fit the response budget. All observations and failures remain in the result.

The only Verified task in the selection, `scikit-learn__scikit-learn-13142`, also
receives a matched downstream solver check. With one attempt per arm, native and
graph-enabled contexts each resolve **1/1 task**, passing two required regression
tests and 54 preservation tests. This establishes no comparative or population
Pass@1 gain. Both arms and graders use the same pinned image with the selected
clean source tree; a no-network derived image restores file modes changed in the
official parent. The reference-patch check passes separately and is excluded
from agent scores. Reference patches and test feedback never enter either solver.

The [per-task CSV](../assets/contextbench_independent_32.csv) and
[frozen protocol, intervals and solver receipts](../assets/contextbench_independent_summary.json)
retain the negative result. Return to the development cohort for diagnosis;
any revised candidate needs a new validation protocol and independent task IDs.
These 32 tasks must not become a tuning set or a source of best-of retries.
A metadata-only inventory leaves 363 untouched tasks, with no C++ tasks remaining:
six of the release's ten C++ tasks are development data and four are in this
completed validation. Another independent selection requires an explicitly new
sampling protocol or new untouched tasks; the same four-per-language quota cannot
be repeated on this release. No replacement validation tasks have been selected.

## First-source graph policy on 24 development tasks

Runtime `63e035eb` freezes three fresh arms on all 24 disclosed development tasks,
three per language. It includes the eight earlier pilot tasks; these are not new
independent IDs. All arms use actual Opus 5.5, 20 turns, 600 seconds, $3/case and
$12/arm SDK-meter guards, with 2,048-token completion and graph-response budgets.
The candidate requires a graph hop after its first callable-bearing source reply
and prefers callable neighbors under the `unseen` policy.

| Cumulative trajectory, all 24 selected tasks | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools, 24 completed | 53.35% | 31.62% | 31.18% | $5.493784 |
| Function completion + first-source graph, 24 completed | 54.02% | 28.61% | 30.15% | $6.375578 |
| Function completion, interrupted | 12.06% | 4.26% | 5.43% | $0.961724 known |

The complete primary pair changes Recall by **+0.66 points**, paired 95% interval
[-4.11, +5.59], and F1 by **-1.03 points**, interval [-3.71, +1.56]. The graph arm
makes 24 calls, covering all eleven available graphs. Six tasks receive 358
previously unseen physical source lines, including 139 annotated lines on one
Go and two Rust tasks. Subsequent assistant turns confirm delivery. This meets
the declared adoption and two-language novelty gates, but F1 decreases.
Physical overlap counts include blank/comment lines and are distinct from the
official line scores.

A post-hoc comparison with the complete native trajectories finds that 90 of
those 139 annotated graph lines also appear in the native control; the other 49
are confined to one Go task. Across all 24 tasks, the candidate adds 5,151
physical lines and omits 4,204 that the native control reads, including 472 added
and 464 omitted annotated lines. These are descriptive source-set differences,
not new official scores or causal graph effects. The interrupted function-only
arm cannot separate completion from graph navigation. Delivery-time novelty and
broader graph availability therefore remain insufficient evidence of a gain.

A further zero-model diagnostic attributes the existing replies before checking
annotations. Removing grep completion from the fixed trajectories would remove
635 annotated physical lines; disabling it is not supported by this evidence.
Completion also repeats 726 previously delivered lines. The optional
`--completion-source-policy unseen` omits verified prior source while retaining
the original read/grep reply and completing each selected callable across the
conversation. It does not use old observations to select additional callables.
The historical callable selection and budget reservations are retained before
removing duplicate lines; freed space cannot add a different function.

On all 24 fixed trajectories, the historical policy reproduces all 234 source
replies exactly. The new policy changes 20 replies, removes all 726 repeated
completion-line deliveries, and reduces reply characters from 1,008,050 to
971,508 (3.63%). Cumulative physical source is identical. An initial version
refilled the freed budget, adding 63 non-annotated lines and displacing 14 other
non-annotated lines in 16K delivery. That result is retained; the implementation
now preserves selection to isolate deduplication. These are offline diagnostics, not
new model results, token-cost measurements or a demonstrated quality gain.
Repacking every revised trajectory at 16K and 32K produces byte-identical
contexts, token counts and source predictions to the archived historical payloads.

Keeping the same 24 actual graph queries and source prefixes, the separately
prepared 19-graph composition returns 932 new physical lines versus 358 with
the original eleven graphs. After correcting annotation paths, novelty at delivery
rises 139 → 356, and annotated source absent from the complete native control
rises **49 → 194 lines**: 145 on C and 49 on Go. This supplies useful offline
mechanism evidence; fresh matched agent behavior is still required to establish
quality. Query timing and target selection remain open.
Both replays retain all tasks, reproduce their historical controls exactly and
verify frozen inputs and runtime hashes. Neither makes model calls or changes
the failed development/independent gates.

The function-only arm completes four tasks, then requests an unregistered graph
tool on a C task. The event normalizer aborts before the SDK's terminal usage;
the remaining 19 tasks are not run. Four completed traces and the partial failed
trace have valid official observations, with the 19 unstarted tasks retained as
zero. The interrupted arm's scores are failure accounting, not an estimate of
the completion policy or graph's marginal effect. Its missing usage remains
unknown. The predeclared three-arm development gate fails; neither retries nor
the later reliability smoke replace these records.

Fix `25e5e31e` preserves unavailable tool attempts and SDK error replies until
terminal usage, gives them no source credit, and includes graph instructions only
when that arm registers the tool. Extra advertised capabilities still fail the
tool-isolation check. A fresh eight-turn reliability check on the original C task
finishes with a terminal result and a $0.426619 SDK estimate; it has no quality
score and does not recover the historical missing usage.

An offline preparation diagnostic explains one missing graph. The first C++
development task, `fmtlib__fmt-2394`, has no compilation database under the
read-only preparation policy. With the same source and 300-second bound, enabling
project preparation in a private clone builds 8,247 nodes and 39,953 edges in
18.18 seconds; the control returns no graph in 4.16 seconds. The original source
remains clean. This availability check makes no model calls and does not alter
the frozen graph artifacts or scores above. Broader graph coverage and a revised
matched quality comparison remain open.

The existing [development summary](../assets/contextbench_online_development_summary.json)
retains all 72 task positions, intervals, failed gates, graph-source counts,
implementation hashes and the separate reliability check. The negative independent
32-task record remains sealed; the official 500 has not been launched.

## Query timing and anchor selection follow-up

A retrospective audit found that auxiliary overlap calculations compared raw
annotation paths with repository-relative source paths. Ten of the 24 tasks have
container-prefixed annotations. The official scorer already normalizes these
paths, so official scores and intervals are unchanged. Auxiliary counts in this
page and the development export now use the pinned official
`Gold.line_spans_init()` representation. The [correction record](../assets/contextbench_annotation_correction.json)
retains per-case amendments and original input hashes. Original frozen experiment
files remain intact. The evaluator bridge now exports canonical `gold_line_spans`
for future audits; regression checks cover relative, `/testbed/` and
`/workspace/<repo>/` annotations without exposing them to model inputs.

A zero-model probe retains all 24 development tasks and the 19 available composed
graphs. It chooses anchors from actual native observations before checking
annotations, queries both directions with the same 2,048-token bounds, and makes
38 queries per policy. Deferring the same first-read anchor until after the
complete native investigation leaves annotated source absent from native unchanged
at **80 physical lines** across Python and Go. Ranking observed callables by
symbol/file BM25 instead reduces that coverage to **50 lines**, all on one Go task.
This does not establish a benefit from delay or name matching. These are different
anchors and two directions per task, so the counts are not a matched comparison
with the earlier single-query 49-line result.

The next development control, `--graph-query-policy late_issue_reference_hop`,
asks the model to investigate first and then name a relationship question that
could change the fix or its validation. It selects an already observed callable
for that question and checks incoming or outgoing references before its final
handoff. The prompt permits at most two graph calls; this is an audited prompt
constraint, not an executor quota. It does not use the diagnostic BM25 ranking.
The historical `first_callable_source_reference_hop` remains the default. Both
runtimes record the policy; direct timing-policy comparisons require every other
graph setting, completion policy, tool, model and budget to match.

Runtime `5b91323c` freezes three fresh arms on the first task per language in the
existing 24-task development order. All eight tasks have source; seven have
authenticated composed graphs. They share Opus 5.5, 20 turns, 600 seconds,
2,048-token completion and graph budgets, conversation-aware completion, and
$2/case and $6/arm SDK-meter guards. All 24 task positions finish and score,
with terminal usage, the same actual model and no API-key route or fallback.

| Cumulative trajectory, all eight selected tasks | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 39.85% | 34.14% | 32.85% | $2.038043 |
| Function completion | 40.40% | 28.87% | 31.38% | $2.433953 |
| Function completion + late graph check | 41.62% | 34.56% | 35.29% | $2.324711 |

The primary route changes Recall by **+1.77 points**, paired 95% interval
**[-4.36, +8.23]**, and F1 by **+2.45 points**, interval **[-1.06, +7.06]**.
Completion alone changes Recall/F1 by +0.55/-1.47 points. The exploratory graph
versus completion contrast changes Recall by +1.22 points, interval
[-5.49, +9.16], and F1 by +3.91 points, interval [+0.33, +8.92], without
multiple-comparison adjustment. These are reused development tasks, not an
independent validation or a leaderboard score.

Nine graph calls cover all seven available graphs. Every anchor was already
observed after at least two source replies; every result is followed by an
assistant response. No task exceeds the prompt's two-call limit. Only C and Rust
receive source: 200 and 20 new physical lines respectively. Canonical annotation
overlap is **200 lines on C, zero on Rust**; 197 of the C lines are absent from
the matched native trajectory. The two-language novelty gate therefore fails,
despite passing the point-score and execution gates. Python and Go have no unseen
neighbors; TypeScript and C++ return metadata for oversized source; Java has no
graph; both JavaScript probes have no definition at the requested line.

A zero-model fixed-trajectory check reproduces every original prediction, then
removes graph replies while keeping all later calls fixed. Macro Recall changes
41.62% → 39.80% and F1 35.29% → 34.00%. All Recall loss is on the C task. This
confirms direct source-score contribution, not what an agent would do without
graph feedback or a population improvement.
All runtime/input hashes match. The [24-row CSV](../assets/contextbench_late_graph_8.csv)
and [protocol, intervals, gate and source attribution](../assets/contextbench_late_graph_summary.json)
retain the complete result, the original raw-path audit's correction, and the
failed gate. The independent 32 remains sealed; the official 500 is not launched.

A subsequent zero-model diagnosis reproduces all nine original graph replies and
all three cached C++ development graphs before changing range selection. The
clangd graph materializer now chooses the smallest containing parsed range,
instead of the first overlapping chunk. This corrects 38 ranges across the three
graphs while preserving every compiler symbol, definition point and edge. At
`color.h:411`, navigation now selects the observed `ansi_color_escape` constructor
instead of an unrelated `rgb` constructor assigned a 520-line range. The corrected
query returns no neighbors; the other eight replies remain identical. No source
coverage or online score improvement is established. Existing frozen graphs and
scores remain unchanged; future graphs need regeneration to adopt the fix.

The oversized TypeScript neighbor has no unseen source within 16 lines of its
reference anchor, five lines within 32 and 37 within 64. The old C++ anchor has
none within 64 lines. These windows do not justify a new delivery policy. The
JavaScript file has 36 defined field nodes and 42 references owned by its file
node. Its compiler index lacks callable definitions for the assigned functions;
the chunker already supports their syntax. Adding inferred call edges would not
repair that compiler evidence gap. The linked summary includes this diagnostic's
hashes and aggregate evidence; it adds no model calls or API spend.

A compiler-only JavaScript follow-up separates that SCIP limitation from a
second problem in the existing LSP route. Identical syntax fixtures under three
project configurations reproduce missing assigned callable definitions in
`scip-typescript` 0.4.0. The TypeScript 5.9.3 language service can resolve the
NodeBB functions, but `typescript-language-server` 5.3.0 drops a function's name
span when it precedes the initializer, making reference lookup query the body
instead. The LSP client now recovers that span through the server's advertised
`navtree` command, requiring an exact name/body match and an unambiguous compiler
name span. Missing, stale or conflicting evidence retains the original symbol.
This serves existing LSP indexing and incremental updates; it neither changes
the active JavaScript SCIP route nor synthesizes relationships.

The complete existing LSP route processes all 764 original source files,
including tests, and 31,351 reference requests in 377.57 seconds with no request
errors or timeouts. A private clone uses a generated `allowJs` source list and
the observed TypeScript version is pinned; original source and runtime hashes
remain unchanged. The two targets have 26 and 18 compiler reference locations.
Replaying the original queries and message prefixes at the same 2,048-token
budget adds 132 unique physical lines and **zero annotated lines**, matching the
earlier partial probe. Even omitted neighbor ranges contain no unseen annotated
lines, so this result does not support increasing that delivery budget. The
summary retains the initial cleanup/PATH failures separately from the accepted
complete graph. The old-selection regression is reproduced and 206 related
tests pass. Useful relationship selection across languages and the active SCIP
definition gap remain open; this correction adds no model calls or API spend
and establishes no online quality gain.

A broader zero-model diagnosis reproduces all 72 frozen source predictions and
official line totals/intersections: both complete 24-task arms and all three
late eight-task arms. In the first-source candidate's 5,917 missed annotated
physical lines, 653 are in touched but incomplete callables, 3,476 elsewhere in
already observed files, and 1,457 in untouched files. The remaining 331 of 9,904
annotated lines have unavailable source locations: 303 refer to missing
canonical paths and 28 exceed the source reader's line count. Nine of the 24
tasks are affected. Eleven of the 28 out-of-range positions are just after a
file's final line. These pooled counts are not macro Recall averages.

For example, a Pylint annotation names `pylint/tests/...` while the source is
under `tests/...`; the pinned [official path normalizer](https://github.com/EuniAI/ContextBench/blob/1436c28a8eb95496da4ea69ad458b9f8a8eb7d61/contextbench/parsers/gold.py)
also removes leading dots from four Go configuration paths. The upstream main
revision remains the pinned revision as checked on October 17. The evaluator
now exports evaluation-only `gold_source_issues`, retaining every official path,
line and denominator. It does not remap plausible alternatives or grant them
read credit. Regression evaluation retains five gold lines and 40% Recall when
only two annotated lines exist and were read. The metadata matches the separate
24-task location audit; future runtime archives also include the evaluator bridge.

A predeclared follow-up spends only unused space in the same 2,048-token
completion budget on a whole remainder of an already touched short file. It
preserves every original reply, reproduces 310 source controls and all 32
original trajectory predictions, and adds no model calls. The unchanged official
evaluator scores the fixed-trajectory replay as follows:

| Fixed trajectory cohort | Recall, original → replay | F1, original → replay | Added physical / annotated lines |
| --- | ---: | ---: | ---: |
| First-source 24 | 54.02% → 56.08% | 30.15% → 29.36% | 1,918 / 153 |
| Late graph eight | 41.62% → 43.97% | 35.29% → 35.07% | 731 / 155 |

The eight tasks repeat from the 24; these are neither 32 independent tasks nor
new online outcomes. Source additions absent from matched native contain
annotations across Go, Java and Rust, but F1 falls in both cohorts. The declared
feasibility gate fails, so the file-remainder policy is not added to the agent
or advanced to model inference. The linked aggregate summary retains all rows,
protocol hashes, the initial diagnostic's missing-path failure and the amended
source classification. The next priority is useful relationship selection and
source attribution from actual solver/test feedback. All earlier failed gates,
the sealed independent 32 and the unlaunched official 500 remain unchanged.

An offline graph-opportunity audit now reproduces all 33 original graph queries
across the 24-task and overlapping eight-task development traces. It keeps the
original anchors, compiler artifacts, preceding messages and 1,000-edge scan
bounds. Among omitted definitions that individually fit a 2K reply, only Rust
has additional canonical annotated source absent from both complete trajectories
in the original direction. A source-ranking change therefore fails the declared
multi-language opportunity gate. The opposite direction has theoretical
opportunity in C, Go and TypeScript; the union of those definitions is an upper
bound, not a feasible response or new source-read credit.

A separate frozen replay tests one opposite-direction fallback per task, only
when the requested direction returns no source. It retains the original source,
anchor and later messages, uses the existing ordering and limits every reply to
2K tokens. Actual and requested directions remain explicit. Five replies change,
adding one unannotated line in the early cohort and 90 lines in the late cohort.
The latter include 71 annotated lines, 59 absent from matched native, across Go
and TypeScript.

| Fixed-trajectory cohort | Recall, original graph → replay | F1, original graph → replay |
| --- | --- | --- |
| First-source 24 | 54.017% → 54.017% | 30.148% → 30.143% |
| Late graph 8 | 41.621% → 45.659% | 35.292% → 36.591% |

The predeclared gate requires higher Recall and non-decreasing F1 in **both**
cohorts, plus new annotated source across two languages. It fails. The general
fallback is not implemented or run online. All 32 original predictions and 33
graph responses reproduce, no original source is lost, and all frozen hashes
match. A missing output directory interrupted the first scoring step after
candidate generation; a separate scoring-only follow-up retains the unchanged
predictions and gate. These diagnostics make zero model calls.

The separate late-only hypothesis now has a completed fresh three-arm comparison
on eight other disclosed development tasks. Selection takes the first remaining
task per language in the frozen file order, excludes the prior late eight, and
uses no score or graph-availability filter. Five graphs are available; all eight
tasks remain in each arm. Runtime `cf86e0a7`, actual model `claude-opus-5-5`,
20-turn limits, 2K function/graph replies and the analysis were frozen before
inference. Only the graph arm enables the explicit late unseen empty fallback:
if an available anchor returns no source, scan the opposite direction once,
keeping exact direction attribution and the same reply budget. Each direction
retains its 1,000-edge limit. This per-call policy is distinct from the failed
general replay; it inherits no passing gate.

| Fresh follow-up route, all 8 tasks | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| Native | 77.90% | 30.55% | 33.67% |
| Function completion | 72.14% | 31.55% | 32.95% |
| Functions + late graph with empty fallback | 80.57% | 31.52% | 37.55% |

All 24 runs finish `ok`, with complete metering and valid official scores.
The primary Recall difference is +2.67 points, with a paired 95% interval
[-14.86, +20.23]; F1 changes +3.88 points, with interval [-7.61, +14.89].
The declared development gate **fails**: no opposite fallback returns source,
and no annotated graph source is absent from the complete matched native trace.
The ordinary graph replies add 42 physical lines to the candidate prefix,
including five annotated Rust lines already read by native. All eight graph
calls follow observed anchors after initial source investigation; availability,
call-count, reply-budget and subscription-route checks pass.

A zero-model replay reproduces all eight replies and finds them byte-identical
with the fallback disabled. Two queries qualify for the opposite scan, but
neither returns source. The point differences therefore cannot be credited to
extra fallback source; tool guidance and the resulting search trajectories still
differ. This is a disclosed development sample, with no repair outcome or new
independent-validation claim. Do not repeat this fallback online without new
source-bound evidence of a useful mechanism. Compiler coverage and missing
anchors remain concrete follow-up work. The failed general replay, sealed
independent 32 and full-500 gate remain unchanged. Protocols, paired intervals
and aggregate outcomes are retained in the public development summary.

## Compiler coverage follow-up without model calls

The source-bound follow-up improves graph availability on the latest eight
development tasks from five to seven. It does **not** establish a retrieval gain:
replaying the same eight queries adds 146 physical source lines and **zero
annotated lines**. All original replies reproduce before replacing artifacts;
queries, preceding observations and 2K response budgets stay fixed. Candidate
replies are frozen before annotation analysis. These replies have not been
delivered to a new agent, and earlier official scores remain unchanged.

| Repaired surface | Verified outcome | Extra physical / annotated lines at the saved query |
| --- | --- | ---: |
| C++ callable names and conversion methods | Three compiler graphs rebuilt with every symbol ID, file, selection and edge preserved; 2,127 ranges corrected and 101 conversion symbols classified as methods | 1 / 0 |
| TypeScript graph preparation | Existing native decoder and read-only preparation complete within the original 300-second limit; 212,904 nodes and 467,440 edges | 108 / 0 |
| Java graph preparation | Existing managed tool, explicit Maven and command-line `java.version=8` produce 4,385 nodes and 31,158 edges; all 345 tracked files remain byte-identical | 37 / 0 |
| C graph preparation | Rejected: the private build rewrites seven tracked Autotools helper files in the pinned oniguruma submodule | 0 / 0 |

Runtime `1edb2a68` fixes conversion/overloaded operators, destructors and qualified
or pointer/reference-return declarators. Python and native clangd queries agree
on conversion-function kind 24; normalization profile v4 invalidates the former
native interpretation. Existing materialized graphs need regeneration. The
observed C++ conversion anchor now covers lines 78–81 rather than line 78 alone.
Thirteen regression cases fail before the fixes; 198 related unit tests and three C++
integration tests pass afterward, including the rebuilt native binding.

TypeScript readiness takes 268.17 seconds and Java 22.28 seconds in private
clones. These are environment diagnostics, not controlled speed comparisons.
Prior unsuccessful Java/C probes and the first overly broad C++ display-label
assertion remain recorded. The C submodule commit is unchanged, but its seven
tracked helper changes correctly fail the source guard; no rejected graph is
accepted. Original source checkouts remain clean.

The combined replay still has no language with annotated graph source absent
from full matched native. No additional model batch follows from this evidence.
The [sanitized compiler and per-query receipts](../assets/contextbench_online_development_summary.json)
record the protocols and failures. This follow-up makes zero model calls and
adds $0 in API charges; the independent 32 remains sealed and the official 500
has not started.

## Sparse discovery feasibility without model calls

The existing production BM25 indexer is also checked on all 24 disclosed
development tasks. Before retrieval, all 352,494 bounded corpus nodes are
verified against their pinned source. Each task uses its original issue, capped
at the production 16,000-character limit, to rank 40 candidates including tests.
The existing packer returns at most ten chunks and 2,048 tokens. Candidate pools
and replies are frozen before reading annotations or native source sets.

The following diagnostic unions those replies with source already observed by
native. It measures static source selection, not a new agent trajectory or
official score. The latest eight tasks overlap the 24.

| Frozen native cohort | Recall, native → source union | F1, native → source union | Newly added annotated lines |
| --- | ---: | ---: | ---: |
| First-source 24 | 53.35% → 53.57% | 31.18% → 26.62% | 38 |
| Latest follow-up eight | 77.90% → 77.90% | 33.67% → 29.51% | 0 |

All 24 preparations succeed, but both declared opportunity gates fail. F1 falls
in both cohorts; only C and C++ have novel annotated source among the first five
results, below the three-language selective-discovery gate. This evidence does
not justify automatic injection, another BM25 agent tool or a paid comparison.
It does not rule out other queries or selection policies. The
[development summary](../assets/contextbench_online_development_summary.json)
retains per-task counts, protocols and source hashes. Model calls and new API
charges are zero; earlier scores and the sealed independent sample are unchanged.

## Planned grep plus Jev on 24 development tasks

The next control uses **484 original Opus planned-grep candidates** across all
24 disclosed tasks, with at most 40 per task and no BM25 or graph selection.
It reuses the immutable plans and source-bound pools from the earlier 100-task
run; only Jev ranking makes new model calls. This is a fixed one-shot planner
comparison, not an adaptive online-agent run.

| Same grep candidates, top-ten / 4K delivery | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| Original grep order | 32.46% | 26.17% | 22.90% |
| Jev order | 36.56% | 35.45% | 28.52% |

The ranking development gate passes: Recall improves **4.10 percentage points**
and F1 **5.62 points**. Exploratory repository-bootstrap 95% intervals are
[+0.84, +7.80] and [+1.70, +10.02], with 5,000 draws and seed 20261017. All
selected tasks remain in the denominator. The full candidate pool's mean Recall
is 40.89%; the 4K budget still requires source selection. Do not compare these
4K delivery scores directly with the separate 2K BM25 control below.

Static addition remains rejected. Union with the existing native source changes
Recall 53.35% to 57.68% and F1 31.18% to 30.16%, adding 382 annotated physical
lines absent from native across four languages. The overlapping eight-task check
also loses F1 under static addition. Its ranking-only point differences are
positive, but its intervals cross zero. These fixed source sets cannot establish
what an agent would read or solve after receiving a ranked result.

All **55 Jev calls succeed**, resolve to `typesafe/jev-1.13-20260917`, and record
**$0.012341112** in API usage with no missing receipts. This prices only the new
reranking pass: reused planning, source preparation and a downstream agent are
excluded. Exact candidate permutations, every delivered source line, all frozen
hashes and the 32 baseline source-count comparisons verify. Protocol
`680e669f4b40a798a441ffed6052a6e08279ec255713ca0dab1a1c766cbd46a9`
and [per-task evidence](../assets/contextbench_online_development_summary.json)
retain the result. Stop further BM25 variants and prioritize selective Jev
ranking before source delivery in a fresh matched online-agent experiment,
without requiring graph navigation. Keep the independent 32 sealed and the
full-500 quality gate closed.

## Jev ordering control on 24 development tasks

A new matched control freezes the same 40 BM25 candidates per disclosed task,
then compares original ordering with Jev before packing source. Both arms use
at most ten chunks and 2,048 tokens, preserve whole physical lines, and include
tests. Jev uses the existing four-level relevance rubric in batches of ten;
queries, candidates and settings freeze before calls, and all ranked outputs
freeze before canonical annotation analysis. No graph is required.

| Source-set diagnostic, all 24 | Recall | F1 |
| --- | ---: | ---: |
| Original BM25 order, 2K payload | 0.53% | 0.83% |
| Same candidates, Jev order, 2K payload | 9.11% | 7.77% |
| Existing native trajectory | 53.35% | 31.18% |
| Native source union with the Jev payload | 54.03% | 27.95% |

The ranking-only development gate passes. Its exploratory repository-bootstrap
95% intervals are +2.43 to +17.98 points for Recall and +2.97 to +11.39 for F1,
using 5,000 draws and seed 20261017. Absolute coverage remains low: a post-run
candidate diagnostic finds that even the entire frozen 40-candidate pool covers
only 11.99% of annotated source on average. Reranking cannot recover source
outside that pool. It adds 143 annotated lines absent from native across four
languages, but static addition still reduces F1. The overlapping eight-task
check likewise changes native Recall 77.90% to 78.77% and F1 33.67% to 31.99%.
The static-injection gate fails; these results do not authorize automatic source
addition or establish an online-agent effect.

All 96 calls succeed as `typesafe/jev-1.13-20260917`, with **$0.025743858** in
recorded API usage and no missing cost receipts. This is a dedicated reranker
experiment within the authorized $100 API envelope, not a main-model fallback.
Every ranked candidate is an exact permutation of its frozen pool, every
rendered line matches source, and all baseline source counts reproduce the
official evaluator. The [sanitized results](../assets/contextbench_online_development_summary.json)
include per-task metrics, uncertainty, costs and integrity checks. Protocol
`056973a8b33efe9fe7666713cd1f5e1ad0ffcbb6217e75c1500794ce17e38989`
remains development-only. The separate planned-grep control above follows this
selection hypothesis; no further BM25 variant is planned. The independent 32
and official-500 gates stay unchanged.

## Keeping read completion after a canonical-source ablation

A separate fixed-trace diagnostic tests whether completion should be limited to
grep fragments. It removes only automatic completion appended to read replies
in the existing graph-arm traces. Every explicitly requested read, grep
completion, graph reply, tool call and later message stays fixed. These later
messages do not constitute a valid counterfactual agent conversation; only the
resulting source sets are compared. All transformed source sets are frozen
before annotation analysis, and both native and graph baseline line counts
reproduce the official evaluator exactly.

| Existing graph trace | Recall, original → grep-only completion | F1, original → grep-only completion | Removed physical / annotated lines |
| --- | ---: | ---: | ---: |
| First-source 24 | 54.02% → 53.14% | 30.15% → 30.12% | 1,346 / 219 |
| Latest follow-up eight | 80.57% → 80.26% | 37.55% → 38.14% | 178 / 10 |

Of the removed annotated source, 150 lines in the first cohort and seven in the
overlapping eight were also absent from matched native. The declared gate
requires preserving every already observed canonical annotated line and
non-decreasing F1 in both cohorts. It fails, so read completion remains enabled;
there is no new policy or online ablation. This result uses official normalized
annotation paths, not the early diagnostic's raw paths. The
[development summary](../assets/contextbench_online_development_summary.json)
retains all task counts and hashes. This check makes zero model calls and
changes no earlier official score or independent-validation result.

## Historical post-run 24-task results

The sample contains three tasks per language, 24 repositories and eight languages,
selected deterministically from the earlier 100-task development cohort. Every
task remains in the denominator, and all results below have 24 valid official
evaluations. Recall, precision and F1 are macro means of per-task line scores.

| Route / final context | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| Opus native agent, complete observed trajectory | 57.50% | 25.92% | 29.76% |
| Opus native agent, 16K delivery | 56.68% | 27.12% | 30.81% |
| Same observations + decision rerank, 16K | 56.76% | 27.09% | 30.81% |
| Same search + function completion, 16K | **64.97%** | 23.12% | **29.06%** |
| Opus native agent, 32K delivery | 57.26% | 26.06% | 29.94% |
| Same observations + decision rerank, 32K | 57.24% | 26.06% | 29.94% |
| Same search + function completion, 32K | **66.95%** | 21.09% | **27.42%** |
| Published JiuwenSwarm, same task IDs | 64.46% | 22.13% | 27.45% |

The public reference comes from the
[JiuwenSwarm submission](https://github.com/ContextBench/contextbench.github.io/tree/main/submissions/2026-09-12_openjiuwen-jiuwenswarm).
It includes a complete solving agent; our routes retrieve context only. The
leaderboard's full-population 75.3% Recall is a different denominator and must
not be directly compared with this 24-task estimate.

Function completion uses the existing language chunkers to read the entire
function or method touched by an observed source line, capped at 1500 lines per
callable. It does not complete whole classes/files or recursively expand from
new lines. The added source is part of a new serving stage; it is never credited
retroactively to the original Opus trajectory. This stage makes no model calls.

At 16K, completion improves Recall by 8.29 points over unchanged source delivery.
Its point estimates exceed the matched reference in Recall and F1, but the paired
95% Recall interval versus that reference is [-7.71, +7.61] points. At 32K the
Recall difference is +2.48 points, with interval [-5.75, +9.37]. Both intervals
cross zero, and this subset was used for development. Independent validation and
the official verified 500 have not been run for these policies.

## What the decision model contributed

The earlier 100-task, fixed-4K experiment is a different population:

| Opus grep route, 100 selected tasks | Recall | F1 |
| --- | ---: | ---: |
| Raw grep candidate order | 39.88% | 22.60% |
| Decision reranking | 45.70% | 27.84% |
| Graph expansion + decisions | 47.64% | 28.95% |

Two checkout failures remain zero in that denominator; graphs were available on
48 tasks. These results support reranking when a tight delivery budget forces
selection. They do not establish that decisions should control search actions or
stopping. In the later 24-task native agent, most observations already fit in the
larger context, leaving little benefit for another ordering pass.

Earlier chunk-based feedback loops also exposed the output bottleneck: using the
same candidates and scores, removing the ten-chunk cap and increasing delivery
from 4K to 32K raised Recall from 37.90% to 55.11%, while F1 fell from 30.04% to
19.72%. Increasing volume without better source selection is therefore not a
complete solution. No new decision-based product default is introduced.

## Cost and Claude subscription support

The completed 24-task native Opus run used 278 successful model calls and cost
**$23.7864** in recorded OpenRouter API charges, or $0.9911/task. Successful calls
were reused during recovery rather than billed twice. The extra decision-only
selection pass cost **$0.05941** in total; function completion cost no additional
API dollars. These exclude local compute and a downstream solver. This strong
search reference is not an ultra-low-cost serving result.

The source-checkout experiment also supports the official Claude Agent SDK with
an existing Claude Code subscription login. A Claude Max smoke completed with
actual **Opus 5.5**, eight turns and seven source-tool calls, using the same
confined `read`, `grep` and `glob` tools and the official scorer. It did not call
OpenRouter. SDK-reported dollar metering is kept separate from actual billing:
the smoke reported $0.116873 as an estimate; actual billed dollars are unknown.
Subscription limits still apply. See the
[official subscription guidance](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).

The subsequent SDK batch was stopped at the user's request after three completed
tasks and one interrupted task; twenty were not run. It is not reported as a
24-task quality result. A new online comparison was started on October 16;
the historical results in this page do not describe that new policy.

The historical Claude SDK/Jev subtotal, through the completed first-read
source-window comparisons, early/late Jev probes and the amended late-source
independent32, including all terminal failed packet attempts, has the
following known usage values. This table excludes the
historical OpenRouter reference and decision controls above; their recorded
costs remain separate. The subsequent native-Haiku and callable-tail sessions
bring known program API usage to$59.186247105 as reported above; this historical
table is unchanged. Actual account charges and credit deductions have not
been reconciled.

| Route | Known usage value | Billing interpretation |
| --- | ---: | --- |
| Direct Anthropic SDK | $48.57231736 | API estimate; promotional-credit deduction unverified |
| Separate Jev decisions | $0.369977370 | Metered API receipts |
| Direct Anthropic Messages | $9.72147600 | One paid HTTP request per source packet; token-based estimate |
| Direct Anthropic Batch | $0.43021800 | Eight paid items in one Batch; token-based estimate |
| **Combined API usage** | **$59.093988730** | **Usage value, not confirmed cash spending** |
| Claude Max SDK | $113.75041130 | SDK list-price estimate, not a confirmed subscription bill |

SDK estimates across both routes, including reported auxiliary usage, sum to
$162.32272866; that mixed-route sum is not a cash-spending total. Two historical attempts remain unmetered: one
aborted solver and one interrupted retrieval case. The earlier C graph-arm
receipt correction recovers its preserved terminal event without replacing or
rerunning that failed attempt.

The two eight-task API controls complete 48 SDK sessions. Together with the two
fixed-pool Jev controls, there are **1012 Jev requests** for **$0.369977370**.
Known API usage value totals **$59.093988730**, including the Haiku repairs
and eight subsequent file-map sessions ($1.496507) plus eight nearby-source
sessions ($1.76050270), plus eight remaining-four replication sessions
($1.88522710). The independent32 adds 64 SDK sessions for
$12.63056860. All nearby-source inference and scoring are terminal. The three new Jev probes add 731 requests for
$0.316778658; all cached replays make no new model calls. First-read source-window
online comparisons add 48 retrieval SDK sessions for
$9.86110630, with no new repairs.
The 36 packet-review SDK sessions add **$4.63143900**, including both failed
mechanism attempts. The 184 earlier retrieval sessions, 36 packet-review sessions
and 55 historical repair SDK queries total 275 direct-API SDK sessions; these
are distinct from underlying API request counts. The latest read-hint retrieval control costs
**$4.848424762** against its $10 reservation; its four Max repairs add
**$6.03557620**. No completed-batch usage is missing beyond the two historical
unknowns. The completed notes ablation adds $9.058375 in Max SDK estimates
against its $17 reservation, with no new API-key inference.

The development direct-Messages study adds48 paid requests for **$4.25474400**
and96 free token-count requests. The amended independent32 adds32 native retrieval
SDK sessions for **$5.71699380** and64 paid Messages for **$5.46673200**, with64 free counts.
Total direct-API counts are **307 SDK sessions** (216 retrieval,36 packet review,
55 historical repair), **112 Messages** and **160 free counts**. Jev stays at1012.
The mixed SDK estimate is **$162.32272866**; Messages and Batch remain separate.
The original $2.56634860 failure phase is already included, with no double billing
or unknown new usage. All three32 cohorts are sealed; no repairs/full500 started.

The four-task Max/Batch route adds **$1.2485557** in Max SDK nominal usage
and **$0.430218** in Batch API usage. Direct API SDK307 and standard Messages112
stay unchanged; four Max SDK sessions and eight Batch inferences are separate.
Free token-count requests total168 (160 prior plus8). One Batch create is not
one paid inference. All new usage is known; the same two historical Max gaps
remain. No new repair or500 model calls.

These are usage estimates and decision receipts, not reconciled account bills.
Remaining promotional credit, subscription billing and account-level extra usage
are unverified. The authorized ceiling for new API spending remains **$100**.
Provider routes are explicit, with no automatic API fallback. An in-flight call
can exceed its SDK guard. Native readiness/reference checks and artifact audits
make no model calls; their reference patches and feedback stay outside solvers.

The historical late-only eight-task control projects mechanically to about
$104–107 in SDK estimates per 500-task retrieval route, or $211 for both arms.
That is not a representative full-run budget and excludes repair/grading, retries
and quota delays. Those rejected historical policies do not justify scaling. The
late-source policy now passes the disclosed amended independent gate above; use
its separate measured cost/readiness plan before any full run. No full run has started.

## Online function-completion controls

The source-checkout runners now accept `--source-policy native|functions`.
`functions` adds the missing physical lines of a function or method touched by
a real `read` or content-mode `grep` result **before the next model turn**.
The added lines are visible to the agent and belong to that actual tool step.
This differs from the historical post-run serving refinement above.

Original observations stay first. Completion does not follow newly added lines
recursively or expand entire classes/files. An added callable must fit in full
within the additional-source budget (`--completion-tokens`, default 2,048) and
the native runner's 16,000-character response limit; skipped completion is
reported when space permits. Names-only results cannot trigger reads. These
policies remain experiments and do not change the product retrieval defaults.
The opt-in `--completion-source-policy unseen` omits lines already verified in
the conversation; the default `definitions` retains the historical behavior.
Earlier source and the added missing lines together must complete the callable.
It retains the historical callable selection before omitting duplicates, so
unused budget does not expand a different function.
This option is recorded in run configurations and checked on resume and paired
comparison. Its offline diagnostic above does not establish an online gain.

The first fresh online comparison is complete on eight development tasks, one
per language selected by CSV order from the disclosed 24-task cohort. Both arms
use actual Opus 5.5 through SDK 0.2.165 / CLI 2.1.301, the same implementation and
source snapshots, a 20-turn SDK limit, 600-second timeout, $3/case and $15/arm
SDK-meter guards. All eight cases completed and scored in both arms.
The table uses task-level macro means, including per-task F1, as in the cited
JiuwenSwarm submission. The official evaluator's CLI summary also provides
pooled-count micro aggregates; those use a different weighting.

| Observed trajectory | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 41.13% | 33.94% | 33.67% | $2.153231 |
| Online function completion | 41.67% | 29.33% | 31.91% | $2.406782 |

The paired 95% bootstrap interval for the Recall change is [-2.03, +3.36]
percentage points; for F1 it is [-4.94, +1.04]. Neither establishes an improvement.
The independent 32-task results above evaluate the frozen policy selected by
the later directed-graph development trial below.
These SDK estimates are not paid receipts, and this eight-task population is not
directly comparable with the historical 24-task table or the full leaderboard.

Use a fresh directory for each arm, with the same prepared tasks, model,
runtime, turn limit and cost settings. Existing clean task checkouts can be
reused during preparation with `--repos-root`; they are audited, never reset.
For the optional subscription SDK environment described below:

```bash
python scripts/benchmark_context_claude.py \
  --prepared results/contextbench-dev/prepared \
  --output results/contextbench-dev/sdk-native \
  --model claude-opus-5-5 --max-turns 20 --source-policy native --live
python scripts/benchmark_context_claude.py \
  --prepared results/contextbench-dev/prepared \
  --output results/contextbench-dev/sdk-functions \
  --model claude-opus-5-5 --max-turns 20 --source-policy functions --live
```

`--max-meter-usd` and `--case-meter-usd` bound SDK-reported metering, not measured
account billing. `--case-timeout` bounds each case. Unknown or invalid metering
stops later tasks; those tasks remain in the selected population. An in-flight
request can exceed an observed meter limit. Subscription quotas still apply.

Evaluate both directories using `benchmark_context_agent.py evaluate` and the
isolated official evaluator, then compare the cumulative observed trajectories:

```bash
python scripts/benchmark_context_agent.py compare \
  --baseline results/contextbench-dev/sdk-native \
  --candidate results/contextbench-dev/sdk-functions \
  --output results/contextbench-dev/sdk-comparison.json
```

Comparison rejects different harnesses, actual model identities, code snapshots,
task selections or budgets, and currently requires fresh runs rather than resumed
ones. It averages per-task Recall, precision and F1 over every selected task,
counts scoring failures as zero, preserves runtime failures and usage records,
and reports paired task-bootstrap intervals. It does not produce Pass@1 or turn
a development subset into a leaderboard result.

## Directed graph and downstream solver controls

The optional `graph_neighbors` tool follows one directed compiler-reference hop
from a source definition. Incoming references include tests; file hubs and
containment edges are excluded. It returns complete numbered definitions when
they fit the response budget, otherwise explicit metadata without source-read
credit. References are not a runtime call trace. Graphs are verified against a
frozen source manifest and graph-file hashes before use.
Inside a function or method, a selected line navigates from the enclosing callable
rather than a smaller local-symbol definition. Within the bounded raw-edge scan,
function/method neighbors precede other defined symbols while preserving direction
and edge order within each group. The default development prompt requires a hop
after the first source reply showing a callable, including grep completion,
before further source tools. This guidance is included only for graph-enabled
arms. Actual use and new source must still be checked in the recorded trajectory.
The opt-in `--graph-query-policy late_issue_reference_hop` instead requires a
relationship check after initial investigation and before the final handoff, as
described in the timing follow-up above. Neither prompt is a product default or
evidence that an agent followed the requested timing.

The experimental `--graph-source-policy unseen` skips fully observed definitions
and returns only the missing lines of partly observed definitions when all of
those lines fit. It derives observations from actual, source-checked tool replies,
not requested ranges. Known neighbors do not consume the result limit; the raw
edge scan limit remains. The default `definitions` policy returns whole
definitions. Both options retain the original reference direction and budgets.
On an offline replay of the eight development calls, `unseen` removes all 220
repeated lines but adds no new source. This is a mechanism check, not evidence of
an online quality gain. The subsequent development comparison pairs fresh native,
function-completion and early-hop/unseen graph arms under runtime `768785a4`:

| Observed trajectory, eight development tasks | Recall | Precision | F1 | Graph calls | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Native source tools | 39.15% | 35.74% | 34.27% | unavailable | $2.010707 |
| Function completion | 43.97% | 31.57% | 34.02% | unavailable | $2.666574 |
| Function completion + early/unseen graph tool | 44.03% | 36.41% | 36.60% | 0 | $2.588926 |

All tasks finish and score, but the candidate makes **zero graph calls**. The
prespecified requirement for previously unseen annotated graph source therefore
fails. The primary whole-route Recall change is +4.88 points, paired 95% interval
[-0.11, +10.45]; F1 changes +2.33 points, interval [-6.44, +11.03]. These point
differences cannot be attributed to graph-returned source. The completed 32-task
negative result remains unchanged, and no new independent validation is started.
Future development must establish actual tool use and useful novel source before
another quality cohort. Detailed values and the failed gate are retained in the
[development summary](../assets/contextbench_online_development_summary.json).

An offline diagnostic then fixes the first callable-bearing read and its observed
source prefix in each of the same eight development traces. Before reordering,
the TypeScript and Go queries fill all five outgoing result slots with fields or
types. Prioritizing callable neighbors raises the Go query's new annotated source
from 1 to 16 physical lines; the other four available graphs add no annotated
lines at those anchors. Incoming output is unchanged. Across both directions,
the union of new physical source changes from 256 to 412 lines. These counts
include blank/comment lines and do not establish a Recall/F1 gain or predict the
agent's subsequent behavior. The old annotated Go line is displaced by different
source. All three unavailable graphs remain reported.

A first original-issue Go probe calls the graph after grep supplies a complete
function, before its first read. It delivers 132 previously unseen annotated
physical lines, but fails the literal predeclared after-first-read timing gate.
That failure and its $0.201015 SDK estimate remain recorded. A revised prompt
explicitly counts callable source from grep completion or read, and is frozen for
a second one-task mechanism version. Both versions have an eight-turn, $1 SDK
meter guard, receive no selected source anchor and stay outside quality scores.
The second version passes its declared source-observation timing and novelty
gate, delivers 132 new annotated physical lines to a subsequent assistant turn,
and reports $0.126101 in SDK metering. These are two versions on one development
task, not independent quality evidence. The later 24-task development controls
above establish broader tool use but fail the quality gate. Both mechanism
records accompany the public development summary; no new independent quality
cohort is started.

Prepare graphs locally, then run two fresh `functions` arms with the same
`--graphs` directory; add `--graph-navigation` only to the candidate. The paired
comparison permits that declared tool difference while retaining the other
runtime and budget checks. The eight-task development selection has five
available graphs; all eight tasks remain selected for the graph comparison.

The first optional-graph comparison completed all eight tasks in both arms:

| Observed trajectory | Recall | Precision | F1 | Graph calls | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Function completion control | 40.23% | 28.19% | 31.11% | unavailable | $2.682236 |
| Function completion + optional graph tool | 44.56% | 32.14% | 34.28% | 0 | $2.482844 |

The Recall difference is +4.34 percentage points, paired 95% interval
[-0.06, +11.88]; F1 is +3.17 points, interval [-0.81, +7.46]. The candidate
never called the graph tool, so these scores do not demonstrate a benefit from
graph-returned source. An explicit-call integration probe succeeds, but is excluded
from quality scores. A separate fresh paired trial requests a targeted hop:

| Observed trajectory, targeted-hop implementation | Recall | Precision | F1 | Graph calls | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Function completion control | 37.43% | 28.40% | 30.18% | unavailable | $2.497661 |
| Function completion + targeted graph tool | 41.29% | 32.16% | 32.85% | 8 | $2.702899 |

All eight tasks complete and score in both arms. The Recall difference is +3.86
points, paired 95% interval [-0.51, +11.82]; F1 is +2.67 points, interval
[-1.11, +6.84]. Three graph calls return source, one returns an oversized-definition
notice, one finds no definition at the selected line, and three report unavailable
graphs. All five available graphs are called. This passes the development
candidate-selection gate fixed before scoring: actual graph use, higher Recall
and non-decreasing F1. The intervals still do not establish a reliable gain.

A subsequent development-only source audit finds that the three successful
graph responses contain 220 physical lines, all already observed by the agent.
Actual graph use therefore did not add new source in this trial. In the same
eight-task traces, grep-triggered completion contributes most added source but
also most newly observed annotated lines. These descriptive counts do not prove
that removing grep completion would improve an online agent. Future development
must measure useful new source and test selection policies before another
independent validation run; the completed 32 tasks remain excluded from tuning.

The policy at `3f04993f` is frozen for independent 32-task validation with native,
functions and functions-plus-graph arms. Eleven cases have available graphs;
all three arms have completed and scored. The primary contrast is the complete
graph-enabled route against native source tools, with the other two contrasts as
explanatory ablations. The independent quality gate fails, as reported above.
The 32 tasks remain excluded from tuning. Neither graph development trial is
directly comparable with the earlier native/functions implementation freeze.
Preflight retains 31 ready snapshots and one failed Git submodule preparation.
Before held-out inference, `4152c23d` fixes failure accounting so that known
preparation failures remain zero without cancelling other selected tasks. The
model-facing policy is unchanged and all 32 tasks remain in every arm.

```bash
python scripts/contextbench_graph_navigation.py \
  --prepared results/contextbench-dev/prepared \
  --output results/contextbench-dev/graphs --timeout 120
```

For a fresh graph build, `--allow-project-preparation` permits the existing
backend's dependency and build preparation inside its disposable clone. The
worker rejects changes to the selected commit or tracked source before publishing
a graph; generated build files are allowed. Timeout cleanup reaps the worker
before deleting that clone. The source checkout used by the agent stays clean.
The option defaults off, is recorded with the timeout in the graph manifest,
and cannot be combined with `--reuse-cohort`. Freeze these new artifacts before
any matched inference; they do not revise earlier results.

An offline 24-task preparation check obtains 14 graphs, compared with eleven
previously: eight become available and five are lost. Six Go/Java attempts lack
the managed indexers on their invocation PATH, two TypeScript builds time out,
and two builds are rejected after changing tracked source. Both the timeout and
preparation policy also differ, so this is not a policy-only comparison. All
source checkouts remain clean. On fixed native-trace prefixes, new physical
source rises from 687 to 890 lines, while newly covered annotated lines fall from
177 to 44 after canonical path normalization, largely because the Go graphs are
unavailable. This replay makes no
model calls and is not a Recall/F1 score or an autonomous agent result.

A separate prospective input combines fourteen fresh graphs with five
authenticated prior graphs for the identical source snapshots, leaving five
unavailable cases in the 24-task population. Artifact selection uses availability
alone. Its 19 available graphs have not undergone new online quality evaluation;
all prior build failures and negative quality results remain in the record.

`scripts/benchmark_context_solver.py` provides an initial downstream adapter for
**SWE-bench Verified and the Multi-SWE-bench fmt, Gson and ponyc pilots**. It runs the
pinned upstream mini SWE-agent loop with
the already-observed 16K/32K context, an existing Claude subscription and an
already-local Docker image identified by digest. The image must have the same
clean source tree as the selected base commit. The solver container has no
network access or host credentials. SDK metering, failed attempts and missing
usage remain explicit.

The `grade` command requires an isolated `swebench==4.1.0` environment and the
corresponding native Verified dataset row in JSON format. It uses the native test
script, reference test patch and resolution grader on the exact solver image:

```bash
python scripts/benchmark_context_solver.py grade \
  --solver-run results/contextbench-dev/solver-case \
  --dataset results/contextbench-dev/native-verified-case.json \
  --output results/contextbench-dev/solver-case-grade
```

The first completed development attempt produced a patch that applied but failed
the required native tests: **0/1 resolved**. Its generation reported $0.555974 in
SDK metering; preliminary aborted attempts include missing usage, so this is not
a total paid-cost figure. This single attempt is not a comparative Pass@1 result.
Other Multi repositories, Poly/Pro adapters and full-agent source accounting
remain open. Arbitrary
shell reads from this solver are not added to the retrieval trajectory or claimed
as a complete leaderboard artifact.

The first Multi-SWE-bench native environment check is complete on the development
C++ task `fmtlib__fmt-2394`. The
[pinned native harness](https://github.com/multi-swe-bench/multi-swe-bench/tree/24f493f8a103e72312ded4f6b9c89f081d69cb09)
rejects a test-only
control and accepts the reference repair, passing all 18 required tests. Both
use the exact selected source tree and native patch hashes in the same official
image; both owned containers are removed. This check makes no model calls and
produces no CodeNib repair score. A separate solver image removes the reference
patches and scripts, retains only one source commit, and verifies an identical
clean tree. The fmt pilot now has a source-only solver route and a separate native
grader. The solver checks for retained reference files and history before any
model request. The grader authenticates native patches, replaces the production
reference with the exact solver patch, and then runs the original script. Other
Multi repositories require their own native environment validation and adapters.
The public development summary retains image provenance, module hashes and
reports separately from agent quality results.

Run Multi grading from the repository root in an isolated environment containing
the pinned `multi-swe-bench==1.1.0` installation. The native grader entry point
does not import retrieval or model dependencies:

```bash
python -m scripts.contextbench_multiswe \
  --solver-run results/contextbench-dev/solver-fmt \
  --dataset results/contextbench-dev/native-fmt-case.jsonl \
  --native-image sha256:EXACT_LOCAL_NATIVE_IMAGE_ID \
  --output results/contextbench-dev/solver-fmt-grade
```

The native image must differ from the source-only solver image. Both must match
the selected source tree; their mutable tags are insufficient. Timeout, failed
patches, incomplete native reports and cleanup failures remain explicit. This
adapter supports the validated `fmtlib/fmt`, `google/gson` and `ponylang/ponyc`
pilots; it does not
imply complete Multi benchmark coverage. All three native test entrypoints are
authenticated against the pinned harness before any submitted patch executes.

The new adapter rejects its unchanged-issue control. Two initial reference checks
fail at the 60-second container-start bound before tests begin; both containers
are removed. The bounded startup allowance is now 180 seconds, recorded
separately from test time. A fresh check at that bound rejects the unchanged-issue
control and accepts the reference repair with all 18 tests passing; both
containers are removed and the frozen runtime hashes match. The original two
startup failures remain recorded. These environment checks make no model calls
or agent scores.

A subsequent frozen C++ development pair at `fe4cbb72` reuses the first-source
trial's native and graph-configured 16K contexts for `fmtlib__fmt-2394`. Both
solvers submit a production-only patch and both resolve the task, **1/1 each**,
passing all 18 native tests. Each makes five model calls; all ten inputs have
confirmed receipts and terminal metering. SDK estimates are $0.734907 native and
$0.586895 candidate, $1.321802 combined. Both use Claude Max and the same source-only
image, model, 20-step limit and $2 SDK-meter guard; every solver and grader
container is removed. Runtime and retrieval-input hashes remain unchanged through
grading. This is one unique development task and does not establish a comparative
Pass@1 gain. Its graph was unavailable during the original retrieval, so this
result cannot establish a contribution from graph-returned source. The negative
24-task and independent 32-task retrieval gates remain unchanged.

The Java `google__gson-1391` development task now passes the same native readiness
gates. Its test-only control fails the required regression class while preserving
95 other required classes; the reference repair passes all 96 required classes.
Both Maven processes exit 1 because `DefaultDateTypeAdapterTest` also fails in
the pinned original dataset. Complete passed/failed/skipped class sets match that
dataset, and the official grader accepts the reference. These are test-class
counts, not individual JUnit assertion counts.

A separate source-only Gson image retains the exact clean source tree, removes
reference files and history, and passes a real controlled-reader preflight with
Python 3.10.12. Grading-only fixtures through the implemented adapter reject a
behavior-preserving production comment and accept the reference patch; neither
fixture is an agent repair. All owned containers are removed, frozen hashes
match, and these checks make zero model calls. The
[public development summary](../assets/contextbench_online_development_summary.json)
retains the pinned dataset, image provenance, source-only Dockerfile, parser
parity and distinct control outcomes. The new entrypoint checks also match the
existing fmt image.

A fresh Java solver pair at `0039b2a2` then reuses the same task's frozen native
and function-completion contexts, with identical source-only images, actual
Opus 5.5, common reader instructions, 16K context limits, 20 steps and $2 SDK-meter
guards. Both submit production-only fixes and resolve **1/1 task**, passing all
96 required classes; the known unrelated class failure remains recorded. All
eleven model inputs are confirmed, every SDK init reports `apiKeySource=none`,
and no usage is missing. SDK estimates are $0.500642 native and $0.587861 candidate,
**$1.088503 combined**. Both solvers and both graders remove their containers;
runtime and input hashes still match after grading.

Each arm makes one confirmed base-version read after an explicit usage error.
Initial source coverage is 318/293 lines; the reader adds 0/1 new original lines,
leaving partial totals of 318/294. Two/three delivered ordinary shell observations
remain unattributed. The task had no graph in its original retrieval. These
matched outcomes establish Java pipeline coverage, not a comparative repair,
graph-source or independent quality gain.

Jackson-databind remains deferred: its pinned preparation script rewrites tracked
build configuration, conflicting with the current exact-clean-source gate. This
is a source-inspection finding; no Jackson image or model probe was run.

The C task `ponylang__ponyc-2205` now has native reference/control and isolated
source-image checks. Its test-only control preserves 1,010 tests and fails seven
required regression tests. The reference passes all 1,057 required tests,
including 40 later-stage runtime tests. Both complete parser sets match the
pinned dataset. The full reference command still exits 2: standard-library
linking cannot find `crypto`, `pcre2-8` and `ssl`. The unmodified source-only
image reproduces that failure after all 1,045 baseline tests pass. Required-label
grading succeeds; the full standard-library suite remains unverified.

The official image lacks the Python 3 interpreter required by the reader. The
source-only image adds a
[fixed standalone CPython 3.11.14 release](https://github.com/astral-sh/python-build-standalone/releases/tag/20260211),
verified against its published archive hash, and precompiles only the original
source. The original native grader image remains unchanged. Both cold and warm
source-only images pass exact-tree, removed-reference/history and controlled-read
checks; the warm image remains clean after baseline testing. All owned containers
are removed. These checks make no model calls and are not agent repair results.

This task is chosen from disclosed development data because its earlier graph
query supplied previously unseen source: all 200 returned lines survive the
candidate's 16K payload, with 197 absent from the native payload. Selection uses prior
development evidence and is not an unbiased or independent sample. Function
completion already contains the full range covering that graph response, so it
is not source unique to the graph arm. The candidate also adds 190 and removes
283 initial source lines relative to the function arm; these contexts do not
isolate a graph-only causal effect.

A fresh three-arm solver comparison at frozen `6c1585b8` is complete, using the
same model, common reader, 20-step limit and $3 SDK guard per arm:

| Arm | Native outcome | Passing / failing tests | Model calls | Reconciled SDK estimate |
| --- | --- | --- | --- | --- |
| Native | Unresolved | 1,005 / 12 | 9 | $2.134897 |
| Functions | Unresolved | 1,006 / 11 | 9 | $2.684968 |
| Functions + graph | Budget error; no prediction | Not graded | 11 | $3.295746 |

Native fixes none of the seven required regressions; functions fixes one.
Both also fail five preservation tests and stop before later runtime and
standard-library tests. The graph arm's final request triggers the SDK budget
error and produces no submitted patch. Its failure counts as zero, with no retry
or invented native report. All 29 model inputs are confirmed, with subscription
initializers reporting no API key. The controlled reader adds 0/191/0 new
original-source lines; partial totals are 1,025/1,677/1,393. Ordinary shell output
remains unattributed. Five owned solver/grader containers are removed and verified
absent; frozen runtime/input hashes match after grading.

A separate source audit explains the five preservation failures more precisely:
those compatibility tests are introduced by the grader's test patch, in a file
absent from base source. Both submitted solvers report passing all 1,005 base
compiler tests they run. Native preservation labels describe behavior that must
pass, not necessarily tests already available to the solver. All three initial
contexts already contain the complete 25-line argument-casting helper and
91-line FFI call implementation. This evidence does not establish a missing
source-delivery gap for those constraints or justify expanding the same functions
again. No grading-only test or reference patch is supplied to a model.

The final graph request preserves a valid $0.321885 terminal event before the
SDK raises. Original aggregation misses it because accounting occurred after
stream completion. An offline amendment retains the original failed run and
reconciles total SDK metering to $8.115611; no current call remains unmetered.
The adapter now persists terminal usage before advancing the stream, with a
regression reproducing the old loss. SDK errors still stop inference. This
single disclosed task establishes no comparative repair gain or independent
quality result. Provenance, recipes, original and corrected accounting, and the
incomplete standard-library coverage remain in the public development summary.

New solver runs save each exact model request with a content hash and confirm
delivery only when the SDK returns an assistant message. The local
`model-observations.json` export therefore contains the shell output after the
native loop's formatting and truncation. Elided middle output and a final
submission diff that never reaches another model request are excluded. Missing
historical receipts and unconfirmed requests remain explicit; their billing may
still be unknown. This completes the raw observation boundary, while mapping
arbitrary shell output to source lines remains an open evaluation gate.

The adapter supplies a controlled source command in the same native loop:
`codenib-read PATH START END`, with 1-based inclusive lines, a 2 MiB text-file
limit and at most 2,000 requested lines. Both arms receive the same command and
instructions. Each read records the exact file version, pinned original blob,
rendered observation and its position in the conversation. Only complete lines
in a later confirmed model input receive coverage; native output truncation
still applies. Initial delivered context is also checked against its pinned
source before inference. Use the reader as the entire bash action; compound
invocations receive an explicit usage error. Edits and tests continue through
the existing shell.

The same boundary now supports `codenib-grep PATTERN PATH [LIMIT]`, using a
Python regex and repository-relative file or directory. It searches tracked and
non-ignored untracked text, including tests, with 100 matches by default and at
most 200. Each file remains bounded at 2 MiB; the search scans at most 512 paths
and returns at most 12,000 characters of complete match records. The output
reports scan/output limits and skipped files. Narrow the path or use the reader
when a limit is reached. Every JSON match carries its own file, line and text,
so a truncated multi-file response cannot attribute identical text to another
file. Each file's captured bytes supply both its source hash and matches.

`model-observations.json` labels this source attribution **partial**. Ordinary
shell output remains explicitly unattributed, and changed files retain their
current-version observations without guessed original line numbers. These
coverage records are not submitted to the official scorer as full trajectories.
The added instruction changes the solver protocol and therefore requires fresh
matched runs; earlier results remain frozen.

A zero-model search verification in the pinned fmt source-only container passes
with eight scripted replies. It captures 20 implementation matches and six test
matches; a larger search returns 113 matches, of which 94 complete records
survive native truncation. Twenty matches after an edit receive no original-file
credit, as do ordinary grep, an unconfirmed input and the terminal diff. This
checks instrumentation, not real model adoption or repair quality. The first
scripted attempt had an invalid edit-command string and is retained as failed;
the corrected fixture passes without changing the implementation. Both owned
containers are removed. The [sanitized verification](../assets/contextbench_online_development_summary.json)
records hashes, limits and outcomes. Relevant verification totals 104 unit and
three pinned upstream integration passes, with no model calls or API charges.

A separately frozen actual-model check on the disclosed ponyc development task
does **not** establish controlled-search adoption: Opus 5.5 makes nine confirmed
model calls and two controlled reads, but no controlled search. The reads add
70 original-source lines to the initial 1,025; six ordinary shell observations
remain unattributed. The submitted repair fails eleven native tests. Inference
and grading preserve every frozen input/runtime hash and remove both owned
containers. This repeated one-task check costs $2.284219 in SDK estimates through
Claude Max, with no API-key charge. It provides no comparative quality result;
source coverage remains partial. Its protocol hash is
`050dd033f6ce718b8d5df39c53fc4fcbabb609e071902c7396f6bfd5730020b5`.

The initial [source-receipt verification](../assets/contextbench_solver_source_summary.json)
makes no model calls. An audit of the existing fmt pair preserves all ten actual
model-input receipts and verifies 411/375 initial source lines. Each arm still
has four delivered shell observations without execution receipts; none gains
retrospective source credit. A separate scripted loop in the pinned source-only
container delivers 36 complete lines from one read and 217 from a larger,
627-line file after native truncation. Four lines read after an edit receive no
original-source credit, and the final submission diff is excluded. Scripted
replies test the instrumentation only; they are not additional model runs or
repair outcomes. The container is removed and source/runtime hashes match.

A separate, predeclared real adoption check then reuses that fmt task and the
native 16K context. With the frozen reader implementation, actual Opus 5.5,
12 turns and a $2 SDK-meter guard, it makes five model calls at **$0.695992** in
SDK estimates. The first action chains the reader after `cd` and combines two
reads; the standalone-only adapter forwards it to the shell, which reports the
command missing. The model falls back to `sed`. **Reader adoption fails**:
there are no confirmed controlled reads, and four delivered shell observations
remain unattributed. The original 411 initial-source lines retain credit only
in the explicitly partial coverage artifact.

The submitted production patch resolves this repeated development task and
passes all 18 native tests; it establishes no comparative improvement. Both
containers are removed and all frozen hashes still match after grading. The
compound-invocation bug is reproduced, then fixed with explicit feedback about
the available reader's standalone interface. Quoted mentions remain ordinary
shell output. No retry or further model batch belongs to that closed protocol.

A new, separately frozen check at `0b2b5964` keeps the same source, native 16K
context, model, 12-turn limit and $2 guard. After one explicit compound-invocation
error, the model uses two standalone reads that reach confirmed model inputs.
The eight calls report **$0.932850** in SDK estimates; every SDK init reports
`apiKeySource=none`. Reader adoption passes, and the submitted patch passes all
18 native tests. Both reads cover source already present in the initial context:
there are **zero new original-source lines**, and coverage stays at 411 lines.
Four ordinary shell observations remain unattributed. Both containers are removed
and frozen hashes match after grading. This verifies the corrected interface,
not a comparative repair or retrieval gain. Fresh matched runs and complete
source accounting remain open; no earlier quality gate or official score changes.

An earlier development solver pair using the model-input recorder is **0/1
resolved in each arm** on the same previously disclosed Python task. All seven native-context
inputs and eleven graph-enabled-context inputs have confirmed receipts. These
are repeated development attempts, not additional independent solver tasks.

A subsequent matched check feeds the targeted-hop trial's two 16K contexts into
fresh solver runs on that same Python development task. Both use the same pinned
image, implementation, actual model, 30-step limit and $3 SDK-meter guard. Both
patches apply, but each arm is **0/1 resolved**: the native test patch imports a
definition omitted by both generated production patches. The official reference
production patch passes in the same image and native harness; that grading-only
check is excluded from agent scores and makes no model calls. Paired solver SDK
estimates are $0.826470 for the control and $0.442726 for the graph candidate.
These repeated development attempts do not establish comparative Pass@1, and
reference patches or test feedback are never passed back to either solver.

## Evidence and reproduction

The [per-case CSV](../assets/contextbench_development_24.csv) and
[summary JSON](../assets/contextbench_development_summary.json) contain the exact
24-task metrics, costs, revisions and uncertainty estimates. They exclude source
text, prompts, provider identifiers and credentials. The raw local receipts remain
under ignored `verification/` directories; these summary exports are not a complete
official submission artifact bundle.

The initial October 16 online comparison has a separate
[eight-task CSV](../assets/contextbench_online_development_8.csv) and
[protocol, metering and paired-interval summary](../assets/contextbench_online_development_summary.json).
They retain the negative result and exclude prompts, source excerpts, account
identities and provider request identifiers. The summary also contains the later
24-task first-source trial, including its interrupted function-only arm; the
eight-task CSV remains the original comparison.

The dataset revision is `c2855792b006af41c67202d33883fb9d46362853` and evaluator
revision is `1436c28a8eb95496da4ea69ad458b9f8a8eb7d61`. The recorded native and
refinement experiments preceded integration with current `main`; archived
implementation hashes are retained with their receipts. See
[ContextBench setup and scoring](contextbench.md) and
[dataset/evaluation ownership](architecture.md).

To rerun the same development IDs from a source checkout, first follow the
[installation steps](contextbench.md#installation), then export the pinned tasks
in the CSV's recorded order:

```bash
python - <<'PY'
import csv
import json
from pathlib import Path
from codenib.dataset.contextbench import load_contextbench_cases

with Path("docs/assets/contextbench_development_24.csv").open() as stream:
    ids = [row["instance_id"] for row in csv.DictReader(stream)]
cases = load_contextbench_cases(config="contextbench_verified", instance_ids=ids)
by_id = {case.instance_id: case for case in cases}
output = Path("results/contextbench-dev")
output.mkdir(parents=True, exist_ok=True)
(output / "selected.jsonl").write_text(
    "".join(json.dumps(by_id[key].to_record()) + "\n" for key in ids)
)
PY
python scripts/benchmark_context_agent.py prepare \
  --cases results/contextbench-dev/selected.jsonl \
  --output results/contextbench-dev/prepared
```

The following `run` explicitly enables new OpenRouter charges. Its cost guards
use observed spending; in-flight requests can exceed the thresholds. Function
completion and evaluation make no model calls:

```bash
python scripts/benchmark_context_agent.py run \
  --prepared results/contextbench-dev/prepared \
  --output results/contextbench-dev/native \
  --max-turns 20 --workers 4 --max-cost-usd 30 --case-cost-usd 3 --live
python scripts/benchmark_context_agent.py refine \
  --input results/contextbench-dev/native \
  --output results/contextbench-dev/functions --method functions
python scripts/benchmark_context_agent.py evaluate \
  --output results/contextbench-dev/functions \
  --upstream /tmp/ContextBench \
  --evaluator-python /tmp/contextbench-evaluator/bin/python
```

The SDK uses an optional `claude-agent-sdk==0.2.165` environment and an existing
logged-in Claude Code CLI; the smoke used CLI 2.1.296. Model calls require an
explicit `--live` flag. `benchmark_context_claude.py --help` describes the alternate
subscription runner; it uses a different agent loop and is not a transport-only
ablation. Reproduction runs use the current implementation and live model endpoint,
so they need not reproduce the historical scores exactly. The completed independent
32-task selection excludes all 105 earlier development IDs and rejects the tested
candidate. Future leaderboard claims require a new independently validated policy,
matched failure accounting and actual solver outcomes for Pass@1.
