import { describe, expect, it } from "vitest";
import { highlightSource, languageForFile } from "./highlight";

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
});
