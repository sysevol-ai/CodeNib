---
description: Open this repository's CodeNib Wiki in a browser, or set the repository up to publish its Wiki on GitHub Pages.
disable-model-invocation: true
---

Ask the user whether they want to read the Wiki locally or publish it, unless
they already said.

To read it locally:

1. Start it in the background from the repository root. Ask first whether
   Claude Code should write the pages:
   - With pages written by Claude Code, run `codenib wiki . --no-open
     --generate`. No API key is needed: the pages are written by this
     machine's Claude Code, on the user's Claude Code plan, at roughly
     $0.10 to $0.20 a page. The Overview is written first and the other
     pages follow in the background.
   - Without, run `codenib wiki . --no-open`; the pages are built from the
     code index and need no model.
   Either way it indexes the repository and serves the Wiki on
   http://127.0.0.1:3000. Give the user the full address it prints after
   `CodeNib Wiki is ready:`. Its `?session=` key lets them annotate pages: they
   select words to ask a question, request a change or leave a note, and send
   questions and changes to their own Claude Code, whose answers come back on
   the page. Notes and tasks are saved as files under `.codenib/` in the
   repository.
2. To write the pages with another model instead, ask which model and how its
   key is set, then add `--model <provider/model> --api-key-env <VARIABLE>` to
   `--generate`, and tell the user it costs money before starting. Check the
   model first with
   `codenib doctor --probe-model --model <provider/model> --api-key-env <VARIABLE>`.
3. Stop the server when the user is done.

To publish it on GitHub Pages:

1. Check that `codenib pages --help` works. If it does not, this CodeNib is too
   old; tell the user to upgrade it.
2. Run `codenib pages init` in the repository root. It writes
   `.github/workflows/codenib-wiki.yml` and prints the steps left in the
   repository's settings (the Pages source and the model key secret) and a
   README badge. A published Wiki is always written by a model, so the key
   secret is required.
3. Show the user the workflow and the printed steps. Do not commit or push
   unless they ask.
