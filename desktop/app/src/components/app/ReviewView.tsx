import { Check, Copy, MessageCircleQuestion, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuShortcut,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { Kbd } from "@/components/ui/kbd";
import { message } from "@/lib/backend";
import { agentName, ago, taskNeed, taskTitle } from "@/lib/model";
import { repoPath, useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { CodeLine } from "./CodeLines";
import { Markdown } from "./markdown";
import { StatusMark } from "./StatusMark";

interface Hunk {
  id: string;
  path: string;
  header: string;
  added: number;
  deleted: number;
  state: "open" | "accepted" | "rejected";
  lines: string[];
}

/** Line numbers on the new side, from the hunk header's +start. */
function numbered(header: string, lines: string[]) {
  let n = Number(/\+(\d+)/.exec(header)?.[1] ?? "0");
  return lines.map((line) => {
    const sign = line[0] === "+" || line[0] === "-" ? line[0] : " ";
    const shown = sign === "-" ? "" : String(n);
    if (sign !== "-") n += 1;
    return { sign, text: line.slice(1), n: shown };
  });
}

/** Review a change hunk by hunk: a to apply, x to discard, right-click for more. */
export function ReviewView({ repo, taskId }: { repo: string; taskId: string }) {
  const { data, call, refreshNotes, go, repos } = useStore();
  const d = data[repo];
  const [hunks, setHunks] = useState<Hunk[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const annotations = d?.notes?.annotations ?? [];
  const task = d?.notes?.tasks.find((t) => t.id === taskId);
  const asked = annotations.find((a) => task?.inputs.includes(a.id));
  const summary = annotations.find((a) => a.kind === "summary" && a.task === taskId);
  const base = repoPath(d, `/tasks/${taskId}`);

  const load = useCallback(async () => {
    if (!base) return;
    try {
      const review = await call<{ hunks: Hunk[] }>(repo, "GET", `${base}/diff`);
      setHunks(review.hunks);
    } catch (e) {
      setError(message(e));
    }
  }, [base, call, repo]);

  useEffect(() => {
    void load();
  }, [load]);

  const decide = useCallback(
    async (decision: "accept" | "reject", ids: string[]) => {
      if (!base || busy) return;
      setBusy(true);
      try {
        await call(repo, "POST", `${base}/${decision}`, { hunks: ids });
        const where = repos.find((r) => r.path === repo)?.name ?? "the repository";
        toast(decision === "accept" ? `Applied to ${where}` : "Discarded", {
          description: ids.length === 1 ? hunks?.find((h) => h.id === ids[0])?.path : `${ids.length} hunks`,
        });
        await Promise.all([load(), refreshNotes(repo)]);
      } catch (e) {
        toast.error(message(e));
      } finally {
        setBusy(false);
      }
    },
    [base, busy, call, repo, repos, hunks, load, refreshNotes],
  );

  const next = hunks?.find((h) => h.state === "open");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest("input, textarea, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
      if (!next) return;
      if (e.key === "a") void decide("accept", [next.id]);
      if (e.key === "x") void decide("reject", [next.id]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [decide, next]);

  if (error) {
    return (
      <div className="empty-state">
        <h2>This change did not open</h2>
        <p>{error}</p>
      </div>
    );
  }

  const open = hunks?.filter((h) => h.state === "open") ?? [];
  const files = new Set((hunks ?? []).map((h) => h.path));
  return (
    <section className="review review-page" aria-label="Review the change">
      <div className="review-head">
        <div className="note-who">
          {task && <StatusMark status={taskNeed(task, annotations)} />}
          Change · you asked {asked ? (ago(asked.created) === "now" ? "just now" : `${ago(asked.created)} ago`) : ""}
        </div>
        <h2 className="review-title">{task ? taskTitle(task, annotations) : "A change"}</h2>
        <p className="review-sub">
          {agentName(task?.agent)} changed {files.size} {files.size === 1 ? "file" : "files"} in a separate worktree. Your working
          tree stays as it is until you apply.
          {asked?.on.page && (
            <>
              {" "}
              <button
                type="button"
                className="sb-link"
                onClick={() => go({ kind: "page", repo, page: asked.on.page ?? "" })}
              >
                Open the page it was asked on
              </button>
            </>
          )}
        </p>
        {summary && (
          <div className="note-text note-md review-summary">
            <Markdown text={summary.text} />
          </div>
        )}
        {open.length > 1 && (
          <div className="review-all">
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => void decide("reject", open.map((h) => h.id))}>
              Discard the rest
            </Button>
            <Button size="sm" disabled={busy} onClick={() => void decide("accept", open.map((h) => h.id))}>
              Apply the rest ({open.length})
            </Button>
          </div>
        )}
      </div>

      {hunks === null && <p className="empty-wait">Reading the change…</p>}
      {hunks?.length === 0 && <p className="empty-wait">This task changed no code.</p>}
      {hunks?.map((hunk, index) => (
        <ContextMenu key={hunk.id}>
          <ContextMenuTrigger asChild>
            <div
              tabIndex={0}
              className={cn("hunk", hunk.state === "accepted" && "is-applied", hunk.state === "rejected" && "is-discarded")}
              aria-label={`Hunk ${index + 1} of ${hunks.length} in ${hunk.path}`}
            >
              <div className="hunk-head">
                <span className="mono hunk-file">{hunk.path}</span>
                <span className="hunk-acts">
                  {hunk.state === "open" ? (
                    <>
                      <Button variant="ghost" size="sm" disabled={busy} onClick={() => void decide("reject", [hunk.id])}>
                        <X />
                        Discard {hunk === next && <Kbd>x</Kbd>}
                      </Button>
                      <Button size="sm" disabled={busy} onClick={() => void decide("accept", [hunk.id])}>
                        <Check />
                        Apply {hunk === next && <Kbd className="kbd-on-primary">a</Kbd>}
                      </Button>
                    </>
                  ) : (
                    <span className="hunk-done">{hunk.state === "accepted" ? "Applied" : "Discarded"}</span>
                  )}
                </span>
              </div>
              <pre className="diff">
                <code>
                  <span className="diff-row is-meta">
                    <span className="diff-n" aria-hidden="true" />
                    <span className="diff-sign" aria-hidden="true" />
                    <span className="diff-t">{hunk.header}</span>
                  </span>
                  {numbered(hunk.header, hunk.lines).map((l, i) => (
                    <span key={i} className={cn("diff-row", l.sign === "+" && "is-add", l.sign === "-" && "is-del")}>
                      <span className="diff-n" aria-hidden="true">
                        {l.n}
                      </span>
                      <span className="diff-sign" aria-hidden="true">
                        {l.sign === " " ? "" : l.sign}
                      </span>
                      <span className="diff-t">
                        <CodeLine text={l.text} />
                      </span>
                    </span>
                  ))}
                </code>
              </pre>
            </div>
          </ContextMenuTrigger>
          <ContextMenuContent className="w-64">
            <ContextMenuItem onSelect={() => void decide("accept", [hunk.id])} disabled={hunk.state !== "open"}>
              <Check />
              Apply this hunk
              <ContextMenuShortcut>
                <Kbd>a</Kbd>
              </ContextMenuShortcut>
            </ContextMenuItem>
            <ContextMenuItem onSelect={() => void decide("reject", [hunk.id])} disabled={hunk.state !== "open"}>
              <X />
              Discard this hunk
              <ContextMenuShortcut>
                <Kbd>x</Kbd>
              </ContextMenuShortcut>
            </ContextMenuItem>
            <ContextMenuSeparator />
            <ContextMenuItem
              onSelect={() =>
                void navigator.clipboard
                  .writeText([hunk.header, ...hunk.lines].join("\n"))
                  .then(() => toast("Copied the hunk"))
              }
            >
              <Copy />
              Copy this hunk
            </ContextMenuItem>
            {asked?.on.page && (
              <ContextMenuItem onSelect={() => go({ kind: "page", repo, page: asked.on.page ?? "" })}>
                <MessageCircleQuestion />
                Ask about it on its page
              </ContextMenuItem>
            )}
          </ContextMenuContent>
        </ContextMenu>
      ))}
    </section>
  );
}
