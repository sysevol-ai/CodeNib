# Guidance for source queries

The exploration entry now explains how to ask for implementation source with
`query` alone and when to request indexed relationships. This is a bounded
usability change after the [source-first assessment](codegraph_source_first_results.md)
records no CodeNib calls. It does not establish increased voluntary use or
whole-workflow benefit.

## Product change

The tool description and indexed server guides start with the source question,
file/line evidence and a query containing any known names or file hints.
Relationship seeds are explicit requests: in on-demand graph mode they can
install providers and build the graph. The guides retain warm graph reuse,
the explicit false override, source verification and static-reference limits.
The MCP reference corrects `include_dependencies` from the obsolete `true`
default to its actual nullable default and explains both preparation modes.

The shared tool description changes; grep/Jev's dedicated instructions,
preload policy, credentials, cost guards and execution route do not. Conditional
graph guidance applies to on-demand graph mode. No grep/Jev result supports a
CodeGraph benefit claim.

No tool signature, implementation body, input/output schema, visibility setting,
preload annotation or response limit changes. Tool choice remains voluntary;
there is no instruction to always call CodeNib or a promised speed/accuracy gain.

## Actual producer and consumer checks

The [control packet](codegraph_exploration_guidance_2026_10_30.json) retains
seventeen lossless public files and five private SDK-fixture hashes.

- Separate producer processes import the explicit baseline/candidate files
  from outside both checkouts and verify their bytes. Full mode has twelve
  tools; both exploration modes have one. All schemas, non-exploration
  descriptions and visibility/preload metadata match. The 31 server function
  signatures and bodies match after excluding decorator metadata.
- A network-disabled owned container runs the pinned CLI 2.1.310 and Agent
  SDK 0.2.174 with scripted loopback HTTP. It receives both actual producer
  advertisements and verifies the complete description, schema and instructions
  in decoded outgoing requests. Both instruction sets are complete, at 797
  and 772 characters. This makes no provider/model call or voluntary-use
  observation; it does not intercept a historical native-subscription request.
- The control's only owned container is independently absent. Raw decoded
  request bodies, SDK defaults and CLI diagnostics remain local by hash;
  generated SDK configuration is excluded and untouched.

These producer exports establish the selected source runtime. The separate
[installed admission](codegraph_installed_guidance.md) now verifies the exact
main CI wheel, all 485 installed Python files, its native extension and real
stdio delivery of complete source. A prospective real-task adoption probe
remains required before attributing a use or benefit change.

## Validation and open gates

The existing MCP suite passes 433 cases with seven skips under both MCP 2.0.0
and 2.3.0. The smallest surface/exploration check passes 41 cases. Strict MkDocs,
public-document boundaries, support-matrix checks and pinned changed-file hooks
pass.

All 4,936 prior execution inputs and the complete model/native outcome remain
unchanged. No exposed cohort is rerun and no frozen criterion is revised.
Public package 0.2.4 remains unchanged. Keep the current insufficient-evidence
decision, representative update coverage, both independent success
confirmations and external consent/seven-day reuse open at 0/5. No outreach
is authorized. Current gates belong to [adoption A0](../adoption_roadmap.md)
and [#874](https://github.com/sysevol-ai/CodeNib/issues/874).
