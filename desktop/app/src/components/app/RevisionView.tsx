import { Check, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { message } from "@/lib/backend";
import { agentName, ago } from "@/lib/model";
import { repoPath, useStore } from "@/lib/store";
import { Markdown } from "./markdown";
import { StatusMark } from "./StatusMark";

/** A better paragraph the agent proposed from a comment: the paragraph as it
 *  is and as proposed, side by side, to accept into the page or discard. */
export function RevisionView({ repo, id }: { repo: string; id: string }) {
  const { data, call, refreshNotes, reload, go } = useStore();
  const d = data[repo];
  const revision = d?.revisions?.find((r) => r.id === id);
  const [busy, setBusy] = useState(false);
  if (!revision) return <p className="view-sub">This proposal is no longer here.</p>;
  const who = agentName(revision.agent);
  const decide = async (decision: "accept" | "reject") => {
    const path = repoPath(d, `/revisions/${revision.id}/${decision}`);
    if (!path) return;
    setBusy(true);
    try {
      await call(repo, "POST", path);
      await refreshNotes(repo);
      if (decision === "accept") {
        await reload(repo);
        toast("The page now has the new paragraph.");
        go({ kind: "page", repo, page: revision.page });
      } else {
        toast("Proposal discarded. The page is unchanged.");
      }
    } catch (error) {
      toast.error(message(error));
    } finally {
      setBusy(false);
    }
  };
  const need =
    revision.status === "needs_review"
      ? "needs"
      : revision.status === "working"
        ? "working"
        : revision.status === "failed"
          ? "failed"
          : "done";
  return (
    <div className="view revision">
      <div className="note-who">
        <StatusMark status={need} />
        Wiki · {revision.page_title} · you commented {ago(new Date(revision.created * 1000).toISOString())} ago
      </div>
      <h1 className="view-title">
        Improve this passage. <span className="two-tone">“{revision.quote.slice(0, 80)}”</span>
      </h1>
      <blockquote className="revision-comment">{revision.comment}</blockquote>
      {revision.status === "working" && (
        <p className="view-sub">
          <StatusMark status="working" /> {who} is reading the passage and the code it cites…
        </p>
      )}
      {revision.status === "failed" && <p className="add-warn">{revision.error || `${who} could not finish.`}</p>}
      {revision.replacement && (
        <>
          {revision.summary && <p className="view-sub">{revision.summary}</p>}
          <div className="revision-pair">
            <section>
              <h2>Now</h2>
              <div className="article revision-old">
                <Markdown text={revision.original} />
              </div>
            </section>
            <section>
              <h2>Proposed by {who}</h2>
              <div className="article revision-new">
                <Markdown text={revision.replacement} />
              </div>
            </section>
          </div>
        </>
      )}
      {revision.status === "needs_review" && (
        <div className="walk-nav">
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void decide("reject")}>
            <X />
            Discard
          </Button>
          <Button size="sm" disabled={busy} onClick={() => void decide("accept")}>
            <Check />
            Use the new paragraph
          </Button>
        </div>
      )}
      {revision.status === "applied" && <p className="view-sub">Applied to the page.</p>}
      {revision.status === "rejected" && <p className="view-sub">Discarded. The page is unchanged.</p>}
    </div>
  );
}
