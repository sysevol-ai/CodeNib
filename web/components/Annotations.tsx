import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import Markdown from "@/components/Markdown";
import {
  AnnotationApiError,
  anchorBlock,
  anchorFromCodeSelection,
  anchorFromSelection,
  createAnnotation,
  decideTask,
  deleteAnnotation,
  dispatchTask,
  fetchTaskPatch,
  keyProvider,
  providerKey,
  setProviderKey,
  fetchAnnotations,
  fetchTaskDiff,
  foldAnswer,
  isLive,
  linkCodeRefs,
  localSession,
  placeAnchor,
  threadsOf,
  updateAnnotation,
  usageLine,
  type AgentInfo,
  type AgentTask,
  type Annotation,
  type AnnotationAnchor,
  type CodeRef,
  type DiffHunk,
  type HumanKind,
  type Thread,
  type WorktreeStatus,
} from "@/lib/annotations";
import "./Annotations.css";

const KIND_LABEL: Record<string, string> = {
  question: "Question",
  change: "Change request",
  note: "Note",
  answer: "Answer",
  summary: "Change made",
};

const STATUS_LABEL: Partial<Record<Annotation["status"], string>> = {
  draft: "Draft",
  dispatched: "With the agent",
  answered: "Answered",
  needs_review: "Diff to review",
  applied: "Applied",
  rejected: "Discarded",
  failed: "No answer",
  resolved: "Resolved",
};

const COMPOSER_KINDS: { kind: HumanKind; label: string; hint: string }[] = [
  { kind: "question", label: "Ask", hint: "1" },
  { kind: "change", label: "Change", hint: "2" },
  { kind: "note", label: "Note", hint: "3" },
];

const PLACEHOLDER: Record<HumanKind, string> = {
  question: "What do you want to know about this?",
  change: "What should change in the code?",
  note: "A fact later tasks must respect, e.g. “this path must stay lock-free”.",
};

const ANCHOR_NOTE: Record<string, string> = {
  moved: "Moved with the code.",
  changed: "The code was edited since; following the closest match.",
  detached: "This code is no longer in the file.",
};

const HIGHLIGHT = "codenib-annotation";
const HIGHLIGHT_ACTIVE = "codenib-annotation-active";
const DRAFT_KEY = "codenib-composer-draft";

// --- State ------------------------------------------------------------------

export interface AnnotationsModel {
  ready: boolean;
  needsSession: boolean;
  error: string | null;
  annotations: Annotation[];
  tasks: AgentTask[];
  agents: AgentInfo[];
  /** The public Wiki: annotations kept per browser, runs on the visitor's key. */
  hosted: boolean;
  /** The person's own checkout: pages may be edited. False until the server says so. */
  local: boolean;
  worktree: WorktreeStatus | null;
  refresh: () => Promise<void>;
}

export function useAnnotations(repoId: string, pageId: string, enabled: boolean): AnnotationsModel {
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [worktree, setWorktree] = useState<WorktreeStatus | null>(null);
  // Unknown until the server answers, so nothing local-only shows on the
  // hosted Wiki while its state loads (or when it fails to).
  const [mode, setMode] = useState<"local" | "hosted" | null>(null);
  const [ready, setReady] = useState(false);
  const [needsSession, setNeedsSession] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef(0);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    const mine = ++request.current;
    try {
      const state = await fetchAnnotations(repoId, pageId);
      if (mine !== request.current) return;
      setAnnotations(state.annotations);
      setTasks(state.tasks);
      setAgents(state.agents);
      setWorktree(state.worktree ?? null);
      setMode(state.mode === "hosted" ? "hosted" : "local");
      setNeedsSession(false);
      setError(null);
    } catch (failure) {
      if (mine !== request.current) return;
      if (failure instanceof AnnotationApiError && failure.status === 403) {
        setNeedsSession(true);
      } else {
        setError(failure instanceof Error ? failure.message : String(failure));
      }
    } finally {
      if (mine === request.current) setReady(true);
    }
  }, [enabled, repoId, pageId]);

  useEffect(() => {
    setAnnotations([]);
    setTasks([]);
    setReady(false);
    if (enabled) localSession();
    void refresh();
  }, [refresh, enabled]);

  const live = tasks.some(isLive);
  // A running task is watched closely; a local Wiki that follows its working
  // tree is checked now and then, to notice files changing under it.
  const following = worktree !== null;
  useEffect(() => {
    if (!live && !following) return;
    const timer = window.setInterval(() => void refresh(), live || worktree?.state === "updating" ? 1500 : 5000);
    return () => window.clearInterval(timer);
  }, [live, following, worktree?.state, refresh]);

  const hosted = mode === "hosted";
  const local = mode === "local";
  return { ready, needsSession, error, annotations, tasks, agents, hosted, local, worktree, refresh };
}

// --- Highlights ---------------------------------------------------------------

interface Placed {
  thread: Thread;
  range: Range | null;
  /** The annotated words were rewritten; this is the closest passage. */
  moved?: boolean;
}

function supportsHighlights(): boolean {
  return typeof CSS !== "undefined" && "highlights" in CSS && typeof Highlight !== "undefined";
}

