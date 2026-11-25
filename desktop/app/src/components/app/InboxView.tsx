import { Columns3, List } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ago, flattenPages, taskNeed, taskTitle, type Annotation, type Need, type Task } from "@/lib/model";
import { useStore, type RepoData } from "@/lib/store";
import { StatusMark } from "./StatusMark";

const GROUPS: { key: Need; label: string }[] = [
  { key: "needs", label: "Needs you" },
  { key: "working", label: "Working" },
  { key: "done", label: "Done" },
  { key: "failed", label: "Failed" },
];

interface Row {
  repo: string;
  repoName: string;
  task: Task;
  need: Need;
  title: string;
  page: string;
  pageTitle: string;
  what: string;
  age: string;
}

function what(task: Task, need: Need, annotations: Annotation[]): string {
  if (task.status === "needs_review") return "Review the change";
  if (need === "needs") return "New answer";
  if (need === "working") return task.progress || (task.status === "queued" ? "Waiting" : "Working");
  if (need === "failed") return task.error ? task.error.slice(0, 60) : "Did not finish";
  if (task.kind === "change") {
    const applied = annotations.some((a) => task.inputs.includes(a.id) && a.status === "applied");
    return applied ? "Applied" : "Closed";
  }
  return "Answered";
}

function rowsOf(repo: string, repoName: string, d: RepoData | undefined): Row[] {
  const annotations = d?.notes?.annotations ?? [];
  const titles = new Map<string, string>([
    ...(d?.tree?.docs ?? []).map((doc) => [doc.id, doc.path === "README.md" ? "README" : doc.title] as [string, string]),
    ...flattenPages(d?.tree?.pages ?? []).map((p) => [p.id, p.title] as [string, string]),
  ]);
  return (d?.notes?.tasks ?? []).map((task) => {
    const need = taskNeed(task, annotations);
    const page = annotations.find((a) => task.inputs.includes(a.id))?.on.page ?? "";
    return {
      repo,
      repoName,
      task,
      need,
      title: taskTitle(task, annotations),
      page,
      pageTitle: titles.get(page) ?? page,
      what: what(task, need, annotations),
      age: ago(task.finished || task.created),
    };
  });
}

export function InboxView() {
  const { repos, data, go } = useStore();
  const rows = repos
    .flatMap((r) => rowsOf(r.path, r.name, data[r.path]))
    .sort((a, b) => (a.task.created < b.task.created ? 1 : -1));
  const count = (need: Need) => rows.filter((r) => r.need === need).length;
  const open = (row: Row) =>
    row.task.status === "needs_review"
      ? go({ kind: "review", repo: row.repo, task: row.task.id })
      : go({ kind: "page", repo: row.repo, page: row.page });

  const Line = ({ row, dense = false }: { row: Row; dense?: boolean }) => (
    <button type="button" className={dense ? "task-row is-dense" : "task-row"} onClick={() => open(row)}>
      <StatusMark status={row.need} />
      <span className="task-main">
        <span className="task-title">{row.title}</span>
        <span className="task-where">
          {row.repoName}
          {row.pageTitle ? ` · ${row.pageTitle}` : ""} · This Mac
        </span>
      </span>
      {!dense && <span className={row.need === "needs" ? "task-what is-needs" : "task-what"}>{row.what}</span>}
      <span className="task-age">{row.age}</span>
    </button>
  );

  const columns: { label: string; rows: Row[] }[] = [
    { label: "Needs you", rows: rows.filter((r) => r.need === "needs" || r.need === "failed") },
    { label: "Working", rows: rows.filter((r) => r.need === "working") },
    { label: "Done", rows: rows.filter((r) => r.need === "done") },
  ];

  return (
    <div className="inbox">
      <Tabs defaultValue="list" className="inbox-tabs">
        <div className="inbox-head">
          <h1 className="inbox-title">
            Inbox.{" "}
            <span className="two-tone">
              {rows.length === 0
                ? "Nothing yet."
                : `${count("needs")} need you, ${count("working")} working.`}
            </span>
          </h1>
          <TabsList aria-label="View">
            <TabsTrigger value="list">
              <List />
              List
            </TabsTrigger>
            <TabsTrigger value="board">
              <Columns3 />
              Board
            </TabsTrigger>
          </TabsList>
        </div>
        <p className="inbox-sub">
          {rows.length === 0
            ? "Ask about a paragraph or request a change on any page; your agent's answers and changes land here."
            : `Tasks from ${repos.length === 1 ? "your repository" : `your ${repos.length} repositories`} on this Mac, newest first.`}
        </p>

        <TabsContent value="list" className="inbox-list">
          {GROUPS.map((g) => {
            const group = rows.filter((r) => r.need === g.key);
            if (!group.length) return null;
            return (
              <section key={g.key} className="inbox-group" aria-label={g.label}>
                <h2 className="inbox-group-head">
                  {g.label} <span className="inbox-n">{group.length}</span>
                </h2>
                {group.map((r) => (
                  <Line key={`${r.repo}:${r.task.id}`} row={r} />
                ))}
              </section>
            );
          })}
        </TabsContent>

        <TabsContent value="board" className="board">
          {columns.map((c) => (
            <section key={c.label} className="board-col" aria-label={c.label}>
              <h2 className="inbox-group-head">
                {c.label} <span className="inbox-n">{c.rows.length}</span>
              </h2>
              {c.rows.map((r) => (
                <Line key={`${r.repo}:${r.task.id}`} row={r} dense />
              ))}
            </section>
          ))}
        </TabsContent>
      </Tabs>
    </div>
  );
}
