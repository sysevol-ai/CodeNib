# Keep isolated SDK result files readable without trusting tool paths

The stopped task-fit round exposes a controller defect: Claude saves a large
MCP result on the SDK host, but the next native Bash command runs in a container
without that file. This source-checkout pilot helper authenticates and mirrors
only the current session's original result. It changes the experiment runner,
not CodeNib's product tools, public package or prior task outcomes.

The [execution record](codegraph_sdk_output_delivery_2026_10_30.json) retains
model-free controls using the real Claude CLI 2.1.310, Agent SDK 0.2.174, MCP
stdio and isolated containers. Scripted local HTTP responses drive the tool
calls. No model inference, provider request, voluntary selection or task
acceptance is measured.

## Authenticate the result before granting access

The native callback binds each tool call to its SDK session, transcript,
workspace and call ID. A private stdio proxy records the exact original MCP
request/reply bytes before forwarding them unchanged. The model and tool
container cannot access that private receipt directory.

Two observed output paths require different timing:

- A specialized MCP wrapper names a text file before the post-tool callback.
  File bytes must match the original single text reply and its declared
  character count.
- The CLI can write a JSON content-block file **after** the post-tool callback.
  Before the next tool runs, inspect only the completed call ID's filename in
  that session's result directory. Require every block to match both the
  original callback and the private MCP reply. Do not infer file authority from
  a shell command or arbitrary tool text.

Both paths reject other sessions, missing/wrong replies, earlier completed
receipts, changed arguments, foreign owners, links and file swaps. Reads use
bounded descriptors through non-symlink directory components. Registration
occurs only after a verified copy in the same owned container; a failed copy
grants no host access. Native Read/Grep revalidate registered host bytes, while
Bash checks registered container copies before execution. Edit/Write remain
limited to the source checkout. Callback failures return an explicit denial.

The copy is read-only in the container and contains one authenticated result,
not a mounted SDK home or credential directory. Copying proves availability,
not model consumption or source credit. Arbitrary Bash output still needs its
own exact observation/version mapping before any solver source credit.
Wire frames and individual files have a 16 MiB cap. Oversized, changed or
unmatched results are rejected rather than credited as complete observations.

Reconnects create separate private receipt directories, preventing request-ID
or file-counter reuse without a cross-process counter or retained lock. The
proxy owns its active child process group; the workflow owns the target
container and removes outstanding server work after SDK disconnect. Lost
container-create replies recover by the pre-recorded owner label; recovery
does not remove another owner.

## Actual CLI control

The original 82,346-byte payload remains SHA-256
`ea6321aab3ba78593a2680d0dee60449e765633cda3bb599ba20a5a60c44892d`.
Both conditions use fresh controllers and targets, each with two CPUs, 4 GiB
and networking disabled. Only the trusted controller receives the Docker
socket. The target receives neither that socket nor SDK credentials. The CLI
uses an empty configuration and a dummy credential for its loopback HTTP
fixture; no real login is copied or account quota used.

| Original observed boundary | Frozen bridge | Repaired bridge |
| --- | --- | --- |
| CLI spills the complete MCP block array | Yes | Yes |
| Following Bash opens its SDK result in the target | FileNotFoundError | Pass |
| Following fixture request contains the original text SHA | No | Yes |
| Following fixture request contains the complete selected source | No | Yes |
| Original SDK successful terminal | Yes | Yes |

The SDK's JSON block file is 88,568 bytes, SHA-256
`24b94785269bf96239416bb42988bc2b2c11c4ddb46fe1fac79e9c255be50b70`.
Its encoding differs from the original text file; the single text block keeps
every original byte. The Bash read emits that text's hash and one complete
selected source string into the next **fixture HTTP request**. This is genuine
CLI/file/transport execution, not evidence of a real model reading the whole
payload or solving the task. Both fixtures finish; the original bridge's file
failure remains a failure despite its synthetic successful SDK terminal.

Keep all earlier controller outcomes. The first attempt cannot import the
SDK's missing `sniffio` dependency. The second exposes the later JSON-file
renderer, and its old-wrapper-only fixture handler times out. Corrected
controls complete with immutable runtime snapshots; a follow-up includes the
reconnection-safe proxy. The original pre-deferred helper is reconstructed
against its exact recorded SHA, preserving the earlier input version rather
than claiming that its live checkout stayed unchanged. No failed model task
is retried or replaced.

## Verification and next gate

Twenty-three focused tests cover both spill formats, multiple blocks,
reconnection, exact wire IDs/bytes, foreign sessions/owners, links, file swaps,
tampering, failed copies, typed native Bash output and explicit hook denials.
All 8,945 unit cases pass, with 24 skips. The actual container finalizer also
recovers a created-but-unrecorded container and leaves the foreign-owner
control intact.
The two-part public packet reconstructs 74 published files, including exact
tool observations and original MCP frames. Complete original SDK archives stay
local and hash-bound; the publication omits unrelated default system/tool
specifications and generated session keys. All 5,855 prior sealed inputs and
416 preceding JSON records remain unchanged, and all 14 control/recovery
containers are independently confirmed absent.

This fixes a bounded runner admission. Before a new model cohort, bind all
conditions to the same helper/transport policy, re-admit the actual products,
verify native subscription authentication and effective model settings, then
seal new inputs. Preserve the original 300-second timeout, missing usage,
source judgments and every first-use cost. The stopped task-fit cohort stays
closed. Correctness parity, 20% complete-task improvement, two independent
confirmation sets and external enrollment/reuse at 0/5 remain open.
