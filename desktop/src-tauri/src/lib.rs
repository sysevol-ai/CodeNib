//! CodeNib for macOS.
//!
//! One window shows every repository the person added. For each repository
//! the app runs `codenib wiki` in the background, started when the repository
//! is first opened and stopped when the app quits. The window never talks to
//! those services itself: the `api` command forwards its requests to the
//! repository's service on 127.0.0.1 with the session key, so the key stays in
//! this process and the page needs no cross-origin access. Nothing leaves the
//! machine; tasks run on the person's own Claude Code.

mod accounts;
mod github;

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Read};
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager, RunEvent};
use tauri_plugin_dialog::DialogExt;

/// The line `codenib wiki` prints once both of its services answer.
const READY: &str = "CodeNib Wiki is ready: ";
/// How long one forwarded request may take. Opening a page that is not
/// written yet, or writing one again, waits for the person's agent and for
/// pages written before it: 87 s of model calls took over 15 minutes behind
/// the background writer, so five minutes reported a failure that was not one.
const API_TIMEOUT: Duration = Duration::from_secs(20 * 60);
/// The last lines of a starting service, kept to explain a failure.
const TAIL: usize = 12;

/// A repository the person added, as it is saved between launches.
#[derive(Clone, Serialize, Deserialize)]
struct RepoConfig {
    path: String,
    /// Build the symbol graph and map (`--preset graph`).
    #[serde(default = "yes")]
    analysis: bool,
    /// Have Claude Code write the pages. Kept for repositories saved before
    /// `writer`; it means `writer: "claude-code"`.
    #[serde(default, skip_serializing)]
    generate: bool,
    /// The agent that writes the pages (`--writer`): "claude-code" or "codex".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    writer: Option<String>,
    /// The GitHub repository the folder was cloned from, as `owner/repo`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    remote: Option<String>,
}

impl RepoConfig {
    fn writer(&self) -> Option<&str> {
        match self.writer.as_deref() {
            Some(w) if accounts::Agent::parse(w).is_some() => Some(w),
            Some(_) => None,
            None if self.generate => Some("claude-code"),
            None => None,
        }
    }
}

fn yes() -> bool {
    true
}

#[derive(Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
enum Status {
    Stopped,
    Starting,
    Ready,
    Failed,
}

/// The service behind one repository.
struct Backend {
    status: Status,
    child: Option<Child>,
    api_port: u16,
    session: String,
    error: String,
    lines: Vec<String>,
}

impl Backend {
    fn stopped() -> Self {
        Backend {
            status: Status::Stopped,
            child: None,
            api_port: 0,
            session: String::new(),
            error: String::new(),
            lines: Vec::new(),
        }
    }
}

#[derive(Default)]
struct State {
    repos: Mutex<Vec<RepoConfig>>,
    backends: Mutex<HashMap<String, Backend>>,
    github: github::Session,
    /// Set to stop a device sign-in that is waiting for the person.
    device_cancel: AtomicBool,
}

/// What the window knows about one repository.
#[derive(Clone, Serialize)]
struct RepoView {
    path: String,
    name: String,
    analysis: bool,
    generate: bool,
    writer: Option<String>,
    remote: Option<String>,
    status: Status,
    error: String,
    /// The latest line the service printed while starting.
    line: String,
}

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
    /// True when the app runs the CodeNib it ships inside itself.
    bundled: bool,
}

/// How to run CodeNib: the Python inside the app, or a `codenib` command.
struct Runner {
    program: PathBuf,
    /// Arguments before CodeNib's own, such as `-m codenib`.
    prefix: &'static [&'static str],
    /// A folder to put first on PATH, so the runtime's own tools are found.
    bin: Option<PathBuf>,
    bundled: bool,
}

