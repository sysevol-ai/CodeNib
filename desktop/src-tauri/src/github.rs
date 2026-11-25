//! GitHub: who the person is signed in as, their repositories, and cloning.
//!
//! The app never asks for a password. It uses, in order, a token it saved in
//! the macOS keychain itself (pasted, or from GitHub's device sign-in), the
//! GitHub CLI's sign-in (`gh auth token`), `GITHUB_TOKEN`, and the token git
//! already keeps for github.com in its credential helper. A token reaches git
//! only through the environment of the one `git clone` that needs it; nothing
//! is written into the cloned repository's configuration.

use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::Value;

/// The keychain item that holds a token the person gave CodeNib.
const KEYCHAIN_SERVICE: &str = "ai.codenib.desktop.github";
const KEYCHAIN_ACCOUNT: &str = "github.com";
const API: &str = "https://api.github.com";
const USER_AGENT: &str = "CodeNib-Desktop";
/// Where a cloned repository goes: ~/CodeNib/<owner>/<repo>.
const CLONE_ROOT: &str = "CodeNib";

/// Where the token in use came from, shown so the person knows what to change.
#[derive(Clone, Copy, PartialEq, Eq, Serialize, Debug)]
#[serde(rename_all = "kebab-case")]
pub enum Source {
    /// Saved in the keychain by this app.
    Codenib,
    /// `gh auth token`.
    GithubCli,
    /// The `GITHUB_TOKEN` or `GH_TOKEN` environment variable.
    Environment,
    /// git's credential helper for github.com.
    Git,
}

#[derive(Clone, Serialize, Default)]
pub struct Status {
    pub signed_in: bool,
    pub login: Option<String>,
    pub name: Option<String>,
    pub source: Option<Source>,
    /// True when GitHub's device sign-in is configured in this build.
    pub device_sign_in: bool,
    /// Why a token that was found did not work, if one did not.
    pub problem: Option<String>,
}

#[derive(Clone, Serialize)]
pub struct Repository {
    pub full_name: String,
    pub description: Option<String>,
    pub private: bool,
    pub pushed_at: Option<String>,
    pub language: Option<String>,
}

#[derive(Clone, Serialize, serde::Deserialize)]
pub struct DeviceCode {
    pub user_code: String,
    pub verification_uri: String,
    pub device_code: String,
    pub interval: u64,
    pub expires_in: u64,
}

/// The token found and where, cached so each window request does not run
/// `gh` and git again.
#[derive(Default)]
pub struct Session {
    found: Mutex<Option<(String, Source, Instant)>>,
}

const CACHE: Duration = Duration::from_secs(300);

impl Session {
    pub fn forget(&self) {
        *self.found.lock().unwrap_or_else(|e| e.into_inner()) = None;
    }

    /// The token to use, looking again when the cached one is old.
    pub fn token(&self, search: &str) -> Option<(String, Source)> {
        {
            let found = self.found.lock().unwrap_or_else(|e| e.into_inner());
            if let Some((token, source, at)) = found.as_ref() {
                if at.elapsed() < CACHE {
                    return Some((token.clone(), *source));
                }
            }
        }
        let fresh = find_token(search);
        *self.found.lock().unwrap_or_else(|e| e.into_inner()) = fresh
            .clone()
            .map(|(token, source)| (token, source, Instant::now()));
        fresh
    }
}

/// The OAuth app used for GitHub's device sign-in, when this build has one.
pub fn client_id() -> Option<String> {
    std::env::var("CODENIB_GITHUB_CLIENT_ID")
        .ok()
        .or_else(|| option_env!("CODENIB_GITHUB_CLIENT_ID").map(str::to_owned))
        .filter(|id| !id.trim().is_empty())
}

fn keychain() -> Option<keyring::Entry> {
    keyring::Entry::new(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT).ok()
}

pub fn saved_token() -> Option<String> {
    keychain()?
        .get_password()
        .ok()
        .filter(|t| !t.trim().is_empty())
}

