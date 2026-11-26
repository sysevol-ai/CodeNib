<!--
SPDX-FileCopyrightText: 2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib for macOS

A Mac app for reading your repositories' Wikis and working on them with your
own Claude Code. One window lists every repository you add, with its README
and docs, its Wiki pages and its tasks; notes and Claude Code's answers sit in
the margin beside the words they are about. For each repository the app runs
`codenib wiki` in the background, started when you first open it and stopped
when the app quits. Everything stays on your Mac: the services listen on
`127.0.0.1`, the window reaches them only through the app (which holds their
session keys), notes and tasks are files under `.codenib/` in the repository,
and tasks run on your own Claude Code sign-in.

The app carries its own CodeNib: a relocatable Python with this checkout's
CodeNib installed, so a Mac needs nothing else. Tasks still run on the Claude
Code you have installed and signed in to.

## Try a build

Every pull request that touches `desktop/` builds the app on an Apple silicon
runner. Download `CodeNib-macos-apple-silicon` from the run's artifacts, open
the disk image and drag CodeNib to Applications.

A pull request's build is signed ad hoc, not with a Developer ID, so macOS
stops it the first time. Open it once, close the warning, then open System
Settings, then Privacy & Security, and choose **Open Anyway** for CodeNib. The
button shows for a while after the blocked attempt. Builds from `main` open
without this step once release signing is set up (see
[Sign and notarize](#sign-and-notarize)).

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
npm --prefix app ci    # the window's own frontend
npm run tauri dev      # run the app
npm run build          # build CodeNib.app and the disk image
cargo test --manifest-path src-tauri/Cargo.toml
scripts/smoke-bundle.sh src-tauri/target/release/bundle/macos/CodeNib.app
```

Without the bundled runtime (`tauri dev` before `bundle-python.sh`), the app
uses a `codenib` on your login shell's `PATH` instead.

| Path | Holds |
| --- | --- |
| `app/` | The window: React, Tailwind and shadcn/ui in the Paper and Ink look |
| `src-tauri/src/lib.rs` | Saved repositories, one `codenib wiki` per repository, and the `api` command that forwards the window's requests |
| `src-tauri/tauri.conf.json` | App name, window, content policy and bundle settings |
| `src-tauri/Info.plist` | Lets the app load the Wiki from `127.0.0.1` |
| `scripts/bundle-python.sh` | Fetches a pinned, checksummed Python and installs this CodeNib into `src-tauri/resources/python/` |
| `scripts/smoke-bundle.sh` | Runs a built app's CodeNib on a small repository: the Wiki, the symbol graph and a note |
| `scripts/check-notarizable.sh` | Checks every binary in a built app against what notarization asks for |
| `src-tauri/entitlements.plist` | The one exception the app needs from the hardened runtime: Apple events, to open Terminal |

`CODENIB_BIN` points the app at another `codenib`. Otherwise it runs its own,
and without one it looks on your login shell's `PATH`, then in `~/.local/bin`,
`/opt/homebrew/bin` and `/usr/local/bin`.

To check the window without a Mac, run `codenib wiki <repo> --no-open --api-port <port>`,
build the frontend (`npm --prefix app run build`), start
`python app/tools/preview.py 8735` and run `node app/tools/check.mjs <out>
'[{"path":"<repo>","name":"<name>","port":<port>,"session":"<key>"}]'`:
`app/tools/bridge-mock.js` stands in for the app process and forwards `api`
calls to that service, and the check opens each step in Chromium and WebKit,
light and dark.

## Sign and notarize

A build signed with a Developer ID and notarized by Apple opens with a
double-click: no warning, no Open Anyway. Every build already signs each
binary with the hardened runtime that notarization requires, and
`scripts/check-notarizable.sh` checks the result, so turning release signing
on only needs an Apple Developer Program membership and five secrets.

1. Enroll in the [Apple Developer Program](https://developer.apple.com/programs/enroll/).
   An individual membership is enough; the signature then names you.
2. Create a **Developer ID Application** certificate: in Xcode, open
   Settings, then Accounts, then Manage Certificates, and add one. In Keychain
   Access, export it with its private key as a `.p12` file with a password.
3. Create an app-specific password for notarization at
   [account.apple.com](https://account.apple.com), under Sign-In and Security.
4. Note your Team ID, shown under Membership details at
   [developer.apple.com/account](https://developer.apple.com/account).
5. Add these repository secrets (Settings, then Secrets and variables, then
   Actions):

   | Secret | Value |
   | --- | --- |
   | `APPLE_CERTIFICATE` | The `.p12` file as base64: `base64 -i DeveloperID.p12 \| pbcopy` |
   | `APPLE_CERTIFICATE_PASSWORD` | The `.p12` file's password |
   | `APPLE_ID` | Your Apple Account email |
   | `APPLE_PASSWORD` | The app-specific password |
   | `APPLE_TEAM_ID` | Your Team ID |

6. Run the **Desktop app** workflow on `main` (Actions, then Desktop app, then
   Run workflow). It signs the bundled Python and the app with your Developer
   ID, has Apple notarize the app and staples its ticket, builds the disk
   image around it, notarizes and staples that too, and checks both with
   `spctl`. Pull requests keep building ad hoc.

To sign on your own Mac, pass the identity to both steps:
`CODENIB_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
scripts/bundle-python.sh`, then `APPLE_SIGNING_IDENTITY="…" npm run build`, and
check the result with `scripts/check-notarizable.sh --release
src-tauri/target/release/bundle/macos/CodeNib.app`.

## Publish a release

A release puts the disk image on a GitHub release page, where anyone can
download it without a GitHub account. Set the new version in
`src-tauri/tauri.conf.json` and `src-tauri/Cargo.toml`, merge that to `main`,
then tag the merge and push the tag:

```bash
git tag desktop-v0.1.0 && git push origin desktop-v0.1.0
```

The **Desktop app** workflow checks that the tag names that version, builds,
signs and notarizes the app (with the secrets above), and creates the release
**CodeNib for macOS 0.1.0** with `CodeNib-0.1.0-macos-apple-silicon.dmg` and
its checksum. A desktop release is never marked as the repository's latest
release, which stays the Python package's. Without the Apple secrets the build
is signed ad hoc, and the release is marked as a pre-release that says how to
open it.
