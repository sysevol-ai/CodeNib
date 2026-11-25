import { BookOpen, ChevronRight, Crosshair, Footprints, Network, Sparkles } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export function Crumbs({ parts }: { parts: string[] }) {
  return (
    <nav className="crumbs" aria-label="Where you are">
      {parts.map((p, i) => (
        <span key={`${i}-${p}`} className="crumb">
          {i > 0 && <ChevronRight className="crumb-sep" aria-hidden="true" />}
          <span className={i === parts.length - 1 ? "crumb-here" : undefined}>{p}</span>
        </span>
      ))}
    </nav>
  );
}

export type Mode = "page" | "map" | "impact" | "walkthrough";

export const MODES: { key: Mode; label: string; icon: ReactNode; tip: string }[] = [
  { key: "page", label: "Wiki", icon: <BookOpen />, tip: "Read the page" },
  { key: "map", label: "Map", icon: <Network />, tip: "The repository's areas and the calls between them" },
  { key: "impact", label: "Impact", icon: <Crosshair />, tip: "Every caller a change here reaches" },
  { key: "walkthrough", label: "Walkthrough", icon: <Footprints />, tip: "Follow the call path one function at a time" },
];

/** The page's ways of looking at the same code, as one switcher. */
export function ModeSwitch({ mode, modes, go }: { mode: Mode; modes: Mode[]; go: (m: Mode) => void }) {
  return (
    <div className="modes" role="tablist" aria-label="View this page as">
      {MODES.filter((m) => modes.includes(m.key)).map((m) => (
        <Tooltip key={m.key}>
          <TooltipTrigger asChild>
            <button
              type="button"
              role="tab"
              aria-selected={mode === m.key}
              className={cn("mode", mode === m.key && "is-on")}
              onClick={() => go(m.key)}
            >
              {m.icon}
              {m.label}
            </button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{m.tip}</TooltipContent>
        </Tooltip>
      ))}
    </div>
  );
}

export function Toolbar({ parts, children, onAsk }: { parts: string[]; children?: ReactNode; onAsk?: () => void }) {
  return (
    <header className="toolbar" data-tauri-drag-region>
      <Crumbs parts={parts} />
      <div className="toolbar-tools">
        {children}
        {onAsk && (
          <Button variant="outline" size="sm" onClick={onAsk}>
            <Sparkles />
            Ask
          </Button>
        )}
      </div>
    </header>
  );
}
