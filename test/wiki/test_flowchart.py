# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.wiki.flowchart import (
    drop_flow_duplicate_interactions,
    flow_edges,
    qualify_flow_caption,
    recorded_edges,
)

PAGE = """## Error flow

```mermaid
flowchart LR
  n0["Context.MustBindWith()"]
  n1["Context.AbortWithError()"]
  n2["Context.Error()"]
  n0 -->|binding failure triggers abort| n1
  n1 -->|records the error| n2
```

Error flow. Each arrow is a call site recorded in the index. [R3](#evidence-R3)

## Aborting

Text. [E2](#evidence-E2)

**Interactions**
- `Context.AbortWithError()` → `Context.Error()`: records the error [R1](#evidence-R1)

## Binding

**Interactions**
- `Context.MustBindWith()` → `Context.AbortWithError()`: triggers abort [R3](#evidence-R3)
- `Context.Bind()` → `Context.MustBindWith()`: binds [R4](#evidence-R4)

## Related pages
"""


def test_flow_edges_reads_labels_not_opaque_ids():
    assert flow_edges(PAGE) == {
        ("Context.MustBindWith", "Context.AbortWithError"),
        ("Context.AbortWithError", "Context.Error"),
    }


def test_rows_repeating_a_drawn_arrow_are_dropped():
    out = drop_flow_duplicate_interactions(PAGE)
    assert "`Context.AbortWithError()` → `Context.Error()`" not in out
    assert "`Context.MustBindWith()` → `Context.AbortWithError()`" not in out
    # A row the flow does not draw keeps its header.
    assert "**Interactions**\n- `Context.Bind()` → `Context.MustBindWith()`" in out
    # The emptied section loses its header and does not leave a double gap.
    assert out.count("**Interactions**") == 1
    assert "Text. [E2](#evidence-E2)\n\n## Binding" in out
    # The diagram itself is untouched.
    assert "n0 -->|binding failure triggers abort| n1" in out


def test_pages_without_a_flow_are_returned_unchanged():
    text = "## A\n\n**Interactions**\n- `a()` → `b()`: calls [R1](#evidence-R1)\n"
    assert drop_flow_duplicate_interactions(text) == text


RELATIONS = [
    {
        "id": "R1",
        "source": "context.go:Context.AbortWithError()",
        "target": "context.go:Context.Error()",
        "anchors": ["context.go:250"],
    },
    {
        "id": "R3",
        "source": "context.go:Context.MustBindWith()",
        "target": "context.go:Context.AbortWithError()",
        "anchors": ["context.go:843"],
    },
]


def test_recorded_edges_strip_file_prefix_but_keep_rust_paths():
    pairs = recorded_edges(
        RELATIONS
        + [
            {
                "source": "src/nb.rs:Notebook::from_reader()",
                "target": "src/nb.rs:Notebook::from_raw()",
                "anchors": ["src/nb.rs:4"],
            },
            {"source": "a.py:x()", "target": "a.py:y()", "anchors": []},
        ]
    )
    assert ("Notebook::from_reader", "Notebook::from_raw") in pairs
    assert ("x", "y") not in pairs


def test_caption_claim_kept_only_when_every_arrow_is_recorded():
    assert qualify_flow_caption(PAGE, RELATIONS) == PAGE
    partial = qualify_flow_caption(PAGE, RELATIONS[:1])
    assert "Arrows with a line number are call sites recorded" in partial
    assert "Each arrow is a call site" not in partial
    none = qualify_flow_caption(PAGE, [])
    assert "The index records none of these calls" in none