impl Runner {
    fn command(&self, search: &str) -> Command {
        let mut command = Command::new(&self.program);
        command.args(self.prefix);
        let path = match &self.bin {
            Some(bin) => format!("{}:{search}", bin.display()),
            None => search.to_owned(),
        };
        command.env("PATH", path);
        if self.bundled {
            // The app's runtime must not pick up the person's own packages,
            // and its folder may be read-only.
            command
                .env("PYTHONNOUSERSITE", "1")
                .env("PYTHONDONTWRITEBYTECODE", "1");
        }
        command
    }
}

/// The Python that the app bundle carries, with CodeNib installed into it.
fn bundled_python(app: &AppHandle) -> Option<PathBuf> {
    let python = app
        .path()
        .resource_dir()
        .ok()?
        .join("python/bin/python3.12");
    is_executable(&python).then_some(python)
}

/// `CODENIB_BIN` when set, else the app's own CodeNib, else one on the path.
fn runner(app: &AppHandle, search: &str) -> Option<Runner> {
    if let Ok(explicit) = std::env::var("CODENIB_BIN") {
        let explicit = PathBuf::from(explicit);
        if is_executable(&explicit) {
            return Some(Runner {
                program: explicit,
                prefix: &[],
                bin: None,
                bundled: false,
            });
        }
    }
    if let Some(python) = bundled_python(app) {
        let bin = python.parent().map(Path::to_path_buf);
        return Some(Runner {
            program: python,
            prefix: &["-m", "codenib"],
            bin,
            bundled: true,
        });
    }
    find_codenib(search).map(|program| Runner {
        program,
        prefix: &[],
        bin: None,
        bundled: false,
    })
}

/// The address in a `codenib wiki` ready line, when the line is one.
fn ready_url(line: &str) -> Option<&str> {
    let start = line.find(READY)? + READY.len();
    let url = line[start..].trim();
    let local = url.starts_with("http://127.0.0.1:") || url.starts_with("http://localhost:");
    local.then_some(url)
}

/// The session key in a ready address.
fn session_key(url: &str) -> Option<&str> {
    let query = url.split_once('?')?.1;
    query
        .split('&')
        .find_map(|pair| pair.strip_prefix("session="))
        .filter(|key| {
            !key.is_empty()
                && key
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        })
}

/// A request path the window may forward: CodeNib's API on one repository.
fn forwardable(path: &str) -> bool {
    path.starts_with("/api/")
        && !path.contains("..")
        && !path.contains("://")
        && !path.chars().any(|c| c.is_whitespace() || c == '#')
}

/// The search path a terminal would have.
///
/// An app opened from the Dock gets a minimal PATH, without the folders that
/// `uv`, `pipx`, Homebrew and Claude Code install into, so the login shell's
/// PATH comes first and the usual install folders are added after it.
fn search_path() -> String {
    static FOUND: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    FOUND.get_or_init(compute_search_path).clone()
}

fn compute_search_path() -> String {
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

/// The first `codenib` on the path.
fn find_codenib(search: &str) -> Option<PathBuf> {
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

fn repo_name(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.to_owned())
}

fn config_file(app: &AppHandle) -> Option<PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|folder| folder.join("repositories.json"))
}

fn load_repos(app: &AppHandle) -> Vec<RepoConfig> {
    config_file(app)
        .and_then(|file| std::fs::read(file).ok())
        .and_then(|bytes| serde_json::from_slice::<Vec<RepoConfig>>(&bytes).ok())
        .unwrap_or_default()
}

fn save_repos(app: &AppHandle, repos: &[RepoConfig]) {
    if let Some(file) = config_file(app) {
        if let Some(folder) = file.parent() {
            let _ = std::fs::create_dir_all(folder);
        }
        if let Ok(bytes) = serde_json::to_vec_pretty(repos) {
            let _ = std::fs::write(file, bytes);
        }
    }
}

