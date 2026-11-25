//! The coding agents on this Mac: whether Claude Code and Codex are installed
//! and signed in, and a way to sign in.
//!
//! CodeNib never holds an agent's credentials. Each agent keeps its own
//! sign-in; the app only asks the agent's command whether it is signed in and,
//! to sign in, opens Terminal on the agent's own login command.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::Value;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Agent {
    ClaudeCode,
    Codex,
}

impl Agent {
    pub fn parse(id: &str) -> Option<Agent> {
        match id {
            "claude-code" => Some(Agent::ClaudeCode),
            "codex" => Some(Agent::Codex),
            _ => None,
        }
    }

    pub fn id(self) -> &'static str {
        match self {
            Agent::ClaudeCode => "claude-code",
            Agent::Codex => "codex",
        }
    }

    fn name(self) -> &'static str {
        match self {
            Agent::ClaudeCode => "Claude Code",
            Agent::Codex => "Codex",
        }
    }

    fn program(self) -> &'static str {
        match self {
            Agent::ClaudeCode => "claude",
            Agent::Codex => "codex",
        }
    }

    fn install(self) -> &'static str {
        match self {
            Agent::ClaudeCode => "curl -fsSL https://claude.ai/install.sh | bash",
            Agent::Codex => "npm install -g @openai/codex",
        }
    }

    fn docs(self) -> &'static str {
        match self {
            Agent::ClaudeCode => "https://code.claude.com/docs/en/setup",
            Agent::Codex => "https://developers.openai.com/codex/cli",
        }
    }

    fn login_args(self) -> &'static [&'static str] {
        match self {
            Agent::ClaudeCode => &["auth", "login"],
            Agent::Codex => &["login"],
        }
    }
}

#[derive(Clone, Serialize)]
pub struct AgentStatus {
    pub id: &'static str,
    pub name: &'static str,
    pub installed: bool,
    pub signed_in: bool,
    /// How it is signed in, in a few words: "Claude Max plan", "ChatGPT".
    pub detail: Option<String>,
    pub path: Option<String>,
    /// The command that installs it, for a person to run.
    pub install: &'static str,
    pub docs: &'static str,
}

fn find(search: &str, program: &str) -> Option<PathBuf> {
    search
        .split(':')
        .map(|folder| Path::new(folder).join(program))
        .find(|candidate| candidate.is_file())
}

/// Run a short command; (exit success, stdout, stderr) within `limit`.
fn quick(mut command: Command, limit: Duration) -> Option<(bool, String, String)> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + limit;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    let mut out = String::new();
    let mut err = String::new();
    if let Some(mut stdout) = child.stdout.take() {
        let _ = stdout.read_to_string(&mut out);
    }
    if let Some(mut stderr) = child.stderr.take() {
        let _ = stderr.read_to_string(&mut err);
    }
    Some((status.success(), out, err))
}

/// Read `claude auth status --json`.
pub fn claude_detail(json: &str) -> (bool, Option<String>) {
    let Ok(value) = serde_json::from_str::<Value>(json) else {
        return (false, None);
    };
    let signed_in = value.get("loggedIn").and_then(Value::as_bool).unwrap_or(false);
    if !signed_in {
        return (false, None);
    }
    let plan = value
        .get("subscriptionType")
        .and_then(Value::as_str)
        .filter(|plan| !plan.is_empty());
    let method = value.get("authMethod").and_then(Value::as_str).unwrap_or("");
    let detail = match (plan, method) {
        (Some(plan), _) => {
            let mut letters = plan.chars();
            let plan = match letters.next() {
                Some(first) => first.to_uppercase().collect::<String>() + letters.as_str(),
                None => String::new(),
            };
            Some(format!("Claude {plan} plan"))
        }
        (None, "apiKey" | "api_key" | "console") => Some("API key".to_owned()),
        (None, "") => None,
        (None, other) => Some(other.to_owned()),
    };
    (true, detail)
}

/// Read `codex login status`: "Logged in using ChatGPT", "Not logged in".
pub fn codex_detail(success: bool, out: &str, err: &str) -> (bool, Option<String>) {
    let text = format!("{out}\n{err}");
    let line = text
        .lines()
        .map(str::trim)
        .find(|line| line.to_ascii_lowercase().contains("logged in"))
        .unwrap_or("");
    let lower = line.to_ascii_lowercase();
    if !success || lower.contains("not logged in") {
        return (false, None);
    }
    let detail = lower
        .find("using ")
        .map(|at| line[at + "using ".len()..].trim().to_owned())
        .filter(|detail| !detail.is_empty());
    (true, detail)
}

