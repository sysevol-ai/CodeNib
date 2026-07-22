"use client";

import { useEffect, useRef, useState } from "react";
import { submitFeedback, FEEDBACK_CATEGORIES, type FeedbackCategory } from "@/lib/api";

const MAX_CHARS = 4000;

/**
 * Anonymous site feedback: a header button that opens a small dialog.
 *
 * Nothing identifying is collected or sent — just the message, a category, and
 * which repo/page it came from. The server appends it to a JSONL file; there is
 * no third-party service involved.
 */
export default function FeedbackButton({ repo }: { repo?: string }) {
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState<FeedbackCategory>("suggestion");
  const [message, setMessage] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const [error, setError] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Focus the message box on open, and allow Escape to dismiss.
  useEffect(() => {
    if (!open) return;
    textareaRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  function close() {
    setOpen(false);
    // Reset only after a success, so a failed attempt keeps what was typed.
    if (state === "sent") {
      setMessage("");
      setState("idle");
    }
  }

  async function send() {
    const text = message.trim();
    if (!text || state === "sending") return;
    setState("sending");
    setError("");
    try {
      await submitFeedback({
        message: text,
        category,
        repo,
        page: typeof window !== "undefined" ? window.location.pathname : undefined,
      });
      setState("sent");
    } catch (e) {
      setState("error");
      setError(
        String(e).includes("429")
          ? "Too many submissions just now — please try again in a minute."
          : "Could not send feedback. Please try again."
      );
    }
  }

  return (
    <>
      <button
        className="icon-btn feedback-trigger"
        onClick={() => setOpen(true)}
        title="Send feedback"
        aria-label="Send feedback"
      >
        ✉
      </button>

      {open && (
        <div className="feedback-scrim" onClick={close} role="presentation">
          <div
            className="feedback-dialog"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label="Send feedback"
          >
            {state === "sent" ? (
              <div className="feedback-done">
                <div className="feedback-done-mark">✓</div>
                <p className="feedback-title">Thanks — that&apos;s been recorded.</p>
                <p className="feedback-sub">
                  Feedback is anonymous; we have no way to reply, so please include
                  anything needed to reproduce an issue.
                </p>
                <button className="btn-primary" onClick={close}>
                  Close
                </button>
              </div>
            ) : (
              <>
                <p className="feedback-title">Send feedback</p>
                <p className="feedback-sub">
                  Anonymous — no email, no account, nothing identifying is stored.
                </p>

                <div className="feedback-cats">
                  {FEEDBACK_CATEGORIES.map((c) => (
                    <button
                      key={c.value}
                      className={`feedback-cat ${category === c.value ? "on" : ""}`}
                      onClick={() => setCategory(c.value)}
                      type="button"
                    >
                      {c.label}
                    </button>
                  ))}
                </div>

                <textarea
                  ref={textareaRef}
                  className="feedback-text"
                  value={message}
                  maxLength={MAX_CHARS}
                  onChange={(e) => setMessage(e.target.value)}
                  placeholder="What worked, what didn't, what you expected instead…"
                  rows={6}
                />

                <div className="feedback-foot">
                  <span className="feedback-count">
                    {message.length}/{MAX_CHARS}
                  </span>
                  <div className="feedback-actions">
                    <button className="btn-ghost" onClick={close} type="button">
                      Cancel
                    </button>
                    <button
                      className="btn-primary"
                      onClick={send}
                      disabled={!message.trim() || state === "sending"}
                      type="button"
                    >
                      {state === "sending" ? "Sending…" : "Send"}
                    </button>
                  </div>
                </div>

                {state === "error" && <p className="feedback-error">{error}</p>}
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
