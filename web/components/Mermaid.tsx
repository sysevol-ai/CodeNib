"use client";

import { useEffect, useRef, useState } from "react";
import mermaid from "mermaid";

export default function Mermaid({ chart }: { chart: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const render = async () => {
      const dark = document.documentElement.dataset.theme === "dark";
      mermaid.initialize({
        startOnLoad: false,
        theme: dark ? "dark" : "neutral",
        securityLevel: "loose",
      });
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
        if (!cancelled && ref.current) ref.current.innerHTML = svg;
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
