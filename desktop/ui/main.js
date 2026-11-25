// The launcher: pick a repository, watch `codenib wiki` start, open its window.
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const openButton = document.getElementById("open");
const recentSection = document.getElementById("recent-section");
const recentList = document.getElementById("recent");
const opening = document.getElementById("opening");
const openingTitle = document.getElementById("opening-title");
const openingLine = document.getElementById("opening-line");
const errorLine = document.getElementById("error");
const setup = document.getElementById("setup");

let installed = false;
let busy = false;

const folderName = (path) => path.split("/").filter(Boolean).pop() || path;

function showError(message) {
  errorLine.textContent = message;
  errorLine.hidden = !message;
}

async function checkSetup() {
  setup.textContent = "Looking for CodeNib on this Mac…";
  const status = await invoke("setup_status");
  installed = Boolean(status.codenib);
  openButton.disabled = !installed || busy;
  setup.replaceChildren();
  if (installed) {
    const found = document.createElement("span");
    found.textContent = `${status.version || "codenib"} · ${status.codenib}`;
    setup.append(found);
    return;
  }
  const help = document.createElement("p");
  help.style.margin = "0";
  help.append(
    "CodeNib isn't installed on this Mac yet. In Terminal, run ",
    Object.assign(document.createElement("code"), {
      textContent: 'uv tool install "codenib[graph]"',
    }),
    " or ",
    Object.assign(document.createElement("code"), {
      textContent: 'pipx install "codenib[graph]"',
    }),
    ", then check again.",
  );
  const again = Object.assign(document.createElement("button"), {
    type: "button",
    textContent: "Check again",
  });
  again.addEventListener("click", checkSetup);
  setup.append(help, again);
}

async function showRecent() {
  const recent = await invoke("recent_repositories");
  recentSection.hidden = recent.length === 0;
  recentList.replaceChildren(
    ...recent.map((path) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.append(
        Object.assign(document.createElement("span"), {
          className: "name",
          textContent: folderName(path),
        }),
        Object.assign(document.createElement("span"), {
          className: "path",
          textContent: path,
        }),
      );
      button.addEventListener("click", () => openRepository(path));
      item.append(button);
      return item;
    }),
  );
}

async function openRepository(path) {
  if (busy || !installed) return;
  busy = true;
  openButton.disabled = true;
  showError("");
  opening.hidden = false;
  openingTitle.textContent = `Opening ${folderName(path)}`;
  openingLine.textContent = "Starting codenib wiki…";
  try {
    await invoke("open_repository", { path });
    opening.hidden = true;
  } catch (error) {
    showError(String(error));
  } finally {
    busy = false;
    openButton.disabled = !installed;
    showRecent();
  }
}

async function chooseFolder() {
  if (busy || !installed) return;
  const path = await invoke("pick_repository");
  if (path) openRepository(path);
}

openButton.addEventListener("click", chooseFolder);
window.addEventListener("keydown", (event) => {
  if (event.metaKey && event.key.toLowerCase() === "o") {
    event.preventDefault();
    chooseFolder();
  }
});

await listen("codenib://progress", (event) => {
  const line = event.payload.line.trim();
  if (busy && line) openingLine.textContent = line;
});

await checkSetup();
await showRecent();
