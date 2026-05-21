import Link from "next/link";
import { notFound } from "next/navigation";
import { getFile, getWiki } from "@/lib/wiki";

export function generateStaticParams() {
  const params: { path: string[] }[] = [];
  for (const m of getWiki().modules) {
    for (const f of m.files) {
      params.push({ path: f.path.split("/") });
    }
  }
  return params;
}

export default function FilePage({ params }: { params: { path: string[] } }) {
  const path = params.path.join("/");
  const found = getFile(path);
  if (!found) notFound();
  const { module: m, file } = found;

  return (
    <article className="page">
      <div className="breadcrumb">
        <Link href="/">Home</Link>
        <span className="sep">/</span>
        <Link href={`/modules/${m.id}`}>{m.title}</Link>
        <span className="sep">/</span>
        <span>{file.name}</span>
      </div>

      <h1>
        <code>{file.name}</code>
      </h1>
      <p className="module-card-path">{file.path}</p>
      <div className="hero-chips">
        <span className="chip">{file.language}</span>
        <span className="chip">{file.lines} LOC</span>
        <span className="chip">{file.symbols.length} symbols</span>
        <a className="chip strong" href={file.source_url} target="_blank" rel="noreferrer">
          View source ↗
        </a>
      </div>

      {file.docstring && (
        <p className="lede" style={{ marginTop: 18 }}>
          {file.docstring}
        </p>
      )}

      {file.parse_error && (
        <p className="ask-error">Could not parse: {file.parse_error}</p>
      )}

      <div className="block">
        <h2>Symbols</h2>
        {file.symbols.length === 0 && (
          <p className="muted">No top-level public symbols.</p>
        )}
        {file.symbols.map((s) => (
          <div className="symbol" key={`${s.name}-${s.lineno}`} id={s.name}>
            <div className="symbol-head">
              <span className={`symbol-kind ${s.kind}`}>{s.kind}</span>
              <span className="symbol-sig">{s.signature}</span>
              <span className="symbol-line">L{s.lineno}</span>
            </div>
            {s.docstring && <div className="symbol-doc">{s.docstring}</div>}
            {s.methods.length > 0 && (
              <ul className="symbol-methods">
                {s.methods.map((mm) => (
                  <li key={`${mm.name}-${mm.lineno}`}>
                    <span className="msig">{mm.signature}</span>{" "}
                    <span className="symbol-line">L{mm.lineno}</span>
                    {mm.docstring && (
                      <div className="mdoc">{mm.docstring}</div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </article>
  );
}
