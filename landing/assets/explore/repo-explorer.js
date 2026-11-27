// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import evidence from "./requests.js";
import syntax from "./requests-highlight.js";
import { excerptRows } from "./excerpt-rows.js";
import styles from "./repo-explorer-styles.js";

const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const symbol = (node) => node.symbol.split(".").at(-1);
const sourceUrl = (file, line, end = line) =>
  `https://github.com/${evidence.repository}/blob/${evidence.commit}/${file}#L${line}-L${end}`;
const nodeNote = (node, index) => index === 3
  ? "Hostname check shown. The full method also handles scheme and port changes."
  : `Excerpt from ${node.symbol}. Open GitHub to see the surrounding method.`;
const edgeNote = (edge) => `Indexed call at line ${edge.anchor.line}. This is a selected path, not the whole runtime flow.`;
// The four functions live in sessions.py, which the Requests Wiki's map
// draws in its Session Orchestration area: the path takes that area's hue,
// as it does in the Wiki's own Overview.
const AREA = { title: "Session Orchestration", color: "#d95926" };
// These questions use only the four symbols and three calls in the captured
// artifact. Request and impact answers describe this path, not full coverage.
const tasks = {
  auth: {
    question: "Why does Requests drop auth on a redirect?",
    answer: 'A different hostname makes <code>should_strip_auth()</code> return true. The caller removes the <code>Authorization</code> header.',
    selection: ["edge", 2],
  },
  request: {
    question: "Which functions handle a redirected request?",
    answer: '<code>Session.send()</code> follows redirects through <code>resolve_redirects()</code>, which calls <code>rebuild_auth()</code> and its destination check. This is the authentication path within a request.',
    selection: ["node", 0],
  },
  impact: {
    question: "What could change if I edit rebuild_auth()?",
    answer: '<code>resolve_redirects()</code> calls <code>rebuild_auth()</code> and is reached from <code>Session.send()</code>. Redirect handling in these callers could be affected; these are the callers on this selected path.',
    selection: ["edge", 1],
  },
};
const STEP_S = 0.62;
const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

/** One small, source-pinned preview shared by the static site and Wiki app.
 * All exploration is local. No source, graph, or inference API is contacted.
 * The host can observe bounded experience events; none carry user input.
 *
 * The path falls as the Wiki's Overview draws one: each call a step right of
 * its caller, joined by an elbow in its area's colour. When it first comes
 * into view the path pours down one call at a time while the source pane
 * steps through each call site, and lands on the line that answers the
 * question, joined to it by one line.
 */
class RepoExplorer extends HTMLElement {
  static get observedAttributes() { return ["task"]; }

  attributeChangedCallback() {
    if (!this.shadowRoot) return;
    this.settle();
    this.showTask();
  }