/** Find each thread's words on the page and mark them without touching the DOM. */
function usePlacedThreads(
  root: RefObject<HTMLElement | null>,
  threads: Thread[],
  activeId: string | null,
  renderKey: string,
): Placed[] {
  const [placed, setPlaced] = useState<Placed[]>([]);
  // The page's text can re-render after it first appears (the Overview swaps
  // its lead once the map loads); a range into replaced nodes collapses, so
  // place the threads again whenever the text changes.
  const [domVersion, setDomVersion] = useState(0);
  useEffect(() => {
    const element = root.current;
    if (!element || typeof MutationObserver === "undefined") return;
    let frame = 0;
    const observer = new MutationObserver(() => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        setDomVersion((v) => v + 1);
      });
    });
    observer.observe(element, { childList: true, subtree: true, characterData: true });
    return () => {
      observer.disconnect();
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [root, renderKey]);
  useLayoutEffect(() => {
    const element = root.current?.querySelector(".wiki-story") ?? root.current;
    if (!element) {
      setPlaced(threads.map((thread) => ({ thread, range: null })));
      return;
    }
    const found = threads.map((thread) => {
      const place =
        thread.root.on.quote && !thread.root.on.path ? placeAnchor(element, thread.root.on) : null;
      return { thread, range: place?.range ?? null, moved: place?.moved ?? false };
    });
    found.sort((a, b) => {
      if (a.range && b.range) return a.range.compareBoundaryPoints(Range.START_TO_START, b.range);
      if (a.range) return -1;
      if (b.range) return 1;
      return a.thread.root.created.localeCompare(b.thread.root.created);
    });
    setPlaced(found);
  }, [root, threads, renderKey, domVersion]);

  useEffect(() => {
    if (!supportsHighlights()) return;
    const open = placed.filter((p) => p.range && p.thread.root.status !== "resolved");
    const all = new Highlight(...open.filter((p) => p.thread.root.id !== activeId).map((p) => p.range!));
    const active = new Highlight(...open.filter((p) => p.thread.root.id === activeId).map((p) => p.range!));
    CSS.highlights.set(HIGHLIGHT, all);
    CSS.highlights.set(HIGHLIGHT_ACTIVE, active);
    return () => {
      CSS.highlights.delete(HIGHLIGHT);
      CSS.highlights.delete(HIGHLIGHT_ACTIVE);
    };
  }, [placed, activeId]);

  return placed;
}

function caretAt(x: number, y: number): { node: Node; offset: number } | null {
  const doc = document as Document & {
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
    caretRangeFromPoint?: (x: number, y: number) => Range | null;
  };
  if (doc.caretPositionFromPoint) {
    const position = doc.caretPositionFromPoint(x, y);
    return position ? { node: position.offsetNode, offset: position.offset } : null;
  }
  const range = doc.caretRangeFromPoint?.(x, y);
  return range ? { node: range.startContainer, offset: range.startOffset } : null;
}

// --- Composer -----------------------------------------------------------------

interface Pending {
  anchor: AnnotationAnchor;
  rect: { left: number; top: number; bottom: number };
  kind: HumanKind | null;
}

function readDraft(quote: string): string {
  try {
    const stored = JSON.parse(window.sessionStorage.getItem(DRAFT_KEY) ?? "null");
    return stored?.quote === quote ? String(stored.text ?? "") : "";
  } catch {
    return "";
  }
}

function writeDraft(quote: string, text: string) {
  try {
    if (text.trim()) window.sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ quote, text }));
    else window.sessionStorage.removeItem(DRAFT_KEY);
  } catch {
    // The text stays in the open composer.
  }
}

function SelectionComposer({
  root,
  pageId,
  agentName,
  onSave,
}: {
  root: RefObject<HTMLElement | null>;
  pageId: string;
  agentName: string;
  onSave: (kind: HumanKind, text: string, anchor: AnnotationAnchor, send: boolean) => Promise<void>;
}) {
  const [pending, setPending] = useState<Pending | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const card = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function pick() {
      const selection = window.getSelection();
      if (!selection || selection.rangeCount === 0) return;
      const range = selection.getRangeAt(0);
      // Code in the source panel, or the words of the page.
      const inCode = range.startContainer.parentElement?.closest(".source-modal .hl-code");
      const element = root.current?.querySelector(".wiki-story");
      const anchor = inCode
        ? anchorFromCodeSelection(range, pageId)
        : element
          ? anchorFromSelection(element, range, pageId)
          : null;
      if (!anchor) return;
      const box = range.getBoundingClientRect();
      setPending({
        anchor,
        rect: { left: box.left + box.width / 2, top: box.top, bottom: box.bottom },
        kind: null,
      });
      setFailure(null);
    }
    function onUp(event: MouseEvent) {
      if (card.current?.contains(event.target as Node)) return;
      window.setTimeout(pick, 0);
    }
    function onDown(event: MouseEvent) {
      if (card.current?.contains(event.target as Node)) return;
      setPending((current) => (current?.kind ? current : null));
    }
    document.addEventListener("mouseup", onUp);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("mouseup", onUp);
      document.removeEventListener("mousedown", onDown);
    };
  }, [root, pageId]);

  useEffect(() => setPending(null), [pageId]);

  const choose = useCallback((kind: HumanKind) => {
    setPending((current) => {
      if (!current) return current;
      setText(readDraft(current.anchor.quote ?? ""));
      return { ...current, kind };
    });
    window.setTimeout(() => textarea.current?.focus(), 0);
  }, []);

  useEffect(() => {
    if (!pending || pending.kind) return;
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
      const index = ["1", "2", "3"].indexOf(event.key);
      if (index !== -1) {
        event.preventDefault();
        choose(COMPOSER_KINDS[index].kind);
      } else if (event.key === "Escape") {
        setPending(null);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [pending, choose]);

  if (!pending) return null;
  const close = () => {
    if (pending.anchor.quote) writeDraft(pending.anchor.quote, text);
    setPending(null);
    setText("");
  };
  const save = async (send: boolean) => {
    if (!pending.kind || !text.trim() || busy) return;
    setBusy(true);
    setFailure(null);
    try {
      await onSave(pending.kind, text.trim(), pending.anchor, send && pending.kind !== "note");
      writeDraft(pending.anchor.quote ?? "", "");
      window.getSelection()?.removeAllRanges();
      setPending(null);
      setText("");
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };
  const onKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void save(event.metaKey || event.ctrlKey);
    }
  };
  const width = 360;
  const left = Math.max(12, Math.min(window.innerWidth - width - 12, pending.rect.left - width / 2));
  const below = pending.rect.bottom + 10;
  const style = pending.kind
    ? { left, top: Math.min(below, window.innerHeight - 260) }
    : { left: Math.max(12, pending.rect.left - 110), top: Math.max(8, pending.rect.top - 46) };

  return createPortal(
    <div
      ref={card}
      data-annotation-ui
      className={pending.kind ? "cn-composer" : "cn-picker"}
      style={style}
      role={pending.kind ? "dialog" : "toolbar"}
      aria-label={pending.kind ? `New ${KIND_LABEL[pending.kind].toLowerCase()}` : "Annotate the selection"}
      onMouseDown={(event) => {
        // Keep the page selection while the picker is clicked.
        if (!pending.kind) event.preventDefault();
      }}
    >
      {!pending.kind ? (
        COMPOSER_KINDS.map((item) => (
          <button key={item.kind} type="button" onClick={() => choose(item.kind)}>
            {item.label}
            <kbd>{item.hint}</kbd>
          </button>
        ))
      ) : (
        <>
          <div className="cn-composer-kinds" role="tablist" aria-label="Kind">
            {COMPOSER_KINDS.map((item) => (
              <button
                key={item.kind}
                type="button"
                role="tab"
                aria-selected={pending.kind === item.kind}
                onClick={() => setPending({ ...pending, kind: item.kind })}
              >
                {KIND_LABEL[item.kind]}
              </button>
            ))}
          </div>
          {pending.anchor.path ? (
            <div className="cn-code-anchor">
              <span className="mono">
                {pending.anchor.path}:{pending.anchor.start_line}
                {pending.anchor.end_line !== pending.anchor.start_line ? `-${pending.anchor.end_line}` : ""}
              </span>
              <pre className="cn-code-quote">{pending.anchor.quote}</pre>
            </div>
          ) : (
            <blockquote className="cn-quote">{pending.anchor.quote}</blockquote>
          )}
          <textarea
            ref={textarea}
            autoFocus
            value={text}
            rows={3}
            placeholder={PLACEHOLDER[pending.kind]}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKeyDown}
            aria-label="Annotation text"
          />
          {failure && (
            <p className="cn-error" role="alert">
              {failure}
            </p>
          )}
          <div className="cn-composer-actions">
            <span className="cn-hint">
              {pending.kind === "note" ? "↵ save" : `↵ save draft · ⌘↵ send to ${agentName}`}
            </span>
            <button type="button" className="cn-button" onClick={close}>
              Cancel
            </button>
            {pending.kind === "note" ? (
              <button type="button" className="cn-button cn-primary" disabled={!text.trim() || busy} onClick={() => void save(false)}>
                Save note
              </button>
            ) : (
              <>
                <button type="button" className="cn-button" disabled={!text.trim() || busy} onClick={() => void save(false)}>
                  Save draft
                </button>
                <button type="button" className="cn-button cn-primary" disabled={!text.trim() || busy} onClick={() => void save(true)}>
                  Send
                </button>
              </>
            )}
          </div>
        </>
      )}
    </div>,
    document.body,
  );
}

