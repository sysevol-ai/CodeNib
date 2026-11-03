<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# pyrefly Glean captures for the Python identity cases

Each directory holds the `pyrefly check --report-glean` output (pyrefly 1.4.0)
for one source from `../scip_python_identity`, one JSON file per module. The
capture ran with the config `write_pyrefly_config` writes and
`--python-version 3.12`.

To keep the files small, only the predicates the converter reads are kept
(`src.FileLines.1`, `python.DefinitionLocation.4`,
`python.DeclarationLocation.4`, `python.xrefs.XRefsByFile.1`,
`python.FileCall.4`), and `typeInfo`, `returnsInfo`, `docstring` and `value`
fields are removed. Paths are relative to the fixture root.

| Directory | Source |
| --- | --- |
| `nested_collision` | `NESTED_COLLISION` in `test/scip/test_pyrefly_glean.py` (#871) |
| `receivers_<case>` | the `<case>` row of `attribute_write_receivers.fixture` |
| any other | `<directory>.source` (and `.other.source` as `other.py`) |

`module_walrus` and the `receivers_other_parameter`/`receivers_staticmethod`
writes record pyrefly gaps: no definition for a walrus inside a module-level
comprehension, and no cross reference at an attribute write.
