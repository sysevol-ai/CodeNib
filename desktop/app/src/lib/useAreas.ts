// One meaning for each hue, everywhere the window shows code: the area the
// code lives in. With a written outline the areas are its pages; without one
// they come from the file call graph (deriveFileAreas).

import { useMemo } from "react";
import { areaPages, hueFor, slotHue } from "./areas";
import { flattenPages } from "./model";
import { useStore } from "./store";

export interface AreaOf {
  id: string;
  title: string;
  hue: string;
}

const NONE: AreaOf = { id: "", title: "No area", hue: "var(--area-none)" };

export function useAreas(repo: string) {
  const { data } = useStore();
  const d = data[repo];
  return useMemo(() => {
    const pages = flattenPages(d?.tree?.pages ?? []);
    const outline = d?.mapAreas ?? null;
    const derived = d?.fileAreas ?? null;
    const outlineAreas = areaPages(d?.tree?.pages ?? [], outline);
    const modules = pages
      .filter((p) => p.id.startsWith("mod__"))
      .map((p) => ({ id: p.id, folder: p.title.replace(/\/$/, "") }))
      .sort((a, b) => b.folder.length - a.folder.length);

    const ofFile = (file: string): AreaOf => {
      if (derived) {
        const area = derived.areas.find((a) => a.id === derived.byFile[file]);
        return area ? { id: area.id, title: area.title, hue: slotHue(area.slot) } : NONE;
      }
      const found = modules.find((m) => file === m.folder || file.startsWith(`${m.folder}/`));
      return found ? { id: found.id, title: found.folder, hue: hueFor(found.id, outlineAreas) } : NONE;
    };

    /** A page's mark: its outline area, or the area most of its folder's files are in. */
    const ofPage = (pageId: string): string => {
      if (outline || !derived) return hueFor(pageId, outlineAreas);
      const page = pages.find((p) => p.id === pageId);
      if (!page || !page.id.startsWith("mod__")) return "var(--area-none)";
      const folder = page.title.replace(/\/$/, "");
      const counts = new Map<string, number>();
      for (const [file, area] of Object.entries(derived.byFile)) {
        if (file.startsWith(`${folder}/`)) counts.set(area, (counts.get(area) ?? 0) + 1);
      }
      const top = [...counts.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))[0];
      const area = derived.areas.find((a) => a.id === top?.[0]);
      return area ? slotHue(area.slot) : "var(--area-none)";
    };
    return { ofFile, ofPage, derived };
  }, [d?.tree, d?.mapAreas, d?.fileAreas]);
}
