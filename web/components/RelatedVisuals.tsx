import { wikiVisualEvidenceMediaUrl, type Citation, type WikiPage, type WikiVisualEvidence } from "@/lib/api";
import { relatedVisuals } from "@/lib/related-visuals";

export default function RelatedVisuals({ evidence, page, repoId, onOpenCitation }: {
  evidence: WikiVisualEvidence;
  page: WikiPage;
  repoId: string;
  onOpenCitation: (citation: Citation) => void;
}) {
  const visuals = relatedVisuals(evidence, page);
  if (!visuals.length) return null;
  return (
    <details className="related-visuals">
      <summary>
        <strong>Related diagrams</strong>
        <span>{visuals.length} · Explore {visuals[0].topics.join(" / ")}</span>
      </summary>
      <div className="related-visuals-content">
        {visuals.map(({ fact, topics, citations, reason, document }) => {
          const url = wikiVisualEvidenceMediaUrl(repoId, evidence.source_commit, fact.artifact_path);
          const title = topics.join(" · ");
          return (
            <article key={fact.artifact_path} className="related-visual">
              <a href={url} target="_blank" rel="noreferrer" className="related-visual-image" aria-label={`Open diagram: ${title}`}>
                <img src={url} alt={`Repository diagram mentioning ${title}`} loading="lazy" />
              </a>
              <div className="related-visual-copy">
                <h3>{title}</h3>
                <p className="related-visual-reason">{reason}</p>
                <p className="related-visual-caption">{fact.claims.find((claim) => claim.text.trim())?.text}</p>
                <small>Image summary · compare with the source</small>
                <div className="related-visual-actions">
                  <a href={url} target="_blank" rel="noreferrer">View original ↗</a>
                  {document && (
                    <button type="button" onClick={() => onOpenCitation({
                      file: document.file, start_line: Math.max(1, document.line - 6),
                      end_line: document.line + 6, node_name: document.section || document.file,
                      type: "visual_context", score: null, content: null,
                    })}>Read in context →</button>
                  )}
                  {citations.map((citation) => (
                    <button type="button" key={`${citation.file}:${citation.start_line}`} onClick={() => onOpenCitation(citation)}>
                      {citation.node_name || citation.file} →
                    </button>
                  ))}
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </details>
  );
}
