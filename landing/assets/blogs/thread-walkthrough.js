// SPDX-FileCopyrightText: 2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// The walkthrough in the thread context article: one made-up pull request,
// the records stored for it, the document built from them, and what each
// reader is sent. Every view is recomputed from the steps up to the current
// one, so stepping back and forth cannot drift.
(() => {
  const root = document.querySelector("[data-thread-walk]");
  if (!root) return;

  // kind "event": GitHub adds a record. "pulls": the open pull requests are
  // synced. "read": a reader asks for context.
  const STEPS = [
    {
      kind: "event", address: "#812/opened/812", what: "pull request opened",
      say: "A pull request is opened. It is stored as the first record, and the document gets its ask.",
      doc: { ask: "Fix a short read in Reader.read()" },
    },
    {
      kind: "event", address: "#812/comment/1041", what: "comment with a stack trace",
      say: "A comment pastes a stack trace. Line 212 of pkg/io.py lies in Reader.read(), so the document names that function and the evidence.",
      technique: 3,
      doc: { code: "pkg/io.py:190-229 Reader.read()  (frame; #812/comment/1041)" },
    },
    {
      kind: "event", address: "#812/check/3310", what: "lint failed, 300 error lines",
      say: "A check fails with 300 lines of one error. The document shows two of them and counts the rest.",
      technique: 2,
      doc: { ci: "lint failed · 2 lines shown · [... 298 more lines like the two above ...]" },
    },
    {
      kind: "read", reader: "A", command: "codenib threads context 812",
      say: "Agent A starts work. It gets the whole thread in one read, and the document says it is complete.",
      technique: 1,
    },
    {
      kind: "event", address: "#812/review_comment/2207", what: "review comment on line 198",
      say: "A reviewer comments on line 198. One more record, one more line in the document.",
      doc: { review: "pkg/io.py:198  “this loop exits early”  (#812/review_comment/2207)" },
    },
    {
      kind: "event", address: "#812/comment/1041", what: "the first comment is edited", edit: true,
      say: "The first comment is edited. Nothing is overwritten: the edit is a new record under the old address.",
      technique: 4,
    },
    {
      kind: "read", reader: "A", command: "codenib threads show 812 --since <cursor>",
      say: "Agent A comes back. It is sent the two records after its cursor, not the thread again.",
      technique: 4,
    },
    {
      kind: "pulls",
      say: "Another open pull request, #790, changes lines 205 to 214. They lie inside Reader.read(), so the document reports it.",
      technique: 3,
      doc: { also: "#790 (open PR) changes Reader.read()" },
    },
    {
      kind: "read", reader: "B", command: "codenib threads open 812",
      say: "A second agent opens a ticket on the same thread. Nothing is copied: it reads the same records and keeps its own position.",
      technique: 4,
    },
  ];
  const TECHNIQUES = {
    1: "Technique 1 · the agent's own thread stays whole",
    2: "Technique 2 · failed logs are folded",
    3: "Technique 3 · mentions become places in the code",
    4: "Technique 4 · events are stored, the document is built",
  };
  const SECTIONS = [
    ["ask", "The ask"],
    ["code", "Code locations"],
    ["also", "Also in progress on this code"],
    ["ci", "Failing CI"],
    ["review", "Review comments"],
  ];
  const READERS = { A: "Agent A", B: "Ticket B" };

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  root.textContent = "";
  const bar = el("div", "walk-bar");
  const back = el("button", "walk-btn", "Back");
  const play = el("button", "walk-btn walk-play", "Play");
  const next = el("button", "walk-btn", "Step");
  const count = el("span", "walk-count");
  for (const b of [back, play, next]) b.type = "button";
  bar.append(play, back, next, count);
  const say = el("p", "walk-say");
  say.setAttribute("aria-live", "polite");
  const grid = el("div", "walk-grid");
  const panel = (label, note) => {
    const box = el("section", "walk-panel");
    box.append(el("h3", "walk-label", label), el("p", "walk-note", note));
    const body = el("div", "walk-body");
    box.append(body);
    grid.append(box);
    return body;
  };
  const storeBody = panel("Stored records", "Only ever added to. The position is the number on the left.");
  const docBody = panel("THREAD_CONTEXT.md", "Built from the records each time a reader asks.");
  const readBody = panel("Readers", "Each keeps a position. Blue is what the last read sent.");
  root.append(bar, say, grid);

  let at = 0; // number of steps applied
  let timer = null;

  function render() {
    const done = STEPS.slice(0, at);
    const last = done[done.length - 1];
    const records = [];
    const doc = {};
    const changed = new Set();
    const readers = {};
    for (const step of done) {
      const isLast = step === last;
      if (step.kind === "event") records.push({ ...step, fresh: isLast });
      if (step.doc) {
        Object.assign(doc, step.doc);
        if (isLast) Object.keys(step.doc).forEach(key => changed.add(key));
      }
      if (step.kind === "read") {
        const from = readers[step.reader] ? readers[step.reader].position : 0;
        const prior = readers[step.reader] ? readers[step.reader].reads : [];
        readers[step.reader] = {
          position: records.length,
          from,
          command: step.command,
          reads: [...prior, { sent: records.length - from, all: records.length }],
          fresh: isLast,
        };
      }
    }
    const reading = last && last.kind === "read" ? readers[last.reader] : null;

    storeBody.textContent = "";
    if (!records.length) storeBody.append(el("p", "walk-empty", "No records yet."));
    records.forEach((record, index) => {
      const row = el("div", "walk-record");
      if (record.fresh) row.classList.add("is-fresh");
      if (reading && index >= reading.from) row.classList.add("is-sent");
      row.append(el("span", "walk-pos", String(index)), el("code", null, record.address));
      row.append(el("span", "walk-what", record.edit ? "edit, appended" : record.what));
      storeBody.append(row);
      for (const [key, reader] of Object.entries(readers)) {
        if (reader.position === index + 1) {
          storeBody.append(el("div", "walk-cursor", `${READERS[key]} has read up to here`));
        }
      }
    });

    docBody.textContent = "";
    const coverage = el("div", "walk-line walk-coverage");
    coverage.append(el("span", "walk-key", "Coverage"), el("span", null, records.length ? "complete" : "nothing stored yet"));
    if (reading) coverage.classList.add("is-sent");
    docBody.append(coverage);
    for (const [key, title] of SECTIONS) {
      if (!(key in doc)) continue;
      const line = el("div", "walk-line");
      if (changed.has(key)) line.classList.add("is-fresh");
      line.append(el("span", "walk-key", title), el("code", null, doc[key]));
      docBody.append(line);
    }
    const cursor = el("div", "walk-line walk-cursor-line");
    cursor.append(el("span", "walk-key", "Cursor"), el("code", null, records.length ? records[records.length - 1].address : "none"));
    docBody.append(cursor);

    readBody.textContent = "";
    if (!Object.keys(readers).length) readBody.append(el("p", "walk-empty", "No one has read the thread yet."));
    for (const [key, reader] of Object.entries(readers)) {
      const card = el("div", "walk-reader");
      if (reader.fresh) card.classList.add("is-sent");
      card.append(el("strong", null, READERS[key]), el("code", null, reader.command));
      const sent = reader.reads.reduce((sum, read) => sum + read.sent, 0);
      const reread = reader.reads.reduce((sum, read) => sum + read.all, 0);
      const latest = reader.reads[reader.reads.length - 1];
      let text = `Sent ${latest.sent} of ${latest.all} records.`;
      if (reader.reads.length > 1) {
        text += ` Over ${reader.reads.length} reads: ${sent} records, against ${reread} for reading the thread again each time.`;
      }
      card.append(el("p", null, text));
      readBody.append(card);
    }

    say.textContent = "";
    if (last && last.technique) say.append(el("span", "walk-tag", TECHNIQUES[last.technique]));
    say.append(document.createTextNode(last ? last.say : "One pull request, from the first record to a second agent. Press Play, or Step through it."));
    count.textContent = `${at} / ${STEPS.length}`;
    back.disabled = at === 0;
    next.disabled = at === STEPS.length;
    play.textContent = timer ? "Pause" : at === STEPS.length ? "Replay" : "Play";
  }

  function stop() {
    clearInterval(timer);
    timer = null;
  }
  function start() {
    if (at === STEPS.length) at = 0;
    timer = setInterval(() => {
      at += 1;
      if (at >= STEPS.length) stop();
      render();
    }, 2600);
    at += 1;
    render();
  }
  play.addEventListener("click", () => {
    if (timer) stop();
    else start();
    render();
  });
  next.addEventListener("click", () => {
    stop();
    at = Math.min(STEPS.length, at + 1);
    render();
  });
  back.addEventListener("click", () => {
    stop();
    at = Math.max(0, at - 1);
    render();
  });

  // Without motion, show the finished state; the reader can step back through it.
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) at = STEPS.length;
  render();
})();
