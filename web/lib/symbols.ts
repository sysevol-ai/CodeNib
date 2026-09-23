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
