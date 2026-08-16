import { describe, expect, it } from "vitest";
import type { Citation } from "./api";
import { codeRefs, lineLabel, matchCitation } from "./citations";

function citation(overrides: Partial<Citation> = {}): Citation {
  return {
    file: "src/auth.py",
    start_line: 11,
    end_line: 20,
    node_name: "src/auth.py:authenticate()",
    type: "function",
    score: 0.9,
    content: null,
    ...overrides,
  };
}

describe("codeRefs", () => {
  it("keeps API line numbers unchanged because citations are already 1-based", () => {
    const refs = codeRefs([citation({ start_line: 11, end_line: 20 })]);

    expect(refs[0].start_line).toBe(11);
    expect(refs[0].end_line).toBe(20);
  });

  it("filters non-source citations and caps the shared list at twelve", () => {
    const citations = [
      citation({ file: null }),
      citation({ file: "src/empty.py", start_line: null, content: null }),
      ...Array.from({ length: 13 }, (_, i) =>
        citation({
          file: `src/file${i}.py`,
          node_name: `src/file${i}.py:fn${i}()`,
        })
      ),
    ];

    const refs = codeRefs(citations);

    expect(refs).toHaveLength(12);
    expect(refs[0].file).toBe("src/file0.py");
    expect(refs[11].file).toBe("src/file11.py");
  });

  it("keeps embedded source even when a legacy result has no line range", () => {
    const refs = codeRefs([
      citation({
        file: "src/embedded.py",
        start_line: null,
        end_line: null,
        content: "def embedded(): pass",
      }),
    ]);

    expect(refs).toHaveLength(1);
    expect(refs[0].file).toBe("src/embedded.py");
  });
});

describe("matchCitation", () => {
  const refs = [
    citation(),
    citation({
      file: "src/config.py",
      start_line: 4,
      end_line: 4,
      node_name: "src/config.py:load_settings()",
    }),
  ];

  it("matches inline code naming a cited symbol", () => {
    expect(matchCitation("authenticate", refs)).toEqual({ index: 0, kind: "symbol" });
    expect(matchCitation("load_settings()", refs)).toEqual({ index: 1, kind: "symbol" });
  });

  it("matches inline code naming a cited file", () => {
    expect(matchCitation("config.py", refs)).toEqual({ index: 1, kind: "file" });
    expect(matchCitation("src/auth.py:11", refs)).toEqual({ index: 0, kind: "file" });
  });
});

describe("lineLabel", () => {
  it("renders the 1-based API range without shifting it", () => {
    expect(lineLabel(citation({ start_line: 11, end_line: 20 }))).toBe("11-20");
    expect(lineLabel(citation({ start_line: 11, end_line: 11 }))).toBe("11");
    expect(lineLabel(citation({ start_line: null, end_line: null }))).toBe("");
  });
});
