// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import Header from "@/components/Header";
import { GUARD_CASES, type GuardCase, type GuardHop } from "@/lib/guardCases";
import { recordExperience } from "@/lib/experience";
import { AppLink } from "@/lib/router";
import "./guard.css";

const INSTALL_URL = "https://docs.codenib.ai/guard/";
const METHOD_URL =
  "https://github.com/sysevol-ai/CodeNib/pull/976";

function label(symbol: string) {
  const qualified = symbol.split(":").slice(1).join(":") || symbol;
  return qualified.split(".").slice(-2).join(".");
}

function fileName(path: string | null) {
  return path ? path.split("/").pop() : "";
}

function CallPath({ path }: { path: GuardHop[] }) {
  return (
    <ol className="guard-chain">
      {path.map((hop, index) => (
        <li key={`${hop.symbol}-${index}`} className={index === path.length - 1 ? "changed" : undefined}>
          <span className="guard-chain-name">{label(hop.symbol)}</span>
          <span className="guard-chain-where">
            {index === path.length - 1
              ? "changed code"
              : `calls at ${fileName(hop.file)}:${hop.call_line ?? "?"}`}
          </span>
        </li>
      ))}
    </ol>
  );
}

function testName(test: string) {
  // PatchDiff names Django tests "path::method (dotted.Class)"; keep the method.
  const plain = test.replace(/\s*\(.*\)$/, "");
  return plain.split("::").slice(1).join("::") || plain;
}

function Patch({ text }: { text: string }) {
  // Number each line from its hunk header: old and new line, then the marker.
  let oldLine = 0;
  let newLine = 0;
  const rows = text.split("\n").map((line, index) => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      return (
        <span key={index} className="hunk">
          {line}
        </span>
      );
    }
    if (line.startsWith("+++ ")) {
      return (
        <span key={index} className="file">
          {line.slice(4).replace(/^b\//, "")}
        </span>
      );
    }
    if (/^(diff |index |--- |new file|deleted file|similarity|rename )/.test(line)) return null;
    const mark = line.charAt(0);
    let left = "";
    let right = "";
    let kind = "ctx";
    if (mark === "+") {
      right = String(newLine++);
      kind = "add";
    } else if (mark === "-") {
      left = String(oldLine++);
      kind = "del";
    } else {
      left = String(oldLine++);
      right = String(newLine++);
    }
    return (
      <span key={index} className={kind}>
        <i className="ln">{left}</i>
        <i className="ln">{right}</i>
        <i className="mk">{mark === "+" || mark === "-" ? mark : ""}</i>
        <code>{line.slice(1) || " "}</code>
      </span>
    );
  });
  return <pre className="guard-patch">{rows}</pre>;
}

function CaseDetail({ item }: { item: GuardCase }) {
  const caught = item.broken.some((test) => test.flagged);
  const shownBroken = item.broken.slice(0, 4);
  const firstPath = item.broken.find((test) => test.path)?.path;
  return (
    <article className="guard-detail" aria-labelledby={`${item.id}-title`}>
      <header className="guard-detail-head">
        <h2 id={`${item.id}-title`}>
          {item.instance} <span>patch by {item.agent}</span>
        </h2>
        <p className={caught ? "guard-verdict caught" : "guard-verdict missed"}>
          {caught ? "Broken test flagged" : "Missed"}
        </p>
      </header>
      <p className="guard-note">{item.note}</p>
      <dl className="guard-facts">
        <div>
          <dt>Tests at risk</dt>
          <dd>
            <strong>{item.flaggedCount.toLocaleString("en-US")}</strong> of{" "}
            {item.totalTests.toLocaleString("en-US")}, within 3 calls
          </dd>
        </div>
        <div>
          <dt>Changed code</dt>
          <dd className="mono">{item.changed.map(label).join(", ")}</dd>
        </div>
        <div>
          <dt>Developer&rsquo;s fix</dt>
          <dd>
            <a href={item.fixUrl} target="_blank" rel="noreferrer">
              {item.fixUrl.replace("https://github.com/", "")}
            </a>
          </dd>
        </div>
      </dl>

      <section className="guard-block" aria-label="Tests the full suite found broken">
        <h3>Broken in the full suite</h3>
        <ul className="guard-broken">
          {shownBroken.map((test) => (
            <li key={test.test} className={test.flagged ? "flagged" : "missed"}>
              <span className="mono">{testName(test.test)}</span>
              <span className="guard-broken-state">
                {test.flagged ? `flagged, ${test.depth} ${test.depth === 1 ? "call" : "calls"} away` : "not flagged"}
              </span>
            </li>
          ))}
        </ul>
        {item.broken.length > shownBroken.length && (
          <p className="guard-more">and {item.broken.length - shownBroken.length} more, none flagged</p>
        )}
        {firstPath && (
          <>
            <h4>Why it was flagged</h4>
            <CallPath path={firstPath} />
          </>
        )}
      </section>

      <section className="guard-block" aria-label="The agent's patch">
        <h3>The agent&rsquo;s patch</h3>
        <Patch text={item.patch} />
      </section>

      <section className="guard-block" aria-label="Other tests the guard listed">
        <h3>First tests the guard listed</h3>
        <ul className="guard-tests">
          {item.tests.slice(0, 8).map((test) => (
            <li key={test.id}>
              <span className="mono">{test.id}</span>
              <span className="guard-tests-path">{test.path.map((hop) => label(hop.symbol)).join(" → ")}</span>
            </li>
          ))}
        </ul>
      </section>
      <p className="guard-source">
        Output of <code>codenib guard check --json</code> at base commit{" "}
        <code>{item.baseCommit.slice(0, 12)}</code> with the agent&rsquo;s patch applied. Patch and
        full-suite results: Wang, Pradel and Liu, ICSE 2026, CC BY 4.0.
      </p>
    </article>
  );
}

