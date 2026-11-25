import { FolderOpen, Github, LoaderCircle, Lock, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  backend,
  listen,
  message,
  type AgentStatus,
  type CloneProgress,
  type GitHubLookup,
  type GitHubRepository,
  type WriterId,
} from "@/lib/backend";
import { useStore } from "@/lib/store";

type Writer = WriterId | "none";

/** Why an agent cannot write pages now, or null when it can. */
function unavailable(agent: AgentStatus | undefined): string | null {
  if (!agent) return "Checking…";
  if (!agent.installed) return "Not installed";
  if (!agent.signed_in) return "Not signed in";
  return null;
}

function WriterChoice({ value, onChange }: { value: Writer; onChange: (w: Writer) => void }) {
  const { agents, setAccountsOpen } = useStore();
  const options: { id: Writer; label: string; agent?: AgentStatus }[] = [
    { id: "none", label: "Off" },
    { id: "claude-code", label: "Claude Code", agent: agents?.find((a) => a.id === "claude-code") },
    { id: "codex", label: "Codex", agent: agents?.find((a) => a.id === "codex") },
  ];
  const chosen = options.find((o) => o.id === value);
  const problem = chosen?.agent ? unavailable(chosen.agent) : null;
  return (
    <div className="add-option is-block">
      <span>
        <strong>Write pages with</strong>
        <span className="add-sub">
          Explanations written by your own agent, on its own plan. About $0.10 to $0.20 a page. Without one, pages
          are built from the code index.
        </span>
      </span>
      <div className="segmented" role="radiogroup" aria-label="Write pages with">
        {options.map((option) => {
          const why = option.id === "none" ? null : unavailable(option.agent);
          return (
            <button
              key={option.id}
              type="button"
              role="radio"
              aria-checked={value === option.id}
              className="segment"
              data-active={value === option.id ? "yes" : "no"}
              onClick={() => onChange(option.id)}
              title={why ?? undefined}
            >
              {option.label}
              {why && option.agent && <span className="segment-note">{why}</span>}
            </button>
          );
        })}
      </div>
      {problem && (
        <p className="add-warn" role="status">
          {chosen?.label} is {problem.toLowerCase()} on this Mac.{" "}
          <button type="button" className="sb-link" onClick={() => setAccountsOpen(true)}>
            Open Accounts
          </button>
        </p>
      )}
    </div>
  );
}

function ago(iso: string | null): string {
  if (!iso) return "";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);
  if (days < 1) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  return months < 12 ? `${months} mo ago` : `${Math.floor(months / 12)} yr ago`;
}

function GitHubSide({
  input,
  setInput,
  picked,
  setPicked,
}: {
  input: string;
  setInput: (value: string) => void;
  picked: GitHubLookup | null;
  setPicked: (lookup: GitHubLookup | null) => void;
}) {
  const { github, setAccountsOpen } = useStore();
  const [mine, setMine] = useState<GitHubRepository[] | null>(null);
  const [listError, setListError] = useState("");
  const [looking, setLooking] = useState(false);
  const [lookError, setLookError] = useState("");
  const box = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => box.current?.focus(), 40);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!github?.signed_in) return;
    let live = true;
    backend
      .githubRepositories()
      .then((list) => live && setMine(list))
      .catch((error) => live && setListError(message(error)));
    return () => {
      live = false;
    };
  }, [github?.signed_in]);

  // A pasted address or owner/repo is looked up as the person types.
  const looksLikeRepo = /^\s*(https?:\/\/(www\.)?github\.com\/|github\.com\/|git@github\.com:)?[\w.-]+\/[\w.-]+/.test(input);
  useEffect(() => {
    setLookError("");
    if (!looksLikeRepo) {
      setPicked(null);
      return;
    }
    let live = true;
    setLooking(true);
    const timer = window.setTimeout(() => {
      backend
        .githubLookup(input)
        .then((found) => live && setPicked(found))
        .catch((error) => {
          if (!live) return;
          setPicked(null);
          setLookError(message(error));
        })
        .finally(() => live && setLooking(false));
    }, 350);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [input, looksLikeRepo, setPicked]);

  const needle = input.trim().toLowerCase();
  const shown = useMemo(
    () => (mine ?? []).filter((r) => !needle || r.full_name.toLowerCase().includes(needle)).slice(0, 8),
    [mine, needle],
  );

  return (
    <div className="add-field">
      <span className="add-label">Repository</span>
      <div className="gh-search">
        <Search className="gh-search-icon" />
        <Input
          ref={box}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="owner/repo or https://github.com/owner/repo"
          aria-label="GitHub repository"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
        />
        {looking && <LoaderCircle className="status-spin gh-search-busy" />}
      </div>
      {lookError && (
        <p className="add-warn" role="alert">
          {lookError}
        </p>
      )}
      {picked && (
        <div className="gh-picked">
          <div className="gh-picked-name">
            {picked.repository.private && <Lock aria-label="Private" />}
            <strong>{picked.repository.full_name}</strong>
          </div>
          {picked.repository.description && <p className="add-sub">{picked.repository.description}</p>}
          <p className="add-sub mono">
            {picked.present ? "Already on this Mac: " : "Clones to "}
            {picked.destination.replace(/^\/Users\/[^/]+/, "~")}
          </p>
        </div>
      )}
      {!picked && github?.signed_in && (
        <ul className="gh-list" aria-label="Your repositories">
          {mine === null && !listError && <li className="add-sub">Loading your repositories…</li>}
          {listError && <li className="add-warn">{listError}</li>}
          {shown.map((repo) => (
            <li key={repo.full_name}>
              <button type="button" className="gh-row" onClick={() => setInput(repo.full_name)}>
                <span className="gh-row-name">
                  {repo.private && <Lock aria-label="Private" />}
                  {repo.full_name}
                </span>
                <span className="gh-row-meta">
                  {[repo.language, ago(repo.pushed_at)].filter(Boolean).join(" · ")}
                </span>
              </button>
            </li>
          ))}
          {mine?.length === 0 && <li className="add-sub">No repositories on this account yet.</li>}
        </ul>
      )}
      {!github?.signed_in && (
        <p className="add-sub">
          Public repositories open without signing in.{" "}
          <button type="button" className="sb-link" onClick={() => setAccountsOpen(true)}>
            Sign in to GitHub
          </button>{" "}
          to open private ones and pick from your list.
        </p>
      )}
    </div>
  );
}

