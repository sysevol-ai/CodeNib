import { GripVertical, MessageCircleQuestion, PencilLine, RotateCw, StickyNote, Sparkles } from "lucide-react";
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
import { repoPath, stillWriting, useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { Markdown, splitBlocks, type MdBlock } from "./markdown";
import { SourceMark } from "./SourceMark";
import { StatusMark } from "./StatusMark";

export type Kind = "ask" | "change" | "note" | "revise";

const KIND: Record<Kind, { api: "question" | "change" | "note" | "revise"; placeholder: string; send: string }> = {
  revise: { api: "revise", placeholder: "What should this passage say better?", send: "Improve" },
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
    async (kind: Kind, text: string, on: Anchor, replyTo?: string, agent: WriterId = taskAgent) => {
      const d = data[repo];
      const base = repoPath(d, "");
      if (!base) throw new Error("This repository is not open yet");
      const problem = kind === "note" ? null : agentProblem(agents, agent);
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
        await call<Task>(repo, "POST", `${base}/tasks`, { annotations: [created.id], agent });
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
  onSend: (text: string, agent: WriterId) => Promise<unknown>;
  onClose: () => void;
  initial?: string;
}) {
  const { taskAgent, setTaskAgent, agents } = useStore();
  // Starts on the last agent used; a pick here becomes the next default.
  const [agent, setAgent] = useState<WriterId>(taskAgent);
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
      await onSend(text.trim(), agent);
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
          {kind === "note" ? "Saved in the repository's .codenib/notes/." : "Runs on this Mac with"}{" "}
          {kind !== "note" && (
            <span className="agent-pick" role="radiogroup" aria-label="Send to">
              {(["claude-code", "codex"] as WriterId[]).map((id) => {
                const problem = agentProblem(agents, id);
                return (
                  <button
                    key={id}
                    type="button"
                    role="radio"
                    aria-checked={agent === id}
                    className={cn("agent-pick-option", agent === id && "is-on", problem && "is-off")}
                    title={problem ?? `Send to ${agentName(id)}`}
                    onClick={() => {
                      setAgent(id);
                      setTaskAgent(id);
                    }}
                  >
                    {agentName(id)}
                  </button>
                );
              })}
            </span>
          )}
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
        agent: thread.task?.agent === "codex" ? "codex" : taskAgent,
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
          onSend={(text, agent) => annotate("ask", text, root.on, all[all.length - 1].id, agent)}
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

/** The words a thread is about, as a DOM range inside its block, when the
 *  quote is still on the page. */
function quoteRange(root: HTMLElement, on: Anchor): Range | null {
  const quote = (on.quote ?? "").replace(/\s+/g, " ").trim();
  if (quote.length < 2) return null;
  const block = anchorBlock(root, on);
  if (!block) return null;
  // Walk the block's text, mapping whitespace-squashed offsets back to nodes.
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      node.parentElement?.closest(".block-handle, .cite, .src-mark, .composer")
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT,
  });
  const map: { node: Text; offset: number }[] = [];
  let flat = "";
  let lastSpace = true;
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    const value = node.data;
    for (let i = 0; i < value.length; i++) {
      const space = /\s/.test(value[i]);
      if (space && lastSpace) continue;
      flat += space ? " " : value[i];
      map.push({ node, offset: i });
      lastSpace = space;
    }
  }
  const at = flat.toLowerCase().indexOf(quote.toLowerCase());
  if (at < 0) return null;
  const first = map[at];
  const last = map[at + quote.length - 1];
  if (!first || !last) return null;
  const range = document.createRange();
  range.setStart(first.node, first.offset);
  range.setEnd(last.node, last.offset + 1);
  return range;
}

/** Marks every thread's words in the text with the CSS Highlight API, which
 *  colours ranges without changing the page's DOM (React owns it). */