fn view(state: &State, config: &RepoConfig) -> RepoView {
    let backends = state.backends.lock().unwrap_or_else(|e| e.into_inner());
    let backend = backends.get(&config.path);
    let writer = config.writer().map(str::to_owned);
    RepoView {
        path: config.path.clone(),
        name: config
            .remote
            .clone()
            .unwrap_or_else(|| repo_name(&config.path)),
        analysis: config.analysis,
        generate: writer.is_some(),
        writer,
        remote: config.remote.clone(),
        status: backend.map_or(Status::Stopped, |b| b.status),
        error: backend.map(|b| b.error.clone()).unwrap_or_default(),
        line: backend
            .and_then(|b| b.lines.last().cloned())
            .unwrap_or_default(),
    }
}

fn announce(app: &AppHandle, path: &str) {
    let state = app.state::<State>();
    let config = state
        .repos
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .iter()
        .find(|repo| repo.path == path)
        .cloned();
    if let Some(config) = config {
        let _ = app.emit("codenib://status", view(&state, &config));
    }
}

/// Start the repository's service unless it is running or starting.
fn start_backend(app: &AppHandle, config: &RepoConfig) -> Result<(), String> {
    let state = app.state::<State>();
    {
        let backends = state
            .backends
            .lock()
            .map_err(|_| "app state is unavailable")?;
        if let Some(backend) = backends.get(&config.path) {
            if matches!(backend.status, Status::Starting | Status::Ready) {
                return Ok(());
            }
        }
    }
    let repo = PathBuf::from(&config.path);
    if !repo.is_dir() {
        return Err(format!("{} is not a folder any more", config.path));
    }
    let search = search_path();
    let runner = runner(app, &search).ok_or("CodeNib is not available in this app")?;
    let (port, api_port) = (free_port()?, free_port()?);
    let mut command = runner.command(&search);
    command
        .arg("wiki")
        .arg(&repo)
        .args(["--no-open", "--port", &port.to_string()])
        .args(["--api-port", &api_port.to_string()]);
    if config.analysis {
        command.args(["--preset", "graph"]);
    }
    if let Some(writer) = config.writer() {
        command.args(["--writer", writer]);
    }
    begin_log(&config.path, &format!("{:?}", command).replace('"', ""));
    let mut child = command
        .current_dir(&repo)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
        .map_err(|error| format!("could not start CodeNib: {error}"))?;
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    {
        let mut backends = state
            .backends
            .lock()
            .map_err(|_| "app state is unavailable")?;
        let mut backend = Backend::stopped();
        backend.status = Status::Starting;
        backend.child = Some(child);
        backend.api_port = api_port;
        backends.insert(config.path.clone(), backend);
    }
    announce(app, &config.path);
    watch(app.clone(), config.path.clone(), stdout);
    watch(app.clone(), config.path.clone(), stderr);
    supervise(app.clone(), config.path.clone());
    Ok(())
}

/// Where a repository's service output is kept:
/// ~/Library/Logs/CodeNib/<folder>-<hash>.log, one file per repository.
fn log_file(path: &str) -> Option<PathBuf> {
    use std::hash::{Hash, Hasher};
    let home = std::env::var("HOME").ok()?;
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    path.hash(&mut hasher);
    let name: String = repo_name(path)
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect();
    Some(
        Path::new(&home)
            .join("Library/Logs/CodeNib")
            .join(format!("{name}-{:08x}.log", hasher.finish() as u32)),
    )
}

/// A line as it goes into the log: never with the session key in it.
fn loggable(line: &str) -> String {
    match line.find("session=") {
        Some(at) => {
            let rest = &line[at + "session=".len()..];
            let end = rest
                .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '-'))
                .unwrap_or(rest.len());
            format!("{}session=…{}", &line[..at], &rest[end..])
        }
        None => line.to_owned(),
    }
}

/// Start a fresh log for a service that is starting.
fn begin_log(path: &str, argv: &str) {
    let Some(file) = log_file(path) else { return };
    if let Some(folder) = file.parent() {
        let _ = std::fs::create_dir_all(folder);
    }
    let started = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let _ = std::fs::write(
        &file,
        format!("# started at unix time {started}\n# {argv}\n"),
    );
}

fn append_log(path: &str, line: &str) {
    use std::io::Write;
    if let Some(file) = log_file(path) {
        if let Ok(mut out) = std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(file)
        {
            let _ = writeln!(out, "{}", loggable(line));
        }
    }
}

