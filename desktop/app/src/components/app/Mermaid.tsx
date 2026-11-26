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
  const installed = document.importNode(root, true) as unknown as SVGSVGElement;
  // As Notion does: at its natural size when it fits, scaled down to the
  // card's width when it does not, and centred. A floor keeps labels legible;
  // past it the card scrolls instead of shrinking further.
  const naturalWidth = Number.parseFloat((installed.getAttribute("viewBox") || "").split(/\s+/)[2] || "");
  if (Number.isFinite(naturalWidth) && naturalWidth > 0) {
    installed.removeAttribute("height");
    installed.setAttribute("width", "100%");
    installed.style.maxWidth = `${Math.ceil(naturalWidth)}px`;
    installed.style.minWidth = `${Math.min(Math.ceil(naturalWidth), Math.ceil(naturalWidth * 0.62))}px`;
    installed.style.height = "auto";
  }
  container.replaceChildren(installed);
  return true;
}

function token(name: string, fallback: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

/** Notion-like: the page's sans-serif type, soft neutral boxes with rounded
 *  corners, quiet grey lines, labels that wrap instead of crossing arrows. */
function config(dark: boolean) {
  const sheet = token("--sheet", dark ? "#121315" : "#fbfcfd");
  return {
    startOnLoad: false,
    theme: "base" as const,
    darkMode: dark,
    securityLevel: "strict" as const,
    // HTML labels sit in <foreignObject>, which the sanitizer removes.
    htmlLabels: false,
    flowchart: {
      htmlLabels: false,
      useMaxWidth: true,
      // Wide enough for a qualified name such as
      // SessionRedirectMixin.resolve_redirects() on one line.
      wrappingWidth: 330,
      nodeSpacing: 36,
      rankSpacing: 56,
      padding: 14,
      curve: "basis" as const,
    },
    sequence: { useMaxWidth: true, mirrorActors: false },
    themeVariables: {
      fontFamily: '-apple-system, BlinkMacSystemFont, "Geist", "Segoe UI", sans-serif',
      fontSize: "14px",
      primaryColor: dark ? "#2a2c30" : "#f4f5f7",
      primaryBorderColor: dark ? "#3a3d43" : "#dcdfe4",
      primaryTextColor: dark ? "#e8eaed" : "#22262c",
      secondaryColor: dark ? "#23252a" : "#eef1f5",
      tertiaryColor: sheet,
      lineColor: dark ? "#7c828c" : "#9aa1ab",
      textColor: dark ? "#c4c8ce" : "#4a515b",
      edgeLabelBackground: sheet,
      background: sheet,
      clusterBkg: dark ? "#1c1e22" : "#f8f9fa",
      clusterBorder: dark ? "#3a3d43" : "#dcdfe4",
      noteBkgColor: dark ? "#2d2a1e" : "#fbf3db",
      noteBorderColor: dark ? "#5c5530" : "#ecd9a0",
      actorBkg: dark ? "#2a2c30" : "#f4f5f7",
      actorBorder: dark ? "#3a3d43" : "#dcdfe4",
    },
  };
}

const SIDEWAYS = /^(\s*(?:flowchart|graph)\s+)(LR|RL)\b/;

/** Node labels without the file path in front of a symbol: the flow is
 *  about calls, and "src/requests/auth.py:" breaks the name across lines. */
export function tidyLabels(chart: string): string {
  return chart.replace(/(^|[\["(\s|>])(?:[\w.-]+\/)*[\w-]+\.(?:py|pyi|go|rs|ts|tsx|js|jsx|mjs|java|kt|rb|php|cs|cpp|cc|cxx|hpp|h|c):(?=[A-Za-z_])/g, "$1");
}

/** The same flow drawn top to bottom. */
export function topDown(chart: string): string {
  return chart.replace(SIDEWAYS, "$1TD");
}

export default function Mermaid({ chart: source }: { chart: string }) {
  // A sideways flow wider than the text column is drawn again top to bottom,
  // where it fits without scrolling or shrinking its labels.
  const [turned, setTurned] = useState(false);
  useEffect(() => setTurned(false), [source]);
  const tidy = tidyLabels(source);
  const chart = turned ? topDown(tidy) : tidy;
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
        if (!cancelled && ref.current) {
          const ok = installSanitizedSvg(ref.current, svg);
          setFailed(!ok);
          const natural = Number.parseFloat(
            (ref.current.querySelector("svg")?.getAttribute("viewBox") || "").split(/\s+/)[2] || "",
          );
          const room = ref.current.clientWidth - 40;
          if (ok && !turned && SIDEWAYS.test(source) && natural > room * 1.1) setTurned(true);
        }
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
  }, [chart, source, turned]);


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
