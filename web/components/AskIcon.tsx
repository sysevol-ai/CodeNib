// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

/** A question in a speech bubble, drawn in the header icons' 2px stroke. */
export default function AskIcon({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M4 4.5h16a1 1 0 0 1 1 1V16a1 1 0 0 1-1 1h-8.5L7 20.5V17H4a1 1 0 0 1-1-1V5.5a1 1 0 0 1 1-1z" />
      <path d="M9.6 8.9a2.4 2.4 0 1 1 3.4 2.2c-.6.3-1 .8-1 1.4" />
      <path d="M12 14.8h.01" />
    </svg>
  );
}
