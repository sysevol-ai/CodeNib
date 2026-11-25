// A ```mermaid block drawn as a diagram, in the window's own colours. The
// output is sanitized before it reaches the page, as on the Wiki's site.

import mermaid from "mermaid";
import { useEffect, useRef, useState } from "react";

function installSanitizedSvg(container: HTMLElement, svg: string): boolean {
  const parsed = new DOMParser().parseFromString(svg, "image/svg+xml");
  const root = parsed.documentElement;
  if (root.nodeName.toLowerCase() !== "svg" || parsed.querySelector("parsererror")) return false;
  parsed.querySelectorAll("script, iframe, object, embed, foreignObject").forEach((node) => node.remove());
  parsed.querySelectorAll("*").forEach((node) => {
    for (const attribute of Array.from(node.attributes)) {
      const name = attribute.name.toLowerCase();
      if (
        name.startsWith("on") ||
        name === "href" ||
        name === "xlink:href" ||
        name === "src" ||
        name === "formaction" ||
        (name === "style" && /(?:javascript:|vbscript:|data:|expression\s*\(|@import)/i.test(attribute.value))
      ) {
        node.removeAttribute(attribute.name);
      }
    }
  });
  parsed.querySelectorAll("style").forEach((node) => {
    if (/(?:javascript:|vbscript:|data:|expression\s*\(|@import)/i.test(node.textContent || "")) node.remove();
  });
  const installed = document.importNode(root, true);
  // Keep a wide flow at its natural size and let the card scroll, so its
  // labels stay readable instead of shrinking to fit.
  const naturalWidth = Number.parseFloat((installed.getAttribute("viewBox") || "").split(/\s+/)[2] || "");
  if (Number.isFinite(naturalWidth) && naturalWidth > 0) {
    installed.setAttribute("width", String(Math.ceil(naturalWidth)));
    installed.style.maxWidth = "none";
  }
  container.replaceChildren(installed);
  return true;
}

function token(name: string, fallback: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function config(dark: boolean) {
  return {
    startOnLoad: false,
    theme: "base" as const,
    darkMode: dark,
    securityLevel: "strict" as const,
    // HTML labels sit in <foreignObject>, which the sanitizer removes.
    htmlLabels: false,
    flowchart: { htmlLabels: false, wrappingWidth: 360 },
    themeVariables: {
      fontFamily: '"Geist Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
      fontSize: "13px",
      primaryColor: token("--nib-soft", dark ? "#1b2433" : "#eef3fd"),
      primaryBorderColor: token("--nib-line", dark ? "#8ab4f8" : "#1d4fd8"),
      primaryTextColor: token("--ink", dark ? "#e8eaed" : "#16191e"),
      secondaryColor: token("--well", dark ? "#17181b" : "#eef1f5"),
      tertiaryColor: token("--sheet", dark ? "#121315" : "#fbfcfd"),
      lineColor: token("--ink-2", dark ? "#bdc1c6" : "#39414b"),
      textColor: token("--ink-2", dark ? "#bdc1c6" : "#39414b"),
      edgeLabelBackground: token("--sheet", dark ? "#121315" : "#fbfcfd"),
      background: token("--sheet", dark ? "#121315" : "#fbfcfd"),
      clusterBkg: token("--well", dark ? "#17181b" : "#eef1f5"),
      clusterBorder: token("--line", dark ? "#25262a" : "#c6cdd6"),
      noteBkgColor: token("--well", dark ? "#17181b" : "#eef1f5"),
      actorBkg: token("--nib-soft", dark ? "#1b2433" : "#eef3fd"),
      actorBorder: token("--nib-line", dark ? "#8ab4f8" : "#1d4fd8"),
    },
  };
}

export default function Mermaid({ chart }: { chart: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const render = async () => {
      mermaid.initialize(config(document.documentElement.classList.contains("dark")));
      let ok = true;
      try {
        // A failed render leaves an error drawing in the page; check first.
        ok = (await mermaid.parse(chart, { suppressErrors: true })) !== false;
      } catch {
        ok = false;
      }
      if (cancelled) return;
      if (!ok) {
        setFailed(true);
        return;
      }
      try {
        const { svg } = await mermaid.render(`m${Math.random().toString(36).slice(2)}`, chart);
        if (!cancelled && ref.current) setFailed(!installSanitizedSvg(ref.current, svg));
      } catch {
        if (!cancelled) setFailed(true);
      }
    };
    void render();
    // Draw again when the system switches between light and dark.
    const observer = new MutationObserver(() => void render());
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => {
      cancelled = true;
      observer.disconnect();
    };
  }, [chart]);

  if (failed) {
    return (
      <div className="code-card md-code">
        <pre className="code-lines">
          <code>{chart}</code>
        </pre>
      </div>
    );
  }
  return <div className="mermaid-diagram" ref={ref} role="img" aria-label="Diagram" />;
}
