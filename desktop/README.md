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

The app carries its own CodeNib: a relocatable Python with this checkout's
CodeNib installed, so a Mac needs nothing else. Tasks still run on the Claude
Code you have installed and signed in to.

## Try a build

Every pull request that touches `desktop/` builds the app on an Apple silicon
runner. Download `CodeNib-macos-apple-silicon` from the run's artifacts, open
the disk image and drag CodeNib to Applications.

The app is signed ad hoc, not with a Developer ID, so macOS stops it the first
time. Open it once, close the warning, then open System Settings, then
Privacy & Security, and choose **Open Anyway** for CodeNib. The button shows
for a while after the blocked attempt.

`xattr -dr com.apple.quarantine /Applications/CodeNib.app` works only when
Terminal has App Management access (System Settings, then Privacy & Security,
then App Management); without it macOS answers "Operation not permitted".
Alternatively, clear the flag on the download before copying the app:
`xattr -d com.apple.quarantine ~/Downloads/CodeNib*.dmg`.

## Develop

On a Mac with Rust, Node.js 22 and Xcode's command-line tools:

```bash
(cd web && npm ci && npm run build)    # the Wiki frontend CodeNib serves
desktop/scripts/bundle-python.sh       # Python and CodeNib into the app
cd desktop
npm ci
npm run tauri dev      # run the app
npm run build          # build CodeNib.app and the disk image
cargo test --manifest-path src-tauri/Cargo.toml
scripts/smoke-bundle.sh src-tauri/target/release/bundle/macos/CodeNib.app
```

Without the bundled runtime (`tauri dev` before `bundle-python.sh`), the app
uses a `codenib` on your login shell's `PATH` instead.

| Path | Holds |
| --- | --- |
| `ui/` | The launcher window: plain HTML, CSS and JavaScript |
| `src-tauri/src/lib.rs` | Finding `codenib`, running `codenib wiki`, opening and closing Wiki windows |
| `src-tauri/tauri.conf.json` | App name, window, content policy and bundle settings |
| `src-tauri/Info.plist` | Lets the app load the Wiki from `127.0.0.1` |
| `scripts/bundle-python.sh` | Fetches a pinned, checksummed Python and installs this CodeNib into `src-tauri/resources/python/` |
| `scripts/smoke-bundle.sh` | Runs a built app's CodeNib on a small repository and saves a note |

`CODENIB_BIN` points the app at another `codenib`. Otherwise it runs its own,
and without one it looks on your login shell's `PATH`, then in `~/.local/bin`,
`/opt/homebrew/bin` and `/usr/local/bin`.
