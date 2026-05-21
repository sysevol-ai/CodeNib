import Link from "next/link";
import { notFound } from "next/navigation";
import { getModule, getWiki } from "@/lib/wiki";

export function generateStaticParams() {
  return getWiki().modules.map((m) => ({ id: m.id }));
}

export default function ModulePage({ params }: { params: { id: string } }) {
  const m = getModule(params.id);
  if (!m) notFound();

  return (
    <article className="page">
      <div className="breadcrumb">
        <Link href="/">Home</Link>
        <span className="sep">/</span>
        <span>Modules</span>
        <span className="sep">/</span>
        <span>{m.title}</span>
      </div>

      <h1>{m.title}</h1>
      <p className="module-card-path">{m.path}</p>
      <p className="lede">{m.description}</p>
      <div className="hero-chips">
        <span className="chip">{m.file_count} files</span>
        <span className="chip">{m.symbol_count} symbols</span>
        <span className="chip">{m.loc.toLocaleString()} LOC</span>
      </div>

      <div className="block">
        <h2>Files</h2>
        <div className="table-wrap">
        <table className="file-table">
          <thead>
            <tr>
              <th>File</th>
              <th>Lang</th>
              <th className="num">LOC</th>
              <th className="num">Symbols</th>
            </tr>
          </thead>
          <tbody>
            {m.files.map((f) => (
              <tr key={f.path}>
                <td>
                  <Link href={`/files/${f.path}`}>
                    <code>{f.path.replace("codeminer/", "")}</code>
                  </Link>
                </td>
                <td>{f.language}</td>
                <td className="num">{f.lines}</td>
                <td className="num">{f.symbols.length}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </div>
    </article>
  );
}
