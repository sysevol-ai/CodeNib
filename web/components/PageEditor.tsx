import { useEffect, useRef, useState } from "react";
import { fetchPageSource, savePageSource, type PageSource } from "@/lib/annotations";
import "./Annotations.css";

/**
 * Edit a page's Markdown in place. A Wiki page saves as a person's edit that
 * regeneration never overwrites; a repository document saves to its file.
 */
export default function PageEditor({
  repoId,
  pageId,
  onDone,
}: {
  repoId: string;
  pageId: string;
  onDone: (saved: boolean) => void;
}) {
  const [source, setSource] = useState<PageSource | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const area = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let cancelled = false;
    fetchPageSource(repoId, pageId)
      .then((loaded) => {
        if (cancelled) return;
        setSource(loaded);
        setText(loaded.markdown);
        window.setTimeout(() => area.current?.focus(), 0);
      })
      .catch((error) => !cancelled && setFailure(error instanceof Error ? error.message : String(error)));
    return () => {
      cancelled = true;
    };
  }, [repoId, pageId]);

  const dirty = !!source && text !== source.markdown;
  const save = async () => {
    if (!source || busy) return;
    if (!dirty) {
      onDone(false);
      return;
    }
    setBusy(true);
    setFailure(null);
    try {
      await savePageSource(repoId, pageId, text, source.base);
      onDone(true);
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="cn-editor" data-annotation-ui>
      <div className="cn-editor-bar">
        <span className="cn-meta">
          {source
            ? source.kind === "document"
              ? <>Editing <code>{source.path}</code> in the repository</>
              : <>Your edit is saved as <code>{source.path}</code>; regenerating the Wiki keeps it</>
            : "Loading the page…"}
        </span>
        <span className="cn-hint">⌘S save · Esc cancel</span>
        <button type="button" className="cn-button" onClick={() => onDone(false)} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="cn-button cn-primary" onClick={() => void save()} disabled={!source || busy}>
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
      {failure && (
        <p className="cn-error" role="alert">
          {failure}
        </p>
      )}
      <textarea
        ref={area}
        className="cn-editor-text"
        value={text}
        spellCheck
        disabled={!source}
        aria-label="Page Markdown"
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
            event.preventDefault();
            void save();
          } else if (event.key === "Escape") {
            event.preventDefault();
            onDone(false);
          }
        }}
      />
    </div>
  );
}
