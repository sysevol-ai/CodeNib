// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

/** The map's states, taught before a run and shown under the live map during one. */
export default function WikiStateLegend({ folders = false, references = false }: { folders?: boolean; references?: boolean }) {
  return (
    <ul className="wg-states" aria-label="Map legend">
      {folders && (
        <li>
          <i className="is-folder" /> Folder, sized by code files
        </li>
      )}
      <li>
        <i className="is-pending" /> Planned chapter
      </li>
      <li>
        <i className="is-running" /> Being written
      </li>
      <li>
        <i /> Ready to read
      </li>
      {references && (
        <li>
          <i className="is-reference" /> Reference in the code index
        </li>
      )}
    </ul>
  );
}
