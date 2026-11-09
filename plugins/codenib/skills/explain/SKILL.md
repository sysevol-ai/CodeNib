---
description: Explain how something in this repository works, where it is implemented, or what calls it, with every claim tied to source lines. Use for "how does X work", "where is X handled", "who calls X" and "what does changing X affect" questions about the current codebase.
---

Answer from the code, citing the file and lines behind each claim.

1. Call CodeNib's `explore_context` tool with the question in the reader's words.
   Pass the identifiers you already know as `symbols`, and set
   `include_dependencies` to true when the question is about callers, callees or
   impact. It returns ranked source windows and, once the code graph is ready,
   the recorded references between them.
2. Read the returned windows before you conclude anything. Open more source
   with your own file reads when a window stops short.
3. Write the answer as a short explanation. Cite each claim as `path:line`.
   Say when a relationship is a static reference and you have not confirmed
   that the call runs, and say when the graph was still being prepared
   (a `graph_preparing` or `graph_unavailable` diagnostic).

For a known file, error string or identifier, your own search is faster; use
CodeNib when the question spans files or needs references.

If the tool is not available, tell the user to run `/codenib:setup`.