/// Read a service's output: progress lines while it starts, and its address.
fn watch<R: Read + Send + 'static>(app: AppHandle, path: String, stream: Option<R>) {
    let Some(stream) = stream else { return };
    thread::spawn(move || {
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            let line = line.trim_end().to_owned();
            append_log(&path, &line);
            if line.is_empty() {
                continue;
            }
            let mut became_ready = false;
            {
                let state = app.state::<State>();
                let mut backends = state.backends.lock().unwrap_or_else(|e| e.into_inner());
                if let Some(backend) = backends.get_mut(&path) {
                    if let Some(key) = ready_url(&line).and_then(session_key) {
                        if backend.status == Status::Starting {
                            backend.session = key.to_owned();
                            backend.status = Status::Ready;
                            became_ready = true;
                        }
                    }
                    if backend.status == Status::Starting {
                        backend.lines.push(line.clone());
                        let excess = backend.lines.len().saturating_sub(TAIL);
                        backend.lines.drain(..excess);
                    }
                }
            }
            if became_ready {
                announce(&app, &path);
            } else {
                let _ = app.emit(
                    "codenib://progress",
                    Progress {
                        repo: path.clone(),
                        line: line.clone(),
                    },
                );
            }
        }
    });
}

/// Notice when a service stops on its own, and say why.
fn supervise(app: AppHandle, path: String) {
    thread::spawn(move || loop {
        thread::sleep(Duration::from_millis(500));
        let ended = {
            let state = app.state::<State>();
            let mut backends = state.backends.lock().unwrap_or_else(|e| e.into_inner());
            let Some(backend) = backends.get_mut(&path) else {
                return;
            };
            let Some(child) = backend.child.as_mut() else {
                return;
            };
            match child.try_wait() {
                Ok(Some(status)) => {
                    backend.child = None;
                    backend.status = Status::Failed;
                    let tail = backend.lines.iter().rev().take(4).rev().cloned();
                    let tail: Vec<String> = tail.collect();
                    append_log(&path, &format!("# CodeNib stopped ({status})"));
                    let log = log_file(&path)
                        .map(|file| {
                            format!(
                                " Log: {}",
                                file.display().to_string().replacen(
                                    &std::env::var("HOME").unwrap_or_default(),
                                    "~",
                                    1
                                )
                            )
                        })
                        .unwrap_or_default();
                    backend.error = if tail.is_empty() {
                        format!("CodeNib stopped ({status}).{log}")
                    } else {
                        format!("CodeNib stopped ({status}): {}.{log}", tail.join(" · "))
                    };
                    true
                }
                Ok(None) => false,
                Err(_) => false,
            }
        };
        if ended {
            announce(&app, &path);
            return;
        }
    });
}

fn stop_backend(app: &AppHandle, path: &str) {
    let child = {
        let state = app.state::<State>();
        let mut backends = state.backends.lock().unwrap_or_else(|e| e.into_inner());
        backends
            .remove(path)
            .and_then(|mut backend| backend.child.take())
    };
    if let Some(child) = child {
        stop(child);
    }
}

fn find_repo(app: &AppHandle, path: &str) -> Result<RepoConfig, String> {
    app.state::<State>()
        .repos
        .lock()
        .map_err(|_| "app state is unavailable")?
        .iter()
        .find(|repo| repo.path == path)
        .cloned()
        .ok_or_else(|| format!("{path} is not in CodeNib"))
}

#[tauri::command]
async fn setup_status(app: AppHandle) -> Setup {
    let search = search_path();
    let Some(runner) = runner(&app, &search) else {
        return Setup {
            codenib: None,
            version: None,
            bundled: false,
        };
    };
    let version = runner
        .command(&search)
        .arg("--version")
        .stdin(Stdio::null())
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|text| text.trim().to_owned())
        .filter(|text| !text.is_empty());
    Setup {
        codenib: Some(runner.program.display().to_string()),
        version,
        bundled: runner.bundled,
    }
}