function QuoteHighlights({
  root,
  threads,
  focus,
}: {
  root: React.RefObject<HTMLDivElement | null>;
  threads: Thread[];
  focus: string | null;
}) {
  useLayoutEffect(() => {
    const registry = (CSS as unknown as { highlights?: Map<string, unknown> }).highlights;
    const HighlightCtor = (window as unknown as { Highlight?: new (...ranges: Range[]) => unknown }).Highlight;
    if (!registry || !HighlightCtor || !root.current) return;
    const draw = () => {
      if (!root.current) return;
      const all: Range[] = [];
      const focused: Range[] = [];
      for (const thread of threads) {
        const range = quoteRange(root.current, thread.root.on);
        if (!range) continue;
        (thread.root.id === focus ? focused : all).push(range);
      }
      registry.set("note-anchor", new HighlightCtor(...all));
      registry.set("note-anchor-focus", new HighlightCtor(...focused));
    };
    draw();
    const timer = window.setTimeout(draw, 400);
    return () => {
      window.clearTimeout(timer);
      registry.delete("note-anchor");
      registry.delete("note-anchor-focus");
    };
  }, [root, threads, focus]);
  return null;
}

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

/** Keep a diagram only when it came from the validated fact plan, as the
 *  Wiki's site does: its edges were checked against the page's evidence. A
 *  mechanical fan-out of every module, or free-form model Mermaid, is noise. */