export default function GuardCasesPage() {
  const detailRef = useRef<HTMLDivElement>(null);
  const firstRender = useRef(true);
  const [selected, setSelected] = useState(() => {
    const requested = new URLSearchParams(window.location.search).get("case");
    return GUARD_CASES.find((item) => item.id === requested)?.id ?? GUARD_CASES[0].id;
  });
  const item = GUARD_CASES.find((entry) => entry.id === selected) ?? GUARD_CASES[0];

  // Switching cases replaces the whole detail. If its top has scrolled out of
  // view, bring the new case's header back below the site header, instead of
  // leaving the reader in the middle of a different case.
  useLayoutEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    const detail = detailRef.current;
    if (!detail) return;
    const header = document.querySelector(".site-header")?.getBoundingClientRect().height ?? 64;
    const top = detail.getBoundingClientRect().top;
    if (top < header || top > window.innerHeight * 0.7) {
      window.scrollTo({ top: Math.max(0, window.scrollY + top - header - 16) });
    }
  }, [selected]);

  useEffect(() => {
    document.title = "CodeNib guard: real agent patches";
    recordExperience("guard_case_view");
  }, []);

  function choose(id: string) {
    setSelected(id);
    const url = new URL(window.location.href);
    url.searchParams.set("case", id);
    window.history.replaceState(null, "", url);
  }

  return (
    <div className="landing guard-page">
      <Header />
      <main className="guard-main">
        <div className="guard-intro">
          <h1>Real agent patches, and what the guard said.</h1>
          <p>
            Each patch passed SWE-bench&rsquo;s tests and still broke tests elsewhere in the project&rsquo;s
            full suite. For each one, this is what <code>codenib guard check</code> reports from the code
            before the patch: the tests the change reaches, and the calls in between. One of the five
            is a miss.
          </p>
          <p className="guard-links">
            <a className="guard-install" href={INSTALL_URL}>
              Add it to Claude Code or Codex
            </a>
            <a href={METHOD_URL} target="_blank" rel="noreferrer">
              How it did on all 68 regressions
            </a>
            <AppLink href="/wikis">Repository Wikis</AppLink>
          </p>
        </div>
        <div className="guard-layout">
          <nav className="guard-list" aria-label="Cases">
            {GUARD_CASES.map((entry) => {
              const caught = entry.broken.some((test) => test.flagged);
              return (
                <button
                  key={entry.id}
                  type="button"
                  aria-pressed={entry.id === selected}
                  onClick={() => choose(entry.id)}
                >
                  <span className="guard-list-name">{entry.instance}</span>
                  <span className="guard-list-meta">
                    {entry.agent} · {caught ? "flagged" : "missed"} ·{" "}
                    {entry.flaggedCount.toLocaleString("en-US")} tests listed
                  </span>
                </button>
              );
            })}
          </nav>
          <div className="guard-detail-slot" ref={detailRef}>
            <CaseDetail key={item.id} item={item} />
          </div>
        </div>
      </main>
    </div>
  );
}
