// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

(() => {
  try {
    const theme = localStorage.getItem("cm-theme");
    if (theme === "dark" || theme === "light") {
      document.documentElement.dataset.theme = theme;
      return;
    }
  } catch {}
  // Dark unless the reader has chosen otherwise: people come here from their editor.
  document.documentElement.dataset.theme = "dark";
})();
