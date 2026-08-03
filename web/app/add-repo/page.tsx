"use client";

import Header from "@/components/Header";
import { AppLink } from "@/lib/router";

/** Keep this path intentionally short; the linked quickstart remains the
 *  authority for provider-specific and advanced options. */
const STEPS: {
  title: string;
  body: string;
  code: string;
  note?: string;
}[] = [
  {
    title: "Install and check the runtime",
    body: "codenib doctor reports which language backends and optional extras are present before you index anything.",
    code: "pip install codenib\ncodenib doctor --require core --require wiki",
  },
  {
    title: "Open a wiki for your repository",
    body: "CodeNib detects the languages, builds its views, registers the repository with a local service, and opens the wiki at localhost:3000.",
    code: "codenib wiki /path/to/repository",
    note: "The release wheel ships the compiled frontend, so this needs no Node.js and no source checkout. Indexes live under ~/.codenib; your repository is left unchanged.",
  },
  {
    title: "Turn on agent-authored pages",
    body: "Static pages are the default. The narrated, source-anchored pages you see in this demo come from a model you point CodeNib at — any LiteLLM-supported provider.",
    code: 'pip install "codenib[agent]"\nexport OPENAI_API_KEY=...\ncodenib doctor --require agent \\\n  --model openai/gpt-4o-mini --api-key-env OPENAI_API_KEY --probe-model\ncodenib wiki . --generate --model openai/gpt-4o-mini',
    note: "A hosted provider receives the source evidence needed to write each page. To keep generation local, point --api-base http://127.0.0.1:8080/v1 at a self-hosted OpenAI-compatible endpoint; add --api-key-env only when that gateway requires authentication.",
  },
];

export default function AddRepoPage() {
  return (
    <div className="landing">
      <Header />

      <main className="addrepo">
        <AppLink className="addrepo-back" href="/">
          ← All repositories
        </AppLink>

        <h1>Index your own repository</h1>

        {/* Say plainly why the hosted set is fixed. A reader who clicked "Add
            repo" is owed the reason, not a "coming soon". */}
        <p className="addrepo-lede">
          This demo serves a fixed set of repositories. Writing a wiki means reading a
          codebase through a model, so the hosted demo cannot do that on demand for
          any repository you point it at. The same indexing pipeline runs on your own
          machine, where the checkout and generated indexes remain local. You choose
          the model; source evidence is sent only to that configured provider when you
          opt into agent-authored pages.
        </p>

        <ol className="addrepo-steps">
          {STEPS.map((step, index) => (
            <li key={step.title}>
              <div className="addrepo-step-head">
                <span className="addrepo-step-n" aria-hidden="true">
                  {index + 1}
                </span>
                <h2>{step.title}</h2>
              </div>
              <p>{step.body}</p>
              <pre className="addrepo-code">
                <code>{step.code}</code>
              </pre>
              {step.note && <p className="addrepo-note">{step.note}</p>}
            </li>
          ))}
        </ol>

        <p className="addrepo-foot">
          Full options — presets, language selection, incremental reindexing, MCP —
          are in the{" "}
          <a href="https://docs.codenib.ai/quickstart/" target="_blank" rel="noreferrer">
            quickstart
          </a>
          .
        </p>
      </main>
    </div>
  );
}
