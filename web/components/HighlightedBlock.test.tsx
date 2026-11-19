import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import HighlightedBlock from "./HighlightedBlock";

const EXCERPT = "    def prepare_url(\n⋯\n        if not scheme:\n            raise MissingSchema(url)";

describe("source excerpt block", () => {
  it("offers the cited definition's hidden lines and marks only named lines", () => {
    const html = renderToStaticMarkup(
      <HighlightedBlock
        text={EXCERPT}
        language="python"
        highlightLines={new Set([516])}
        segmentStarts={[483, 515]}
        source={{ start: 483, end: 563, load: async () => null }}
      />,
    );
    expect(html).toContain('aria-label="Show 31 lines hidden"');
    expect(html).toContain('aria-label="Show 47 lines below"');
    expect(html).not.toContain("lines above");
    expect(html.match(/is-marked/g)).toHaveLength(1);
    expect(html).toContain(">516<");
  });

  it("keeps a plain gap row when the definition cannot be loaded", () => {
    const html = renderToStaticMarkup(
      <HighlightedBlock text={EXCERPT} language="python" segmentStarts={[483, 515]} />,
    );
    expect(html).not.toContain("<button type=\"button\"");
    expect(html).toContain('title="31 lines hidden"');
  });
});
