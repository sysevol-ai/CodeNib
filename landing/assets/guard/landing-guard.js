// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Homepage behaviour. The page reads in full without this module: the case,
// the chart and the commands are static HTML.
import { installExperienceEvents, recordExperience } from "../explore/experience.js";

// A host may opt in after proxying this first-party endpoint to CodeNib.
const endpoint = document.querySelector('meta[name="codenib-experience-endpoint"]')?.content;
if (endpoint) installExperienceEvents(endpoint, "landing");

// Copy the install commands without their "$" prompts.
document.querySelectorAll("button[data-copy]").forEach(button => {
  // Shown only once this module runs, so a failed script leaves no dead button.
  button.hidden = !navigator.clipboard;
  const label = button.textContent;
  button.addEventListener("click", async () => {
    const source = document.getElementById(button.dataset.copy);
    if (!source) return;
    const text = [...source.childNodes]
      .filter(node => !(node.nodeType === Node.ELEMENT_NODE && node.classList.contains("prompt")))
      .map(node => node.textContent).join("")
      .split("\n").map(line => line.trim()).filter(Boolean).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = button.dataset.copied || "Copied";
      recordExperience("install_command_copy");
    } catch {
      // Fall back to a selection the visitor can copy by hand.
      const range = document.createRange();
      range.selectNodeContents(source);
      getSelection()?.removeAllRanges();
      getSelection()?.addRange(range);
      button.textContent = "Press Ctrl+C";
    }
    setTimeout(() => { button.textContent = label; }, 1600);
  });
});

// Count a view of the real case once it is mostly on screen.
const caseSection = document.getElementById("real-case");
if (caseSection && "IntersectionObserver" in window) {
  const observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) {
      recordExperience("guard_case_view");
      observer.disconnect();
    }
  }, { threshold: 0.5 });
  observer.observe(caseSection);
}

// Name the case under the pointer in the unit chart. The table under
// "All 68 cases" carries the same data for keyboard and screen readers.
const figure = document.querySelector(".units");
const tip = figure?.querySelector(".unit-tip");
if (figure && tip) {
  let active = null;
  const hide = () => {
    active?.classList.remove("active");
    active = null;
    tip.hidden = true;
  };
  figure.addEventListener("pointermove", event => {
    const unit = event.target.closest?.(".strip .u");
    if (!unit) return hide();
    if (unit === active) return;
    active?.classList.remove("active");
    active = unit;
    unit.classList.add("active");
    tip.textContent = unit.dataset.case;
    tip.hidden = false;
    const box = figure.getBoundingClientRect();
    const mark = unit.getBoundingClientRect();
    const left = Math.min(Math.max(mark.left - box.left + mark.width / 2 - tip.offsetWidth / 2, 0),
      box.width - tip.offsetWidth);
    tip.style.left = `${left}px`;
    tip.style.top = `${mark.top - box.top - tip.offsetHeight - 8}px`;
  });
  figure.addEventListener("pointerleave", hide);
}
