// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";

import { searchSymbols, type SymbolMatch } from "@/lib/api";
import { rankByTitle } from "@/lib/jumpSearch";
import "./JumpPalette.css";

export interface JumpPage {
  id: string;
  title: string;
  /** The top-level area the page belongs to, when it is not one itself. */
  area?: string;
  color: string;
}

export interface JumpWalk {
  id: string;
  /** The name the page list gives it. */
  title: string;
  /** Its longer question, also searched. */
  also?: string;
}

type Item =
  | { kind: "page"; key: string; page: JumpPage }
  | { kind: "walk"; key: string; walk: JumpWalk }
  | { kind: "code"; key: string; symbol: SymbolMatch };

const SEARCH_DELAY_MS = 120;
const PAGE_LIMIT = 8;

const fileName = (path: string) => path.split("/").pop() ?? path;

/**
 * Jump anywhere in a Wiki from the keyboard: a page, a walkthrough, or a
 * function or class by name, which opens at its definition. Code results come
 * from the index, so a name only appears where the index recorded it.
 */
export default function JumpPalette({
  repoId,
  repoName,
  pages,
  walks,
  searchCode,
  onClose,
  onPage,
  onWalk,
  onSymbol,
}: {
  repoId: string;
  repoName: string;
  pages: JumpPage[];
  walks: JumpWalk[];
  /** Whether this repository has an index to search for definitions. */
  searchCode: boolean;
  onClose: () => void;
  onPage: (id: string) => void;
  onWalk: (id: string) => void;
  onSymbol: (symbol: SymbolMatch) => void;
}) {
  const [query, setQuery] = useState("");
  const [symbols, setSymbols] = useState<{ query: string; found: SymbolMatch[] } | null>(null);
  const [searching, setSearching] = useState(false);
  const [failed, setFailed] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const trimmed = query.trim();

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Names are looked up as the reader types, after a short pause.
  useEffect(() => {
    if (!searchCode || trimmed.length < 2) {
      setSymbols(null);
      setSearching(false);
      setFailed(false);
      return undefined;
    }
    const controller = new AbortController();
    setSearching(true);
    const timer = window.setTimeout(() => {
      searchSymbols(repoId, trimmed, { signal: controller.signal })
        .then((result) => {
          setSymbols({ query: trimmed, found: result.symbols });
          setFailed(false);
          setSearching(false);
        })
        .catch((error: unknown) => {
          if ((error as Error)?.name === "AbortError") return;
          setFailed(true);
          setSearching(false);
        });
    }, SEARCH_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [repoId, trimmed, searchCode]);

  const groups = useMemo(() => {
    const code = symbols && symbols.query === trimmed ? symbols.found : [];
    // Nothing typed: the areas, which is the Wiki's own table of contents.
    const foundPages = trimmed
      ? rankByTitle(pages, (page) => page.title, trimmed, code.length ? 4 : PAGE_LIMIT)
      : pages.filter((page) => !page.area);
    const foundWalks = rankByTitle(walks, (walk) => `${walk.title} ${walk.also ?? ""}`, trimmed, 4);
    const pageGroup = { label: "Pages", items: foundPages.map((page): Item => ({ kind: "page", key: `p:${page.id}`, page })) };
    const walkGroup = { label: "Walkthroughs", items: foundWalks.map((walk): Item => ({ kind: "walk", key: `w:${walk.id}`, walk })) };
    const codeGroup = {
      label: "Code",
      items: code.map((symbol): Item => ({ kind: "code", key: `c:${symbol.file}:${symbol.line}:${symbol.name}`, symbol })),
    };
    // A name typed exactly is a jump to code: its definition comes first.
    const leaf = (name: string) => name.replace(/\(\)$/, "").split(/\.|::/).pop()?.toLowerCase();
    const exact = code.some((symbol) => leaf(symbol.name) === trimmed.replace(/\(\)$/, "").toLowerCase());
    return (exact ? [codeGroup, pageGroup, walkGroup] : [pageGroup, walkGroup, codeGroup]).filter(
      (group) => group.items.length,
    );
  }, [pages, walks, symbols, trimmed]);
  const items = useMemo(() => groups.flatMap((group) => group.items), [groups]);
  const colors = useMemo(() => new Map(pages.map((page) => [page.id, page.color])), [pages]);
  const colorOf = (id: string) => colors.get(id);

  useEffect(() => {
    setActive(0);
  }, [trimmed, items.length]);

  // Keep the chosen row in view by scrolling the list alone: scrollIntoView
  // would also scroll the dialog and push the search field out of sight.
  useEffect(() => {
    const list = listRef.current;
    const row = list?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    if (!list || !row) return;
    // The list is positioned, so a row's offsetTop is measured inside it.
    const top = row.offsetTop;
    if (top < list.scrollTop) list.scrollTop = top - 6;
    else if (top + row.offsetHeight > list.scrollTop + list.clientHeight) {
      list.scrollTop = top + row.offsetHeight - list.clientHeight + 6;
    }
  }, [active]);

  const choose = (item: Item | undefined) => {
    if (!item) return;
    onClose();
    if (item.kind === "page") onPage(item.page.id);
    else if (item.kind === "walk") onWalk(item.walk.id);
    else onSymbol(item.symbol);
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => (items.length ? (index + 1) % items.length : 0));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => (items.length ? (index - 1 + items.length) % items.length : 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(items[active]);
    } else if (event.key === "Tab") {
      // The dialog keeps focus on its search field; the list is driven by arrows.
      event.preventDefault();
      inputRef.current?.focus();
    }
  };

  const codeNote = !searchCode
    ? null
    : trimmed.length < 2
      ? "Type two letters or more to find a function or class."
      : failed
        ? "Code search is not answering. Pages and walkthroughs still open."
        : searching && !symbols
          ? "Looking up definitions…"
          : symbols && symbols.query === trimmed && !symbols.found.length
            ? `No function or class named like "${trimmed}" in the index.`
            : null;

  let index = -1;
  return (
    <div className="jump-scrim" onMouseDown={onClose}>
      <div
        ref={dialogRef}
        className="jump"
        role="dialog"
        aria-modal="true"
        aria-label={`Jump to a page or definition in ${repoName}`}
        onMouseDown={(event) => event.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <div className="jump-field">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
            <circle cx="11" cy="11" r="7" />
            <path d="M20 20l-3.5-3.5" />
          </svg>
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={searchCode ? "Pages, walkthroughs, functions" : "Pages and walkthroughs"}
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={items[active] ? `${listId}-${active}` : undefined}
            aria-autocomplete="list"
            spellCheck={false}
            autoComplete="off"
          />
          <kbd className="jump-esc">Esc</kbd>
        </div>
        <div ref={listRef} id={listId} className="jump-list" role="listbox" aria-label="Results">
          {groups.map((group) => (
            <div key={group.label} role="group" aria-label={group.label} className="jump-group">
              <p className="jump-group-label" aria-hidden="true">
                {group.label}
              </p>
              {group.items.map((item) => {
                index += 1;
                const at = index;
                return (
                  <div
                    key={item.key}
                    id={`${listId}-${at}`}
                    data-index={at}
                    role="option"
                    aria-selected={at === active}
                    className={`jump-item${at === active ? " is-active" : ""}`}
                    onMouseMove={() => setActive(at)}
                    onClick={() => choose(item)}
                  >
                    {item.kind === "page" && (
                      <>
                        <span className="jump-dot" style={{ background: item.page.color }} aria-hidden="true" />
                        <span className="jump-name">{item.page.title}</span>
                        {item.page.area && (
                          <span className="jump-meta">
                            <span className="jump-meta-page">{item.page.area}</span>
                          </span>
                        )}
                      </>
                    )}
                    {item.kind === "walk" && (
                      <>
                        <svg className="jump-glyph" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                          <circle cx="5" cy="6" r="2" />
                          <circle cx="19" cy="18" r="2" />
                          <path d="M7 6h7a3 3 0 0 1 0 6H10a3 3 0 0 0 0 6h7" />
                        </svg>
                        <span className="jump-name">{item.walk.title}</span>
                      </>
                    )}
                    {item.kind === "code" && (
                      <>
                        {item.symbol.page && colorOf(item.symbol.page.id) ? (
                          <span className="jump-dot" style={{ background: colorOf(item.symbol.page.id) }} aria-hidden="true" />
                        ) : (
                          <svg className="jump-glyph" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                            <path d="M8 6l-6 6 6 6M16 6l6 6-6 6" />
                          </svg>
                        )}
                        <code className="jump-name">{item.symbol.name}</code>
                        <span className="jump-meta" title={item.symbol.file}>
                          {item.symbol.page && <span className="jump-meta-page">{item.symbol.page.title}</span>}
                          <span className="mono">
                            {fileName(item.symbol.file)}:{item.symbol.line}
                          </span>
                        </span>
                      </>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
          {!items.length && !codeNote && <p className="jump-empty">Nothing in this Wiki matches "{trimmed}".</p>}
          {codeNote && <p className="jump-note">{codeNote}</p>}
        </div>
        <p className="jump-foot" aria-hidden="true">
          <span>Arrow keys to move</span>
          <span>Enter to open</span>
          {searchCode && <span>Functions open at their definition, from the code index</span>}
        </p>
      </div>
    </div>
  );
}
