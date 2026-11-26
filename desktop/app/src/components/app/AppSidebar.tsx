import { ChevronRight, ChevronsUpDown, FileText, FolderTree, Github, Inbox, MoreHorizontal, Plus, RotateCw, Search, UserRound } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Kbd } from "@/components/ui/kbd";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
} from "@/components/ui/sidebar";
import { backend, message, type RepoView } from "@/lib/backend";
import { useAreas } from "@/lib/useAreas";
import { agentName, flattenPages, taskNeed, taskTitle, type Task } from "@/lib/model";
import { stillWriting, useStore } from "@/lib/store";
import { NibMark } from "./NibMark";
import { StatusMark } from "./StatusMark";

function RepoActions({ repo }: { repo: RepoView }) {
  const { removeRepository, restart } = useStore();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <SidebarMenuAction showOnHover aria-label={`More for ${repo.name}`}>
          <MoreHorizontal />
        </SidebarMenuAction>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="right" align="start">
        <DropdownMenuItem onSelect={() => backend.showInFinder(repo.path).catch((e) => toast.error(message(e)))}>
          Show in Finder
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => restart(repo.path).catch((e) => toast.error(message(e)))}>
          Restart CodeNib for it
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => removeRepository(repo.path).catch((e) => toast.error(message(e)))}>
          Remove from CodeNib
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

interface Folder {
  name: string;
  path: string;
  folders: Map<string, Folder>;
  files: { name: string; path: string; cites: number }[];
}

function buildTree(files: { path: string; cited_by: unknown[] }[]): Folder {
  const root: Folder = { name: "", path: "", folders: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let at = root;
    for (const part of parts.slice(0, -1)) {
      const next = at.folders.get(part) ?? {
        name: part,
        path: at.path ? `${at.path}/${part}` : part,
        folders: new Map(),
        files: [],
      };
      at.folders.set(part, next);
      at = next;
    }
    at.files.push({ name: parts[parts.length - 1], path: file.path, cites: file.cited_by.length });
  }
  return root;
}

/** The repository's files, as folders, under "Files". A file the Wiki cites
 *  carries how many pages cite it. */