#[tauri::command]
fn repositories(app: AppHandle) -> Vec<RepoView> {
    let state = app.state::<State>();
    let repos = state
        .repos
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .clone();
    repos.iter().map(|repo| view(&state, repo)).collect()
}

#[tauri::command]
async fn pick_folder(app: AppHandle) -> Option<String> {
    app.dialog()
        .file()
        .set_title("Add a repository")
        .blocking_pick_folder()
        .and_then(|picked| picked.into_path().ok())
        .map(|path| path.display().to_string())
}

#[tauri::command]
async fn add_repository(
    app: AppHandle,
    path: String,
    analysis: bool,
    writer: Option<String>,
) -> Result<RepoView, String> {
    if !Path::new(&path).is_dir() {
        return Err(format!("{path} is not a folder"));
    }
    let writer = match writer.as_deref() {
        None | Some("") | Some("none") => None,
        Some(id) => Some(
            accounts::Agent::parse(id)
                .ok_or_else(|| format!("unknown writer {id}"))?
                .id()
                .to_owned(),
        ),
    };
    let remote = github::origin_slug(Path::new(&path), &search_path());
    let config = RepoConfig {
        path: path.clone(),
        analysis,
        generate: false,
        writer,
        remote,
    };
    {
        let state = app.state::<State>();
        let mut repos = state.repos.lock().map_err(|_| "app state is unavailable")?;
        repos.retain(|repo| repo.path != path);
        repos.push(config.clone());
        save_repos(&app, &repos);
    }
    // New options take effect on a fresh start.
    stop_backend(&app, &path);
    start_backend(&app, &config)?;
    Ok(view(&app.state::<State>(), &config))
}

#[tauri::command]
async fn remove_repository(app: AppHandle, path: String) -> Result<(), String> {
    stop_backend(&app, &path);
    let state = app.state::<State>();
    let mut repos = state.repos.lock().map_err(|_| "app state is unavailable")?;
    repos.retain(|repo| repo.path != path);
    save_repos(&app, &repos);
    Ok(())
}

#[tauri::command]
async fn start_repository(app: AppHandle, path: String) -> Result<RepoView, String> {
    let config = find_repo(&app, &path)?;
    start_backend(&app, &config)?;
    Ok(view(&app.state::<State>(), &config))
}

#[tauri::command]
async fn restart_repository(app: AppHandle, path: String) -> Result<RepoView, String> {
    let config = find_repo(&app, &path)?;
    stop_backend(&app, &path);
    start_backend(&app, &config)?;
    Ok(view(&app.state::<State>(), &config))
}

