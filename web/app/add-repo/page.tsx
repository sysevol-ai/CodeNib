"use client";

import Header from "@/components/Header";
import { navigate } from "@/lib/router";

/** Steps are quoted from the published quickstart rather than written here, so
 *  the page cannot drift from what the package actually does. */
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
    code: 'pip install "codenib[agent]"\nexport OPENAI_API_KEY=...\ncodenib wiki . --generate --model openai/gpt-4o-mini',
    note: "For a local or self-hosted OpenAI-compatible endpoint, add --api-base http://127.0.0.1:8000/v1 --api-key-env LOCAL_LLM_KEY.",
  },
];

export default function AddRepoPage() {
  return (
    <div className="landing">
      <Header />

      <main className="addrepo">
        <button type="button" className="addrepo-back" onClick={() => navigate("/")}>
          ← All repositories
        </button>

        <h1>Index your own repository</h1>

        {/* Say plainly why the hosted set is fixed. A reader who clicked "Add
            repo" is owed the reason, not a "coming soon". */}
        <p className="addrepo-lede">
          This demo serves a fixed set of repositories. Writing a wiki means reading a
          codebase through a model, so the hosted demo cannot do that on demand for
          any repository you point it at. The same pipeline runs on your own machine,
          where your source never leaves it and you choose the model and pay for it
          directly.
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
