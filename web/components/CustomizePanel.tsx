"use client";

import { useEffect, useRef, useState } from "react";
import {
  customizeWiki,
  resetCustomization,
  type CustomizeScope,
} from "@/lib/api";

const MAX_CHARS = 2000;

/**
 * Reader "human prior" injection: rewrite the current wiki page (or the whole
 * wiki) to how the reader wants it read — a style instruction and/or a section
 * structure. Server-side, ephemeral: customizations vanish on restart and the
 * default page is always one Reset away.
 *
 * `onApplied` hands back the transformed markdown so the page can swap it in
 * without a re-fetch; `onReset` tells the page to re-fetch the default.
 */
export default function CustomizePanel({
  repoId,
  pageId,
  pageTitle,
  customized,
  onApplied,
  onReset,
}: {
  repoId: string;
  pageId: string;
  pageTitle: string;
  customized: boolean;
  onApplied: (markdown: string) => void;
  onReset: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState<CustomizeScope>("page");
  const [instruction, setInstruction] = useState("");
  const [structure, setStructure] = useState("");
  const [state, setState] = useState<"idle" | "working" | "error">("idle");
  const [error, setError] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!open) return;
    taRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  function structureList(): string[] {
    return structure
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  async function apply() {
    if (state === "working") return;
    const instr = instruction.trim();
    const sections = structureList();
    if (!instr && sections.length === 0) {
      setError("Add an instruction or at least one section.");
      setState("error");
      return;
    }
    setState("working");
    setError("");
    try {
      const res = await customizeWiki(repoId, {
        scope,
        target: scope === "page" ? pageId : "",
        instruction: instr,
        structure: sections,
      });
      // Page scope returns the transformed markdown directly. Whole-wiki applies
      // lazily on view, so re-fetch to pull this page through the transform.
      if (scope === "page" && res.markdown) onApplied(res.markdown);
      else onReset(); // triggers a re-fetch, which now returns the customized page
      setOpen(false);
    } catch (e) {
      setState("error");
      setError(String(e).includes("live backend")
        ? "Customization needs the live demo (not available in the static build)."
        : "Could not apply. Try a shorter instruction or again in a moment.");
      return;
    }
    setState("idle");
  }

  async function reset() {
    setState("working");
    try {
      await resetCustomization(repoId, "page", pageId);
      await resetCustomization(repoId, "wiki", "");
      onReset();
      setOpen(false);
    } catch {
      /* reset is best-effort; the page re-fetch still shows current state */
      onReset();
      setOpen(false);
    }
    setState("idle");
  }

  return (
    <>
      <button
        className="refresh-wiki cz-trigger"
        title="Rewrite this page the way you want to read it"
        onClick={() => setOpen(true)}
      >
        {customized ? "✎ Customized — edit" : "✎ Customize this wiki"}
      </button>

      {open && (
        <div className="cz-scrim" onClick={() => setOpen(false)} role="presentation">
          <div
            className="cz-dialog"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label="Customize wiki"
          >
            <p className="cz-title">Customize the wiki</p>
            <p className="cz-sub">
              Rewrite it to how you like to read — your instruction and/or a
              section structure. Facts and citations are kept; this is a reader
              lens, not a re-check of grounding. Resets on server restart.
            </p>

            <div className="cz-scopes" role="tablist" aria-label="Scope">
              <button
                type="button"
                className={`cz-scope ${scope === "page" ? "on" : ""}`}
                onClick={() => setScope("page")}
              >
                This page{pageTitle ? ` · ${pageTitle}` : ""}
              </button>
              <button
                type="button"
                className={`cz-scope ${scope === "wiki" ? "on" : ""}`}
                onClick={() => setScope("wiki")}
              >
                Whole wiki
              </button>
            </div>

            <label className="cz-label">Instruction</label>
            <textarea
              ref={taRef}
              className="cz-text"
              value={instruction}
              maxLength={MAX_CHARS}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder="e.g. Explain like a blog post with intuition first, fewer bullet lists, a worked example."
              rows={4}
            />

            <label className="cz-label">
              Structure <span className="cz-hint">(optional — one section per line)</span>
            </label>
            <textarea
              className="cz-text cz-structure"
              value={structure}
              onChange={(e) => setStructure(e.target.value)}
              placeholder={"Intuition\nHow it works\nWorked example\nGotchas"}
              rows={4}
            />

            <div className="cz-foot">
              <span className="cz-count">{instruction.length}/{MAX_CHARS}</span>
              <div className="cz-actions">
                {customized && (
                  <button className="cz-ghost" onClick={reset} type="button" disabled={state === "working"}>
                    Reset to default
                  </button>
                )}
                <button className="cz-ghost" onClick={() => setOpen(false)} type="button">
                  Cancel
                </button>
                <button
                  className="btn-primary"
                  onClick={apply}
                  type="button"
                  disabled={state === "working"}
                >
                  {state === "working" ? "Rewriting…" : "Apply"}
                </button>
              </div>
            </div>
            {state === "error" && <p className="cz-error">{error}</p>}
          </div>
        </div>
      )}
    </>
  );
}