export function keepValidatedDiagrams(markdown: string, validated: boolean): string {
  if (validated) return markdown;
  return markdown
    .replace(/\n#{1,6}[^\n]*\n+```mermaid[\s\S]*?```(?:\n+(?![#\n])[^\n]*)?/g, "")
    .replace(/```mermaid[\s\S]*?```(?:\n+(?![#\n])[^\n]*)?/g, "")
    .trimEnd();
}

/** Ask about the page or leave a note on it; always at the top of the margin. */
function RailActions({ who, onCompose }: { who: string; onCompose: (kind: Kind) => void }) {
  return (
    <section className="rail-group rail-actions" aria-label="Notes and questions">
      <Button size="sm" onClick={() => onCompose("ask")}>
        <MessageCircleQuestion />
        Ask {who} about this page
      </Button>
      <Button size="sm" variant="outline" onClick={() => onCompose("note")}>
        <StickyNote />
        Add a note
      </Button>
    </section>
  );
}

/** The margin when a page has no notes yet: where you are on the page, the
 *  code it rests on, and how to start a note. */
function PageRail({ repo, page, blocks }: { repo: string; page: Page; blocks: MdBlock[] }) {
  const sections = blocks
    .map((block, i) => ({ title: block.title, heading: block.heading, index: i + 1 }))
    .filter((b) => b.heading === 2 && b.title);
  const [here, setHere] = useState<number | null>(null);
  useEffect(() => {
    const targets = sections
      .map((s) => document.querySelector<HTMLElement>(`[data-block="${s.index}"]`))
      .filter((el): el is HTMLElement => !!el);
    if (!targets.length) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const seen = entries.filter((e) => e.isIntersecting).map((e) => Number((e.target as HTMLElement).dataset.block));
        if (seen.length) setHere(Math.min(...seen));
      },
      { rootMargin: "0px 0px -70% 0px" },
    );
    targets.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
    // Sections change only with the page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page.id, sections.length]);

  // The files the page cites, most cited first, each opening its code.
  const counts = new Map<string, number>();
  for (const m of page.markdown.matchAll(/\(#evidence-(E\d+)\)/g)) counts.set(m[1], (counts.get(m[1]) ?? 0) + 1);
  const files = new Map<string, { file: string; start: number; end: number; cites: number }>();
  for (const item of page.evidence?.items ?? []) {
    if (!item.file || !item.start_line) continue;
    const known = files.get(item.file);
    const cites = (known?.cites ?? 0) + (counts.get(item.id) ?? 0);
    files.set(item.file, {
      file: item.file,
      start: Math.min(known?.start ?? item.start_line, item.start_line),
      end: Math.max(known?.end ?? item.end_line, item.end_line),
      cites,
    });
  }
  const sources = [...files.values()].sort((a, b) => b.cites - a.cites).slice(0, 8);

  return (
    <div className="page-rail">
      {sections.length > 1 && (
        <nav className="rail-group" aria-label="On this page">
          <h3>On this page</h3>
          <ol>
            {sections.map((s) => (
              <li key={s.index}>
                <button
                  type="button"
                  className={cn("rail-link", here === s.index && "is-here")}
                  onClick={() =>
                    document.querySelector(`[data-block="${s.index}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" })
                  }
                >
                  {s.title}
                </button>
              </li>
            ))}
          </ol>
        </nav>
      )}
      {sources.length > 0 && (
        <section className="rail-group" aria-label="Code this page cites">
          <h3>Code it cites</h3>
          <ul>
            {sources.map((s) => (
              <li key={s.file}>
                <SourceMark
                  repo={repo}
                  file={s.file}
                  start={s.start}
                  end={s.end}
                  label={s.file.split("/").slice(-2).join("/")}
                  className="rail-source"
                  title={`${s.file} · lines ${s.start}–${s.end}`}
                />
                {s.cites > 0 && <span className="rail-count">{s.cites}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}
      <p className="notes-empty">
        To ask about one paragraph, or request a change to it, point at it and use the <GripVertical className="inline-grip" />{" "}
        handle at its left. Notes stay beside the words they are about.
      </p>
    </div>
  );
}

/** A page whose writer failed: say so, show what the index knows, and offer
 *  to write it again now rather than after the writer's cooldown. */
function RewritePage({ writer, reason, onRewrite }: { writer: string; reason: string; onRewrite: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!busy) return;
    setSeconds(0);
    const timer = window.setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);
  const notice =
    reason === "model_unavailable" ? (
      <>
        <strong>This page was not written.</strong> {writer} did not answer while it was being written, so it shows only
        what the index records about these files.
      </>
    ) : (
      <>
        <strong>Read this page with care.</strong> Parts of {writer}'s draft did not pass the checks against the
        source, so not every claim here is confirmed by the code it cites.
      </>
    );
  return (
    <div className="page-degraded" role="status">
      <p>{notice}</p>
      {busy ? (
        <p className="page-wait-meta">
          <StatusMark status="working" /> {writer} is writing this page · {Math.floor(seconds / 60)}:
          {String(seconds % 60).padStart(2, "0")}
        </p>
      ) : (
        <div>
          <Button
            size="sm"
            onClick={() => {
              setBusy(true);
              setError("");
              onRewrite()
                .catch((e) => setError(message(e)))
                .finally(() => setBusy(false));
            }}
          >
            <RotateCw />
            Write this page again
          </Button>
        </div>
      )}
      {error && <p className="add-warn">{error}</p>}
    </div>
  );
}

/** Waiting for a page; says when an agent is writing it, and for how long. */
function PageWait({ repo, pageId }: { repo: string; pageId: string }) {
  const { data, repos } = useStore();
  const d = data[repo];
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    setSeconds(0);
    const timer = window.setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => window.clearInterval(timer);
  }, [pageId]);
  const writer = repos.find((r) => r.path === repo)?.writer;
  const writing = stillWriting(d?.progress) && !d!.progress!.written.includes(pageId);
  const clock = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  if (!writing || !writer) {
    return (
      <div className="empty-state">
        <p className="empty-wait">{d?.notes ? "Opening the page…" : "Opening the repository…"}</p>
      </div>
    );
  }
  const { written, pages } = d!.progress!;
  return (
    <div className="empty-state page-wait" aria-live="polite">
      <h2>
        <StatusMark status="working" /> {agentName(writer)} is writing this page
      </h2>
      <p>
        It reads the code behind the page and checks every claim against it, which usually takes one to three
        minutes. Pages already written open at once.
      </p>
      <p className="page-wait-meta">
        Waiting {clock} · {written.length} of {pages.length} pages written
      </p>
    </div>
  );
}

export function WikiView({ repo, pageId, onReview }: { repo: string; pageId: string; onReview: (task: string) => void }) {
  const { data, call, go, palette, setPalette, taskAgent, refreshNotes } = useStore();
  const who = agentName(taskAgent);
  const d = data[repo];
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState("");
  const [composing, setComposing] = useState<{ block: number; kind: Kind; quote?: string } | null>(null);
  const [picked, setPicked] = useState<{ block: number; quote: string; x: number; y: number } | null>(null);
  const [focusThread, setFocusThread] = useState<string | null>(null);
  const annotate = useAnnotate(repo);
  const pageRef = useRef<HTMLDivElement>(null);
  const noteRefs = useRef<Record<string, HTMLElement | null>>({});
  const [layout, setLayout] = useState<Layout | null>(null);
  // Stable, so the page's Markdown keeps its links (and open citations).
  const onPage = useCallback((next: string) => go({ kind: "page", repo, page: next }), [go, repo]);

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
    const markdown = keepValidatedDiagrams(page?.markdown ?? "", page?.generation?.renderer === "fact_plan");
    const all = splitBlocks(markdown).filter((b) => !badges.test(b.text));
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
    // Notes start below the Ask and Note buttons at the top of the margin, so
    // a note anchored near the title never covers them.
    const actions = notesEl.querySelector<HTMLElement>(".rail-actions");
    let floor = actions ? actions.getBoundingClientRect().bottom - box.top + 16 : 0;
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
    return <PageWait repo={repo} pageId={pageId} />;
  }

  const isDoc = pageId.startsWith("doc--");
  const degraded = page.generation?.mode === "degraded";
  const generated = page.generation?.mode && page.generation.mode !== "offline" && !degraded;
  const writerName = page.generation?.model?.startsWith("codex") ? "Codex" : "Claude Code";
  const meta = isDoc
    ? `Docs · from the repository`
    : `Analysis · ${d?.info?.repo ?? ""}${d?.info?.commit_short ? ` at ${d.info.commit_short}` : ""} · ${
        degraded
          ? page.generation?.reason === "model_unavailable"
            ? `from the index; ${writerName} did not write it`
            : `written by ${writerName} · not every claim confirmed`
          : generated
            ? `written by ${writerName}`
            : "built from the index on this Mac"
      }`;
  let section = "";

  const composerFor = (index: number, text: string, sectionTitle: string) =>
    composing?.block === index ? (
      <Composer
        kind={composing.kind}
        initial={index === 0 ? (palette.ask ?? "") : ""}
        onClose={() => {
          setComposing(null);
          if (index === 0 && palette.ask !== undefined) setPalette({ open: false });
        }}
        onSend={async (body, agent) => {
          const who = agentName(agent);
          if (composing.kind === "revise") {
            const path = repoPath(d, "/revisions");
            if (!path) return;
            await call(repo, "POST", path, {
              page: pageId,
              quote: composing.quote ?? plain(text).slice(0, 300),
              comment: body,
              agent,
            });
            await refreshNotes(repo);
            toast(`${who} is improving this passage. Its proposal will be in your Inbox.`);
            return;
          }
          await annotate(composing.kind, body, {
            page: pageId,
            section: sectionTitle,
            quote: composing.quote ?? plain(text).slice(0, 160),
            block: plain(text).slice(0, 600),
          }, undefined, agent);
          toast(composing.kind === "note" ? "Note saved in .codenib/notes/" : `Sent to ${who} on this Mac`);
        }}
      />
    ) : null;

  // Select words in the article to comment on them, as in a shared document.
  const onPointerUp = () => {
    window.setTimeout(() => {
      const selection = window.getSelection();
      const quote = selection?.toString().replace(/\s+/g, " ").trim() ?? "";
      if (!selection || selection.isCollapsed || quote.length < 2 || !selection.rangeCount) {
        setPicked(null);
        return;
      }
      const range = selection.getRangeAt(0);
      const owner = (range.commonAncestorContainer instanceof Element
        ? range.commonAncestorContainer
        : range.commonAncestorContainer.parentElement
      )?.closest<HTMLElement>("[data-block]");
      if (!owner || !pageRef.current?.contains(owner) || owner.closest(".composer")) {
        setPicked(null);
        return;
      }
      const rect = range.getBoundingClientRect();
      setPicked({ block: Number(owner.dataset.block), quote: quote.slice(0, 300), x: rect.left + rect.width / 2, y: rect.top });
    }, 0);
  };

  const startFromSelection = (kind: Kind) => {
    if (!picked) return;
    setComposing({ block: picked.block, kind, quote: picked.quote });
    setPicked(null);
    window.getSelection()?.removeAllRanges();
  };

  // Annotated words are marked in place; a click on them opens their thread.
  const onArticleClick = (event: React.MouseEvent) => {
    const root = pageRef.current;
    if (!root || window.getSelection()?.toString()) return;
    for (const thread of threads) {
      const range = quoteRange(root, thread.root.on);
      if (!range) continue;
      for (const r of Array.from(range.getClientRects())) {
        if (event.clientX >= r.left && event.clientX <= r.right && event.clientY >= r.top && event.clientY <= r.bottom) {
          setFocusThread(thread.root.id);
          noteRefs.current[thread.root.id]?.scrollIntoView({ behavior: "smooth", block: "nearest" });
          return;
        }
      }
    }
  };

  return (
    <div className="wiki-page" ref={pageRef}>
      {picked && (
        <div className="select-bar" style={{ left: picked.x, top: picked.y }} role="toolbar" aria-label="Selected words">
          <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => startFromSelection("note")}>
            <StickyNote />
            Comment
          </button>
          <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => startFromSelection("ask")}>
            <MessageCircleQuestion />
            Ask {who}
          </button>
          <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => startFromSelection("revise")}>
            <Sparkles />
            Improve this passage
          </button>
          <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => startFromSelection("change")}>
            <PencilLine />
            Change the code
          </button>
        </div>
      )}
      <QuoteHighlights root={pageRef} threads={threads} focus={focusThread} />
      <article className="article" onPointerUp={onPointerUp} onClick={onArticleClick}>
        <Block
          index={0}
          label="the page"
          className="block-title"
          onCompose={(kind) => setComposing(kind ? { block: 0, kind } : null)}
          composing={composerFor(0, page.title, "")}
        >
          <h1>{page.title}</h1>
          <p className="page-meta">{meta}</p>
          {degraded && (
            <RewritePage
              writer={writerName}
              reason={page.generation?.reason ?? ""}
              onRewrite={async () => {
                const path = repoPath(d, `/wiki/${encodeURIComponent(pageId)}?retry=1`);
                if (!path) return;
                setPage(await call<Page>(repo, "GET", path));
              }}
            />
          )}
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
              <Markdown text={block.text} onPage={onPage} repo={repo} evidence={page.evidence} />
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
        <RailActions who={who} onCompose={(kind) => setComposing({ block: 0, kind })} />
        {threads.length === 0 && (
          <PageRail repo={repo} page={page} blocks={blocks} />
        )}
        {threads.map((thread) => (
          <aside
            key={thread.root.id}
            ref={(el) => {
              noteRefs.current[thread.root.id] = el;
            }}
            className={cn(
              "note",
              threadNeeds(thread, d?.notes?.annotations ?? []) && "is-needs",
              focusThread === thread.root.id && "is-focus",
            )}
            onClick={() => setFocusThread(thread.root.id)}
            style={{ top: layout?.tops[thread.root.id] ?? 0, visibility: layout ? "visible" : "hidden" }}
          >
            <NoteThread repo={repo} thread={thread} onReview={onReview} />
          </aside>
        ))}
      </div>
    </div>
  );
}
