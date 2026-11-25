import { GripVertical, MessageCircleQuestion, PencilLine, RotateCw, StickyNote } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Kbd } from "@/components/ui/kbd";
import { Textarea } from "@/components/ui/textarea";
import { message, type AgentStatus, type WriterId } from "@/lib/backend";
import { agentName, ago, taskNeed, type Anchor, type Annotation, type Page, type Task } from "@/lib/model";
import { repoPath, useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { Markdown, splitBlocks } from "./markdown";
import { SourceMark } from "./SourceMark";
import { StatusMark } from "./StatusMark";

export type Kind = "ask" | "change" | "note";

const KIND: Record<Kind, { api: "question" | "change" | "note"; placeholder: string; send: string }> = {
  ask: { api: "question", placeholder: "Ask about this", send: "Ask" },
  change: { api: "change", placeholder: "Describe the change you want", send: "Request change" },
  note: { api: "note", placeholder: "Write a note that every later task on this page reads", send: "Save note" },
};

const squash = (text: string) => text.replace(/\s+/g, " ").trim();

/** The words a reader sees in a block of Markdown, for anchoring a note. */
function plain(markdown: string): string {
  return squash(
    markdown
      .replace(/^```.*$/gm, "")
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/^#+\s*/gm, "")
      .replace(/^\s*([-*+]|\d+\.)\s+/gm, "")
      .replace(/[`*_|>]/g, ""),
  );
}

/** Why the chosen agent cannot take a task now, or null when it can. */
export function agentProblem(agents: AgentStatus[] | null, id: WriterId): string | null {
  const agent = agents?.find((a) => a.id === id);
  if (!agents || !agent) return null;
  if (!agent.installed) return `${agent.name} is not installed on this Mac. Open Accounts to install it.`;
  if (!agent.signed_in) return `${agent.name} is not signed in. Open Accounts to sign in.`;
  return null;
}

/** Save an annotation and, for a question or a change, send it to the chosen agent. */
export function useAnnotate(repo: string) {
  const { data, call, refreshNotes, agents, taskAgent, setAccountsOpen } = useStore();
  return useCallback(
    async (kind: Kind, text: string, on: Anchor, replyTo?: string) => {
      const d = data[repo];
      const base = repoPath(d, "");
      if (!base) throw new Error("This repository is not open yet");
      const problem = kind === "note" ? null : agentProblem(agents, taskAgent);
      if (problem) {
        setAccountsOpen(true);
        throw new Error(problem);
      }
      const created = await call<Annotation>(repo, "POST", `${base}/annotations`, {
        kind: KIND[kind].api,
        text,
        on,
        reply_to: replyTo ?? null,
      });
      if (kind !== "note") {
        await call<Task>(repo, "POST", `${base}/tasks`, { annotations: [created.id], agent: taskAgent });
      }
      await refreshNotes(repo);
      return created;
    },
    [repo, data, call, refreshNotes, agents, taskAgent, setAccountsOpen],
  );
}