pub fn save_token(token: &str) -> Result<(), String> {
    keychain()
        .ok_or("the keychain is unavailable")?
        .set_password(token)
        .map_err(|error| format!("could not save to the keychain: {error}"))
}

pub fn delete_token() {
    if let Some(entry) = keychain() {
        let _ = entry.delete_credential();
    }
}

fn find_program(search: &str, name: &str) -> Option<PathBuf> {
    search
        .split(':')
        .map(|folder| Path::new(folder).join(name))
        .find(|candidate| candidate.is_file())
}

/// Run a short command; its trimmed stdout when it succeeds within `limit`.
fn quick(mut command: Command, limit: Duration) -> Option<String> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + limit;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let mut out = String::new();
                child.stdout.take()?.read_to_string(&mut out).ok()?;
                return status.success().then(|| out.trim().to_owned());
            }
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}

fn gh_token(search: &str) -> Option<String> {
    let gh = find_program(search, "gh")?;
    let mut command = Command::new(gh);
    command
        .args(["auth", "token", "--hostname", "github.com"])
        .env("PATH", search);
    quick(command, Duration::from_secs(5)).filter(|t| !t.is_empty())
}

/// The password git's credential helper keeps for https://github.com, if any.
fn git_token(search: &str) -> Option<String> {
    let git = find_program(search, "git")?;
    let mut child = Command::new(git)
        .args(["credential", "fill"])
        .env("PATH", search)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GCM_INTERACTIVE", "never")
        .env("GIT_ASKPASS", "/usr/bin/false")
        .env("SSH_ASKPASS", "/usr/bin/false")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    {
        use std::io::Write;
        let mut stdin = child.stdin.take()?;
        stdin
            .write_all(b"protocol=https\nhost=github.com\n\n")
            .ok()?;
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(Some(_)) => return None,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
    let mut out = String::new();
    child.stdout.take()?.read_to_string(&mut out).ok()?;
    out.lines()
        .find_map(|line| line.strip_prefix("password="))
        .map(str::to_owned)
        .filter(|t| !t.is_empty())
}

fn find_token(search: &str) -> Option<(String, Source)> {
    if let Some(token) = saved_token() {
        return Some((token, Source::Codenib));
    }
    if let Some(token) = gh_token(search) {
        return Some((token, Source::GithubCli));
    }
    for name in ["GITHUB_TOKEN", "GH_TOKEN"] {
        if let Ok(token) = std::env::var(name) {
            if !token.trim().is_empty() {
                return Some((token.trim().to_owned(), Source::Environment));
            }
        }
    }
    git_token(search).map(|token| (token, Source::Git))
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(20))
        .user_agent(USER_AGENT)
        .build()
}

fn get(path: &str, token: Option<&str>) -> Result<Value, (u16, String)> {
    let mut request = agent()
        .get(&format!("{API}{path}"))
        .set("Accept", "application/vnd.github+json")
        .set("X-GitHub-Api-Version", "2022-11-28");
    if let Some(token) = token {
        request = request.set("Authorization", &format!("Bearer {token}"));
    }
    match request.call() {
        Ok(response) => response
            .into_json::<Value>()
            .map_err(|e| (0, e.to_string())),
        Err(ureq::Error::Status(code, response)) => {
            let text = response.into_string().unwrap_or_default();
            let detail = serde_json::from_str::<Value>(&text)
                .ok()
                .and_then(|v| v.get("message").and_then(Value::as_str).map(str::to_owned))
                .unwrap_or(text);
            Err((code, detail))
        }
        Err(error) => Err((0, format!("GitHub did not answer: {error}"))),
    }
}

/// Who the token belongs to.
pub fn whoami(token: &str) -> Result<(String, Option<String>), String> {
    let user = get("/user", Some(token)).map_err(|(code, detail)| match code {
        401 => "GitHub rejected this token (it may have expired or been revoked)".to_owned(),
        0 => detail,
        _ => format!("GitHub answered {code}: {detail}"),
    })?;
    let login = user
        .get("login")
        .and_then(Value::as_str)
        .ok_or("GitHub returned no user for this token")?
        .to_owned();
    let name = user.get("name").and_then(Value::as_str).map(str::to_owned);
    Ok((login, name))
}

