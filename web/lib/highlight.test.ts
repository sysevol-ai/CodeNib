import { describe, expect, it } from "vitest";
import { highlightRows, highlightSource, languageForFile } from "./highlight";

describe("repository source highlighting", () => {
  it("maps supported source extensions to registered grammars", () => {
    expect(languageForFile("src/runner.py")).toBe("python");
    expect(languageForFile("src/main.tsx")).toBe("typescript");
    expect(languageForFile("src/native.hpp")).toBe("cpp");
    expect(languageForFile("README.unknown")).toBe("");
  });

  it("highlights known languages and safely escapes unknown source", () => {
    expect(highlightSource("def run():\\n    return 1", "python")).toContain(
      "hljs-keyword",
    );
    expect(highlightSource("<tag>&", "not-a-language")).not.toContain("<tag>");
  });

  it("splits highlighted markup into well-formed lines that keep their context", () => {
    const rows = highlightRows('def f():\n    """Send it.\n\n    or not.\n    """', "python");
    expect(rows).toHaveLength(5);
    // The docstring's later lines are still string, not keywords.
    expect(rows[3]).toContain("hljs-string");
    expect(rows[3]).not.toContain("hljs-keyword");
    for (const row of rows) {
      expect(row.match(/<span/g)?.length ?? 0).toBe(row.match(/<\/span>/g)?.length ?? 0);
    }
  });
});