pub fn status(agent: Agent, search: &str) -> AgentStatus {
    let path = find(search, agent.program());
    let mut result = AgentStatus {
        id: agent.id(),
        name: agent.name(),
        installed: path.is_some(),
        signed_in: false,
        detail: None,
        path: path.as_ref().map(|p| p.display().to_string()),
        install: agent.install(),
        docs: agent.docs(),
    };
    let Some(path) = path else {
        return result;
    };
    let mut command = Command::new(&path);
    command.env("PATH", search);
    let (signed_in, detail) = match agent {
        Agent::ClaudeCode => {
            command.args(["auth", "status", "--json"]);
            quick(command, Duration::from_secs(10))
                .map(|(_, out, _)| claude_detail(&out))
                .unwrap_or((false, None))
        }
        Agent::Codex => {
            command.args(["login", "status"]);
            quick(command, Duration::from_secs(10))
                .map(|(ok, out, err)| codex_detail(ok, &out, &err))
                .unwrap_or((false, None))
        }
    };
    result.signed_in = signed_in;
    result.detail = detail;
    result
}

/// Quote a word for the shell Terminal runs.
fn shell_quote(word: &str) -> String {
    format!("'{}'", word.replace('\'', r"'\''"))
}

/// Quote a string for AppleScript.
fn applescript_quote(text: &str) -> String {
    format!("\"{}\"", text.replace('\\', "\\\\").replace('"', "\\\""))
}

/// Open Terminal on the agent's own sign-in command.
pub fn sign_in(agent: Agent, search: &str) -> Result<(), String> {
    let path = find(search, agent.program())
        .ok_or_else(|| format!("{} is not installed", agent.name()))?;
    let mut line = format!("export PATH={}; {}", shell_quote(search), shell_quote(&path.display().to_string()));
    for arg in agent.login_args() {
        line.push(' ');
        line.push_str(&shell_quote(arg));
    }
    run_in_terminal(&line)
}

/// Open Terminal on the command that installs the agent, for the person to run.
pub fn install(agent: Agent, search: &str) -> Result<(), String> {
    let line = format!("export PATH={}; {}", shell_quote(search), agent.install());
    run_in_terminal(&line)
}

fn run_in_terminal(line: &str) -> Result<(), String> {
    let script = format!(
        "tell application \"Terminal\"\nactivate\ndo script {}\nend tell",
        applescript_quote(line)
    );
    let status = Command::new("/usr/bin/osascript")
        .args(["-e", &script])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|error| format!("could not open Terminal: {error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err("Terminal did not open. Allow CodeNib to control Terminal in System Settings, then Privacy & Security, then Automation".to_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_claude_code_sign_in() {
        let (yes, detail) = claude_detail(r#"{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}"#);
        assert!(yes);
        assert_eq!(detail.as_deref(), Some("Claude Max plan"));
        let (yes, detail) = claude_detail(r#"{"loggedIn": true, "authMethod": "apiKey"}"#);
        assert!(yes);
        assert_eq!(detail.as_deref(), Some("API key"));
        assert_eq!(claude_detail(r#"{"loggedIn": false}"#), (false, None));
        assert_eq!(claude_detail("not json"), (false, None));
    }

    #[test]
    fn reads_codex_sign_in() {
        assert_eq!(
            codex_detail(true, "Logged in using ChatGPT\n", ""),
            (true, Some("ChatGPT".to_owned()))
        );
        assert_eq!(
            codex_detail(true, "", "Logged in using an API key - sk-proj-***\n"),
            (true, Some("an API key - sk-proj-***".to_owned()))
        );
        assert_eq!(codex_detail(false, "Not logged in\n", ""), (false, None));
        assert_eq!(codex_detail(true, "Not logged in\n", ""), (false, None));
    }

    #[test]
    fn quotes_commands_for_terminal() {
        assert_eq!(shell_quote("a b"), "'a b'");
        assert_eq!(shell_quote("it's"), r"'it'\''s'");
        assert_eq!(applescript_quote(r#"say "hi" \ now"#), r#""say \"hi\" \\ now""#);
    }
}