pub fn status(session: &Session, search: &str) -> Status {
    let device_sign_in = client_id().is_some();
    let Some((token, source)) = session.token(search) else {
        return Status {
            device_sign_in,
            ..Status::default()
        };
    };
    match whoami(&token) {
        Ok((login, name)) => Status {
            signed_in: true,
            login: Some(login),
            name,
            source: Some(source),
            device_sign_in,
            problem: None,
        },
        Err(problem) => Status {
            signed_in: false,
            source: Some(source),
            device_sign_in,
            problem: Some(problem),
            ..Status::default()
        },
    }
}

fn repository(value: &Value) -> Option<Repository> {
    Some(Repository {
        full_name: value.get("full_name")?.as_str()?.to_owned(),
        description: value
            .get("description")
            .and_then(Value::as_str)
            .map(str::to_owned),
        private: value
            .get("private")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        pushed_at: value
            .get("pushed_at")
            .and_then(Value::as_str)
            .map(str::to_owned),
        language: value
            .get("language")
            .and_then(Value::as_str)
            .map(str::to_owned),
    })
}

/// The person's repositories, most recently pushed first.
pub fn repositories(token: &str) -> Result<Vec<Repository>, String> {
    let list = get(
        "/user/repos?sort=pushed&per_page=100&affiliation=owner,collaborator,organization_member",
        Some(token),
    )
    .map_err(|(_, detail)| detail)?;
    Ok(list
        .as_array()
        .map(|items| items.iter().filter_map(repository).collect())
        .unwrap_or_default())
}

/// One repository by name, public or one the token can read.
pub fn lookup(slug: &str, token: Option<&str>) -> Result<Repository, String> {
    let value = get(&format!("/repos/{slug}"), token).map_err(|(code, detail)| match code {
        404 if token.is_some() => {
            format!("{slug} was not found, or your GitHub sign-in cannot read it")
        }
        404 => format!("{slug} was not found. If it is private, sign in to GitHub first"),
        403 if detail.contains("rate limit") => {
            "GitHub's limit for signed-out requests was reached; sign in to GitHub".to_owned()
        }
        0 => detail,
        _ => format!("GitHub answered {code}: {detail}"),
    })?;
    repository(&value).ok_or_else(|| "GitHub returned no repository".to_owned())
}

/// `owner/repo` from what a person pastes: a URL, an SSH address or the name.
pub fn parse_slug(input: &str) -> Option<String> {
    let text = input.trim().trim_end_matches('/');
    let rest = text
        .strip_prefix("https://github.com/")
        .or_else(|| text.strip_prefix("http://github.com/"))
        .or_else(|| text.strip_prefix("https://www.github.com/"))
        .or_else(|| text.strip_prefix("github.com/"))
        .or_else(|| text.strip_prefix("git@github.com:"))
        .or_else(|| text.strip_prefix("ssh://git@github.com/"))
        .unwrap_or(text);
    let mut parts = rest.split('/');
    let owner = parts.next()?;
    let repo = parts.next()?.trim_end_matches(".git");
    // A URL may continue into the repository (/tree/main, /pull/1); a bare
    // name may not have a third part.
    if rest == text && parts.next().is_some() {
        return None;
    }
    let owner_ok = !owner.is_empty()
        && owner.len() <= 39
        && owner.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
        && !owner.starts_with('-');
    let repo_ok = !repo.is_empty()
        && repo.len() <= 100
        && repo != "."
        && repo != ".."
        && repo
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'));
    (owner_ok && repo_ok).then(|| format!("{owner}/{repo}"))
}

/// The GitHub repository a local folder was cloned from, when it was.
pub fn origin_slug(folder: &Path, search: &str) -> Option<String> {
    let git = find_program(search, "git")?;
    let mut command = Command::new(git);
    command
        .args(["config", "--get", "remote.origin.url"])
        .current_dir(folder)
        .env("PATH", search);
    quick(command, Duration::from_secs(3)).and_then(|url| parse_slug(&url))
}