  connectedCallback() {
    if (this.shadowRoot) return;
    this.id ||= "requests-auth";
    this.timers = [];
    const root = this.attachShadow({ mode: "open" });
    const wiki = this.getAttribute("wiki-href") || "https://demo.codenib.ai/psf__requests?p=redirects";
    root.innerHTML = `
      <style>${styles}</style>
      <article aria-label="Explore a real Requests code path" style="--area: ${AREA.color}; --rows-max: ${Math.max(...evidence.nodes.map((node) => excerptRows(node).numbers.length))}">
        <header>
          <h2 class="example-question"></h2>
          <p><span class="repo">psf/requests</span> · Click a call or a call site to read the source behind this answer.</p>
        </header>
        <div class="answer" aria-live="polite"><p></p></div>
        <ol class="flow" role="group" aria-label="Indexed call path" style="--steps: ${evidence.nodes.length - 1}">
          ${evidence.nodes.map((node, index) => `
            <li class="step${index ? "" : " is-entry"}" style="--i: ${index}">
              ${index ? "" : `<span class="area">${escape(AREA.title)}</span>`}
              ${index ? `<button class="edge" data-edge="${index - 1}" aria-label="Show the call from ${escape(symbol(evidence.nodes[index - 1]))} to ${escape(symbol(node))}, at line ${evidence.edges[index - 1].anchor.line}" aria-pressed="false" aria-controls="example-source">calls at <span class="edge-line">:${evidence.edges[index - 1].anchor.line}</span></button>` : ""}
              <button class="node" data-node="${index}" aria-pressed="false" aria-controls="example-source" title="${escape(node.symbol)}">
                <span class="ring" aria-hidden="true">${index + 1}</span>
                <code class="call">${escape(symbol(node))}</code>
                <span class="node-title">${escape(node.title)}</span>
              </button>
            </li>
          `).join("")}
        </ol>
        <section class="source" id="example-source" aria-label="Selected source excerpt">
          <div class="source-head"><span class="source-label"></span><a class="source-link" target="_blank" rel="noopener noreferrer">Open on GitHub ↗</a></div>
          <div class="source-scroll" tabindex="0" role="region" aria-label="Source code">
            <div class="source-code">
              <span class="line-highlight" aria-hidden="true" hidden></span>
              <div class="line-numbers" aria-hidden="true"></div>
              <pre><code class="syntax"></code></pre>
            </div>
          </div>
          <div class="source-note" role="status">
            ${evidence.nodes.map((node, index) => `<p data-note="node-${index}" aria-hidden="true">${escape(nodeNote(node, index))}</p>`).join("")}
            ${evidence.edges.map((edge, index) => `<p data-note="edge-${index}" aria-hidden="true">${escape(edgeNote(edge))}</p>`).join("")}
          </div>
        </section>
        <svg class="landing" aria-hidden="true"><path class="landing-line"></path><circle class="landing-dot" r="3.5"></circle></svg>
        <footer>
          <a class="wiki-link" href="${escape(wiki)}">Read the full Wiki <span aria-hidden="true">→</span></a>
          <button class="share" type="button">Copy example link</button>
          <span class="provenance">Indexed calls · <a href="https://github.com/${evidence.repository}/tree/${evidence.commit}" target="_blank" rel="noopener noreferrer">${evidence.commit.slice(0, 8)}</a></span>
        </footer>
        <p class="share-status" role="status"></p>
      </article>`;
    root.querySelectorAll("[data-node]").forEach((button) => {
      button.addEventListener("click", () => { this.settle(); this.select("node", Number(button.dataset.node), true); });
    });
    root.querySelectorAll("[data-edge]").forEach((button) => {
      button.addEventListener("click", () => { this.settle(); this.select("edge", Number(button.dataset.edge), true); });
    });
    root.querySelector(".wiki-link").addEventListener("click", () => this.report("example_wiki_open"));
    root.querySelector(".source-link").addEventListener("click", () => this.report("example_github_open"));
    root.querySelector(".share").addEventListener("click", async () => {
      const link = new URL(this.getAttribute("share-href") || "/#requests-auth", location.href);
      try {
        await navigator.clipboard.writeText(link.href);
        root.querySelector(".share-status").textContent = "Example link copied.";
        this.report("example_share");
      } catch {
        root.querySelector(".share-status").textContent = `Copy this link: ${link.href}`;
      }
    });
    // Reading the source by hand ends the opening pour where it stands.
    root.querySelector(".source-scroll").addEventListener("pointerdown", () => this.settle());
    this.showTask();
    // The pour plays once, when most of the example is on screen.
    if (!reducedMotion() && typeof IntersectionObserver !== "undefined") {
      root.querySelector("article").classList.add("is-armed");
      this.pourObserver = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          this.pourObserver.disconnect();
          this.pour();
        }
      }, { threshold: 0.45 });
      this.pourObserver.observe(this);
    } else {
      this.settle();
    }
    this.observer = new IntersectionObserver((entries) => {
      if (entries.some(entry => entry.isIntersecting)) {
        this.report("example_view");
        this.observer.disconnect();
      }
    }, { threshold: 0.15 });
    this.observer.observe(this);
    if (typeof ResizeObserver !== "undefined") {
      this.resizer = new ResizeObserver(() => this.drawLanding(false));
      this.resizer.observe(this);
    }
  }

  disconnectedCallback() {
    this.observer?.disconnect();
    this.pourObserver?.disconnect();
    this.resizer?.disconnect();
    this.timers?.forEach(clearTimeout);
  }

  task() {
    const name = this.getAttribute("task");
    return Object.hasOwn(tasks, name) ? tasks[name] : tasks.auth;
  }

  showTask() {
    const task = this.task();
    const root = this.shadowRoot;
    root.querySelector(".example-question").textContent = task.question;
    root.querySelector(".answer p").innerHTML = task.answer;
    this.select(...task.selection, false, this.poured);
  }

  /** The opening: each call drops in turn while the source pane steps to the
   *  line in its caller that made it, then the path lands on the answer. */
  pour() {
    const article = this.shadowRoot.querySelector("article");
    const steps = [...this.shadowRoot.querySelectorAll(".step")];
    article.classList.add("is-pouring");
    steps.forEach((step, index) => {
      this.timers.push(setTimeout(() => {
        step.classList.add("is-shown");
        // The call that reached this step, its line lit in the caller's source.
        if (index === 0) this.select("node", 0, false, true);
        else this.select("edge", index - 1, false, true);
      }, (0.2 + index * STEP_S) * 1000));
    });
    this.timers.push(setTimeout(() => {
      this.select(...this.task().selection, false, true);
      article.classList.add("is-answered");
    }, (0.2 + steps.length * STEP_S) * 1000));
    this.timers.push(setTimeout(() => this.settle(), (0.2 + steps.length * STEP_S) * 1000 + 900));
  }

  /** Show everything at once: after the pour, or when the reader acts first. */
  settle() {
    this.timers?.forEach(clearTimeout);
    this.timers = [];
    this.pourObserver?.disconnect();
    this.poured = true;
    const article = this.shadowRoot?.querySelector("article");
    if (!article) return;
    article.classList.remove("is-armed", "is-pouring");
    article.classList.add("is-answered");
    this.shadowRoot.querySelectorAll(".step").forEach((step) => step.classList.add("is-shown"));
  }

  report(event) {
    this.dispatchEvent(new CustomEvent("codenib:experience", {
      bubbles: true, composed: true, detail: { event },
    }));
  }

  select(kind, index, interacted, animate = false) {
    const root = this.shadowRoot;
    const edge = kind === "edge" ? evidence.edges[index] : null;
    const node = edge ? evidence.nodes.find((item) => item.id === edge.source) : evidence.nodes[index];
    const highlight = edge?.anchor.line;
    const rows = excerptRows(node);
    const end = rows.numbers.at(-1);
    this.selected = { kind, index };
    root.querySelectorAll("[data-node], [data-edge]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset[kind] === String(index)));
      button.classList.toggle("connected", Boolean(edge && button.dataset.node !== undefined &&
        [edge.source, edge.target].includes(evidence.nodes[Number(button.dataset.node)].id)));
    });
    root.querySelector(".source-label").textContent = `sessions.py:${highlight || node.start_line}`;
    root.querySelector(".source-link").href = sourceUrl(node.file, highlight || node.start_line, highlight || end);
    root.querySelector(".line-numbers").innerHTML = rows.numbers.map((number) =>
      `<span class="code-line${number === highlight ? " selected" : ""}${number === null ? " gap" : ""}">${number ?? "⋯"}</span>`,
    ).join("");
    root.querySelector(".syntax").innerHTML = syntax[node.id]?.sha256 === node.sha256
      ? syntax[node.id].html : escape(rows.lines.join("\n"));
    const band = root.querySelector(".line-highlight");
    band.hidden = highlight === undefined;
    band.style.setProperty("--selected-row", highlight === undefined ? 0 : rows.numbers.indexOf(highlight));
    root.querySelectorAll("[data-note]").forEach(note => {
      note.setAttribute("aria-hidden", String(note.dataset.note !== `${kind}-${index}`));
    });
    // Reset only the source pane. Scrolling an element into view can move the
    // whole page and displace the controls the visitor is currently clicking.
    root.querySelector(".source-scroll").scrollTo({ top: 0, left: 0, behavior: "instant" });
    this.drawLanding(animate || interacted);
    if (interacted) this.report("example_source_open");
  }

  /** One line from the selected call to the line of source it lands on. */
  drawLanding(animate) {
    const root = this.shadowRoot;
    const article = root?.querySelector("article");
    const svg = root?.querySelector(".landing");
    if (!article || !svg || !this.selected) return;
    const { kind, index } = this.selected;
    const from = root.querySelector(`[data-${kind}="${index}"]`);
    const source = root.querySelector(".source");
    const band = root.querySelector(".line-highlight");
    const target = band.hidden ? root.querySelector(".code-line") : root.querySelector(".code-line.selected");
    const frame = article.getBoundingClientRect();
    const a = (kind === "node" ? from?.querySelector(".call") : from)?.getBoundingClientRect();
    const s = source.getBoundingClientRect();
    const t = target?.getBoundingClientRect();
    // Only where the path and the source stand side by side.
    if (!a || !t || !a.width || s.left < a.right + 24) {
      svg.classList.remove("is-on");
      return;
    }
    const x1 = a.right - frame.left + 10;
    const y1 = a.top + a.height / 2 - frame.top;
    const x2 = s.left - frame.left + 6;
    const y2 = t.top + t.height / 2 - frame.top;
    const turn = s.left - frame.left - 20;
    const r = Math.min(10, Math.abs(y2 - y1) / 2);
    const down = y2 >= y1 ? 1 : -1;
    const d = Math.abs(y2 - y1) < 1
      ? `M ${x1} ${y1} H ${x2}`
      : `M ${x1} ${y1} H ${turn - r} Q ${turn} ${y1} ${turn} ${y1 + down * r} V ${y2 - down * r} Q ${turn} ${y2} ${turn + r} ${y2} H ${x2}`;
    svg.setAttribute("viewBox", `0 0 ${frame.width} ${frame.height}`);
    const line = svg.querySelector(".landing-line");
    line.setAttribute("d", d);
    const dot = svg.querySelector(".landing-dot");
    dot.setAttribute("cx", String(x2));
    dot.setAttribute("cy", String(y2));
    svg.classList.add("is-on");
    if (animate && !reducedMotion() && line.animate) {
      const length = line.getTotalLength();
      line.style.strokeDasharray = `${length}`;
      line.animate([{ strokeDashoffset: length }, { strokeDashoffset: 0 }], { duration: 420, easing: "cubic-bezier(.16,1,.3,1)" });
      dot.animate([{ opacity: 0 }, { opacity: 0, offset: 0.7 }, { opacity: 1 }], { duration: 520 });
    } else {
      line.style.strokeDasharray = "none";
    }
  }
}

if (!customElements.get("codenib-explorer")) customElements.define("codenib-explorer", RepoExplorer);
