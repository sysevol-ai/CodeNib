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
/* No frame of its own: the path sits on the page, the source is the one surface. */
article { position: relative; display: flex; flex-direction: column; height: 100%; }
header { padding: 0 0 10px; }
.repo { color: var(--text-primary, #0f172a); font-weight: 600; }
h2 { margin: 0 0 6px; font-size: clamp(20px, 2.2vw, 26px); letter-spacing: -.025em; line-height: 1.25; font-weight: 600; }
p { margin: 0; }
header p { color: var(--text-secondary, #475569); font-size: 13px; }
code { font-family: var(--font-geist-mono, "Geist Mono"), ui-monospace, monospace; }
button { font: inherit; cursor: pointer; }
button:focus-visible, a:focus-visible, .source-scroll:focus-visible { outline: 2px solid var(--accent, #2563eb); outline-offset: 3px; border-radius: 4px; }
.answer { margin: 0 0 8px; max-width: 60ch; font-size: 15px; line-height: 1.6; color: var(--text-secondary, #475569); transition: opacity .5s ease; }
.answer code { font-size: 13.5px; color: var(--text-primary, #0f172a); }

/* The path: each call a step right of its caller, joined by an elbow in the
   colour of the area it runs in, as the Wiki's Overview draws its path. */
.flow { --unit: clamp(18px, calc(110px / var(--steps, 3)), 34px); margin: 0; padding: 34px 0 6px; list-style: none; }
.step { position: relative; margin-left: calc(var(--i) * var(--unit)); padding-bottom: 4px; }
.step::before, .step::after { content: ""; position: absolute; pointer-events: none; }
/* Down from this call's ring towards the call it makes. */
.step:not(:last-child)::after { left: 13.5px; top: 62px; bottom: 0; width: 1px; background: color-mix(in srgb, var(--area) 78%, transparent); transform-origin: top; }
.step.is-entry:not(:last-child)::after { top: 30px; }
/* And across into this call. */
.step:not(.is-entry)::before { left: calc(13.5px - var(--unit)); top: 0; width: calc(var(--unit) - 13.5px); height: 46px; border-left: 1px solid color-mix(in srgb, var(--area) 78%, transparent); border-bottom: 1px solid color-mix(in srgb, var(--area) 78%, transparent); border-bottom-left-radius: 11px; }
.area { position: absolute; top: -28px; left: 0; display: inline-flex; align-items: baseline; gap: 7px; color: var(--text-secondary, #64748b); font-size: 13px; white-space: nowrap; }
.area::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--area); transform: translateY(-1px); }
/* Where the call is made, on the line that falls to it. */
.edge { display: block; height: 32px; margin-left: calc(26px - var(--unit)); padding: 0 4px; border: 0; background: transparent; color: var(--text-secondary, #64748b); font-family: var(--font-geist-mono, "Geist Mono"), ui-monospace, monospace; font-size: 13px; text-align: left; white-space: nowrap; }
.edge-line { color: var(--text-primary, #0f172a); }
.edge:hover, .edge[aria-pressed="true"] { color: color-mix(in srgb, var(--area) 62%, #fff); }
.edge:hover .edge-line, .edge[aria-pressed="true"] .edge-line { color: color-mix(in srgb, var(--area) 62%, #fff); }
.node { display: grid; grid-template-columns: 28px minmax(0, 1fr); column-gap: 12px; align-items: center; width: 100%; padding: 0; border: 0; background: transparent; color: inherit; text-align: left; }
.ring { grid-row: span 2; display: grid; place-items: center; width: 28px; height: 28px; border-radius: 50%; background: var(--explore-code-bg, #fff); box-shadow: inset 0 0 0 1.5px var(--area); color: var(--text-primary, #0f172a); font-size: 13px; font-weight: 500; font-variant-numeric: tabular-nums; transition: background-color .2s, color .2s; }
.call { min-width: 0; color: var(--text-primary, #0f172a); font-size: 15px; font-weight: 600; line-height: 22px; letter-spacing: -.01em; overflow-wrap: anywhere; }
.node-title { grid-column: 2; color: var(--text-secondary, #64748b); font-size: 13px; line-height: 18px; }
.node:hover .call { text-decoration: underline; text-decoration-color: color-mix(in srgb, var(--area) 60%, transparent); text-underline-offset: 4px; }
.node[aria-pressed="true"] .ring { background: var(--area); color: #111214; }
.node.connected .ring { box-shadow: inset 0 0 0 2px var(--area); }

/* The source the path lands on. */
.source { display: flex; flex: 1 1 auto; flex-direction: column; min-height: 0; margin-top: 24px; background: var(--explore-code-bg, #101b2c); border: 1px solid var(--explore-code-rule, #263244); border-radius: 10px; color: #e2e8f0; overflow: hidden; }
.source-head { display: flex; gap: 8px; justify-content: space-between; align-items: center; padding: 10px 14px; font-size: 13px; border-bottom: 1px solid var(--explore-code-rule, #263244); }
.source-label { font-family: var(--font-geist-mono, "Geist Mono"), ui-monospace, monospace; }
a { color: var(--accent, #2563eb); text-decoration: none; }
a:hover { text-decoration: underline; }
.source-link { color: var(--accent, #93c5fd); white-space: nowrap; }
/* Whole pixels: Safari snaps each row to one, and the band must land on the same rows. */
.source-scroll { --code-lh: 20px; flex: 1 1 266px; min-height: 266px; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; font-family: var(--font-geist-mono, "Geist Mono"), ui-monospace, monospace; font-size: var(--code-size, 13px); line-height: var(--code-lh); }
.source-code { position: relative; display: flex; min-width: max-content; }
pre { position: relative; margin: 0; padding: 12px 14px 12px 0; font: inherit; tab-size: 4; }
.line-numbers { position: relative; flex: 0 0 44px; padding: 12px 12px 12px 0; color: #8b9099; text-align: right; user-select: none; }
.code-line { display: block; }
.code-line.selected { color: #fff; font-weight: 600; }
.code-line.gap { color: #6b7079; letter-spacing: .1em; }
.line-highlight { position: absolute; inset-inline: 0; top: calc(12px + var(--selected-row) * var(--code-lh)); height: var(--code-lh); background: color-mix(in srgb, var(--area) 17%, transparent); box-shadow: inset 3px 0 var(--area); }
.hljs-keyword { color: #ff9da4; }
.hljs-string { color: #a5d6ff; }
.hljs-comment { color: #9caec4; }
.hljs-title, .hljs-built_in { color: #d2a8ff; }
.hljs-number, .hljs-literal { color: #79c0ff; }
/* All notes share a grid cell: the longest reserves space at every width. */
.source-note { display: grid; padding: 9px 14px; border-top: 1px solid var(--explore-code-rule, #263244); color: #b6bcc6; font-size: 13px; }
.source-note p { grid-area: 1 / 1; visibility: hidden; }
.source-note p[aria-hidden="false"] { visibility: visible; }
/* One line from the selected call to the line it lands on. */
.landing { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; opacity: 0; transition: opacity .25s ease; }
.landing.is-on { opacity: 1; }
.landing-line { fill: none; stroke: var(--area); stroke-width: 1.5; stroke-linecap: round; }
.landing-dot { fill: var(--area); }
footer { display: flex; justify-content: space-between; gap: 12px; padding: 18px 0 0; align-items: center; flex-wrap: wrap; }
.wiki-link { font-weight: 600; font-size: 14px; }
.share { font-size: 13px; color: var(--text-secondary, #475569); border: 0; background: transparent; padding: 6px 0; }
.share:hover { text-decoration: underline; }
.share-status { font-size: 13px; padding: 8px 0 0; overflow-wrap: anywhere; }
.share-status:empty { display: none; }
.provenance { font-size: 13px; color: var(--text-secondary, #64748b); }
.provenance a { color: inherit; }

/* The opening pour: each call drops in as the source steps to its call
   site; the answer and its line arrive last. Armed only by script. */
.is-armed:not(.is-answered) .answer, .is-armed:not(.is-pouring) .landing { opacity: 0; }
.is-armed .step:not(.is-shown) :is(.ring, .call, .node-title, .edge, .area) { opacity: 0; transform: translateY(-5px); }
.is-armed .step:not(.is-shown)::before { clip-path: inset(0 100% 100% 0); }
.is-armed .step:not(.is-shown)::after { transform: scaleY(0); }
.step :is(.ring, .call, .node-title, .edge, .area) { transition: opacity .32s ease, transform .42s cubic-bezier(.16, 1, .3, 1); }
.step:not(.is-entry)::before { transition: clip-path .34s cubic-bezier(.16, 1, .3, 1); }
.step::after { transition: transform .4s cubic-bezier(.16, 1, .3, 1) .28s; }
.is-pouring .step.is-shown .ring { animation: ring-arrive .5s cubic-bezier(.16, 1, .3, 1) both; }
.is-pouring .line-highlight { transition: top .38s cubic-bezier(.16, 1, .3, 1); }
.is-answered .line-highlight { animation: land 1.1s ease-out 1; }
@keyframes ring-arrive {
  0% { transform: scale(.55); box-shadow: inset 0 0 0 1.5px var(--area), 0 0 0 0 color-mix(in srgb, var(--area) 45%, transparent); }
  60% { transform: scale(1.06); box-shadow: inset 0 0 0 1.5px var(--area), 0 0 0 7px color-mix(in srgb, var(--area) 0%, transparent); }
  100% { transform: scale(1); }
}
@keyframes land {
  0% { background: color-mix(in srgb, var(--area) 42%, transparent); }
  100% { background: color-mix(in srgb, var(--area) 17%, transparent); }
}

@media (max-width: 600px) {
  .flow { --unit: clamp(14px, calc(60px / var(--steps, 3)), 20px); }
  .call { font-size: 14px; }
}
/* Wide containers read like a debugger: the path beside the source, joined
   by the line the selected call lands on. */
@container explorer (min-width: 860px) {
  article {
    display: grid;
    grid-template-columns: minmax(300px, 36%) minmax(0, 1fr);
    grid-template-rows: auto auto 1fr auto auto;
    grid-template-areas: "head source" "answer source" "flow source" "foot foot" "status status";
    column-gap: clamp(48px, 5cqi, 64px);
  }
  header { grid-area: head; }
  .answer { grid-area: answer; }
  .flow { grid-area: flow; align-self: start; }
  .source { grid-area: source; margin: 0; }
  .source-scroll { min-height: 360px; }
  footer { grid-area: foot; }
  .share-status { grid-area: status; }
}
/* A very wide example gives the source a larger face, so short lines of
   code do not leave the pane half empty. */
@container explorer (min-width: 1180px) {
  article { grid-template-columns: minmax(340px, 34%) minmax(0, 1fr); }
  /* The pane holds the longest excerpt, so the larger face never hides its
     last lines and choosing a step never changes the example's height. */
  .source-scroll { --code-size: 14px; --code-lh: 23px; flex: 1 1 auto; min-height: calc(var(--rows-max, 14) * var(--code-lh) + 24px); }
}
@media (prefers-reduced-motion: reduce) {
  * { scroll-behavior: auto; transition: none !important; animation: none !important; }
}
`;
