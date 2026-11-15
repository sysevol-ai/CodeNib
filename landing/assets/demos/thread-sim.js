// SPDX-FileCopyrightText: 2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Plays the thread board already in the page: three agents on three pull
// requests that share one store. The finished board stays visible without
// JavaScript and for readers who ask for less motion; this only replays how
// it got there. The pull requests are made up.
const root = document.querySelector("[data-thread-sim]");

if (root && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const rows = [...root.querySelectorAll(".sim-row")].map(row => ({
    track: row.querySelector(".sim-track"),
    count: row.querySelector(".sim-count"),
    note: row.querySelector(".sim-note"),
  }));
  const status = root.querySelector(".sim-status");
  const say = root.querySelector(".sim-say");
  const toggle = root.querySelector(".sim-toggle");
  const shared = number => `Also in progress on <code>Reader.read()</code>: ${number}`;

  // Each step changes the board: records stored per thread, records its agent
  // has read, and what the board says.
  const start = () => ({
    threads: [{ total: 4, read: 4, note: "" }, { total: 3, read: 3, note: "" }, { total: 2, read: 2, note: "" }],
    status: "poll · nothing changed",
    say: "Three agents, each on its own pull request, share one store.",
  });
  const STEPS = [
    board => {
      board.threads[0].total = 5;
      board.status = "poll · #812 changed";
      board.say = "A reviewer comments on #812. One record is added, and only Agent A has something unread.";
    },
    board => {
      board.threads[0].total = 6;
      board.say = "A check fails on #812. Its log is folded into one more record.";
    },
    board => {
      board.threads[0].read = 6;
      board.status = "Agent A · sent 2 of 6 records";
      board.say = "Agent A comes back and is sent the two new records, not the thread again.";
    },
    board => {
      board.threads[1].total = 4;
      board.threads[0].note = shared("#815");
      board.threads[1].note = shared("#812");
      board.status = "poll · #815 changed";
      board.say = "A push to #815 now changes Reader.read(), the function #812 is about. Both agents are told.";
    },
    board => {
      board.threads[1].read = 4;
      board.status = "Agent B · sent 1 of 4 records";
      board.say = "Agent B is sent the one new record.";
    },
    board => {
      board.status = "poll · nothing changed";
      board.say = "The next poll finds nothing new, so nothing is fetched and nothing is sent.";
    },
  ];

  let board = start();
  let next = 0;
  let timer = 0;
  let playing = false;

  const draw = () => {
    board.threads.forEach((thread, index) => {
      const row = rows[index];
      while (row.track.children.length < thread.total) row.track.append(document.createElement("i"));
      while (row.track.children.length > thread.total) row.track.lastChild.remove();
      [...row.track.children].forEach((mark, at) => mark.classList.toggle("is-new", at >= thread.read));
      const unread = thread.total - thread.read;
      row.count.textContent = unread ? `${unread} unread` : "up to date";
      row.count.classList.toggle("is-unread", unread > 0);
      row.note.innerHTML = thread.note;
    });
    status.textContent = board.status;
    say.textContent = board.say;
  };

  const tick = () => {
    if (next >= STEPS.length) {
      board = start();
      next = 0;
    } else {
      STEPS[next](board);
      next += 1;
    }
    draw();
    // Hold the finished board a little longer before starting over.
    timer = setTimeout(tick, next >= STEPS.length ? 4200 : 2600);
  };

  const play = () => {
    if (playing) return;
    playing = true;
    timer = setTimeout(tick, 1200);
  };
  const pause = () => {
    playing = false;
    clearTimeout(timer);
  };

  let wanted = true; // the reader has not pressed Pause
  toggle.hidden = false;
  toggle.addEventListener("click", () => {
    wanted = !wanted;
    toggle.textContent = wanted ? "Pause" : "Play";
    if (wanted) play();
    else pause();
  });

  draw();
  new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting) && wanted) play();
    else pause();
  }, { threshold: 0.4 }).observe(root);
}