export function Composer({
  kind,
  onSend,
  onClose,
  initial = "",
}: {
  kind: Kind;
  onSend: (text: string) => Promise<unknown>;
  onClose: () => void;
  initial?: string;
}) {
  const { taskAgent } = useStore();
  const who = agentName(taskAgent);
  const [text, setText] = useState(initial);
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const copy = KIND[kind];
  // A menu that just closed may still hold focus for a moment.
  useEffect(() => {
    const timer = window.setTimeout(() => box.current?.focus(), 40);
    return () => window.clearTimeout(timer);
  }, []);
  const send = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    try {
      await onSend(text.trim());
      onClose();
    } catch (error) {
      toast.error(message(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="composer">
      <Textarea
        ref={box}
        rows={2}
        value={text}
        placeholder={copy.placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void send();
          if (e.key === "Escape") onClose();
        }}
      />
      <div className="composer-row">
        <span className="composer-hint">
          {kind === "note" ? "Saved in the repository's .codenib/notes/." : `Runs on your ${who}, on this Mac.`}{" "}
          <Kbd>⌘</Kbd>
          <Kbd>Enter</Kbd>
        </span>
        <Button variant="ghost" size="sm" onClick={onClose}>
          Cancel
        </Button>
        <Button size="sm" onClick={() => void send()} disabled={busy || !text.trim()}>
          {busy ? "Sending…" : copy.send}
        </Button>
      </div>
    </div>
  );
}

/** One block of the page with its Notion-style handle. */
function Block({
  children,
  label,
  className,
  onCompose,
  composing,
  index,
}: {
  children: ReactNode;
  label: string;
  className?: string;
  onCompose: (kind: Kind | null) => void;
  composing: ReactNode;
  index: number;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div
      className={cn("block", (open || composing) && "is-active", className)}
      data-block={index}
      onKeyDown={(e) => {
        if (!open) return;
        const kind = ({ "1": "ask", "2": "change", "3": "note" } as Record<string, Kind>)[e.key];
        if (kind) {
          e.preventDefault();
          setOpen(false);
          onCompose(kind);
        }
      }}
    >
      <DropdownMenu open={open} onOpenChange={setOpen}>
        <DropdownMenuTrigger asChild>
          <button type="button" className="block-handle" aria-label={`Ask, change or note on ${label}`}>
            <GripVertical />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          side="bottom"
          align="start"
          sideOffset={6}
          className="block-menu"
          // The composer takes focus; the handle must not take it back.
          onCloseAutoFocus={(e) => e.preventDefault()}
        >
          <DropdownMenuItem onSelect={() => onCompose("ask")}>
            <MessageCircleQuestion />
            Ask about this
            <Kbd className="ml-auto">1</Kbd>
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => onCompose("change")}>
            <PencilLine />
            Request a change
            <Kbd className="ml-auto">2</Kbd>
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => onCompose("note")}>
            <StickyNote />
            Leave a note
            <Kbd className="ml-auto">3</Kbd>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {children}
      {composing}
    </div>
  );
}

interface Thread {
  root: Annotation;
  replies: Annotation[];
  task?: Task;
}

function threadsFor(annotations: Annotation[], tasks: Task[], page: string): Thread[] {
  const children = new Map<string, Annotation[]>();
  for (const a of annotations) {
    if (!a.reply_to) continue;
    children.set(a.reply_to, [...(children.get(a.reply_to) ?? []), a]);
  }
  const roots = annotations.filter(
    (a) => !a.reply_to && a.on.page === page && ["question", "change", "note"].includes(a.kind) && a.status !== "resolved",
  );
  return roots.map((root) => {
    const replies: Annotation[] = [];
    const queue = [...(children.get(root.id) ?? [])];
    while (queue.length) {
      const next = queue.shift()!;
      replies.push(next);
      queue.push(...(children.get(next.id) ?? []));
    }
    replies.sort((a, b) => (a.created < b.created ? -1 : 1));
    const ids = [root.id, ...replies.map((r) => r.id)];
    const related = tasks.filter((t) => t.inputs.some((i) => ids.includes(i)));
    const task = related.sort((a, b) => (a.created < b.created ? 1 : -1))[0];
    return { root, replies, task };
  });
}

function whoLine(a: Annotation): string {
  const when = ago(a.created);
  const draft = a.status === "draft" ? " · not sent" : "";
  if (a.kind === "question") return `${a.reply_to ? "You asked again" : "You asked"} · ${when}${draft}`;
  if (a.kind === "change") return `You asked for a change · ${when}${draft}`;
  return `You left a note · ${when}`;
}

