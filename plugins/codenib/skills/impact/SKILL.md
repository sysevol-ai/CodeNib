---
description: List the tests that the current, uncommitted change can reach, with the call path to each, before finishing a change in a Python repository. Use when asked which tests to run, what a change might break, or to check a change before committing.
---

Find the tests a change can reach, run them, and report what you ran.

1. Run `codenib guard check` in the repository root. It compares the working
   tree with `HEAD`; pass `--base <revision>` for another starting point. It
   prints each reachable test with the call path from the test to the changed
   code, and the tests the change itself rewrote come first.
2. Run the listed tests with the project's own test command. For a test the
   change rewrote, also run it as it was at the base revision, as the report
   shows, so that a weakened test cannot hide a regression.
3. Report which tests you ran and their results. A test the report did not
   list can still fail: the check follows static references and does not see
   dynamic dispatch or code outside the index.

This check is a preview and supports Python only. If `codenib` is missing,
tell the user to run `/codenib:setup`.
