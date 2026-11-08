// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";

/** The page list, map and reader sit side by side from this width. */
export const WORKSPACE_MEDIA = "(min-width: 1200px)";
// Shared with prepared Wikis, so a hidden map or a reader width carries over.
const MAP_HIDDEN_KEY = "cm-wiki-map-hidden";
const READER_WIDTH_KEY = "cm-wiki-reader-width";

function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // A preference that cannot be saved still applies to this visit.
  }
}

/** Whether the three-pane workspace shows, decided before the first paint. */
export function useWikiWorkspace() {
  const [wideEnough, setWideEnough] = useState(
    () => typeof window !== "undefined" && window.matchMedia(WORKSPACE_MEDIA).matches,
  );
  const [mapHidden, setMapHidden] = useState(() => readStored(MAP_HIDDEN_KEY) === "1");
  const [readerWidth, setReaderWidth] = useState<number | null>(() => {
    const stored = Number(readStored(READER_WIDTH_KEY));
    return Number.isFinite(stored) && stored > 0 ? stored : null;
  });
  useEffect(() => {
    const query = window.matchMedia(WORKSPACE_MEDIA);
    const update = () => setWideEnough(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return {
    wideEnough,
    mapHidden,
    workspace: wideEnough && !mapHidden,
    readerWidth,
    setMapHidden: (hidden: boolean) => {
      setMapHidden(hidden);
      writeStored(MAP_HIDDEN_KEY, hidden ? "1" : "0");
    },
    // The reader takes between 480 px and whatever leaves the map 320 px.
    resizeReader: (delta: number) => {
      setReaderWidth((current) => {
        const start = current ?? window.innerWidth / 2;
        const next = Math.round(Math.min(window.innerWidth - 244 - 320, Math.max(480, start + delta)));
        writeStored(READER_WIDTH_KEY, String(next));
        return next;
      });
    },
  };
}
