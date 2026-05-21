import Link from "next/link";
import Mermaid from "@/components/Mermaid";
import { getWiki, relativeTime } from "@/lib/wiki";

export default function Home() {
  const { repo, modules, architecture_mermaid } = getWiki();

  return (
    <article className="page home">
      <section className="hero">
        <h1>{repo.name}</h1>
        <p className="lede">{repo.description}</p>
        <div className="hero-chips">
          <span className="chip strong">{repo.languages.join(", ")}</span>
          <span className="chip">{repo.license}</span>
          <span className="chip">{repo.file_count} files</span>
          <span className="chip">{repo.loc.toLocaleString()} LOC</span>
          <span className="chip" title={repo.commit_subject}>
            {repo.commit_short} · {relativeTime(repo.commit_date)}
          </span>
        </div>
        <p className="hero-supports">
          Parses <strong>{repo.supports.join(", ")}</strong> with tree-sitter.
        </p>
      </section>

      <section className="block">
        <h2>Architecture</h2>
        <p className="muted">
          From source ingestion through index build to query-time retrieval.
        </p>
        <div className="diagram-card">
          <Mermaid chart={architecture_mermaid} />
        </div>
      </section>

      <section className="block">
        <h2>Modules</h2>
        <div className="module-grid">
          {modules.map((m) => (
            <Link
              key={m.id}
              href={`/modules/${m.id}`}
              className="module-card"
            >
              <div className="module-card-title">{m.title}</div>
              <div className="module-card-path">{m.path}</div>
              <p className="module-card-desc">{m.description}</p>
              <div className="module-card-stats">
                <span>{m.file_count} files</span>
                <span>{m.symbol_count} symbols</span>
                <span>{m.loc.toLocaleString()} LOC</span>
              </div>
            </Link>
          ))}
        </div>
      </section>
    </article>
  );
}
