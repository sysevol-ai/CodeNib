<!--
SPDX-FileCopyrightText: 2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib for macOS

A Mac app for reading a repository's Wiki and working on it with your own
Claude Code. Pick a repository folder; the app runs `codenib wiki` for it in
the background and shows the Wiki in its own window. Closing the window stops
that service. Everything stays on your Mac: the Wiki is served on
`127.0.0.1`, notes and tasks are files under `.codenib/` in the repository,
and tasks run on your own Claude Code sign-in.

This first version needs CodeNib installed on the Mac:

```bash
uv tool install "codenib[graph]"     # or: pipx install "codenib[graph]"
```

## Try a build

Every pull request that touches `desktop/` builds the app on an Apple silicon
runner. Download `CodeNib-macos-apple-silicon` from the run's artifacts, open
the disk image and drag CodeNib to Applications.

The app is signed ad hoc, not with a Developer ID, so macOS stops it the first
time. Open System Settings, then Privacy & Security, and choose **Open
Anyway** for CodeNib. Alternatively, run
`xattr -dr com.apple.quarantine /Applications/CodeNib.app` once.

## Develop

On a Mac with Rust, Node.js 22 and Xcode's command-line tools:

```bash
cd desktop
npm ci
npm run tauri dev      # run the app
npm run build          # build CodeNib.app and the disk image
cargo test --manifest-path src-tauri/Cargo.toml
```

| Path | Holds |
| --- | --- |
| `ui/` | The launcher window: plain HTML, CSS and JavaScript |
| `src-tauri/src/lib.rs` | Finding `codenib`, running `codenib wiki`, opening and closing Wiki windows |
| `src-tauri/tauri.conf.json` | App name, window, content policy and bundle settings |
| `src-tauri/Info.plist` | Lets the app load the Wiki from `127.0.0.1` |

The app looks for `codenib` on your login shell's `PATH`, then in
`~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`. Set `CODENIB_BIN` to
use another one.