function FilesTree({ repo }: { repo: string }) {
  const { data, loadFiles, go, view } = useStore();
  const files = data[repo]?.files;
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [shown, setShown] = useState(false);
  const tree = useMemo(() => (files ? buildTree(files) : null), [files]);
  const toggle = (path: string) =>
    setOpen((all) => {
      const next = new Set(all);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  const current = view.kind === "file" && view.repo === repo ? view.file : null;
  const render = (folder: Folder, depth: number): React.ReactNode[] => [
    ...[...folder.folders.values()]
      .sort((a, b) => a.name.localeCompare(b.name))
      .flatMap((f) => [
        <SidebarMenuSubItem key={`d:${f.path}`}>
          <SidebarMenuSubButton onClick={() => toggle(f.path)} style={{ paddingLeft: 8 + depth * 12 }} className="sb-folder">
            <ChevronRight className="sb-chev" data-open={open.has(f.path) ? "yes" : "no"} />
            <span>{f.name}</span>
          </SidebarMenuSubButton>
        </SidebarMenuSubItem>,
        ...(open.has(f.path) ? render(f, depth + 1) : []),
      ]),
    ...folder.files
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((f) => (
        <SidebarMenuSubItem key={`f:${f.path}`}>
          <SidebarMenuSubButton
            isActive={current === f.path}
            onClick={() => go({ kind: "file", repo, file: f.path })}
            style={{ paddingLeft: 22 + depth * 12 }}
            title={f.path}
          >
            <span className="sb-file">{f.name}</span>
            {f.cites > 0 && <span className="sb-file-cites">{f.cites}</span>}
          </SidebarMenuSubButton>
        </SidebarMenuSubItem>
      )),
  ];
  return (
    <>
      <SidebarMenuSubItem>
        <SidebarMenuSubButton
          onClick={() => {
            if (!files) void loadFiles(repo);
            setShown((s) => !s);
          }}
          className="sb-folder"
        >
          <ChevronRight className="sb-chev" data-open={shown ? "yes" : "no"} />
          <FolderTree />
          <span>Files</span>
        </SidebarMenuSubButton>
      </SidebarMenuSubItem>
      {shown && !tree && <li className="sb-note">Listing files…</li>}
      {shown && tree && render(tree, 1)}
    </>
  );
}

/** The mark beside a repository: failed, working, or how many need you. */
function repoMark(repo: RepoView, tasks: Task[], needs: number) {
  if (repo.status === "failed") return <StatusMark status="failed" />;
  if (repo.status === "starting" || tasks.some((t) => t.status === "queued" || t.status === "running"))
    return <StatusMark status="working" />;
  if (needs > 0)
    return (
      <span className="sb-count" aria-label={`${needs} need you`}>
        <StatusMark status="needs" />
        {needs}
      </span>
    );
  return null;
}

function RepoTree({ repo }: { repo: RepoView }) {
  const { data, view, go, open, lines, restart } = useStore();
  const d = data[repo.path];
  const isHere = "repo" in view && view.repo === repo.path;
  const [expanded, setExpanded] = useState(isHere);
  const tasks = d?.notes?.tasks ?? [];
  const annotations = d?.notes?.annotations ?? [];
  const needs = tasks.filter((t) => taskNeed(t, annotations) === "needs").length;
  const pages = flattenPages(d?.tree?.pages ?? []);
  const { ofPage } = useAreas(repo.path);
  const currentPage = view.kind === "page" && isHere ? view.page : null;
  const shownTasks = [...tasks].sort((a, b) => (a.created < b.created ? 1 : -1)).slice(0, 6);

  const toggle = (next: boolean) => {
    setExpanded(next);
    if (next) open(repo.path);
  };

  const pageOfTask = (task: Task) => {
    const first = annotations.find((a) => task.inputs.includes(a.id));
    return first?.on.page ?? "";
  };

  return (
    <Collapsible open={expanded} onOpenChange={toggle} asChild>
      <SidebarMenuItem>
        <CollapsibleTrigger asChild>
          <SidebarMenuButton className="sb-repo" isActive={isHere && !expanded}>
            <ChevronRight className="sb-chev" data-open={expanded ? "yes" : "no"} />
            <span className="sb-repo-name">{repo.name}</span>
            <span className="sb-right">{repoMark(repo, tasks, needs)}</span>
          </SidebarMenuButton>
        </CollapsibleTrigger>
        <RepoActions repo={repo} />
        <CollapsibleContent>
          <SidebarMenuSub className="sb-sub">
            {repo.status === "starting" && (
              <li className="sb-note" aria-live="polite">
                Opening… <span className="sb-note-line">{(lines[repo.path] ?? []).slice(-1)[0] ?? repo.line}</span>
              </li>
            )}
            {repo.status === "failed" && (
              <li className="sb-note is-failed">
                <span>{repo.error || "CodeNib stopped"}</span>
                <button type="button" className="sb-link" onClick={() => restart(repo.path)}>
                  <RotateCw />
                  Try again
                </button>
              </li>
            )}
            {repo.status === "stopped" && !d?.tree && (
              <li className="sb-note">
                <button type="button" className="sb-link" onClick={() => open(repo.path)}>
                  Open
                </button>
              </li>
            )}
            {d?.error && <li className="sb-note is-failed">{d.error}</li>}
            {stillWriting(d?.progress) && (
              <li className="sb-note" aria-live="polite">
                {agentName(repo.writer)} is writing pages · {d!.progress!.written.length} of {d!.progress!.pages.length}
              </li>
            )}
            {(d?.tree?.docs ?? []).map((doc) => (
              <SidebarMenuSubItem key={doc.id}>
                <SidebarMenuSubButton
                  isActive={currentPage === doc.id}
                  onClick={() => go({ kind: "page", repo: repo.path, page: doc.id })}
                >
                  <FileText />
                  <span>{doc.path === "README.md" ? "README" : doc.title}</span>
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            ))}
            {pages.map((p) => (
              <SidebarMenuSubItem key={p.id}>
                <SidebarMenuSubButton
                  isActive={currentPage === p.id}
                  onClick={() => go({ kind: "page", repo: repo.path, page: p.id })}
                  style={{ paddingLeft: 8 + p.depth * 14 }}
                  data-unwritten={
                    stillWriting(d?.progress) && !d!.progress!.written.includes(p.id) ? "yes" : undefined
                  }
                  title={
                    stillWriting(d?.progress) && !d!.progress!.written.includes(p.id)
                      ? `Not written yet; ${agentName(repo.writer)} writes it when you open it`
                      : undefined
                  }
                >
                  <span className="sb-page-pad" aria-hidden="true">
                    <span className="area-mark" style={{ background: ofPage(p.id) }} />
                  </span>
                  <span>{p.title}</span>
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            ))}
            {d?.tree && <FilesTree repo={repo.path} />}
            {shownTasks.map((task) => (
              <SidebarMenuSubItem key={task.id}>
                <SidebarMenuSubButton
                  onClick={() =>
                    task.status === "needs_review"
                      ? go({ kind: "review", repo: repo.path, task: task.id })
                      : go({ kind: "page", repo: repo.path, page: pageOfTask(task) })
                  }
                  isActive={view.kind === "review" && view.task === task.id}
                >
                  <StatusMark status={taskNeed(task, annotations)} />
                  <span>{taskTitle(task, annotations)}</span>
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            ))}
          </SidebarMenuSub>
        </CollapsibleContent>
      </SidebarMenuItem>
    </Collapsible>
  );
}

export function AppSidebar() {
  const { repos, data, view, go, chooseFolder, openFromGitHub, setPalette, setup, agents, github, taskAgent, setAccountsOpen } =
    useStore();
  const needs = repos.reduce((sum, repo) => {
    const d = data[repo.path];
    const annotations = d?.notes?.annotations ?? [];
    const proposals = (d?.revisions ?? []).filter((r) => r.status === "needs_review").length;
    return sum + proposals + (d?.notes?.tasks ?? []).filter((t) => taskNeed(t, annotations) === "needs").length;
  }, 0);
  const running = repos.filter((r) => r.status === "ready").length;
  const agent = agents?.find((a) => a.id === taskAgent);
  const agentLine = !agents
    ? "Checking your agents…"
    : agent?.installed && agent.signed_in
      ? `${agentName(taskAgent)} signed in`
      : agents.some((a) => a.installed && a.signed_in)
        ? `${agentName(taskAgent)} not ready`
        : "No agent signed in";
  const ready = !!(agent?.installed && agent.signed_in);

  return (
    <Sidebar collapsible="none" className="app-sidebar">
      <SidebarHeader className="sb-head">
        <div className="titlebar-room" data-tauri-drag-region />
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" className="sb-brand">
              <NibMark size={16} />
              <span>CodeNib</span>
              <ChevronsUpDown className="sb-brand-chev" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-56">
            <DropdownMenuItem onSelect={() => openFromGitHub()}>Open from GitHub…</DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void chooseFolder()}>Add a folder…</DropdownMenuItem>
            <DropdownMenuItem onSelect={() => setAccountsOpen(true)}>Accounts…</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem disabled>{setup?.version ?? "CodeNib"}</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton onClick={() => setPalette({ open: true })}>
              <Search />
              <span>Search</span>
              <span className="sb-keys">
                <Kbd>⌘</Kbd>
                <Kbd>K</Kbd>
              </span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton onClick={() => go({ kind: "inbox" })} isActive={view.kind === "inbox"}>
              <Inbox />
              <span>Inbox</span>
              {needs > 0 && (
                <span className="sb-count" aria-label={`${needs} need you`}>
                  <StatusMark status="needs" />
                  {needs}
                </span>
              )}
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent className="sb-content">
        <SidebarGroup>
          <SidebarGroupLabel className="sb-label">This Mac</SidebarGroupLabel>
          <SidebarMenu>
            {repos.map((repo) => (
              <RepoTree key={repo.path} repo={repo} />
            ))}
            <SidebarMenuItem>
              <SidebarMenuButton onClick={() => openFromGitHub()} className="sb-quiet">
                <Github />
                <span>Open from GitHub</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
            <SidebarMenuItem>
              <SidebarMenuButton onClick={() => void chooseFolder()} className="sb-quiet">
                <Plus />
                <span>Add a folder</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter className="sb-foot">
        <button
          type="button"
          className="sb-service"
          onClick={() => setAccountsOpen(true)}
          aria-label="Accounts"
          title="Accounts (⌘,)"
        >
          <UserRound className="sb-service-icon" />
          <div>
            <div className="sb-service-line">
              {repos.length === 0 ? "No repositories yet" : `${running} of ${repos.length} open`}
            </div>
            <div className="sb-service-sub">
              <span className="sb-dot" data-ok={ready ? "yes" : "no"} />
              {agentLine}
              {github?.signed_in ? ` · GitHub ${github.login}` : ""}
            </div>
            {!setup?.codenib && setup !== null && <div className="sb-service-sub is-failed">CodeNib not found</div>}
          </div>
        </button>
      </SidebarFooter>
    </Sidebar>
  );
}
