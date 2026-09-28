// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react";

/** Let a long identifier wrap after `.`, `_`, `/` or `::` instead of in the
 *  middle of a name (`should_stri` / `p_auth()`). */
export function breakableCode(text: string): ReactNode {
  const parts = text.split(/(?<=[A-Za-z0-9)\]][._/]|[A-Za-z0-9]::)(?=[^._/:])/);
  if (parts.length < 2) return text;
  return parts.flatMap((part, i) => (i === 0 ? [part] : [<wbr key={i} />, part]));
}
