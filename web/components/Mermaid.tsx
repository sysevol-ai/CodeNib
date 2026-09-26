"use client";

import { useEffect, useRef, useState } from "react";
import mermaid from "mermaid";

function installSanitizedSvg(container: HTMLElement, svg: string): boolean {
  const parsed = new DOMParser().parseFromString(svg, "image/svg+xml");
  const root = parsed.documentElement;
  if (
    root.nodeName.toLowerCase() !== "svg" ||
    parsed.querySelector("parsererror")
  ) {
    return false;
  }

  parsed
    .querySelectorAll("script, iframe, object, embed, foreignObject")
    .forEach((node) => node.remove());
  parsed.querySelectorAll("*").forEach((node) => {
    for (const attribute of Array.from(node.attributes)) {
      const name = attribute.name.toLowerCase();
      const value = attribute.value;
      if (
        name.startsWith("on") ||
        name === "href" ||
        name === "xlink:href" ||
        name === "src" ||
        name === "formaction" ||
        (name === "style" &&
          /(?:javascript:|vbscript:|data:|expression\s*\(|@import)/i.test(value))
      ) {
        node.removeAttribute(attribute.name);
      }
    }
  });
  parsed.querySelectorAll("style").forEach((node) => {
    if (
      /(?:javascript:|vbscript:|data:|expression\s*\(|@import)/i.test(
        node.textContent || "",
      )
    ) {
      node.remove();
    }
  });

  const installed = document.importNode(root, true);
  // Mermaid emits width="100%" with a viewBox, so a wide left-to-right flow
  // is scaled down to fit and its labels become unreadable. Keep the
  // diagram at its natural size and let the container scroll instead.
  const viewBox = (installed.getAttribute("viewBox") || "").split(/\s+/);
  const naturalWidth = Number.parseFloat(viewBox[2] || "");
  if (Number.isFinite(naturalWidth) && naturalWidth > 0) {
    installed.setAttribute("width", String(Math.ceil(naturalWidth)));
    installed.style.maxWidth = "none";
  }
  container.replaceChildren(installed);
  return true;
}

/** Site tokens for the few flows that still draw as a diagram, so they
 *  share the page's palette and type instead of Mermaid's grey default. */
function themeVariables(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const css = getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) =>
    css.getPropertyValue(name).trim() || fallback;
  return {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: "13px",
    primaryColor: token("--accent-soft", "#eff6ff"),
    primaryBorderColor: token("--accent-border", "#bfdbfe"),
    primaryTextColor: token("--text-primary", "#0f172a"),
    lineColor: token("--accent", "#2563eb"),
    textColor: token("--text-secondary", "#475569"),
    edgeLabelBackground: token("--bg-surface", "#ffffff"),
    background: token("--bg-surface", "#ffffff"),
  };
}

export function mermaidConfig(dark: boolean) {
  return {
    startOnLoad: false,
    theme: "base" as const,
    darkMode: dark,
    themeVariables: themeVariables(),
    securityLevel: "strict" as const,
    // Mermaid 11 reads the top-level flag; the flowchart-scoped one alone
    // still emits HTML labels inside <foreignObject>, which the sanitizer
    // above deletes, leaving every node box empty.
    htmlLabels: false,
    // Symbol names are single tokens; wrapping splits them mid-name.
    flowchart: { htmlLabels: false, wrappingWidth: 360 },
  };
}

export default function Mermaid({ chart }: { chart: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const render = async () => {
      const dark = document.documentElement.dataset.theme === "dark";
      mermaid.initialize(mermaidConfig(dark));
      // Validate first: a failed mermaid.render() appends an error graphic to
      // the DOM as a side effect, so on invalid charts we fall back instead.
      let ok = true;
      try {
        ok = (await mermaid.parse(chart, { suppressErrors: true })) !== false;
      } catch {
        ok = false;
      }
      if (cancelled) return;
      if (!ok) {
        setErr("invalid");
        return;
      }
      setErr(null);
      try {
        const id = "m" + Math.random().toString(36).slice(2);
        const { svg } = await mermaid.render(id, chart);
        if (
          !cancelled &&
          ref.current &&
          !installSanitizedSvg(ref.current, svg)
        ) {
          setErr("unsafe diagram output");
        }
      } catch (e) {
        if (!cancelled) setErr(String(e));
      }
    };
    render();
    // Re-render when the theme toggles.
    const obs = new MutationObserver(render);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      cancelled = true;
      obs.disconnect();
    };
  }, [chart]);

  if (err) {
    return (
      <pre className="mermaid-error" title={err}>
        {chart}
      </pre>
    );
  }
  return <div className="mermaid-diagram" ref={ref} aria-label="diagram" />;
}