/// Forward one request to a repository's service and return its JSON.
#[tauri::command]
async fn api(
    app: AppHandle,
    repo: String,
    method: String,
    path: String,
    body: Option<Value>,
) -> Result<Value, String> {
    if !forwardable(&path) {
        return Err(format!("not a CodeNib API path: {path}"));
    }
    let method = method.to_ascii_uppercase();
    if !matches!(method.as_str(), "GET" | "POST" | "PUT" | "PATCH" | "DELETE") {
        return Err(format!("unsupported method {method}"));
    }
    let (port, session) = {
        let state = app.state::<State>();
        let backends = state
            .backends
            .lock()
            .map_err(|_| "app state is unavailable")?;
        match backends.get(&repo) {
            Some(backend) if backend.status == Status::Ready => {
                (backend.api_port, backend.session.clone())
            }
            Some(backend) if backend.status == Status::Starting => {
                return Err("starting: CodeNib is still opening this repository".to_owned())
            }
            _ => return Err("stopped: this repository is not open".to_owned()),
        }
    };
    let url = format!("http://127.0.0.1:{port}{path}");
    tauri::async_runtime::spawn_blocking(move || {
        let agent = ureq::AgentBuilder::new().timeout(API_TIMEOUT).build();
        let request = agent
            .request(&method, &url)
            .set("X-CodeNib-Session", &session)
            .set("Accept", "application/json");
        let response = match body {
            Some(body) => request.send_json(body),
            None => request.call(),
        };
        match response {
            Ok(response) => {
                let text = response.into_string().map_err(|e| e.to_string())?;
                if text.trim().is_empty() {
                    return Ok(Value::Null);
                }
                serde_json::from_str(&text).or(Ok(Value::String(text)))
            }
            Err(ureq::Error::Status(code, response)) => {
                let text = response.into_string().unwrap_or_default();
                let detail = serde_json::from_str::<Value>(&text)
                    .ok()
                    .and_then(|value| value.get("detail").cloned())
                    .map(|detail| match detail {
                        Value::String(text) => text,
                        other => other.to_string(),
                    })
                    .unwrap_or(text);
                Err(format!("HTTP {code}: {detail}"))
            }
            Err(error) => Err(format!("CodeNib did not answer: {error}")),
        }
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Claude Code and Codex on this Mac: installed, signed in, and how.
#[tauri::command]
async fn agents_status() -> Vec<accounts::AgentStatus> {
    let search = search_path();
    let (claude, codex) = thread::scope(|scope| {
        let claude = scope.spawn(|| accounts::status(accounts::Agent::ClaudeCode, &search));
        let codex = scope.spawn(|| accounts::status(accounts::Agent::Codex, &search));
        (claude.join(), codex.join())
    });
    [claude, codex].into_iter().filter_map(Result::ok).collect()
}

fn agent(id: &str) -> Result<accounts::Agent, String> {
    accounts::Agent::parse(id).ok_or_else(|| format!("unknown agent {id}"))
}

/// Open Terminal on the agent's own sign-in command.
#[tauri::command]
async fn agent_sign_in(id: String) -> Result<(), String> {
    accounts::sign_in(agent(&id)?, &search_path())
}

/// Open Terminal on the command that installs the agent.
#[tauri::command]
async fn agent_install(id: String) -> Result<(), String> {
    accounts::install(agent(&id)?, &search_path())
}

#[tauri::command]
async fn github_status(app: AppHandle) -> github::Status {
    github::status(&app.state::<State>().github, &search_path())
}

/// Sign in with a token the person pasted; it is kept in the keychain.
#[tauri::command]
async fn github_use_token(app: AppHandle, token: String) -> Result<github::Status, String> {
    let token = token.trim().to_owned();
    if token.is_empty() || token.chars().any(char::is_whitespace) {
        return Err("Paste the whole token, without spaces".to_owned());
    }
    github::whoami(&token)?;
    github::save_token(&token)?;
    let state = app.state::<State>();
    state.github.forget();
    Ok(github::status(&state.github, &search_path()))
}

/// Forget the token CodeNib saved. Other sign-ins (gh, git) stay as they are.
#[tauri::command]
async fn github_sign_out(app: AppHandle) -> github::Status {
    github::delete_token();
    let state = app.state::<State>();
    state.github.forget();
    github::status(&state.github, &search_path())
}

#[tauri::command]
async fn github_device_start(app: AppHandle) -> Result<github::DeviceCode, String> {
    let code = tauri::async_runtime::spawn_blocking(github::device_start)
        .await
        .map_err(|e| e.to_string())??;
    app.state::<State>()
        .device_cancel
        .store(false, Ordering::SeqCst);
    let _ = open_external(code.verification_uri.clone());
    Ok(code)
}

/// Wait until the person approves the code on github.com.
#[tauri::command]
async fn github_device_wait(
    app: AppHandle,
    code: github::DeviceCode,
) -> Result<github::Status, String> {
    let device = code;
    let waiting = app.clone();
    let token = tauri::async_runtime::spawn_blocking(move || {
        github::device_wait(&device, || {
            waiting
                .state::<State>()
                .device_cancel
                .load(Ordering::SeqCst)
        })
    })
    .await
    .map_err(|e| e.to_string())??;
    github::save_token(&token)?;
    let state = app.state::<State>();
    state.github.forget();
    Ok(github::status(&state.github, &search_path()))
}

#[tauri::command]
fn github_device_cancel(app: AppHandle) {
    app.state::<State>()
        .device_cancel
        .store(true, Ordering::SeqCst);
}

/// The signed-in person's repositories, most recently pushed first.
#[tauri::command]
async fn github_repositories(app: AppHandle) -> Result<Vec<github::Repository>, String> {
    let (token, _) = app
        .state::<State>()
        .github
        .token(&search_path())
        .ok_or("Sign in to GitHub to list your repositories")?;
    tauri::async_runtime::spawn_blocking(move || github::repositories(&token))
        .await
        .map_err(|e| e.to_string())?
}

#[derive(Serialize)]
struct Lookup {
    repository: github::Repository,
    /// Where it will be cloned, or the folder that already holds it.
    destination: String,
    /// True when that folder already holds this repository.
    present: bool,
}

/// What a pasted name or address refers to on GitHub.
#[tauri::command]
async fn github_lookup(app: AppHandle, input: String) -> Result<Lookup, String> {
    let slug = github::parse_slug(&input)
        .ok_or("Enter a GitHub repository as owner/repo or its github.com address")?;
    let token = app
        .state::<State>()
        .github
        .token(&search_path())
        .map(|(t, _)| t);
    let repository =
        tauri::async_runtime::spawn_blocking(move || github::lookup(&slug, token.as_deref()))
            .await
            .map_err(|e| e.to_string())??;
    let destination = github::clone_destination(&repository.full_name)?;
    Ok(Lookup {
        present: destination.join(".git").is_dir(),
        destination: destination.display().to_string(),
        repository,
    })
}

#[derive(Clone, Serialize)]
struct CloneProgress {
    slug: String,
    line: String,
}

/// Clone a GitHub repository into ~/CodeNib/<owner>/<repo>; its folder.
#[tauri::command]
async fn clone_repository(app: AppHandle, input: String) -> Result<String, String> {
    let slug = github::parse_slug(&input)
        .ok_or("Enter a GitHub repository as owner/repo or its github.com address")?;
    let search = search_path();
    let token = app.state::<State>().github.token(&search).map(|(t, _)| t);
    let destination = github::clone_destination(&slug)?;
    let target = destination.clone();
    let events = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        github::clone(&slug, &target, token.as_deref(), &search, |line| {
            let _ = events.emit(
                "codenib://clone",
                CloneProgress {
                    slug: slug.clone(),
                    line,
                },
            );
        })
    })
    .await
    .map_err(|e| e.to_string())??;
    Ok(destination.display().to_string())
}

/// Open a web address in the person's browser, never inside the app.
#[tauri::command]
fn open_external(url: String) -> Result<(), String> {
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return Err("only web addresses open in the browser".to_owned());
    }
    Command::new("open")
        .arg(&url)
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

/// Show a repository folder in Finder.
#[tauri::command]
fn show_in_finder(path: String) -> Result<(), String> {
    if !Path::new(&path).is_dir() {
        return Err(format!("{path} is not a folder"));
    }
    Command::new("open")
        .arg(&path)
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(State::default())
        .setup(|app| {
            let repos = load_repos(app.handle());
            *app.state::<State>()
                .repos
                .lock()
                .unwrap_or_else(|e| e.into_inner()) = repos;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            setup_status,
            repositories,
            pick_folder,
            add_repository,
            remove_repository,
            start_repository,
            restart_repository,
            api,
            open_external,
            show_in_finder,
            agents_status,
            agent_sign_in,
            agent_install,
            github_status,
            github_use_token,
            github_sign_out,
            github_device_start,
            github_device_wait,
            github_device_cancel,
            github_repositories,
            github_lookup,
            clone_repository
        ])
        .build(tauri::generate_context!())
        .expect("CodeNib could not start")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                let children: Vec<Child> = app
                    .state::<State>()
                    .backends
                    .lock()
                    .map(|mut backends| {
                        backends
                            .drain()
                            .filter_map(|(_, mut backend)| backend.child.take())
                            .collect()
                    })
                    .unwrap_or_default();
                for child in children {
                    stop(child);
                }
            }
        });
}

