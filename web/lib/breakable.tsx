// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react";

/** Names this short fit a line; breaking one ("n." / "insertChild") only
 *  scatters it. */
const BREAKABLE_FROM = 23;

/** Let a long identifier wrap after `.`, `_`, `/` or `::` instead of in the
 *  middle of a name (`should_stri` / `p_auth()`), and a long camelCase name
 *  between its words; a short one stays whole. */
export function breakableCode(text: string): ReactNode {
  if (text.length < BREAKABLE_FROM) return text;
  const parts = text
    .split(/(?<=[A-Za-z0-9)\]][._/]|[A-Za-z0-9]::)(?=[^._/:])/)
    // A long name with no separator may break between its words
    // ("…PresentOn" / "ConfigChange"), never mid-word ("ConfigCha" / "nge").
    .flatMap((part) => (part.length >= BREAKABLE_FROM ? part.split(/(?<=[a-z0-9])(?=[A-Z])/) : [part]));
  if (parts.length < 2) return text;
  return parts.flatMap((part, i) => (i === 0 ? [part] : [<wbr key={i} />, part]));
}
