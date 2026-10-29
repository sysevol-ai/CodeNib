<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Python definition identity inputs

These inputs reproduce physical declarations that the Python provider folds
into one symbol. Unit tests replay captured output without running a language
server or making model calls.

Each `.decoded` file is the protobuf text rendering of an actual
`@sourcegraph/scip-python@0.6.6` binary index for its matching `.source` file.
Copy `.source` to `nested.py`; an accompanying `.other.source` belongs at
`other.py`. The original temporary project root in compiler metadata is not
used when replaying the input.

The `.definitions.json` files capture positional `textDocument/definition`
responses from basedpyright 1.39.8. Request and response positions use UTF-16.
Only file URI roots are replaced by document-relative paths. A replay may need
fewer queries when source declarations establish a shared lexical binding.

| Case | Observable regression |
| --- | --- |
| `noncyclic` | Same-named functions in different methods must keep separate callers even without a containment cycle. |
| `parameters` | Parameter reassignments and explicit `nonlocal` uses must keep their owning physical function. |
| `rebinding` | Two class declarations and a later assignment share one binding; retain all three as static potential targets. |
| `unicode` | A method and its same-named inner functions use Chinese identifiers; emoji before a reference exercises UTF-16/UTF-8 conversion. |
| `local_bindings` | Assignments and loop variables can share a local ID with a later function or class. IDs repeat across documents. The omitted `_` definition is recovered from source and an observed parameter owner, without inventing references. |
| `properties` | Getter and setter definitions remain separate physical declarations of one static property binding. |
| `varargs` | LSP selections beginning at `*args` or `**kwargs` must bind to the exact parameter identifier and function. |
| `field_redefinitions` | Repeated classes retain their own member definitions; classes in different enclosing functions keep their distinct fields. |
| `attribute_fields` | Instance fields declared inside constructors of same-named nested classes must keep their owning class and source position. |

The separate `scip_python_nested_collision.fixture` retains the original
three-definition `run` reproduction, including the second inner definition
that the provider labels as a read. All graph edges tested here derive from
captured compiler references; a static target set is not a runtime trace.
