// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import evidence from "./requests.js";
import syntax from "./requests-highlight.js";

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

/** One small, source-pinned preview shared by the static site and Wiki app.
 * All exploration is local. No source, graph, or inference API is contacted.
 * The host can observe bounded experience events; none carry user input.
 */
class RepoExplorer extends HTMLElement {
  connectedCallback() {
    if (this.shadowRoot) return;
    this.id ||= "requests-auth";
    const root = this.attachShadow({ mode: "open" });
    const wiki = this.getAttribute("wiki-href") || "https://demo.codenib.ai/psf__requests?p=redirects";
    root.innerHTML = `
      <link rel="stylesheet" href="${new URL("./repo-explorer.css", import.meta.url).href}">
      <article aria-label="Explore a real Requests code path">
        <header>
          <div class="eyebrow"><span class="status-dot"></span> TRY A REAL EXAMPLE <span class="repo">psf/requests</span></div>
          <h2>Why does Requests drop auth on a redirect?</h2>
          <p>Follow the calls. Click a step or arrow to inspect its source.</p>
        </header>
        <div class="flow" role="group" aria-label="Indexed call path">
          ${evidence.nodes.map((node, index) => `
            ${index ? `<button class="edge" data-edge="${index - 1}" aria-label="Show call from ${escape(symbol(evidence.nodes[index - 1]))} to ${escape(symbol(node))}" aria-pressed="false" aria-controls="example-source"><span aria-hidden="true">→</span></button>` : ""}
            <button class="node" data-node="${index}" aria-pressed="false" aria-controls="example-source">
              <span class="step">0${index + 1}</span>
              <span class="node-title">${escape(node.title)}</span>
              <code>${escape(symbol(node))}</code>
            </button>
          `).join("")}
        </div>
        <div class="answer">
          <span class="answer-marker" aria-hidden="true">↳</span>
          <p>A different hostname makes <code>should_strip_auth()</code> return true. The caller removes the <code>Authorization</code> header.</p>
        </div>
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
        <footer>
          <a class="wiki-link" href="${escape(wiki)}">Read the full Wiki <span aria-hidden="true">→</span></a>
          <button class="share" type="button">Copy example link</button>
          <span class="provenance">Indexed calls · <a href="https://github.com/${evidence.repository}/tree/${evidence.commit}" target="_blank" rel="noopener noreferrer">${evidence.commit.slice(0, 8)}</a></span>
        </footer>
        <p class="share-status" role="status"></p>
      </article>`;
    root.querySelectorAll("[data-node]").forEach((button) => {
      button.addEventListener("click", () => this.select("node", Number(button.dataset.node), true));
    });
    root.querySelectorAll("[data-edge]").forEach((button) => {
      button.addEventListener("click", () => this.select("edge", Number(button.dataset.edge), true));
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
    this.select("edge", 2, false);
    this.observer = new IntersectionObserver((entries) => {
      if (entries.some(entry => entry.isIntersecting)) {
        this.report("example_view");
        this.observer.disconnect();
      }
    }, { threshold: 0.15 });
    this.observer.observe(this);
  }

  disconnectedCallback() { this.observer?.disconnect(); }

  report(event) {
    this.dispatchEvent(new CustomEvent("codenib:experience", {
      bubbles: true, composed: true, detail: { event },
    }));
  }

  select(kind, index, interacted) {
    const root = this.shadowRoot;
    const edge = kind === "edge" ? evidence.edges[index] : null;
    const node = edge ? evidence.nodes.find((item) => item.id === edge.source) : evidence.nodes[index];
    const highlight = edge?.anchor.line;
    const lines = node.content.trimEnd().split("\n");
    const indent = Math.min(...lines.filter(line => line.trim()).map(line => line.match(/^ */)[0].length));
    const end = node.start_line + lines.length - 1;
    root.querySelectorAll("[data-node], [data-edge]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset[kind] === String(index)));
      button.classList.toggle("connected", Boolean(edge && button.dataset.node !== undefined &&
        [edge.source, edge.target].includes(evidence.nodes[Number(button.dataset.node)].id)));
    });
    root.querySelector(".source-label").textContent = `sessions.py:${highlight || node.start_line}`;
    root.querySelector(".source-link").href = sourceUrl(node.file, highlight || node.start_line, highlight || end);
    root.querySelector(".line-numbers").innerHTML = lines.map((line, offset) => {
      const number = node.start_line + offset;
      return `<span class="code-line${number === highlight ? " selected" : ""}">${number}</span>`;
    }).join("");
    root.querySelector(".syntax").innerHTML = syntax[node.id]?.sha256 === node.sha256
      ? syntax[node.id].html : escape(lines.map(line => line.slice(indent)).join("\n"));
    const band = root.querySelector(".line-highlight");
    band.hidden = highlight === undefined;
    band.style.setProperty("--selected-row", highlight === undefined ? 0 : highlight - node.start_line);
    root.querySelectorAll("[data-note]").forEach(note => {
      note.setAttribute("aria-hidden", String(note.dataset.note !== `${kind}-${index}`));
    });
    // Reset only the source pane. Scrolling an element into view can move the
    // whole page and displace the controls the visitor is currently clicking.
    root.querySelector(".source-scroll").scrollTo({ top: 0, left: 0, behavior: "instant" });
    if (interacted) this.report("example_source_open");
  }
}

if (!customElements.get("codenib-explorer")) customElements.define("codenib-explorer", RepoExplorer);
