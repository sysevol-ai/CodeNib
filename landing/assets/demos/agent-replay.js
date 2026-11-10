// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Replays the recorded agent session already in the page. The page rests on the
// finished session: the answer and the whole graph. Nothing moves until the
// reader presses Replay or a step, and nothing is hidden before that.
const root = document.querySelector("[data-replay]");

if (root && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const lines = [...root.querySelectorAll(".line")];
  const items = [...root.querySelectorAll(".g-item")];
  const log = root.querySelector(".replay-log");
  const toggle = root.querySelector(".replay-toggle");
  const seekers = [...root.querySelectorAll("[data-seek]")];
  let next = lines.length;
  let timer = 0;
  let playing = false;

  const markChapter = chapter => {
    seekers.forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.seek === chapter));
    });
  };

  const show = (line, instant) => {
    lines.forEach(other => other.classList.remove("is-last"));
    line.classList.add("is-on", "is-last");
    line.classList.toggle("is-instant", instant);
    if (line.dataset.light) {
      items
        .filter(item => item.dataset.depth === line.dataset.light)
        .forEach(item => item.classList.add("is-on"));
    }
    markChapter(line.dataset.chapter);
    log.scrollTop = log.scrollHeight;
  };

  const tick = () => {
    if (next >= lines.length) {
      playing = false;
      toggle.textContent = "Replay";
      return;
    }
    timer = setTimeout(() => {
      show(lines[next], false);
      next += 1;
      tick();
    }, Number(lines[next].dataset.wait));
  };

  const play = () => {
    playing = true;
    toggle.textContent = "Pause";
    tick();
  };

  const pause = () => {
    playing = false;
    clearTimeout(timer);
    toggle.textContent = "Play";
  };

  const rewindTo = index => {
    clearTimeout(timer);
    root.classList.add("is-armed");
    [...lines, ...items].forEach(item => item.classList.remove("is-on", "is-last", "is-instant"));
    lines.slice(0, index).forEach(line => show(line, true));
    next = index;
    if (index === 0) markChapter("0");
  };

  // The resting frame ends on the answer, as the replay does.
  const rest = () => {
    if (next >= lines.length && !playing) log.scrollTop = log.scrollHeight;
  };
  rest();
  // Web fonts change the transcript's height after the first layout.
  document.fonts?.ready.then(rest);
  toggle.addEventListener("click", () => {
    if (playing) {
      pause();
      return;
    }
    if (next >= lines.length) rewindTo(0);
    play();
  });
  seekers.forEach(button => {
    button.addEventListener("click", () => {
      rewindTo(lines.findIndex(line => line.dataset.chapter === button.dataset.seek));
      markChapter(button.dataset.seek);
      play();
    });
  });
}
