import { Check, LoaderCircle, X } from "lucide-react";
import type { Need } from "@/lib/model";

const LABEL: Record<Need, string> = {
  needs: "Needs you",
  working: "Working",
  done: "Done",
  failed: "Failed",
};

/** The only blue in the app is a task that needs you. */
export function StatusMark({ status }: { status: Need }) {
  return (
    <span className={`status-mark is-${status}`} role="img" aria-label={LABEL[status]}>
      {status === "needs" && <span className="status-dot" />}
      {status === "working" && <LoaderCircle className="status-spin" />}
      {status === "done" && <Check />}
      {status === "failed" && <X />}
    </span>
  );
}
