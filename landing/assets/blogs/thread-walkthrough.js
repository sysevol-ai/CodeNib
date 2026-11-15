// SPDX-FileCopyrightText: 2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// The walkthrough in the thread context article: one made-up pull request,
// the records stored for it, the document built from them, and what each
// agent is sent. Every view is recomputed from the steps up to the current
// one, so moving back and forth cannot drift.
(() => {
  const root = document.querySelector("[data-thread-walk]");
  if (!root) return;

  // kind "event": GitHub adds a record. "pulls": the open pull requests are
  // synced. "read": an agent asks for context. `doc` maps a section of the
  // document to its lines.
  const STEPS = [
    {
      kind: "event", address: "#812/opened/812", what: "pull request opened",
      say: "A pull request is opened. It is stored as the first record, and the document gets its ask.",
      doc: { ask: ["Fix a short read in Reader.read()"] },
    },
    {
      kind: "event", address: "#812/comment/1041", what: "comment with a stack trace",
      say: "A comment pastes a stack trace. Line 212 of pkg/io.py lies in Reader.read(), so the document names that function and the evidence.",
      technique: "code",
      doc: { code: ["- pkg/io.py:190-229 `Reader.read()` (frame; #812/comment/1041)"] },
    },
    {
      kind: "event", address: "#812/check/3310", what: "lint failed, 300 error lines",
      say: "A check fails with 300 lines of one error. The document shows two of them and counts the rest.",
      technique: "logs",
      doc: {
        ci: [
          "[#812/check/3310] lint failed",
          "error: src/app/a.py:12: unused import os",
          "error: src/app/b.py:40: unused import re",
          "[... 298 more lines like the two above ...]",
        ],
      },
    },
    {
      kind: "read", reader: "A", command: "codenib threads context 812",
      say: "Agent A starts work. It gets the whole thread in one read, and the document says it is complete.",
      technique: "whole",
    },
    {
      kind: "event", address: "#812/review_comment/2207", what: "review comment on line 198",
      say: "A reviewer comments on line 198. One more record, one more line in the document.",
      doc: { review: ["[#812/review_comment/2207] pkg/io.py:198: this loop exits early"] },
    },
    {
      kind: "event", address: "#812/comment/1041", what: "the first comment, edited", edit: true,
      say: "The first comment is edited. Nothing is overwritten: the edit is a new record under the old address.",
      technique: "events",
    },
    {
      kind: "read", reader: "A", command: "codenib threads show 812 --since <cursor>",
      say: "Agent A comes back. It is sent the two records after its cursor, not the thread again.",
      technique: "events",
    },
    {
      kind: "pulls",
      say: "Another open pull request, #790, changes lines 205 to 214. They lie inside Reader.read(), so the document reports it.",
      technique: "code",
      doc: { also: ["- #790 (open PR) Cache reads — changes `Reader.read()`"] },
    },
    {
      kind: "read", reader: "B", command: "codenib threads open 812",
      say: "Agent B opens a ticket on the same thread. Nothing is copied: it reads the same records and keeps its own position.",
      technique: "events",
    },
  ];
  // Section ids of the article, and how each technique is named in a sentence.
  const TECHNIQUES = {
    whole: "Technique 1, the agent's own thread stays whole",
    logs: "Technique 2, failed logs are folded",
    code: "Technique 3, mentions become places in the code",
    events: "Technique 4, events are stored and the document is built",
  };
  const SECTIONS = [
    ["ask", "## The ask"],
    ["code", "## Code locations"],
    ["also", "## Also in progress on this code"],
    ["ci", "## Failing CI"],
    ["review", "## Review comments"],
  ];

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const badge = letter => el("span", "walk-agent", letter);

  root.textContent = "";
  const play = el("button", "walk-play", "Play");
  play.type = "button";
  const steps = el("ol", "walk-steps");
  const ticks = STEPS.map((_, index) => {
    const item = el("li");
    const tick = el("button", "walk-tick");
    tick.type = "button";
    tick.setAttribute("aria-label", `Step ${index + 1} of ${STEPS.length}`);
    tick.addEventListener("click", () => go(index + 1));
    item.append(tick);
    steps.append(item);
    return tick;
  });
  const back = el("button", "walk-nav", "Back");
  const next = el("button", "walk-nav", "Next");
  back.type = next.type = "button";
  const bar = el("div", "walk-bar");
  bar.append(play, steps, back, next);
  const say = el("p", "walk-say");
  say.setAttribute("aria-live", "polite");
  const head = el("div", "walk-head");
  head.append(bar, say);

  const column = (title, note, mono) => {
    const box = el("section", "walk-col");
    box.append(el("h3", mono ? "walk-title walk-file" : "walk-title", title), el("p", "walk-note", note));
    const body = el("div", "walk-body");
    box.append(body);
    return [box, body];
  };
  const [storeCol, storeBody] = column("Stored records", "Only ever added to, each under a fixed address.");
  const [docCol, docBody] = column("THREAD_CONTEXT.md", "Built from the records each time an agent asks.", true);
  docBody.classList.add("walk-doc");
  const grid = el("div", "walk-grid");
  grid.append(storeCol, docCol);
  root.append(head, grid);

  let at = 0; // number of steps applied
  let timer = null;

  function render() {
    const done = STEPS.slice(0, at);
    const last = done[done.length - 1];
    const records = [];
    const doc = {};
    const changed = new Set();
    const agents = {};
    for (const step of done) {
      const isLast = step === last;
      if (step.kind === "event") records.push({ ...step, fresh: isLast });
      if (step.doc) {
        Object.assign(doc, step.doc);
        if (isLast) Object.keys(step.doc).forEach(key => changed.add(key));
      }
      if (step.kind === "read") {
        const before = agents[step.reader];
        const from = before ? before.position : 0;
        agents[step.reader] = {
          position: records.length,
          from,
          command: step.command,
          reads: [...(before ? before.reads : []), { sent: records.length - from, all: records.length }],
          fresh: isLast,
        };
      }
    }
    const reading = last && last.kind === "read" ? agents[last.reader] : null;

    storeBody.textContent = "";
    if (!records.length) storeBody.append(el("p", "walk-empty", "No records yet."));
    const list = el("ol", "walk-records");
    records.forEach((record, index) => {
      const row = el("li", "walk-record");
      if (record.fresh) row.classList.add("is-fresh");
      if (reading && index >= reading.from) row.classList.add("is-sent");
      row.append(el("span", "walk-pos", String(index)), el("code", null, record.address));
      row.append(el("span", "walk-what", record.edit ? "edit, appended" : record.what));
      list.append(row);
      const here = Object.keys(agents).filter(key => agents[key].position === index + 1);
      if (here.length) {
        const mark = el("li", "walk-mark");
        here.forEach(key => mark.append(badge(key)));
        mark.append(el("span", null, here.length > 1 ? "have read to here" : "has read to here"));
        list.append(mark);
      }
    });
    if (records.length) storeBody.append(list);
    for (const [key, agent] of Object.entries(agents)) {
      const line = el("p", "walk-sent");
      if (agent.fresh) line.classList.add("is-sent");
      const latest = agent.reads[agent.reads.length - 1];
      let text = `Agent ${key} was sent ${latest.sent} of ${latest.all} records.`;
      if (agent.reads.length > 1) {
        const sent = agent.reads.reduce((sum, read) => sum + read.sent, 0);
        const reread = agent.reads.reduce((sum, read) => sum + read.all, 0);
        text += ` Over ${agent.reads.length} reads that is ${sent}, against ${reread} for reading the thread again each time.`;
      }
      line.append(badge(key), el("code", null, agent.command), el("span", null, text));
      storeBody.append(line);
    }

    docBody.textContent = "";
    if (!records.length) {
      docBody.append(el("p", "walk-empty", "Nothing is stored yet, so there is no document."));
    } else {
      const line = (text, cls) => docBody.append(el("span", cls ? `walk-ln ${cls}` : "walk-ln", text));
      line("# Thread context: #812 (PR)", "is-head");
      line("Coverage: complete. Everything people wrote in #812 is below in full.", reading ? "is-sent" : "");
      for (const [key, title] of SECTIONS) {
        if (!(key in doc)) continue;
        line(title, "is-head");
        doc[key].forEach(text => line(text, changed.has(key) ? "is-kept" : ""));
      }
      line(`Cursor: ${records[records.length - 1].address}`, "is-head");
    }

    say.textContent = "";
    say.append(document.createTextNode(last ? last.say : "One pull request, from its first record to a second agent. Press Play, or move through it a step at a time."));
    if (last && last.technique) {
      const link = el("a", "walk-why", TECHNIQUES[last.technique]);
      link.href = `#${last.technique}`;
      say.append(document.createTextNode(" "), link);
    }
    ticks.forEach((tick, index) => {
      tick.classList.toggle("is-done", index < at);
      if (index === at - 1) tick.setAttribute("aria-current", "step");
      else tick.removeAttribute("aria-current");
    });
    back.disabled = at === 0;
    next.disabled = at === STEPS.length;
    play.textContent = timer ? "Pause" : at === STEPS.length ? "Replay" : "Play";
  }

  function stop() {
    clearInterval(timer);
    timer = null;
  }
  function go(to) {
    stop();
    at = Math.max(0, Math.min(STEPS.length, to));
    render();
  }
  function start() {
    if (at === STEPS.length) at = 0;
    timer = setInterval(() => {
      at += 1;
      if (at >= STEPS.length) stop();
      render();
    }, 2800);
    at += 1;
    render();
  }
  play.addEventListener("click", () => {
    if (timer) stop();
    else start();
    render();
  });
  next.addEventListener("click", () => go(at + 1));
  back.addEventListener("click", () => go(at - 1));

  // Without motion, show the finished state; the reader can move back through it.
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) at = STEPS.length;
  render();
})();