export function AddRepoDialog() {
  const { adding, setAdding, addRepository, agents } = useStore();
  const [analysis, setAnalysis] = useState(true);
  const [writer, setWriter] = useState<Writer>("none");
  const [busy, setBusy] = useState(false);
  const [cloneLine, setCloneLine] = useState("");
  const [picked, setPicked] = useState<GitHubLookup | null>(null);
  const tab = adding?.tab ?? "folder";

  // Default the writer to an agent that is ready, once agents are known.
  const defaulted = useRef(false);
  useEffect(() => {
    if (defaulted.current || !agents) return;
    defaulted.current = true;
    const ready = agents.find((a) => a.installed && a.signed_in);
    if (ready) setWriter(ready.id);
  }, [agents]);

  useEffect(() => {
    if (!adding) {
      setPicked(null);
      setCloneLine("");
    }
  }, [adding]);

  useEffect(() => {
    let stop: (() => void) | undefined;
    let gone = false;
    void listen<CloneProgress>("codenib://clone", ({ line }) => setCloneLine(line)).then((u) =>
      gone ? u() : (stop = u),
    );
    return () => {
      gone = true;
      stop?.();
    };
  }, []);

  const writerId = writer === "none" ? null : writer;
  const canAdd = tab === "folder" ? !!(adding?.tab === "folder" && adding.folder) : !!picked;

  const add = async () => {
    if (!adding || busy) return;
    setBusy(true);
    try {
      let folder: string | null = adding.tab === "folder" ? adding.folder : null;
      if (adding.tab === "github") {
        if (!picked) return;
        setCloneLine(picked.present ? "Using the copy on this Mac" : "Cloning…");
        folder = await backend.cloneRepository(picked.repository.full_name);
      }
      if (!folder) return;
      await addRepository(folder, analysis, writerId);
      setAdding(null);
    } catch (error) {
      toast.error(message(error));
    } finally {
      setBusy(false);
      setCloneLine("");
    }
  };

  return (
    <Dialog open={adding !== null} onOpenChange={(o) => !o && !busy && setAdding(null)}>
      <DialogContent className="add-dialog">
        <DialogHeader>
          <DialogTitle>Add a repository</DialogTitle>
          <DialogDescription>
            {tab === "folder"
              ? "CodeNib reads it on this Mac. The code stays where it is."
              : "CodeNib clones it to ~/CodeNib on this Mac, then reads it there."}
          </DialogDescription>
        </DialogHeader>
        <div className="segmented add-tabs" role="tablist" aria-label="Where the repository is">
          <button
            type="button"
            role="tab"
            className="segment"
            aria-selected={tab === "folder"}
            data-active={tab === "folder" ? "yes" : "no"}
            onClick={() => setAdding({ tab: "folder", folder: adding?.tab === "folder" ? adding.folder : null })}
            disabled={busy}
          >
            <FolderOpen />
            Folder on this Mac
          </button>
          <button
            type="button"
            role="tab"
            className="segment"
            aria-selected={tab === "github"}
            data-active={tab === "github" ? "yes" : "no"}
            onClick={() => setAdding({ tab: "github", input: adding?.tab === "github" ? adding.input : "" })}
            disabled={busy}
          >
            <Github />
            GitHub
          </button>
        </div>
        {adding?.tab === "folder" && (
          <div className="add-field">
            <span className="add-label">Folder</span>
            <div className="add-folder">
              <span className="mono">{adding.folder ?? "No folder chosen"}</span>
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  backend
                    .pickFolder()
                    .then((f) => f && setAdding({ tab: "folder", folder: f }))
                    .catch((e) => toast.error(message(e)))
                }
                disabled={busy}
              >
                <FolderOpen />
                Choose
              </Button>
            </div>
          </div>
        )}
        {adding?.tab === "github" && (
          <GitHubSide
            input={adding.input}
            setInput={(input) => setAdding({ tab: "github", input })}
            picked={picked}
            setPicked={setPicked}
          />
        )}
        <label className="add-option">
          <Checkbox checked disabled />
          <span>
            <strong>Start from README and docs</strong>
            <span className="add-sub">Ready in seconds. Always on.</span>
          </span>
        </label>
        <label className="add-option">
          <Switch checked={analysis} onCheckedChange={setAnalysis} />
          <span>
            <strong>Build analysis tools</strong>
            <span className="add-sub">Index, symbol graph and map. A few minutes, no model.</span>
          </span>
        </label>
        <WriterChoice value={writer} onChange={setWriter} />
        <DialogFooter>
          {busy && cloneLine && (
            <span className="add-progress mono" aria-live="polite">
              {cloneLine}
            </span>
          )}
          <Button variant="ghost" onClick={() => setAdding(null)} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void add()} disabled={busy || !canAdd}>
            {busy ? (tab === "github" ? "Cloning…" : "Adding…") : tab === "github" ? "Clone and open" : "Add repository"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