pub fn clone_destination(slug: &str) -> Result<PathBuf, String> {
    let home = std::env::var("HOME").map_err(|_| "HOME is not set")?;
    Ok(Path::new(&home).join(CLONE_ROOT).join(slug))
}

fn basic_auth(token: &str) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let input = format!("x-access-token:{token}");
    let bytes = input.as_bytes();
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            TABLE[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

/// Clone `slug` into `destination`, reporting git's progress lines.
///
/// A folder that already holds this repository is reused as it is.
pub fn clone(
    slug: &str,
    destination: &Path,
    token: Option<&str>,
    search: &str,
    mut progress: impl FnMut(String),
) -> Result<(), String> {
    if destination.join(".git").is_dir() {
        return match origin_slug(destination, search) {
            Some(found) if found.eq_ignore_ascii_case(slug) => Ok(()),
            _ => Err(format!(
                "{} already holds a different repository",
                destination.display()
            )),
        };
    }
    if destination.exists()
        && std::fs::read_dir(destination)
            .map(|mut entries| entries.next().is_some())
            .unwrap_or(true)
    {
        return Err(format!(
            "{} already exists and is not empty",
            destination.display()
        ));
    }
    if let Some(parent) = destination.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("could not create {}: {e}", parent.display()))?;
    }
    let git = find_program(search, "git").ok_or(
        "git is not installed. Install Apple's command-line tools with `xcode-select --install`",
    )?;
    let mut command = Command::new(git);
    command
        .args(["clone", "--progress", "--origin", "origin"])
        .arg(format!("https://github.com/{slug}.git"))
        .arg(destination)
        .env("PATH", search)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GCM_INTERACTIVE", "never")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    if let Some(token) = token {
        // Only this process sees the header; it is not saved anywhere.
        command
            .env("GIT_CONFIG_COUNT", "1")
            .env("GIT_CONFIG_KEY_0", "http.https://github.com/.extraheader")
            .env(
                "GIT_CONFIG_VALUE_0",
                format!("AUTHORIZATION: basic {}", basic_auth(token)),
            );
    }
    let mut child = command
        .spawn()
        .map_err(|error| format!("could not run git: {error}"))?;
    let mut tail: Vec<String> = Vec::new();
    if let Some(stderr) = child.stderr.take() {
        // git separates progress updates with carriage returns.
        let mut reader = BufReader::new(stderr);
        let mut buffer = Vec::new();
        loop {
            buffer.clear();
            let read = reader.read_until(b'\r', &mut buffer).unwrap_or(0);
            if read == 0 {
                break;
            }
            for line in String::from_utf8_lossy(&buffer).split(['\r', '\n']) {
                let line = line.trim();
                if !line.is_empty() {
                    tail.push(line.to_owned());
                    if tail.len() > 6 {
                        tail.remove(0);
                    }
                    progress(line.to_owned());
                }
            }
        }
    }
    let status = child.wait().map_err(|error| error.to_string())?;
    if status.success() {
        return Ok(());
    }
    let _ = std::fs::remove_dir_all(destination);
    let said = tail.join(" ");
    if said.contains("could not read Username")
        || said.contains("Authentication failed")
        || said.contains("not found")
    {
        return Err(format!(
            "GitHub did not let CodeNib read {slug}. If it is private, sign in to GitHub with an account that can see it"
        ));
    }
    Err(format!(
        "git clone failed: {}",
        tail.last().cloned().unwrap_or_default()
    ))
}

