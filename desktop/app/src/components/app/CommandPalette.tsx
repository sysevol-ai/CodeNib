import { FileText, FolderGit2, Github, Inbox, MessageCircleQuestion, Plus, UserRound } from "lucide-react";
import { useState } from "react";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import { agentName, flattenPages } from "@/lib/model";
import { useStore } from "@/lib/store";

export function CommandPalette() {
  const { palette, setPalette, view, repos, data, go, open, chooseFolder, openFromGitHub, setAccountsOpen, taskAgent } =
    useStore();
  const [query, setQuery] = useState("");
  const here = "repo" in view ? view.repo : repos[0]?.path;
  const d = here ? data[here] : undefined;
  const pages = [
    ...(d?.tree?.docs ?? []).map((doc) => ({ id: doc.id, title: doc.path === "README.md" ? "README" : doc.title, doc: true })),
    ...flattenPages(d?.tree?.pages ?? []).map((p) => ({ id: p.id, title: p.title, doc: false })),
  ];
  const onPage = view.kind === "page";
  const close = () => {
    setPalette({ open: false });
    setQuery("");
  };

  return (
    <CommandDialog
      open={palette.open}
      onOpenChange={(o) => (o ? setPalette({ open: true }) : close())}
      title="Search and run"
      description="Jump to a page, open a repository, or ask your agent"
      className="palette"
      showCloseButton={false}
    >
      <CommandInput placeholder="Jump to a page, or ask a question" value={query} onValueChange={setQuery} />
      <CommandList>
        <CommandEmpty>{onPage ? `Nothing matches. Ask ${agentName(taskAgent)} instead.` : "Nothing matches."}</CommandEmpty>
        {onPage && (
          <CommandGroup heading={`Ask ${agentName(taskAgent)}`}>
            <CommandItem
              value={`ask ${query}`}
              onSelect={() => {
                setPalette({ open: false, ask: query });
                setQuery("");
              }}
            >
              <MessageCircleQuestion />
              {query ? `Ask about this page: ${query}` : "Ask about this page"}
            </CommandItem>
          </CommandGroup>
        )}
        {here && pages.length > 0 && (
          <CommandGroup heading={`Pages in ${repos.find((r) => r.path === here)?.name ?? ""}`}>
            {pages.map((p) => (
              <CommandItem
                key={p.id}
                value={`${p.title} ${p.id}`}
                onSelect={() => {
                  close();
                  go({ kind: "page", repo: here, page: p.id });
                }}
              >
                <FileText />
                {p.title}
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        <CommandSeparator />
        <CommandGroup heading="Go to">
          {repos
            .filter((r) => r.path !== here)
            .map((r) => (
              <CommandItem
                key={r.path}
                value={`repository ${r.name} ${r.path}`}
                onSelect={() => {
                  close();
                  open(r.path);
                  go({ kind: "page", repo: r.path, page: "" });
                }}
              >
                <FolderGit2 />
                {r.name}
              </CommandItem>
            ))}
          <CommandItem
            value="inbox"
            onSelect={() => {
              close();
              go({ kind: "inbox" });
            }}
          >
            <Inbox />
            Inbox
          </CommandItem>
          <CommandItem
            value="add a repository"
            onSelect={() => {
              close();
              void chooseFolder();
            }}
          >
            <Plus />
            Add a folder…
          </CommandItem>
          <CommandItem
            value="open from github clone"
            onSelect={() => {
              close();
              openFromGitHub(query.includes("/") ? query : "");
            }}
          >
            <Github />
            Open from GitHub…
          </CommandItem>
          <CommandItem
            value="accounts github claude codex sign in"
            onSelect={() => {
              close();
              setAccountsOpen(true);
            }}
          >
            <UserRound />
            Accounts…
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