// Entry points for tests/live.rs, which runs against the real GitHub.
#[doc(hidden)]
pub fn github_session() -> github::Session {
    github::Session::default()
}

#[doc(hidden)]
pub fn github_status_for_test(session: &github::Session, search: &str) -> github::Status {
    github::status(session, search)
}

#[doc(hidden)]
pub fn github_lookup_for_test(input: &str, token: Option<&str>) -> Result<(String, bool), String> {
    let slug = github::parse_slug(input).ok_or("not a repository")?;
    github::lookup(&slug, token).map(|r| (r.full_name, r.private))
}

#[doc(hidden)]
pub fn clone_for_test(
    slug: &str,
    destination: &Path,
    token: Option<&str>,
    search: &str,
    progress: impl FnMut(String),
) -> Result<(), String> {
    github::clone(slug, destination, token, search, progress)
}

#[doc(hidden)]
pub fn agents_for_test(search: &str) -> Vec<String> {
    [accounts::Agent::ClaudeCode, accounts::Agent::Codex]
        .into_iter()
        .map(|agent| {
            let s = accounts::status(agent, search);
            format!(
                "{}: installed={} signed_in={} detail={:?}",
                s.name, s.installed, s.signed_in, s.detail
            )
        })
        .collect()
}

#[doc(hidden)]
pub fn github_token_for_test(session: &github::Session, search: &str) -> Option<String> {
    session.token(search).map(|(token, _)| token)
}

