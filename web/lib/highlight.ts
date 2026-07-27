import hljs from "highlight.js/lib/core";
import "highlight.js/styles/github.css";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import go from "highlight.js/lib/languages/go";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import kotlin from "highlight.js/lib/languages/kotlin";
import php from "highlight.js/lib/languages/php";
import python from "highlight.js/lib/languages/python";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import typescript from "highlight.js/lib/languages/typescript";
import yaml from "highlight.js/lib/languages/yaml";

const languages = {
  bash,
  c,
  cpp,
  csharp,
  go,
  java,
  javascript,
  json,
  kotlin,
  php,
  python,
  ruby,
  rust,
  typescript,
  yaml,
};

for (const [name, grammar] of Object.entries(languages)) {
  hljs.registerLanguage(name, grammar);
}

const aliases: Record<string, keyof typeof languages> = {
  "c#": "csharp",
  cc: "cpp",
  cs: "csharp",
  h: "cpp",
  hpp: "cpp",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  py: "python",
  pyx: "python",
  rb: "ruby",
  rs: "rust",
  sh: "bash",
  shell: "bash",
  ts: "typescript",
  tsx: "typescript",
  yml: "yaml",
};

export function languageForFile(file?: string | null): string {
  const extension = (file || "").split(".").pop()?.toLowerCase() || "";
  return aliases[extension] || (extension in languages ? extension : "");
}

function normalizeLanguage(language?: string): string {
  const key = (language || "").trim().toLowerCase();
  return aliases[key] || (key in languages ? key : "");
}

function escapeHtml(code: string): string {
  return code.replace(
    /[&<>]/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[character]!,
  );
}

export function highlightSource(code: string, language?: string): string {
  try {
    const normalized = normalizeLanguage(language);
    return normalized
      ? hljs.highlight(code, { language: normalized }).value
      : hljs.highlightAuto(code).value;
  } catch {
    return escapeHtml(code);
  }
}
