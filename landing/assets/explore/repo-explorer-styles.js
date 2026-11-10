/* SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 * SPDX-License-Identifier: Apache-2.0 */
// Load with the component module so its first painted frame is already styled.
export default `
:host {
  display: block;
  min-width: 0;
  container: explorer / inline-size;
  color: var(--text-primary, #0f172a);
  font: 14px/1.5 var(--font-geist-sans, system-ui), sans-serif;
  text-align: left;
}
* { box-sizing: border-box; }
article { display: flex; flex-direction: column; height: 100%; background: var(--bg-surface, #fff); border: 1px solid var(--border-subtle, #e2e8f0); border-radius: 10px; overflow: hidden; }
header { padding: 22px 24px 12px; }
.repo { color: var(--text-primary, #0f172a); font-weight: 600; }
h2 { margin: 0 0 6px; font-size: clamp(18px, 2vw, 23px); letter-spacing: -.025em; line-height: 1.3; font-weight: 650; }
p { margin: 0; }
header p { color: var(--text-secondary, #475569); font-size: 12px; }
code { font-family: var(--font-geist-mono, ui-monospace), ui-monospace, monospace; }
button { font: inherit; cursor: pointer; }
button:focus-visible, a:focus-visible, .source-scroll:focus-visible { outline: 3px solid var(--accent, #2563eb); outline-offset: 3px; }
.flow { display: grid; grid-template-columns: 1fr 24px 1fr 24px 1fr 24px 1fr; align-items: stretch; padding: 14px 16px; }
.node { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; min-width: 0; padding: 10px 8px; border: 1px solid var(--border-subtle, #e2e8f0); border-radius: 4px; background: var(--bg-muted, #f8fafc); color: inherit; text-align: left; }
.step { color: var(--text-secondary, #64748b); font-size: 11px; }
.node-title { font-size: 12px; font-weight: 650; }
.node code { font-size: 10.5px; letter-spacing: -.02em; overflow-wrap: anywhere; color: var(--text-secondary, #475569); }
.node:hover { border-color: var(--explore-step-line, #94a3b8); }
.node[aria-pressed="true"] { border-color: var(--accent, #2563eb); background: var(--accent-soft, #eff6ff); }
/* A selected call is the arrow; its two ends only step forward, so one thing is in the accent. */
.node.connected { border-color: var(--explore-step-line, #94a3b8); }
.node.connected .step, .node.connected code { color: var(--text-primary, #0f172a); }
.edge { padding: 0; min-height: 44px; background: transparent; border: 0; color: var(--text-secondary, #64748b); font-size: 22px; }
.edge:hover, .edge[aria-pressed="true"] { color: var(--accent, #2563eb); }
.edge[aria-pressed="true"] span { font-weight: 700; }
.answer { display: flex; gap: 9px; margin: 0 24px; font-size: 13px; color: var(--text-secondary, #475569); }
.answer-marker { color: var(--accent, #2563eb); font-size: 20px; }
.answer code { font-size: 12px; color: var(--text-primary, #0f172a); }
.source { display: flex; flex: 1 1 auto; flex-direction: column; min-height: 0; margin: 0 16px; background: var(--explore-code-bg, #101b2c); border-radius: 6px; color: #e2e8f0; overflow: hidden; }
.source-head { display: flex; gap: 8px; justify-content: space-between; align-items: center; padding: 10px 12px; font-size: 11px; border-bottom: 1px solid var(--explore-code-rule, #263244); }
.source-label { font-family: ui-monospace, monospace; }
a { color: var(--accent, #2563eb); text-decoration: none; }
a:hover { text-decoration: underline; }
.source-link { color: #93c5fd; white-space: nowrap; }
/* Whole pixels: Safari snaps each row to one, and the band must land on the same rows. */
.source-scroll { --code-lh: 20px; flex: 1 1 266px; min-height: 266px; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; font-family: ui-monospace, monospace; font-size: var(--code-size, 12px); line-height: var(--code-lh); }
.source-code { position: relative; display: flex; min-width: max-content; }
pre { position: relative; margin: 0; padding: 12px 14px 12px 0; font: inherit; tab-size: 4; }
.line-numbers { position: relative; flex: 0 0 40px; padding: 12px 10px 12px 0; color: #94a3b8; text-align: right; user-select: none; }
.code-line { display: block; }
.code-line.selected { color: #dbeafe; }
.code-line.gap { color: #64748b; letter-spacing: .1em; }
.line-highlight { position: absolute; inset-inline: 0; top: calc(12px + var(--selected-row) * var(--code-lh)); height: var(--code-lh); background: var(--explore-code-line, #1e3a5f); box-shadow: inset 3px 0 var(--explore-code-mark, #60a5fa); }
.hljs-keyword { color: #ff9da4; }
.hljs-string { color: #a5d6ff; }
.hljs-comment { color: #9caec4; }
.hljs-title, .hljs-built_in { color: #d2a8ff; }
.hljs-number, .hljs-literal { color: #79c0ff; }
/* All notes share a grid cell: the longest reserves space at every width. */
.source-note { display: grid; padding: 8px 12px; border-top: 1px solid var(--explore-code-rule, #263244); color: #b6c2d4; font-size: 11px; }
.source-note p { grid-area: 1 / 1; visibility: hidden; }
.source-note p[aria-hidden="false"] { visibility: visible; }
footer { display: flex; justify-content: space-between; gap: 12px; padding: 16px 24px; align-items: center; flex-wrap: wrap; }
.wiki-link { font-weight: 650; font-size: 13px; }
.share { font-size: 11px; color: var(--text-secondary, #475569); border: 0; background: transparent; padding: 6px 0; }
.share:hover { text-decoration: underline; }
.share-status { font-size: 12px; padding: 0 24px 16px; overflow-wrap: anywhere; }
.share-status:empty { display: none; }
.provenance { font-size: 11px; color: var(--text-secondary, #64748b); }
.provenance a { color: inherit; }
@media (max-width: 600px) {
  header { padding: 18px 16px 10px; }
  .flow { grid-template-columns: 1fr; padding: 10px 16px; }
  .node { display: grid; grid-template-columns: 22px 1fr auto; align-items: center; gap: 5px; padding: 10px; min-height: 44px; }
  .node code { font-size: 10px; }
  .edge { height: 24px; min-height: 24px; font-size: 20px; }
  .edge span { display: inline-block; transform: rotate(90deg); }
  .answer { margin: 0 16px; }
  .source { margin: 0 10px; }
  footer { padding: 14px 16px; }
}
/* Wide containers read like a debugger: the call path beside the source. */
@container explorer (min-width: 980px) {
  article {
    display: grid;
    grid-template-columns: minmax(300px, 32%) minmax(0, 1fr);
    grid-template-rows: auto auto 1fr auto auto;
    grid-template-areas: "head source" "answer source" "flow source" "foot foot" "status status";
  }
  header { grid-area: head; }
  .answer { grid-area: answer; }
  .flow { grid-area: flow; grid-template-columns: minmax(0, 1fr); align-content: start; padding: 16px 16px 16px 24px; }
  .node { display: grid; grid-template-columns: 22px 1fr; align-items: center; gap: 2px 6px; }
  .node code { grid-column: 2; }
  .edge { height: 22px; min-height: 22px; font-size: 18px; }
  .edge span { display: inline-block; transform: rotate(90deg); }
  .source { grid-area: source; margin: 16px 16px 0 0; }
  .source-scroll { min-height: 360px; }
  footer { grid-area: foot; }
  .share-status { grid-area: status; }
}
/* A very wide example gives the path more room and the source a larger face,
   so short lines of code do not leave the pane half empty. */
@container explorer (min-width: 1180px) {
  article { grid-template-columns: minmax(340px, 36%) minmax(0, 1fr); }
  /* The pane grows with the excerpt, so the larger face never hides its last lines. */
  .source-scroll { --code-size: 13.5px; --code-lh: 23px; flex: 1 1 auto; }
}
@media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto; } }
`;
