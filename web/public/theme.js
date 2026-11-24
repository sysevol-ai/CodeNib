// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

(() => {
  let theme = "dark";
  try {
    const chosen = localStorage.getItem("cm-theme");
    if (chosen === "dark" || chosen === "light") theme = chosen;
  } catch {}
  // Dark unless the reader has chosen otherwise: people come here from their editor.
  document.documentElement.dataset.theme = theme;
  // The installed app's title bar takes the page colour of the theme.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "light" ? "#ffffff" : "#121315");
})();
