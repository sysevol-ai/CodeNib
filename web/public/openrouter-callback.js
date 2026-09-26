// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

(() => {
  const params = new URLSearchParams(location.search);
  history.replaceState(null, "", location.pathname);
  const nonce = params.get("attempt");
  const code = params.get("code");
  const denied = params.has("error");
  const status = document.getElementById("status");
  if (
    !nonce ||
    !/^[A-Za-z0-9_-]{43}$/.test(nonce) ||
    params.getAll("attempt").length !== 1 ||
    (!denied &&
      (!code ||
        !/^[\x21-\x7e]{1,2048}$/.test(code) ||
        params.getAll("code").length !== 1)) ||
    typeof BroadcastChannel === "undefined"
  ) {
    status.textContent =
      "This authorization cannot be used. Return to CodeNib and connect again.";
    return;
  }
  const channel = new BroadcastChannel(`codenib-openrouter:${nonce}`);
  const send = () =>
    channel.postMessage({
      nonce,
      code: denied ? undefined : code,
      error: denied,
    });
  const retry = setInterval(send, 250);
  const timeout = setTimeout(() => {
    clearInterval(retry);
    channel.close();
    status.textContent =
      "Your original tab is unavailable. Return to CodeNib and connect again.";
  }, 10000);
  channel.onmessage = (event) => {
    if (event.data?.nonce !== nonce || event.data?.received !== true) return;
    clearInterval(retry);
    clearTimeout(timeout);
    channel.close();
    status.textContent = "Authorization received. You can close this tab.";
    window.close();
  };
  send();
})();
