import { LoaderCircle, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import type { RepoView } from "@/lib/backend";
import { useStore } from "@/lib/store";
import { NibMark } from "./NibMark";

/** No repository yet: one thing to do. */
export function Welcome() {
  const { chooseFolder, repos } = useStore();
  return (
    <div className="welcome">
      <NibMark size={28} />
      <h1>{repos.length ? "Open a repository" : "Add a repository"}</h1>
      <p className="lead">
        Read its Wiki, ask about it and change it with your own Claude Code. Notes and tasks stay in the repository,
        on this Mac.
      </p>
      <Button onClick={() => void chooseFolder()}>
        Choose a folder…
        <Kbd>⌘O</Kbd>
      </Button>
    </div>
  );
}

/** A repository whose service is starting, or failed to. */
export function Opening({ repo }: { repo: RepoView }) {
  const { lines, restart } = useStore();
  const shown = (lines[repo.path] ?? []).slice(-10);
  if (repo.status === "failed") {
    return (
      <div className="opening">
        <h1>{repo.name} did not open</h1>
        <p className="lead">{repo.error || "CodeNib stopped before the Wiki was ready."}</p>
        {shown.length > 0 && (
          <ol className="opening-lines">
            {shown.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ol>
        )}
        <Button onClick={() => void restart(repo.path)}>
          <RotateCw />
          Try again
        </Button>
      </div>
    );
  }
  return (
    <div className="opening" aria-live="polite">
      <h1>
        <LoaderCircle className="status-spin" /> Opening {repo.name}
      </h1>
      <p className="lead">
        {repo.analysis
          ? "Reading the docs, then building the index and symbol graph. The first time takes a few minutes; later opens take seconds."
          : "Reading the docs and building the index."}
      </p>
      <ol className="opening-lines">
        {shown.length ? shown.map((line, i) => <li key={i}>{line}</li>) : <li>Starting CodeNib…</li>}
      </ol>
    </div>
  );
}