function NoteThread({ repo, thread, onReview }: { repo: string; thread: Thread; onReview: (task: string) => void }) {
  const { data, call, refreshNotes, taskAgent } = useStore();
  const annotate = useAnnotate(repo);
  const doer = agentName(thread.task?.agent ?? taskAgent);
  const [replying, setReplying] = useState(false);
  const { root, replies, task } = thread;
  const all = [root, ...replies];
  const need = task ? taskNeed(task, data[repo]?.notes?.annotations ?? []) : root.status === "answered" ? "needs" : null;
  const base = repoPath(data[repo], "");

  const resolve = async () => {
    if (!base) return;
    try {
      await call(repo, "PATCH", `${base}/annotations/${root.id}`, { status: "resolved" });
      await refreshNotes(repo);
    } catch (error) {
      toast.error(message(error));
    }
  };
  const resend = async () => {
    if (!base) return;
    const pending = all.filter((a) => a.author === "human" && ["draft", "failed"].includes(a.status));
    try {
      await call(repo, "POST", `${base}/tasks`, {
        annotations: (pending.length ? pending : [root]).map((a) => a.id),
        agent: taskAgent,
      });
      await refreshNotes(repo);
    } catch (error) {
      toast.error(message(error));
    }
  };

  return (
    <>
      {all.map((a) =>
        a.author === "human" ? (
          <div key={a.id}>
            <div className="note-who">
              {a === root && need && need !== "done" && <StatusMark status={need} />}
              {whoLine(a)}
            </div>
            <p className={a.kind === "note" ? "note-text" : "note-ask"}>{a.text}</p>
          </div>
        ) : (
          <div key={a.id}>
            <div className="note-who">{a.kind === "summary" ? `${doer} made the change` : `${doer} answered`}</div>
            <div className="note-text note-md">
              <Markdown text={a.text} />
            </div>
          </div>
        ),
      )}
      {task && (task.status === "queued" || task.status === "running") && (
        <div className="note-who note-working">
          <StatusMark status="working" />
          {task.progress || (task.status === "queued" ? `Waiting for ${doer}` : `${doer} is working`)}
        </div>
      )}
      {task?.status === "failed" && (
        <div className="note-failed">
          <div className="note-who">
            <StatusMark status="failed" />
            {doer} could not finish
          </div>
          {task.error && <p className="note-text">{task.error}</p>}
          <Button variant="outline" size="sm" onClick={() => void resend()}>
            <RotateCw />
            Try again
          </Button>
        </div>
      )}
      {root.status === "draft" && !task && (
        <div className="note-cta">
          <Button variant="outline" size="sm" onClick={() => void resend()}>
            Send to {agentName(taskAgent)}
          </Button>
        </div>
      )}
      {task?.status === "needs_review" && (
        <div className="note-cta">
          <Button variant="outline" size="sm" onClick={() => onReview(task.id)}>
            Review the change
          </Button>
        </div>
      )}
      {replying ? (
        <Composer
          kind="ask"
          onClose={() => setReplying(false)}
          onSend={(text) => annotate("ask", text, root.on, all[all.length - 1].id)}
        />
      ) : (
        <div className="note-actions">
          {root.kind !== "note" && (
            <Button variant="ghost" size="sm" onClick={() => setReplying(true)}>
              Reply
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={() => void resolve()}>
            Resolve
          </Button>
        </div>
      )}
    </>
  );
}

function threadNeeds(thread: Thread, annotations: Annotation[]): boolean {
  if (thread.task) return taskNeed(thread.task, annotations) === "needs";
  return thread.root.status === "answered";
}

type Layout = { tops: Record<string, number>; paths: { id: string; d: string }[] };

/** Where a thread belongs: the block that holds its quote, else its section's
 *  heading, else the top of the page. */
function anchorBlock(root: HTMLElement, on: Anchor): HTMLElement | null {
  const blocks = [...root.querySelectorAll<HTMLElement>(".article [data-block]")];
  const quote = squash(on.quote ?? on.block ?? "").slice(0, 60).toLowerCase();
  if (quote) {
    const hit = blocks.find((b) => squash(b.textContent ?? "").toLowerCase().includes(quote));
    if (hit) return hit;
  }
  const section = squash(on.section ?? "").toLowerCase();
  if (section) {
    const hit = blocks.find((b) => b.querySelector("h2, h3, h4") && squash(b.textContent ?? "").toLowerCase() === section);
    if (hit) return hit;
  }
  return blocks[0] ?? null;
}

export function WikiView({ repo, pageId, onReview }: { repo: string; pageId: string; onReview: (task: string) => void }) {
  const { data, call, go, palette, setPalette, taskAgent } = useStore();
  const who = agentName(taskAgent);
  const d = data[repo];
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState("");
  const [composing, setComposing] = useState<{ block: number; kind: Kind } | null>(null);
  const annotate = useAnnotate(repo);
  const pageRef = useRef<HTMLDivElement>(null);
  const noteRefs = useRef<Record<string, HTMLElement | null>>({});
  const [layout, setLayout] = useState<Layout | null>(null);

  useEffect(() => {
    const path = repoPath(d, `/wiki/${encodeURIComponent(pageId)}`);
    if (!path) return;
    let live = true;
    setPage(null);
    setError("");
    call<Page>(repo, "GET", path)
      .then((p) => live && setPage(p))
      .catch((e) => live && setError(message(e)));
    return () => {
      live = false;
    };
    // The repository's id arrives once; refetch on page changes only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, pageId, d?.info?.id, call]);

  // ⌘K "Ask about this page" opens a composer on the title.
  useEffect(() => {
    if (palette.ask !== undefined && !palette.open) setComposing({ block: 0, kind: "ask" });
  }, [palette]);

  const blocks = useMemo(() => {
    // A row of badge images says nothing without its pictures.
    const badges = /^(\s*\[?!\[[^\]]*\]\([^)]*\)\]?(\([^)]*\))?\s*)+$/;
    const all = splitBlocks(page?.markdown ?? "").filter((b) => !badges.test(b.text));
    return all[0]?.heading === 1 ? all.slice(1) : all;
  }, [page]);
  const threads = useMemo(
    () => threadsFor(d?.notes?.annotations ?? [], d?.notes?.tasks ?? [], pageId),
    [d?.notes, pageId],
  );

  const place = useCallback(() => {
    const root = pageRef.current;
    if (!root) return;
    const box = root.getBoundingClientRect();
    const column = root.querySelector<HTMLElement>(".article");
    const notesEl = root.querySelector<HTMLElement>(".notes");
    if (!column || !notesEl) return;
    const colRight = column.getBoundingClientRect().right - box.left;
    const notesTop = notesEl.getBoundingClientRect().top - box.top;
    const placed = threads
      .map((t) => ({ t, anchor: anchorBlock(root, t.root.on) }))
      .filter((x) => x.anchor)
      .sort((a, b) => a.anchor!.getBoundingClientRect().top - b.anchor!.getBoundingClientRect().top);
    const tops: Record<string, number> = {};
    const paths: Layout["paths"] = [];
    let floor = 0;
    for (const { t, anchor } of placed) {
      const note = noteRefs.current[t.root.id];
      if (!note || !anchor) continue;
      const rect = anchor.getBoundingClientRect();
      const anchorMid = rect.top - box.top + Math.min(rect.height / 2, 14);
      const top = Math.max(anchorMid - 10, floor);
      tops[t.root.id] = top - notesTop;
      floor = top + note.offsetHeight + 24;
      const noteLeft = note.getBoundingClientRect().left - box.left;
      const x0 = colRight + 14;
      const xm = colRight + 30;
      const y1 = top + 10;
      const bend = Math.min(6, Math.abs(y1 - anchorMid) / 2);
      const dir = y1 >= anchorMid ? 1 : -1;
      paths.push({
        id: t.root.id,
        d:
          Math.abs(y1 - anchorMid) < 1
            ? `M ${x0} ${anchorMid} H ${noteLeft - 8}`
            : `M ${x0} ${anchorMid} H ${xm - bend} Q ${xm} ${anchorMid} ${xm} ${anchorMid + bend * dir} V ${y1 - bend * dir} Q ${xm} ${y1} ${xm + bend} ${y1} H ${noteLeft - 8}`,
      });
    }
    setLayout({ tops, paths });
  }, [threads]);

  useLayoutEffect(() => {
    place();
    const root = pageRef.current;
    if (!root) return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(place);
    });
    observer.observe(root);
    void document.fonts?.ready.then(() => place());
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [place, blocks]);

  if (error) {
    return (
      <div className="empty-state">
        <h2>This page did not open</h2>
        <p>{error}</p>
      </div>
    );
  }
  if (!page) {
    return (
      <div className="empty-state">
        <p className="empty-wait">{d?.notes ? "Opening the page…" : "Opening the repository…"}</p>
      </div>
    );
  }

  const isDoc = pageId.startsWith("doc--");
  const generated = page.generation?.mode && page.generation.mode !== "offline";
  const meta = isDoc
    ? `Docs · from the repository`
    : `Analysis · ${d?.info?.repo ?? ""}${d?.info?.commit_short ? ` at ${d.info.commit_short}` : ""} · ${
        generated
          ? `written by ${page.generation?.model?.startsWith("codex") ? "Codex" : "Claude Code"}`
          : "built from the index on this Mac"
      }`;
  let section = "";
  const onPage = (next: string) => go({ kind: "page", repo, page: next });

  const composerFor = (index: number, text: string, sectionTitle: string) =>
    composing?.block === index ? (
      <Composer
        kind={composing.kind}
        initial={index === 0 ? (palette.ask ?? "") : ""}
        onClose={() => {
          setComposing(null);
          if (index === 0 && palette.ask !== undefined) setPalette({ open: false });
        }}
        onSend={async (body) => {
          await annotate(composing.kind, body, {
            page: pageId,
            section: sectionTitle,
            quote: plain(text).slice(0, 160),
            block: plain(text).slice(0, 600),
          });
          toast(composing.kind === "note" ? "Note saved in .codenib/notes/" : `Sent to ${who} on this Mac`);
        }}
      />
    ) : null;

  return (
    <div className="wiki-page" ref={pageRef}>
      <article className="article">
        <Block
          index={0}
          label="the page"
          className="block-title"
          onCompose={(kind) => setComposing(kind ? { block: 0, kind } : null)}
          composing={composerFor(0, page.title, "")}
        >
          <h1>{page.title}</h1>
          <p className="page-meta">{meta}</p>
        </Block>
        {blocks.map((block, i) => {
          if (block.heading) section = block.title;
          const index = i + 1;
          const sectionTitle = section;
          return (
            <Block
              key={index}
              index={index}
              label={block.title || `block ${index}`}
              className={cn(block.text.startsWith("```") && "block-code")}
              onCompose={(kind) => setComposing(kind ? { block: index, kind } : null)}
              composing={composerFor(index, block.text.replace(/^#+\s*/, ""), sectionTitle)}
            >
              <Markdown text={block.text} onPage={onPage} />
            </Block>
          );
        })}
        {(page.citations ?? []).length > 0 && (
          <section className="sources" aria-label="Sources">
            <h2>Sources</h2>
            <ul>
              {(page.citations ?? []).slice(0, 24).map((c, i) => (
                <li key={`${c.file}:${c.start_line}:${i}`}>
                  <span className="sources-name">{c.node_name || c.file.split("/").pop()}</span>
                  <SourceMark repo={repo} file={c.file} start={c.start_line} end={c.end_line} label={`${c.file}:${c.start_line}`} />
                </li>
              ))}
            </ul>
          </section>
        )}
      </article>

      <svg className="leaders" aria-hidden="true">
        {layout?.paths.map((p) => (
          <path key={p.id} d={p.d} />
        ))}
      </svg>
      <div className="notes" aria-label="Notes on this page">
        {threads.length === 0 && (
          <p className="notes-empty">
            Point at a paragraph and use its handle to ask {who}, request a change or leave a note.
          </p>
        )}
        {threads.map((thread) => (
          <aside
            key={thread.root.id}
            ref={(el) => {
              noteRefs.current[thread.root.id] = el;
            }}
            className={cn("note", threadNeeds(thread, d?.notes?.annotations ?? []) && "is-needs")}
            style={{ top: layout?.tops[thread.root.id] ?? 0, visibility: layout ? "visible" : "hidden" }}
          >
            <NoteThread repo={repo} thread={thread} onReview={onReview} />
          </aside>
        ))}
      </div>
    </div>
  );
}
