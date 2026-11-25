// Syntax colour for code in the window, with highlight.js. A whole snippet is
// highlighted at once, so a string or comment that spans lines keeps its
// colour, then split into lines for the numbered rows.

import hljs from "highlight.js/lib/core";
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

const languages = { bash, c, cpp, csharp, go, java, javascript, json, kotlin, php, python, ruby, rust, typescript, yaml };
for (const [name, grammar] of Object.entries(languages)) hljs.registerLanguage(name, grammar);

const aliases: Record<string, keyof typeof languages> = {
  "c#": "csharp",
  cc: "cpp",
  cs: "csharp",
  cxx: "cpp",
  h: "cpp",
  hpp: "cpp",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  console: "bash",
  py: "python",
  pycon: "python",
  pyx: "python",
  pyi: "python",
  rb: "ruby",
  rs: "rust",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  ts: "typescript",
  tsx: "typescript",
  yml: "yaml",
};

/** A highlight.js language for a file name or a fence's info string. */
export function languageFor(nameOrFence?: string | null): string {
  const raw = (nameOrFence ?? "").trim().toLowerCase().split(/\s+/)[0] ?? "";
  const key = raw.includes(".") || raw.includes("/") ? (raw.split(".").pop() ?? "") : raw;
  return aliases[key] ?? (key in languages ? key : "");
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[ch]!);
}

/** Split highlighted HTML into lines, closing and reopening the spans that
 *  are open at each line break so every line is well formed on its own. */
export function splitHighlighted(html: string): string[] {
  const lines: string[] = [];
  const open: string[] = [];
  let line = "";
  const re = /<span[^>]*>|<\/span>|\n|[^<\n]+|</g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html))) {
    const token = m[0];
    if (token === "\n") {
      lines.push(line + "</span>".repeat(open.length));
      line = open.join("");
    } else if (token.startsWith("<span")) {
      open.push(token);
      line += token;
    } else if (token === "</span>") {
      open.pop();
      line += token;
    } else {
      line += token;
    }
  }
  lines.push(line + "</span>".repeat(open.length));
  return lines;
}

/** Each line of `code` as safe HTML, coloured when the language is known. */
export function highlightLines(code: string, language?: string): string[] {
  const lang = languageFor(language);
  let html: string;
  try {
    html = lang
      ? hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
      : hljs.highlightAuto(code, Object.keys(languages)).value;
  } catch {
    html = escapeHtml(code);
  }
  return splitHighlighted(html);
}
