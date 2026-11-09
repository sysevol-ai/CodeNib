---
description: Open this repository's CodeNib Wiki in a browser, or set the repository up to publish its Wiki on GitHub Pages.
disable-model-invocation: true
---

Ask the user whether they want to read the Wiki locally or publish it, unless
they already said.

To read it locally:

1. Start it in the background from the repository root with
   `codenib wiki . --no-open`; it indexes the repository and serves the Wiki on
   http://127.0.0.1:3000. Give the user that address. The pages are derived
   from the index and need no model.
2. For model-written pages, ask which model to use and how its key is set, then
   add `--generate --model <provider/model> --api-key-env <VARIABLE>`. This
   calls the model once per page, so tell the user it costs money before
   starting. Check the model first with
   `codenib doctor --probe-model --model <provider/model> --api-key-env <VARIABLE>`.
3. Stop the server when the user is done.

To publish it on GitHub Pages:

1. Check that `codenib pages --help` works. If it does not, this CodeNib is too
   old; tell the user to upgrade it.
2. Run `codenib pages init` in the repository root. It writes
   `.github/workflows/codenib-wiki.yml` and prints the steps left in the
   repository's settings (the Pages source and the model key secret) and a
   README badge. Pass `--wiki index` for pages that need no model.
3. Show the user the workflow and the printed steps. Do not commit or push
   unless they ask.
