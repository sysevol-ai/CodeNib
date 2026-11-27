// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react";

/** Names this short fit a line; breaking one ("n." / "insertChild") only
 *  scatters it. */
const BREAKABLE_FROM = 23;

/** About the characters of code a phone's reading line holds; a name part
 *  shorter than this never needs splitting between its words. */
const CAMEL_FROM = 34;

/** Let a long identifier wrap after `.`, `_`, `/` or `::` instead of in the
 *  middle of a name (`should_stri` / `p_auth()`), and a long camelCase name
 *  between its words; a short one stays whole. */
export function breakableCode(text: string): ReactNode {
  if (text.length < BREAKABLE_FROM) return text;
  // Text with spaces or quotes (a call with its arguments) breaks at its
  // spaces; only a bare name is split between its words.
  const bare = !/[\s"'`]/.test(text);
  const parts = text
    // After a separator; a dot may also break before a private name
    // ("OneHotEncoder." / "_compute_drop_idx").
    .split(/(?<=[A-Za-z0-9)\]][._/]|[A-Za-z0-9]::)(?=[^._/:])|(?<=[A-Za-z0-9)\]]\.)(?=_[A-Za-z])/)
    // A name too long for a phone's line may break between its words
    // ("…PresentOn" / "ConfigChange") rather than mid-word ("ConfigCha" /
    // "nge"); a shorter one moves to the next line whole.
    .flatMap((part) => (bare && part.length >= CAMEL_FROM ? part.split(/(?<=[a-z0-9])(?=[A-Z])/) : [part]));
  // A piece of three characters or fewer ("(n_") does not stand alone on a
  // line; it stays with the next.
  for (let i = parts.length - 2; i >= 0; i--) {
    if (parts[i].length < 4) parts.splice(i, 2, parts[i] + parts[i + 1]);
  }
  // Nor does a tiny tail ("Y)"): it stays with the piece before it.
  if (parts.length > 1 && parts[parts.length - 1].length < 4) parts.splice(-2, 2, parts[parts.length - 2] + parts[parts.length - 1]);
  if (parts.length < 2) return text;
  return parts.flatMap((part, i) => (i === 0 ? [part] : [<wbr key={i} />, part]));
}
