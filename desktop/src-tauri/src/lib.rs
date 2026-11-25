//! CodeNib for macOS.
//!
//! The app opens a repository the person picks, runs `codenib wiki` for it in
//! the background, and shows the Wiki that command serves in a window of its
//! own. Closing the window stops that command. Nothing leaves the machine: the
//! Wiki is served on 127.0.0.1 and tasks run on the person's own Claude Code.

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Read};
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{mpsc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{
    AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_dialog::DialogExt;

/// The line `codenib wiki` prints once both of its services answer.
const READY: &str = "CodeNib Wiki is ready: ";
const RECENT_LIMIT: usize = 8;

static NEXT_WINDOW: AtomicUsize = AtomicUsize::new(1);

/// One running `codenib wiki` and the window that shows it.
struct Wiki {
    label: String,
    child: Child,
}

/// Running Wikis, keyed by the repository folder as the person picked it.
#[derive(Default)]
struct Wikis(Mutex<HashMap<String, Wiki>>);

#[derive(Clone, Serialize)]
struct Progress {
    repo: String,
    line: String,
}

#[derive(Serialize)]
struct Setup {
    /// Where `codenib` was found, if it was.
    codenib: Option<String>,
    /// What `codenib --version` printed.
    version: Option<String>,
}

/// The address in a `codenib wiki` ready line, when the line is one.
fn ready_url(line: &str) -> Option<&str> {
    let start = line.find(READY)? + READY.len();
    let url = line[start..].trim();
    let local = url.starts_with("http://127.0.0.1:") || url.starts_with("http://localhost:");
    local.then_some(url)
}

/// The search path a terminal would have.
///
/// An app opened from the Dock gets a minimal PATH, without the folders that
/// `uv`, `pipx`, Homebrew and Claude Code install into, so the login shell's
/// PATH comes first and the usual install folders are added after it.
fn search_path() -> String {
    let home = std::env::var("HOME").unwrap_or_default();
    let mut folders: Vec<String> = Vec::new();
    if let Some(login) = login_shell_path() {
        folders.extend(login.split(':').map(str::to_owned));
    }
    if let Ok(own) = std::env::var("PATH") {
        folders.extend(own.split(':').map(str::to_owned));
    }
    folders.extend([
        format!("{home}/.local/bin"),
        "/opt/homebrew/bin".to_owned(),
        "/usr/local/bin".to_owned(),
        "/usr/bin".to_owned(),
        "/bin".to_owned(),
    ]);
    let mut seen = HashSet::new();
    folders.retain(|folder| !folder.is_empty() && seen.insert(folder.clone()));
    folders.join(":")
}

fn login_shell_path() -> Option<String> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".to_owned());
    let output = Command::new(shell)
        .args(["-l", "-c", "printf %s \"$PATH\""])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8(output.stdout).ok()?;
    let text = text.trim();
    (!text.is_empty()).then(|| text.to_owned())
}

fn is_executable(path: &Path) -> bool {
    path.metadata()
        .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

/// `CODENIB_BIN` when it is set, otherwise the first `codenib` on the path.
fn find_codenib(search: &str) -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("CODENIB_BIN") {
        let explicit = PathBuf::from(explicit);
        if is_executable(&explicit) {
            return Some(explicit);
        }
    }
    search
        .split(':')
        .map(|folder| Path::new(folder).join("codenib"))
        .find(|candidate| is_executable(candidate))
}

fn free_port() -> Result<u16, String> {
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|listener| listener.local_addr())
        .map(|address| address.port())
        .map_err(|error| format!("no free local port: {error}"))
}

/// Ask `codenib wiki` and everything it started to stop, then make sure.
fn stop(mut child: Child) {
    let group = child.id() as libc::pid_t;
    // SAFETY: the child leads its own process group (`process_group(0)`).
    unsafe { libc::killpg(group, libc::SIGINT) };
    let deadline = Instant::now() + Duration::from_secs(8);
    while Instant::now() < deadline {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        thread::sleep(Duration::from_millis(100));
    }
    // SAFETY: as above.
    unsafe { libc::killpg(group, libc::SIGKILL) };
    let _ = child.wait();
}

/// Forward each output line to the launcher and report the ready address.
fn pump<R: Read + Send + 'static>(
    app: AppHandle,
    repo: String,
    stream: Option<R>,
    ready: mpsc::Sender<String>,
) {
    let Some(stream) = stream else { return };
    thread::spawn(move || {
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            if let Some(url) = ready_url(&line) {
                let _ = ready.send(url.to_owned());
            }
            let _ = app.emit(
                "codenib://progress",
                Progress {
                    repo: repo.clone(),
                    line,
                },
            );
        }
    });
}

fn recent_file(app: &AppHandle) -> Option<PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|folder| folder.join("recent.json"))
}

fn load_recent(app: &AppHandle) -> Vec<String> {
    recent_file(app)
        .and_then(|file| std::fs::read(file).ok())
        .and_then(|bytes| serde_json::from_slice::<Vec<String>>(&bytes).ok())
        .unwrap_or_default()
}

fn remember(app: &AppHandle, repo: &str) {
    let mut recent = load_recent(app);
    recent.retain(|known| known != repo);
    recent.insert(0, repo.to_owned());
    recent.truncate(RECENT_LIMIT);
    if let Some(file) = recent_file(app) {
        if let Some(folder) = file.parent() {
            let _ = std::fs::create_dir_all(folder);
        }
        if let Ok(bytes) = serde_json::to_vec_pretty(&recent) {
            let _ = std::fs::write(file, bytes);
        }
    }
}