// --- Drawer -------------------------------------------------------------------

function elapsed(since: string, now: number): string {
  const start = Date.parse(since);
  if (!Number.isFinite(start)) return "";
  const seconds = Math.max(0, Math.round((now - start) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function codeLabel(ref: CodeRef): string {
  if (ref.start_line && ref.end_line && ref.end_line !== ref.start_line) {
    return `${ref.path}:${ref.start_line}-${ref.end_line}`;
  }
  return ref.start_line ? `${ref.path}:${ref.start_line}` : ref.path;
}

function lineKind(line: string): "add" | "del" | "ctx" | "meta" {
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  if (line.startsWith("\\")) return "meta";
  return "ctx";
}

const HUNK_STATE: Record<DiffHunk["state"], string> = {
  open: "",
  accepted: "Applied",
  rejected: "Discarded",
};
// On the hosted Wiki a kept hunk goes into the patch the person downloads.
const HOSTED_HUNK_STATE: Record<DiffHunk["state"], string> = { ...HUNK_STATE, accepted: "Kept" };

/** The diff hunk by hunk: apply or discard each one (a / x on a focused hunk). */
function HunkReview({
  hunks,
  busy,
  readOnly,
  hosted = false,
  onDecide,
}: {
  hunks: DiffHunk[];
  busy: boolean;
  /** Shown away from the request (a note on another page): states only. */
  readOnly: boolean;
  hosted?: boolean;
  onDecide: (decision: "accept" | "reject", ids: string[]) => void;
}) {
  const states = hosted ? HOSTED_HUNK_STATE : HUNK_STATE;
  const files: { path: string; hunks: DiffHunk[] }[] = [];
  for (const hunk of hunks) {
    const last = files[files.length - 1];
    if (last && last.path === hunk.path) last.hunks.push(hunk);
    else files.push({ path: hunk.path, hunks: [hunk] });
  }
  return (
    <div className="cn-hunks" aria-label="Diff">
      {files.map((file) => (
        <section key={file.path} className="cn-hunk-file">
          <header className="cn-hunk-path mono">{file.path}</header>
          {file.hunks.map((hunk) => (
            <div
              key={hunk.id}
              className={`cn-hunk is-${hunk.state}`}
              tabIndex={hunk.state === "open" && !readOnly ? 0 : -1}
              aria-label={`Hunk ${hunk.header || file.path}, ${hunk.state === "open" ? "not decided" : states[hunk.state]}`}
              onKeyDown={(event) => {
                if (hunk.state !== "open" || busy || readOnly) return;
                if (event.key === "a") onDecide("accept", [hunk.id]);
                if (event.key === "x") onDecide("reject", [hunk.id]);
              }}
              onClick={(event) => event.stopPropagation()}
            >
              <div className="cn-hunk-head">
                <span className="cn-hunk-range mono">
                  {hunk.header ? hunk.header.replace(/^(@@[^@]*@@).*$/, "$1") : "whole file"}
                </span>
                <span className="cn-add">+{hunk.added}</span>
                <span className="cn-del">−{hunk.deleted}</span>
                {hunk.state === "open" ? (
                  readOnly ? null : (
                  <>
                    <button type="button" className="cn-link cn-push" disabled={busy} onClick={() => onDecide("accept", [hunk.id])}>
                      {hosted ? "Keep" : "Apply"}
                    </button>
                    <button type="button" className="cn-link" disabled={busy} onClick={() => onDecide("reject", [hunk.id])}>
                      Discard
                    </button>
                  </>
                  )
                ) : (
                  <span className="cn-status cn-push">{states[hunk.state]}</span>
                )}
              </div>
              <pre className="cn-diff">
                {hunk.lines.map((line, index) => (
                  <span key={index} className={`cn-diff-${lineKind(line)}`}>
                    {line || " "}
                    {"\n"}
                  </span>
                ))}
              </pre>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}

/** Ask for the visitor's key once per tab, before the first run. */
function KeyPrompt({
  count,
  agentName,
  busy,
  onKey,
  onCancel,
}: {
  count: number;
  agentName: string;
  busy: boolean;
  onKey: (key: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState("");
  const provider = keyProvider(value);
  return (
    <form
      className="cn-key"
      onSubmit={(event) => {
        event.preventDefault();
        if (provider) onKey(value.trim());
      }}
    >
      <label htmlFor="cn-provider-key">
        Your OpenRouter or Anthropic API key, to send {count === 1 ? "this" : `these ${count}`} to {agentName}
      </label>
      <input
        id="cn-provider-key"
        type="password"
        autoComplete="off"
        spellCheck={false}
        autoFocus
        value={value}
        placeholder="sk-or-… or sk-ant-…"
        onChange={(event) => setValue(event.target.value)}
      />
      <p className="cn-meta">
        It goes with each request, pays for that run only and is never saved on our side; this tab keeps it until
        you close it. Get one at{" "}
        <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener noreferrer">
          openrouter.ai
        </a>
        , with a credit limit.
      </p>
      <div className="cn-composer-actions">
        {value && !provider && <span className="cn-hint">Keys start with sk-or- or sk-ant-</span>}
        <button type="button" className="cn-button" onClick={onCancel}>
          Not now
        </button>
        <button type="submit" className="cn-button cn-primary" disabled={!provider || busy}>
          Send
        </button>
      </div>
    </form>
  );
}

function ReplyBox({
  agentName,
  onSave,
}: {
  agentName: string;
  onSave: (text: string, send: boolean) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  if (!open) {
    return (
      <button type="button" className="cn-link" onClick={() => setOpen(true)}>
        Reply
      </button>
    );
  }
  const save = async (send: boolean) => {
    if (!text.trim() || busy) return;
    setBusy(true);
    try {
      await onSave(text.trim(), send);
      setText("");
      setOpen(false);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="cn-reply">
      <textarea
        autoFocus
        rows={2}
        value={text}
        placeholder="Ask a follow-up"
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") setOpen(false);
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            void save(event.metaKey || event.ctrlKey);
          }
        }}
        aria-label="Reply"
      />
      <div className="cn-composer-actions">
        <span className="cn-hint">⌘↵ send to {agentName}</span>
        <button type="button" className="cn-button" disabled={busy || !text.trim()} onClick={() => void save(false)}>
          Save draft
        </button>
        <button type="button" className="cn-button cn-primary" disabled={busy || !text.trim()} onClick={() => void save(true)}>
          Send
        </button>
      </div>
    </div>
  );
}

function ThreadCard({
  placed,
  active,
  tasks,
  selected,
  agentName,
  repoId,
  hosted,
  now,
  onSelect,
  onToggle,
  onFocus,
  onOpenCode,
  onPage,
  act,
}: {
  placed: Placed;
  active: boolean;
  tasks: Map<string, AgentTask>;
  selected: boolean;
  agentName: string;
  repoId: string;
  hosted: boolean;
  now: number;
  onSelect: () => void;
  onToggle: (on: boolean) => void;
  onFocus: () => void;
  onOpenCode: (ref: CodeRef) => void;
  onPage: (pageId: string) => void;
  act: Actions;
}) {
  const { thread, range } = placed;
  const items = [thread.root, ...thread.replies];
  const [diffFor, setDiffFor] = useState<string | null>(null);
  const [hunks, setHunks] = useState<DiffHunk[] | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const last = items[items.length - 1];
  const card = useRef<HTMLElement>(null);

  useEffect(() => {
    if (active) card.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [active]);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setFailure(null);
    try {
      await action();
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };
  const download = async (taskId: string) => {
    const { name, text } = await fetchTaskPatch(repoId, taskId);
    const url = URL.createObjectURL(new Blob([text], { type: "text/x-diff" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const loadHunks = async (taskId: string) =>
    setHunks((await fetchTaskDiff(repoId, taskId)).hunks);
  const showDiff = async (taskId: string) => {
    if (diffFor === taskId) {
      setDiffFor(null);
      return;
    }
    setDiffFor(taskId);
    setHunks(null);
    await run(() => loadHunks(taskId));
  };
  const decideHunks = (taskId: string, decision: "accept" | "reject", ids?: string[]) =>
    run(async () => {
      try {
        await act.decide(taskId, decision, ids);
      } finally {
        if (diffFor === taskId) await loadHunks(taskId);
      }
    });

  return (
    <article
      ref={card}
      className={`cn-thread${active ? " is-active" : ""}${thread.root.status === "resolved" ? " is-resolved" : ""}`}
      onClick={onSelect}
      aria-label={`${KIND_LABEL[thread.root.kind]}: ${thread.root.text.slice(0, 60)}`}
    >
      {thread.root.on.path ? (
        <div className="cn-code-anchor">
          <button
            type="button"
            className="cn-file mono"
            onClick={(event) => {
              event.stopPropagation();
              onOpenCode({
                path: thread.root.on.path!,
                start_line: thread.root.on.start_line,
                end_line: thread.root.on.end_line,
              });
            }}
            title="Open this code"
          >
            {thread.root.on.path}:{thread.root.on.start_line}
            {thread.root.on.end_line !== thread.root.on.start_line ? `-${thread.root.on.end_line}` : ""}
          </button>
          {thread.root.on.quote && <pre className="cn-code-quote">{thread.root.on.quote}</pre>}
          {thread.root.anchor_state && thread.root.anchor_state !== "current" && (
            <p className={`cn-detached is-${thread.root.anchor_state}`}>
              {ANCHOR_NOTE[thread.root.anchor_state]}
            </p>
          )}
        </div>
      ) : thread.root.on.quote ? (
        <button
          type="button"
          className={`cn-quote cn-quote-link${range ? "" : " is-detached"}`}
          onClick={(event) => {
            event.stopPropagation();
            onFocus();
          }}
          title={range ? "Show on the page" : "These words are no longer on the page"}
        >
          {thread.root.on.quote}
        </button>
      ) : null}
      {!range && thread.root.on.quote && !thread.root.on.path && (
        <p className="cn-detached">Not found on this page anymore.</p>
      )}
      {range && placed.moved && (
        <p className="cn-detached">These words were rewritten; shown on the closest passage.</p>
      )}
      {items.map((item) => {
        const task = item.task ? tasks.get(item.task) : undefined;
        const mine = item.author === "human";
        const draft = mine && item.status === "draft";
        const found = mine ? null : linkCodeRefs(item.text);
        const linked = found && {
          markdown: found.markdown,
          refs: found.refs,
          citations: found.refs.map((ref) => ({
            file: ref.path,
            start_line: ref.start_line ?? null,
            end_line: ref.end_line ?? null,
            node_name: "",
            type: "reference",
            score: null,
            content: null,
          })),
        };
        const named = new Set(found?.refs.map((ref) => ref.path) ?? []);
        const unlinked = item.files.filter((ref) => !named.has(ref.path));
        return (
          <section key={item.id} className={`cn-item ${mine ? "is-human" : "is-agent"}`}>
            <header className="cn-item-head">
              {draft && (item.kind === "question" || item.kind === "change") && (
                <input
                  type="checkbox"
                  checked={selected && item.id === last.id}
                  disabled={item.id !== last.id}
                  onChange={(event) => onToggle(event.target.checked)}
                  onClick={(event) => event.stopPropagation()}
                  aria-label="Include in the next task"
                />
              )}
              <span className="cn-kind">{mine ? KIND_LABEL[item.kind] : `${agentName} · ${KIND_LABEL[item.kind]}`}</span>
              {mine && STATUS_LABEL[item.status] && (
                <span className={`cn-status is-${item.status}`}>
                  {item.status === "dispatched" && isLive(task) ? (
                    <>
                      <span className="cn-spinner" aria-hidden />
                      With {agentName} {task?.started ? elapsed(task.started, now) : ""}
                    </>
                  ) : (
                    STATUS_LABEL[item.status]
                  )}
                </span>
              )}
              {draft && (
                <button
                  type="button"
                  className="cn-link cn-push"
                  onClick={(event) => {
                    event.stopPropagation();
                    void run(() => act.remove(item.id));
                  }}
                >
                  Delete
                </button>
              )}
            </header>
            {mine ? (
              <p className="cn-text">{item.text}</p>
            ) : (
              <div className="cn-answer">
                <Markdown
                  repoId={repoId}
                  onPageLink={onPage}
                  citations={linked!.citations}
                  onCite={(index) => {
                    const ref = linked!.refs[index];
                    if (ref) onOpenCode(ref);
                  }}
                >
                  {linked!.markdown}
                </Markdown>
              </div>
            )}
            {unlinked.length > 0 && (
              <ul className="cn-files" aria-label="Code it cites">
                {unlinked.map((ref) => (
                  <li key={codeLabel(ref)}>
                    <button
                      type="button"
                      className="cn-file mono"
                      onClick={(event) => {
                        event.stopPropagation();
                        onOpenCode(ref);
                      }}
                    >
                      {codeLabel(ref)}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {!mine && item.kind === "summary" && task && task.diff.files && (
              <div className="cn-change">
                <p className="cn-meta" title={task.diff.branch ? `Branch ${task.diff.branch}` : undefined}>
                  <span className="cn-add">+{task.diff.files.reduce((n, f) => n + f.added, 0)}</span>{" "}
                  <span className="cn-del">−{task.diff.files.reduce((n, f) => n + f.deleted, 0)}</span>{" "}
                  in {task.diff.files.map((f) => f.path.split("/").pop()).join(", ")}
                </p>
                <div className="cn-row">
                  <button type="button" className="cn-button" onClick={(e) => { e.stopPropagation(); void showDiff(task.id); }}>
                    {diffFor === task.id ? "Hide diff" : "View diff"}
                  </button>
                  {hosted && item.reply_to && (task.status === "needs_review" || item.status === "applied") && (
                    <button
                      type="button"
                      className="cn-button cn-primary"
                      disabled={busy}
                      title="The change without the hunks you discarded; apply it with git apply"
                      onClick={(e) => {
                        e.stopPropagation();
                        void run(() => download(task.id));
                      }}
                    >
                      Download patch
                    </button>
                  )}
                  {task.status === "needs_review" && item.reply_to && !hosted && (
                    <>
                      <button
                        type="button"
                        className="cn-button cn-primary"
                        disabled={busy}
                        onClick={(e) => {
                          e.stopPropagation();
                          void decideHunks(task.id, "accept");
                        }}
                      >
                        {(task.diff.accepted?.length ?? 0) + (task.diff.rejected?.length ?? 0) > 0
                          ? "Apply the rest"
                          : "Apply to working tree"}
                      </button>
                      <button
                        type="button"
                        className="cn-button"
                        disabled={busy}
                        onClick={(e) => {
                          e.stopPropagation();
                          void decideHunks(task.id, "reject");
                        }}
                      >
                        {(task.diff.accepted?.length ?? 0) + (task.diff.rejected?.length ?? 0) > 0
                          ? "Discard the rest"
                          : "Discard"}
                      </button>
                    </>
                  )}
                  {hosted && task.status === "needs_review" && item.reply_to && (
                    <button
                      type="button"
                      className="cn-button"
                      disabled={busy}
                      onClick={(e) => {
                        e.stopPropagation();
                        void decideHunks(task.id, "reject");
                      }}
                    >
                      Discard
                    </button>
                  )}
                  {item.status === "applied" && <span className="cn-status is-applied">{hosted ? "Kept" : "Applied"}</span>}
                  {item.status === "rejected" && <span className="cn-status is-rejected">Discarded</span>}
                </div>
                {diffFor === task.id &&
                  (hunks === null ? (
                    <p className="cn-meta">Loading the diff…</p>
                  ) : (
                    <HunkReview
                      hunks={hunks}
                      busy={busy}
                      hosted={hosted}
                      readOnly={task.status !== "needs_review" || !item.reply_to}
                      onDecide={(decision, ids) => void decideHunks(task.id, decision, ids)}
                    />
                  ))}
                {task.diff.pages && task.diff.pages.length > 1 && (
                  <p className="cn-meta">
                    Also on{" "}
                    {task.diff.pages
                      .filter((p) => p.id !== thread.root.on.page)
                      .map((p, index) => (
                        <span key={p.id}>
                          {index > 0 && ", "}
                          <button type="button" className="cn-link" onClick={(e) => { e.stopPropagation(); onPage(p.id); }}>
                            {p.title || p.id}
                          </button>
                        </span>
                      ))}
                  </p>
                )}
              </div>
            )}
            {!mine && task && task.usage && usageLine(task.usage) && item.id === task.results[0] && (
              <p className="cn-meta cn-usage">{usageLine(task.usage)}</p>
            )}
            {!mine && item.kind === "answer" && !hosted && (
              <div className="cn-row">
                {item.status === "applied" ? (
                  <span className="cn-status is-applied">Added to the page</span>
                ) : (
                  <button
                    type="button"
                    className="cn-button"
                    disabled={busy}
                    title="Add this answer to the page, under its question"
                    onClick={(event) => {
                      event.stopPropagation();
                      void run(() => act.fold(item));
                    }}
                  >
                    Add to page
                  </button>
                )}
              </div>
            )}
            {mine && item.status === "failed" && task?.error && <p className="cn-error">{task.error}</p>}
          </section>
        );
      })}
      {failure && (
        <p className="cn-error" role="alert">
          {failure}
        </p>
      )}
      <footer className="cn-thread-foot" onClick={(event) => event.stopPropagation()}>
        {last.author === "agent" && thread.root.status !== "resolved" && thread.root.kind !== "note" && (
          <ReplyBox
            agentName={agentName}
            onSave={(text, send) => act.reply(thread.root, last, text, send)}
          />
        )}
        {thread.root.status === "resolved" ? (
          <button type="button" className="cn-link" onClick={() => void run(() => act.reopen(thread.root))}>
            Reopen
          </button>
        ) : (
          !items.some((item) => item.status === "dispatched" || item.status === "draft") && (
            <button type="button" className="cn-link" onClick={() => void run(() => act.resolve(thread.root))}>
              Resolve
            </button>
          )
        )}
      </footer>
    </article>
  );
}

interface Actions {
  fold: (answer: Annotation) => Promise<void>;
  remove: (id: string) => Promise<void>;
  reply: (root: Annotation, last: Annotation, text: string, send: boolean) => Promise<void>;
  resolve: (root: Annotation) => Promise<void>;
  reopen: (root: Annotation) => Promise<void>;
  decide: (taskId: string, decision: "accept" | "reject", hunks?: string[]) => Promise<void>;
}

// --- The layer the reader mounts ----------------------------------------------

export function AnnotationLayer({
  repoId,
  pageId,
  pageTitle,
  contentRef,
  renderKey,
  model,
  open,
  onOpenChange,
  onOpenCode,
  onPage,
  onPageChanged,
  onCodeAnnotated,
}: {
  repoId: string;
  pageId: string;
  pageTitle: string;
  contentRef: RefObject<HTMLElement | null>;
  /** Changes whenever the page's text is re-rendered. */
  renderKey: string;
  model: AnnotationsModel;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onOpenCode: (ref: CodeRef) => void;
  onPage: (pageId: string) => void;
  /** The page's text changed (an answer was added); reload it. */
  onPageChanged: () => void;
  /** Code in the source panel was annotated: close the panel to show Notes. */
  onCodeAnnotated?: () => void;
}) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [showResolved, setShowResolved] = useState(false);
  const [pageNote, setPageNote] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const { annotations, tasks, agents, refresh, hosted } = model;
  const agent = agents[0];
  // Drafts waiting for the visitor's key before they can be sent.
  const [awaitingKey, setAwaitingKey] = useState<string[] | null>(null);
  const [hasKey, setHasKey] = useState(() => !!providerKey());
  const agentName = agent?.name ?? "Claude Code";
  const threads = useMemo(() => threadsOf(annotations), [annotations]);
  const placed = usePlacedThreads(contentRef, threads, activeId, `${renderKey}:${pageId}`);
  const taskById = useMemo(() => new Map(tasks.map((task) => [task.id, task])), [tasks]);
  const live = tasks.find(isLive);

  useEffect(() => {
    setActiveId(null);
    setExcluded(new Set());
    setFailure(null);
  }, [pageId]);

  // When the Wiki caught up with changed files, show the page as it is now.
  const updatedAt = model.worktree?.updated_at ?? "";
  const seenUpdate = useRef(updatedAt);
  useEffect(() => {
    if (updatedAt && updatedAt !== seenUpdate.current) onPageChanged();
    seenUpdate.current = updatedAt;
  }, [updatedAt, onPageChanged]);

  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);

  // Clicking marked words opens their thread.
  useEffect(() => {
    const element = contentRef.current;
    if (!element) return;
    function onClick(event: MouseEvent) {
      if (window.getSelection()?.toString()) return;
      const caret = caretAt(event.clientX, event.clientY);
      if (!caret) return;
      const hit = placed.find((p) => p.range && p.thread.root.status !== "resolved" && p.range.isPointInRange(caret.node, caret.offset));
      if (hit) {
        setActiveId(hit.thread.root.id);
        onOpenChange(true);
      }
    }
    element.addEventListener("click", onClick);
    return () => element.removeEventListener("click", onClick);
  }, [contentRef, placed, onOpenChange]);

  const sendable = placed
    .filter((p) => p.thread.root.status !== "resolved")
    .map((p) => {
      const items = [p.thread.root, ...p.thread.replies];
      const last = items[items.length - 1];
      return last.author === "human" && last.status === "draft" && (last.kind === "question" || last.kind === "change") ? last : null;
    })
    .filter((item): item is Annotation => !!item);
  const chosen = sendable.filter((item) => !excluded.has(item.id));

  const send = async (ids: string[], key: string = providerKey()) => {
    if (!ids.length) return;
    if (hosted && !key) {
      setAwaitingKey(ids);
      onOpenChange(true);
      return;
    }
    setSending(true);
    setFailure(null);
    try {
      await dispatchTask(repoId, ids, hosted ? key : undefined);
      setExcluded(new Set());
      setAwaitingKey(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setFailure(message);
      // A refused key is forgotten and asked for again.
      if (hosted && error instanceof AnnotationApiError && (error.status === 400 || error.status === 402) && /key/i.test(message)) {
        setProviderKey("");
        setHasKey(false);
        setAwaitingKey(ids);
      }
    } finally {
      setSending(false);
      await refresh();
    }
  };

  const act: Actions = {
    fold: async (answer) => {
      await foldAnswer(repoId, pageId, answer.id);
      await refresh();
      onPageChanged();
    },
    remove: async (id) => {
      await deleteAnnotation(repoId, id);
      await refresh();
    },
    reply: async (root, last, text, sendNow) => {
      const created = await createAnnotation(repoId, {
        kind: root.kind === "change" ? "change" : "question",
        text,
        on: root.on,
        reply_to: last.id,
      });
      await refresh();
      if (sendNow) await send([created.id]);
    },
    resolve: async (root) => {
      await updateAnnotation(repoId, root.id, { status: "resolved" });
      await refresh();
    },
    reopen: async (root) => {
      await updateAnnotation(repoId, root.id, { status: root.kind === "note" ? "open" : "draft" });
      await refresh();
    },
    decide: async (taskId, decision, hunks) => {
      try {
        await decideTask(repoId, taskId, decision, hunks);
      } finally {
        await refresh();
      }
    },
  };

  const onSave = async (kind: HumanKind, text: string, anchor: AnnotationAnchor, sendNow: boolean) => {
    const created = await createAnnotation(repoId, { kind, text, on: anchor });
    if (anchor.path) onCodeAnnotated?.();
    setActiveId(created.id);
    onOpenChange(true);
    await refresh();
    if (sendNow) await send([created.id]);
  };

  const focus = (placedThread: Placed) => {
    setActiveId(placedThread.thread.root.id);
    if (placedThread.range) {
      const root = contentRef.current?.querySelector(".wiki-story") ?? contentRef.current;
      if (root) anchorBlock(root, placedThread.range).scrollIntoView({ block: "center", behavior: "smooth" });
    }
  };

  const openThreads = placed.filter((p) => p.thread.root.status !== "resolved");
  const resolved = placed.filter((p) => p.thread.root.status === "resolved");
  const pageLevel = openThreads.filter((p) => !p.thread.root.on.quote && !p.thread.root.on.path);
  const onCode = openThreads.filter((p) => p.thread.root.on.path);
  const quoted = openThreads.filter((p) => p.thread.root.on.quote && !p.thread.root.on.path);
  const renderThread = (p: Placed) => (
    <ThreadCard
      key={p.thread.root.id}
      placed={p}
      active={activeId === p.thread.root.id}
      tasks={taskById}
      selected={!excluded.has([p.thread.root, ...p.thread.replies].slice(-1)[0].id)}
      agentName={agentName}
      repoId={repoId}
      hosted={hosted}
      now={now}
      onSelect={() => setActiveId(p.thread.root.id)}
      onToggle={(on) => {
        const id = [p.thread.root, ...p.thread.replies].slice(-1)[0].id;
        setExcluded((current) => {
          const next = new Set(current);
          if (on) next.delete(id);
          else next.add(id);
          return next;
        });
      }}
      onFocus={() => focus(p)}
      onOpenCode={onOpenCode}
      onPage={onPage}
      act={act}
    />
  );

  return (
    <>
      {!model.needsSession && (
        <SelectionComposer root={contentRef} pageId={pageId} agentName={agentName} onSave={onSave} />
      )}
      {open && (
        <aside className="cn-drawer" data-annotation-ui aria-label="Notes on this page">
          <header className="cn-drawer-head">
            <div>
              <h2>Notes</h2>
              <p className="cn-meta">{pageTitle}</p>
            </div>
            <button type="button" className="cn-close" onClick={() => onOpenChange(false)} aria-label="Close notes">
              ×
            </button>
          </header>
          {model.needsSession ? (
            <div className="cn-empty">
              <p>
                Open this Wiki from the link <code>codenib wiki</code> printed in your terminal. It carries this
                session&rsquo;s key, which annotating needs.
              </p>
            </div>
          ) : (
            <>
              <div className="cn-agent">
                <span className={`cn-dot${agent?.available ? " is-on" : ""}`} aria-hidden />
                {hosted ? (
                  <span>
                    {agent?.available ? (
                      <>
                        <strong>{agentName}</strong> answers here, on your own OpenRouter or Anthropic key
                        {agent?.budget_usd ? `, up to $${agent.budget_usd.toFixed(2)} a request` : ""}. Changes come
                        back as a patch.
                      </>
                    ) : (
                      <>{agentName} is not available on this server right now. Notes still save.</>
                    )}
                    {hasKey && (
                      <>
                        {" "}
                        <button
                          type="button"
                          className="cn-link"
                          onClick={() => {
                            setProviderKey("");
                            setHasKey(false);
                          }}
                        >
                          Forget key
                        </button>
                      </>
                    )}
                  </span>
                ) : agent?.available ? (
                  <span>
                    Tasks run on your <strong>{agentName}</strong>, in a worktree for changes
                  </span>
                ) : (
                  <span>
                    {agentName} was not found. Install it with <code>{agent?.install ?? "npm i -g @anthropic-ai/claude-code"}</code>
                  </span>
                )}
              </div>
              {model.worktree?.state === "updating" && (
                <div className="cn-live" role="status">
                  <span className="cn-spinner" aria-hidden />
                  <span>
                    Updating the Wiki for {model.worktree.changed.length || "your"} changed{" "}
                    {model.worktree.changed.length === 1 ? "file" : "files"}
                  </span>
                </div>
              )}
              {model.worktree?.state === "failed" && (
                <p className="cn-error cn-pad" role="alert">
                  The Wiki could not catch up with your changed files: {model.worktree.error}
                </p>
              )}
              {live && (
                <div className="cn-live" role="status">
                  <span className="cn-spinner" aria-hidden />
                  <span>
                    {live.progress || "Starting"} · {elapsed(live.started || live.created, now)}
                  </span>
                </div>
              )}
              {sendable.length > 0 && (
                <div className="cn-dispatch">
                  <span>
                    {chosen.length} of {sendable.length} {sendable.length === 1 ? "draft" : "drafts"} selected
                  </span>
                  <button
                    type="button"
                    className="cn-button cn-primary"
                    disabled={!chosen.length || sending || !!live || !agent?.available}
                    onClick={() => void send(chosen.map((item) => item.id))}
                  >
                    Send to {agentName}
                  </button>
                </div>
              )}
              {(failure || model.error) && (
                <p className="cn-error cn-pad" role="alert">
                  {failure || model.error}
                </p>
              )}
              {hosted && awaitingKey && (
                <KeyPrompt
                  count={awaitingKey.length}
                  agentName={agentName}
                  busy={sending}
                  onCancel={() => setAwaitingKey(null)}
                  onKey={(key) => {
                    setProviderKey(key);
                    setHasKey(true);
                    void send(awaitingKey, key);
                  }}
                />
              )}
              <div className="cn-threads">
                {pageLevel.length > 0 && (
                  <>
                    <h3 className="cn-group">On this page</h3>
                    {pageLevel.map(renderThread)}
                  </>
                )}
                {quoted.map(renderThread)}
                {onCode.length > 0 && (
                  <>
                    <h3 className="cn-group">On the code</h3>
                    {onCode.map(renderThread)}
                  </>
                )}
                {model.ready && openThreads.length === 0 && (
                  <div className="cn-empty">
                    <p>
                      Select words on the page to ask about them, request a change, or leave a note.
                      Questions and change requests go to {agentName}; its answers come back here.
                    </p>
                  </div>
                )}
                {pageNote === null ? (
                  <button type="button" className="cn-link cn-pad" onClick={() => setPageNote("")}>
                    Add a note to this page
                  </button>
                ) : (
                  <div className="cn-reply cn-pad">
                    <textarea
                      autoFocus
                      rows={2}
                      value={pageNote}
                      placeholder={PLACEHOLDER.note}
                      onChange={(event) => setPageNote(event.target.value)}
                      aria-label="Note on this page"
                    />
                    <div className="cn-composer-actions">
                      <button type="button" className="cn-button" onClick={() => setPageNote(null)}>
                        Cancel
                      </button>
                      <button
                        type="button"
                        className="cn-button cn-primary"
                        disabled={!pageNote.trim()}
                        onClick={async () => {
                          await createAnnotation(repoId, { kind: "note", text: pageNote.trim(), on: { page: pageId } });
                          setPageNote(null);
                          await refresh();
                        }}
                      >
                        Save note
                      </button>
                    </div>
                  </div>
                )}
                {resolved.length > 0 && (
                  <>
                    <button type="button" className="cn-link cn-pad" onClick={() => setShowResolved((v) => !v)}>
                      {showResolved ? "Hide" : "Show"} resolved ({resolved.length})
                    </button>
                    {showResolved && resolved.map(renderThread)}
                  </>
                )}
              </div>
            </>
          )}
        </aside>
      )}
    </>
  );
}

/** The header button: how many open threads this page has. */
export function NotesButton({
  model,
  open,
  onClick,
}: {
  model: AnnotationsModel;
  open: boolean;
  onClick: () => void;
}) {
  const threads = threadsOf(model.annotations).filter((t) => t.root.status !== "resolved");
  const live = model.tasks.some(isLive);
  const review = model.tasks.some((task) => task.status === "needs_review");
  return (
    <button
      type="button"
      className={`cn-launch${open ? " is-open" : ""}`}
      onClick={onClick}
      aria-pressed={open}
      title="Questions, change requests and notes on this page"
    >
      {live ? <span className="cn-spinner" aria-hidden /> : <span className={`cn-dot${review ? " is-review" : " is-on"}`} aria-hidden />}
      <span>Notes{threads.length ? ` · ${threads.length}` : ""}</span>
    </button>
  );
}
