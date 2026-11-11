// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Split a graph label such as `context.go:Context.Bind()` or
 * `src/nb.rs:Notebook::from_reader()` into its file and symbol. A label with
 * no path prefix (`Notebook::from_reader()`) is all symbol.
 */
export function splitSymbolLabel(label: string): { file: string; symbol: string } {
  const at = label.indexOf(":");
  if (at > 0 && label[at + 1] !== ":") {
    const head = label.slice(0, at);
    if (head.includes("/") || head.includes(".")) {
      return { file: head, symbol: label.slice(at + 1) };
    }
  }
  return { file: "", symbol: label };
}

/** Clang names an unnamed template parameter `type-parameter-<depth>-<index>`. */
const CLANG_TEMPLATE_PARAMETER = /type-parameter-\d+-(\d+)/g;
const PARAMETER_NAMES = ["T", "U", "V", "W"];
const IDENTIFIER_CHAR = /[A-Za-z0-9_]/;

/** Where the template argument list opening at `open` closes, or -1. */
function closingAngle(text: string, open: number): number {
  let depth = 0;
  for (let index = open; index < text.length; index += 1) {
    const char = text[index];
    if (char === "<") depth += 1;
    else if (char === ">" && text[index - 1] !== "-") {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return -1;
}

/**
 * Template arguments as a reader should see them. The C++ index records a
 * specialisation the way clang spells it, `formatter<type-parameter-0-0,
 * typename enable_if<…>::type>`, which buries the name it belongs to.
 * Parameters read `T`, `U`, …; a short argument list such as `<double>` stays;
 * a long or nested one folds to `<…>`. Text without templates is returned as is.
 */
export function cleanTemplateArguments(text: string): string {
  if (!text.includes("<")) return text;
  const named = text.replace(
    CLANG_TEMPLATE_PARAMETER,
    (_match, index: string) => PARAMETER_NAMES[Number(index)] ?? `T${index}`,
  );
  let out = "";
  let index = 0;
  while (index < named.length) {
    const char = named[index];
    const word = /([A-Za-z_][A-Za-z0-9_]*)$/.exec(out)?.[1] ?? "";
    // `operator<`, `operator<<` and `a < b` are not template arguments.
    if (char === "<" && word && word !== "operator" && IDENTIFIER_CHAR.test(out.at(-1) ?? "")) {
      const close = closingAngle(named, index);
      if (close < 0) return out + named.slice(index);
      const inner = named.slice(index + 1, close);
      const fold =
        inner.length > 20 || /[<(]|\btypename\b|&&|\|\|/.test(inner);
      out += fold ? "<…>" : `<${inner}>`;
      index = close + 1;
      continue;
    }
    out += char;
    index += 1;
  }
  return out;
}

/** Where each `.` or `::` separator sits outside template arguments and parentheses. */
function separatorEnds(symbol: string): number[] {
  const ends: number[] = [];
  let depth = 0;
  for (let index = 0; index < symbol.length; index += 1) {
    const char = symbol[index];
    if (char === "<" || char === "(" || char === "[") depth += 1;
    else if ((char === ">" && symbol[index - 1] !== "-") || char === ")" || char === "]")
      depth = Math.max(0, depth - 1);
    else if (depth === 0 && char === ".") ends.push(index + 1);
    else if (depth === 0 && char === ":" && symbol[index + 1] === ":") {
      ends.push(index + 2);
      index += 1;
    }
  }
  return ends;
}

/**
 * A graph symbol as a reader should see it: without its file prefix, and with
 * clang's template spelling cleaned (see `cleanTemplateArguments`). `short`
 * keeps the owner and the member, which is how a call reads in prose:
 * `include/fmt/base.h:fmt.detail.parse_arg_id()` reads `detail.parse_arg_id()`.
 */
export function displaySymbol(label: string, options: { short?: boolean } = {}): string {
  const symbol = cleanTemplateArguments(splitSymbolLabel(label).symbol.trim());
  if (!options.short || symbol.includes("/")) return symbol;
  const ends = separatorEnds(symbol);
  return ends.length < 2 ? symbol : symbol.slice(ends[ends.length - 2]);
}

const INTERACTION_ROW = /^(\s*[-*]\s+)`([^`]+)`(\s*→\s*)`([^`]+)`/;

/**
 * Interaction rows name both ends of a recorded call by their full graph
 * names (`fmt.detail.parse_replacement_field()` → `fmt.detail.parse_arg_id()`).
 * In a row under its section the owner and member are enough.
 */
export function tidyInteractionRows(markdown: string): string {
  if (!markdown.includes("**Interactions**")) return markdown;
  const lines = markdown.split("\n");
  let inList = false;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim() === "**Interactions**") {
      inList = true;
      continue;
    }
    const row = inList ? INTERACTION_ROW.exec(line) : null;
    if (!row) {
      inList = inList && /^\s*[-*]\s+/.test(line);
      continue;
    }
    const [whole, bullet, source, arrow, target] = row;
    lines[index] =
      `${bullet}\`${displaySymbol(source, { short: true })}\`${arrow}\`${displaySymbol(target, { short: true })}\`` +
      line.slice(whole.length);
  }
  return lines.join("\n");
}
