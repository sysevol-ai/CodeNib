// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import HighlightedBlock from "@/components/HighlightedBlock";
import HighlightedCode from "@/components/HighlightedCode";
import { excerptRows, GAP, parseSegmentStarts, segmentRows } from "./sourceExcerpt";

const SIGNATURE = { start_line: 288, end_line: 288, content: "func (n *node) insertChild(path string) {\n" };

describe("source excerpts keep their definition", () => {
  it("puts the signature and a gap row above a slice that starts inside it", () => {
    expect(excerptRows("child := &node{}\nn.addChild(child)\n", 314, SIGNATURE)).toEqual({
      lines: ["func (n *node) insertChild(path string) {", GAP, "child := &node{}", "n.addChild(child)"],
      numbers: [288, null, 314, 315],
    });
    expect(excerptRows("a\nb", 10).numbers).toEqual([10, 11]);
  });

  it("numbers anchored fence segments and rejects a body that disagrees", () => {
    expect(parseSegmentStarts("go at=288,314 hl=315")).toEqual([288, 314]);
    expect(parseSegmentStarts("go hl=3")).toEqual([]);
    expect(segmentRows(`sig {\n${GAP}\nx\ny`, [288, 314])?.numbers).toEqual([288, null, 314, 315]);
    expect(segmentRows("x\ny", [288, 314])).toBeNull();
    expect(segmentRows(`a\n${GAP}\nb`, [1])).toBeNull();
  });

  it("draws the signature in the code panel without spotlighting it", () => {
    const html = renderToStaticMarkup(
      <HighlightedCode code={"child := &node{}\n"} file="tree.go" startLine={314} definition={SIGNATURE} highlightLine={314} />,
    );
    expect(html).toContain("insertChild");
    expect(html).toMatch(/<div>288<\/div><div class="hl-gutter-gap">⋯<\/div><div class="hl-gutter-on">314<\/div>/);
  });

  it("renders a wiki excerpt with real line numbers and marks by file line", () => {
    const html = renderToStaticMarkup(
      <HighlightedBlock
        text={`func (n *node) insertChild(path string) {\n${GAP}\nchild := &node{}\nn.addChild(child)\nn = child\n`}
        language="go"
        segmentStarts={[288, 314]}
        highlightLines={new Set([315])}
      />,
    );
    expect(html).toContain('<span class="code-ln" aria-hidden="true">288</span>');
    expect(html).toContain('class="code-line is-gap"');
    expect(html).toMatch(/class="code-line is-marked"><span class="code-ln" aria-hidden="true">315</);
  });
});