/// Start GitHub's device sign-in: the code the person types at github.com.
pub fn device_start() -> Result<DeviceCode, String> {
    let id = client_id().ok_or("GitHub sign-in is not configured in this build")?;
    let value: Value = agent()
        .post("https://github.com/login/device/code")
        .set("Accept", "application/json")
        .send_form(&[("client_id", id.as_str()), ("scope", "repo read:user")])
        .map_err(|error| format!("GitHub did not answer: {error}"))?
        .into_json()
        .map_err(|error| error.to_string())?;
    let field = |name: &str| value.get(name).and_then(Value::as_str).map(str::to_owned);
    Ok(DeviceCode {
        user_code: field("user_code").ok_or("GitHub returned no code")?,
        verification_uri: field("verification_uri")
            .unwrap_or_else(|| "https://github.com/login/device".to_owned()),
        device_code: field("device_code").ok_or("GitHub returned no device code")?,
        interval: value.get("interval").and_then(Value::as_u64).unwrap_or(5),
        expires_in: value
            .get("expires_in")
            .and_then(Value::as_u64)
            .unwrap_or(900),
    })
}

/// Wait for the person to approve the device sign-in; the token when they do.
pub fn device_wait(device: &DeviceCode, cancelled: impl Fn() -> bool) -> Result<String, String> {
    let id = client_id().ok_or("GitHub sign-in is not configured in this build")?;
    let deadline = Instant::now() + Duration::from_secs(device.expires_in);
    let mut interval = Duration::from_secs(device.interval.max(1));
    while Instant::now() < deadline {
        thread::sleep(interval);
        if cancelled() {
            return Err("cancelled".to_owned());
        }
        let value: Value = agent()
            .post("https://github.com/login/oauth/access_token")
            .set("Accept", "application/json")
            .send_form(&[
                ("client_id", id.as_str()),
                ("device_code", device.device_code.as_str()),
                ("grant_type", "urn:ietf:params:oauth:grant-type:device_code"),
            ])
            .map_err(|error| format!("GitHub did not answer: {error}"))?
            .into_json()
            .map_err(|error| error.to_string())?;
        if let Some(token) = value.get("access_token").and_then(Value::as_str) {
            return Ok(token.to_owned());
        }
        match value.get("error").and_then(Value::as_str) {
            Some("authorization_pending") => {}
            Some("slow_down") => interval += Duration::from_secs(5),
            Some("expired_token") => return Err("The code expired. Start again".to_owned()),
            Some("access_denied") => return Err("Sign-in was declined on GitHub".to_owned()),
            Some(other) => return Err(format!("GitHub sign-in failed: {other}")),
            None => return Err("GitHub returned no token".to_owned()),
        }
    }
    Err("The code expired. Start again".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_a_repository_from_what_people_paste() {
        for input in [
            "psf/requests",
            "https://github.com/psf/requests",
            "https://github.com/psf/requests/",
            "https://github.com/psf/requests.git",
            "https://github.com/psf/requests/tree/main/src",
            "github.com/psf/requests",
            "git@github.com:psf/requests.git",
            "ssh://git@github.com/psf/requests.git",
            "  psf/requests  ",
        ] {
            assert_eq!(
                parse_slug(input).as_deref(),
                Some("psf/requests"),
                "{input}"
            );
        }
        assert_eq!(
            parse_slug("micropython/micropython.git").as_deref(),
            Some("micropython/micropython")
        );
        assert_eq!(parse_slug("a-b/c_d.e").as_deref(), Some("a-b/c_d.e"));
    }

    #[test]
    fn refuses_what_is_not_a_repository() {
        for input in [
            "",
            "psf",
            "psf/",
            "psf/requests/extra",
            "../etc/passwd",
            "psf/..",
            "-x/y",
            "https://gitlab.com/a/b",
            "psf/re quests",
            "psf/requests;rm",
        ] {
            assert_eq!(parse_slug(input), None, "{input}");
        }
    }

    #[test]
    fn encodes_basic_authorization() {
        // base64("x-access-token:abc")
        assert_eq!(basic_auth("abc"), "eC1hY2Nlc3MtdG9rZW46YWJj");
        assert_eq!(basic_auth("ab"), "eC1hY2Nlc3MtdG9rZW46YWI=");
        assert_eq!(basic_auth("a"), "eC1hY2Nlc3MtdG9rZW46YQ==");
    }

    #[test]
    fn clones_into_the_codenib_folder() {
        let destination = clone_destination("psf/requests").unwrap();
        assert!(destination.ends_with("CodeNib/psf/requests"));
    }
}