#[tauri::command]
async fn setup_status() -> Setup {
    let codenib = find_codenib(&search_path());
    let version = codenib.as_ref().and_then(|path| {
        let output = Command::new(path).arg("--version").output().ok()?;
        let text = String::from_utf8(output.stdout).ok()?;
        let text = text.trim();
        (output.status.success() && !text.is_empty()).then(|| text.to_owned())
    });
    Setup {
        codenib: codenib.map(|path| path.display().to_string()),
        version,
    }
}

#[tauri::command]
fn recent_repositories(app: AppHandle) -> Vec<String> {
    load_recent(&app)
        .into_iter()
        .filter(|repo| Path::new(repo).is_dir())
        .collect()
}

#[tauri::command]
async fn pick_repository(app: AppHandle) -> Option<String> {
    app.dialog()
        .file()
        .set_title("Open a repository")
        .blocking_pick_folder()
        .and_then(|picked| picked.into_path().ok())
        .map(|path| path.display().to_string())
}

#[tauri::command]
async fn open_repository(app: AppHandle, path: String) -> Result<(), String> {
    let repo = PathBuf::from(&path);
    if !repo.is_dir() {
        return Err(format!("{path} is not a folder"));
    }
    // The same repository again brings its window forward.
    {
        let wikis = app.state::<Wikis>();
        let running = wikis.0.lock().map_err(|_| "app state is unavailable")?;
        if let Some(wiki) = running.get(&path) {
            if let Some(window) = app.get_webview_window(&wiki.label) {
                let _ = window.unminimize();
                let _ = window.set_focus();
                return Ok(());
            }
        }
    }

    let search = search_path();
    let codenib = find_codenib(&search).ok_or("CodeNib is not installed on this Mac")?;
    let (port, api_port) = (free_port()?, free_port()?);
    let mut child = Command::new(&codenib)
        .arg("wiki")
        .arg(&repo)
        .args(["--no-open", "--port", &port.to_string()])
        .args(["--api-port", &api_port.to_string()])
        .current_dir(&repo)
        .env("PATH", &search)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
        .map_err(|error| format!("could not start codenib: {error}"))?;

    let (ready, address) = mpsc::channel();
    pump(app.clone(), path.clone(), child.stdout.take(), ready.clone());
    pump(app.clone(), path.clone(), child.stderr.take(), ready);
    remember(&app, &path);

    // Building a new index can take minutes; wait as long as the command runs.
    let url = loop {
        match address.recv_timeout(Duration::from_millis(250)) {
            Ok(url) => break url,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if let Ok(Some(status)) = child.try_wait() {
                    return Err(format!(
                        "codenib wiki stopped ({status}) before the Wiki was ready"
                    ));
                }
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                let status = child.wait().map_err(|error| error.to_string())?;
                return Err(format!(
                    "codenib wiki stopped ({status}) before the Wiki was ready"
                ));
            }
        }
    };

    let label = format!("wiki-{}", NEXT_WINDOW.fetch_add(1, Ordering::SeqCst));
    let title = repo
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.clone());
    let address = url
        .parse()
        .map_err(|error| format!("unexpected Wiki address {url}: {error}"))?;
    if let Err(error) = WebviewWindowBuilder::new(&app, &label, WebviewUrl::External(address))
        .title(title)
        .inner_size(1320.0, 880.0)
        .min_inner_size(720.0, 480.0)
        .build()
    {
        stop(child);
        return Err(format!("could not open the Wiki window: {error}"));
    }
    app.state::<Wikis>()
        .0
        .lock()
        .map_err(|_| "app state is unavailable")?
        .insert(path, Wiki { label, child });
    Ok(())
}

/// Stop the Wiki whose window just closed.
fn window_closed(app: &AppHandle, label: &str) {
    let wiki = {
        let wikis = app.state::<Wikis>();
        let Ok(mut running) = wikis.0.lock() else {
            return;
        };
        let key = running
            .iter()
            .find(|(_, wiki)| wiki.label == label)
            .map(|(key, _)| key.clone());
        key.and_then(|key| running.remove(&key))
    };
    if let Some(wiki) = wiki {
        thread::spawn(move || stop(wiki.child));
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(Wikis::default())
        .invoke_handler(tauri::generate_handler![
            setup_status,
            recent_repositories,
            pick_repository,
            open_repository
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::Destroyed = event {
                window_closed(window.app_handle(), window.label());
            }
        })
        .build(tauri::generate_context!())
        .expect("CodeNib could not start")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                let running: Vec<Wiki> = app
                    .state::<Wikis>()
                    .0
                    .lock()
                    .map(|mut running| running.drain().map(|(_, wiki)| wiki).collect())
                    .unwrap_or_default();
                for wiki in running {
                    stop(wiki.child);
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_address_from_the_ready_line() {
        let line = "CodeNib Wiki is ready: http://127.0.0.1:3190/repo?session=abc_DEF-1";
        assert_eq!(
            ready_url(line),
            Some("http://127.0.0.1:3190/repo?session=abc_DEF-1")
        );
        assert_eq!(
            ready_url("CodeNib Wiki is ready: http://localhost:3000/x "),
            Some("http://localhost:3000/x")
        );
    }

    #[test]
    fn ignores_other_lines_and_remote_addresses() {
        assert_eq!(ready_url("Views:"), None);
        assert_eq!(
            ready_url("CodeNib Wiki is ready: https://example.com/repo"),
            None
        );
    }

    #[test]
    fn a_free_port_can_be_bound() {
        let first = free_port().unwrap();
        assert!(first > 0);
        TcpListener::bind(("127.0.0.1", first)).unwrap();
    }
}