#[doc(hidden)]
pub fn github_repositories_for_test(token: &str) -> Result<Vec<(String, bool)>, String> {
    github::repositories(token)
        .map(|list| list.into_iter().map(|r| (r.full_name, r.private)).collect())
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
    fn reads_the_session_key() {
        assert_eq!(
            session_key("http://127.0.0.1:3190/repo?session=abc_DEF-1"),
            Some("abc_DEF-1")
        );
        assert_eq!(
            session_key("http://127.0.0.1:3190/repo?x=1&session=k"),
            Some("k")
        );
        assert_eq!(session_key("http://127.0.0.1:3190/repo"), None);
        assert_eq!(session_key("http://127.0.0.1:3190/repo?session=a b"), None);
    }

    #[test]
    fn forwards_only_codenib_api_paths() {
        assert!(forwardable("/api/repos/requests/wiki/overview"));
        assert!(forwardable("/api/repos/requests/annotations?page=overview"));
        assert!(!forwardable("/repo"));
        assert!(!forwardable("/api/../etc"));
        assert!(!forwardable("http://example.com/api/x"));
        assert!(!forwardable("/api/x y"));
    }

    #[test]
    fn logs_never_keep_the_session_key() {
        assert_eq!(
            loggable("CodeNib Wiki is ready: http://127.0.0.1:3190/repo?session=abc_DEF-1"),
            "CodeNib Wiki is ready: http://127.0.0.1:3190/repo?session=…"
        );
        assert_eq!(loggable("x?session=k&y=1"), "x?session=…&y=1");
        assert_eq!(loggable("Views:"), "Views:");
        let file = log_file("/Users/me/code/my repo").unwrap();
        assert!(file
            .to_string_lossy()
            .contains("Library/Logs/CodeNib/my_repo-"));
    }

    #[test]
    fn a_free_port_can_be_bound() {
        let first = free_port().unwrap();
        assert!(first > 0);
        TcpListener::bind(("127.0.0.1", first)).unwrap();
    }

    #[test]
    fn saved_repositories_default_to_building_analysis_tools() {
        let repos: Vec<RepoConfig> =
            serde_json::from_str(r#"[{"path": "/Users/me/code/x"}]"#).unwrap();
        assert!(repos[0].analysis);
        assert!(!repos[0].generate);
    }
}
