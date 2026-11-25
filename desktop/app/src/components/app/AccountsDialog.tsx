import { Check, Copy, ExternalLink, Github, KeyRound, LoaderCircle, RotateCw, TerminalSquare } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { backend, message, type AgentStatus, type DeviceCode, type GitHubStatus } from "@/lib/backend";
import { useStore } from "@/lib/store";

const SOURCE: Record<string, string> = {
  codenib: "saved in your keychain by CodeNib",
  "github-cli": "from the GitHub CLI (gh)",
  environment: "from GITHUB_TOKEN",
  git: "from git's saved sign-in for github.com",
};

const TOKEN_URL =
  "https://github.com/settings/tokens/new?scopes=repo,read:user&description=CodeNib%20for%20Mac";

function Row({
  icon,
  name,
  state,
  ok,
  children,
}: {
  icon: React.ReactNode;
  name: string;
  state: string;
  ok: boolean | null;
  children?: React.ReactNode;
}) {
  return (
    <section className="acct-row" aria-label={name}>
      <div className="acct-head">
        <span className="acct-icon">{icon}</span>
        <div className="acct-title">
          <strong>{name}</strong>
          <span className="acct-state" data-ok={ok === null ? "unknown" : ok ? "yes" : "no"}>
            {ok && <Check />}
            {state}
          </span>
        </div>
      </div>
      {children && <div className="acct-body">{children}</div>}
    </section>
  );
}

function GitHubRow() {
  const { github, setGithub } = useStore();
  const [token, setToken] = useState("");
  const [pasting, setPasting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [device, setDevice] = useState<DeviceCode | null>(null);

  const done = (status: GitHubStatus) => {
    setGithub(status);
    setToken("");
    setPasting(false);
    setDevice(null);
    if (status.signed_in) toast(`Signed in to GitHub as ${status.login}`);
  };

  const useToken = async () => {
    setBusy(true);
    try {
      done(await backend.githubUseToken(token));
    } catch (error) {
      toast.error(message(error));
    } finally {
      setBusy(false);
    }
  };

  const deviceSignIn = async () => {
    setBusy(true);
    try {
      const code = await backend.githubDeviceStart();
      setDevice(code);
      done(await backend.githubDeviceWait(code));
    } catch (error) {
      const text = message(error);
      if (text !== "cancelled") toast.error(text);
      setDevice(null);
    } finally {
      setBusy(false);
    }
  };

  const signOut = async () => {
    try {
      setGithub(await backend.githubSignOut());
    } catch (error) {
      toast.error(message(error));
    }
  };

  const state = !github
    ? "Checking…"
    : github.signed_in
      ? `Signed in as ${github.login}${github.source ? ` · ${SOURCE[github.source] ?? github.source}` : ""}`
      : github.problem
        ? github.problem
        : "Not signed in. Public repositories still open.";

  return (
    <Row icon={<Github />} name="GitHub" state={state} ok={github ? github.signed_in : null}>
      {github?.signed_in && github.source === "codenib" && (
        <Button variant="ghost" size="sm" onClick={() => void signOut()}>
          Sign out
        </Button>
      )}
      {github?.signed_in && github.source !== "codenib" && (
        <p className="add-sub">
          CodeNib uses the sign-in you already have. Change it where it lives, or paste a token to use another
          account.{" "}
          <button type="button" className="sb-link" onClick={() => setPasting(true)}>
            Use a token instead
          </button>
        </p>
      )}
      {device && (
        <div className="acct-device" role="status">
          <span className="add-sub">Enter this code on github.com, in the page that just opened:</span>
          <div className="acct-code">
            <code>{device.user_code}</code>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void navigator.clipboard.writeText(device.user_code).then(() => toast("Code copied"))}
            >
              <Copy />
              Copy
            </Button>
          </div>
          <span className="add-sub">
            <LoaderCircle className="status-spin" /> Waiting for GitHub…{" "}
            <button type="button" className="sb-link" onClick={() => void backend.githubDeviceCancel()}>
              Cancel
            </button>
          </span>
        </div>
      )}
      {github && !github.signed_in && !device && (
        <div className="acct-actions">
          {github.device_sign_in && (
            <Button size="sm" onClick={() => void deviceSignIn()} disabled={busy}>
              <Github />
              Sign in with GitHub
            </Button>
          )}
          <Button
            variant={github.device_sign_in ? "outline" : "default"}
            size="sm"
            onClick={() => setPasting(true)}
            disabled={busy}
          >
            <KeyRound />
            Use a token
          </Button>
        </div>
      )}
      {pasting && (
        <div className="acct-token">
          <span className="add-sub">
            Create a token with the <code>repo</code> scope{" "}
            <button type="button" className="sb-link" onClick={() => void backend.openExternal(TOKEN_URL)}>
              on github.com <ExternalLink />
            </button>
            , then paste it here. It is saved in your macOS keychain, never in a file.
          </span>
          <div className="field-row">
            <Input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="ghp_… or github_pat_…"
              aria-label="GitHub token"
              autoComplete="off"
              spellCheck={false}
              onKeyDown={(e) => e.key === "Enter" && token.trim() && void useToken()}
            />
            <Button size="sm" onClick={() => void useToken()} disabled={busy || !token.trim()}>
              {busy ? "Checking…" : "Save"}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setPasting(false)} disabled={busy}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </Row>
  );
}

function AgentRow({ agent }: { agent: AgentStatus }) {
  const { taskAgent, setTaskAgent } = useStore();
  const state = !agent.installed
    ? "Not installed"
    : agent.signed_in
      ? `Signed in${agent.detail ? ` · ${agent.detail}` : ""}`
      : "Installed, not signed in";
  const run = (action: () => Promise<void>, hint: string) =>
    action()
      .then(() => toast(hint))
      .catch((error) => toast.error(message(error)));
  return (
    <Row icon={<TerminalSquare />} name={agent.name} state={state} ok={agent.installed && agent.signed_in}>
      {!agent.installed && (
        <div className="acct-actions">
          <Button
            size="sm"
            onClick={() =>
              void run(() => backend.agentInstall(agent.id), `Installing ${agent.name} in Terminal. Come back when it finishes.`)
            }
          >
            Install in Terminal
          </Button>
          <Button variant="ghost" size="sm" onClick={() => void backend.openExternal(agent.docs)}>
            Setup guide <ExternalLink />
          </Button>
          <code className="acct-cmd">{agent.install}</code>
        </div>
      )}
      {agent.installed && !agent.signed_in && (
        <div className="acct-actions">
          <Button
            size="sm"
            onClick={() =>
              void run(() => backend.agentSignIn(agent.id), `Sign in to ${agent.name} in Terminal, then come back.`)
            }
          >
            Sign in in Terminal
          </Button>
        </div>
      )}
      {agent.installed && agent.signed_in && (
        <label className="acct-radio">
          <input
            type="radio"
            name="task-agent"
            checked={taskAgent === agent.id}
            onChange={() => setTaskAgent(agent.id)}
          />
          Send questions and changes to {agent.name}
        </label>
      )}
    </Row>
  );
}

export function AccountsDialog() {
  const { accountsOpen, setAccountsOpen, agents, refreshAccounts } = useStore();
  const [checking, setChecking] = useState(false);
  const recheck = async () => {
    setChecking(true);
    await refreshAccounts();
    setChecking(false);
  };
  // Signing in happens in Terminal or the browser; check again on opening.
  useEffect(() => {
    if (accountsOpen) void refreshAccounts();
  }, [accountsOpen, refreshAccounts]);

  return (
    <Dialog open={accountsOpen} onOpenChange={setAccountsOpen}>
      <DialogContent className="add-dialog accounts-dialog">
        <DialogHeader>
          <DialogTitle>Accounts</DialogTitle>
          <DialogDescription>
            CodeNib uses your own sign-ins. It never sees your Claude or Codex credentials: each agent keeps its own.
          </DialogDescription>
        </DialogHeader>
        <GitHubRow />
        {(agents ?? []).map((agent) => (
          <AgentRow key={agent.id} agent={agent} />
        ))}
        {agents === null && <p className="add-sub">Checking Claude Code and Codex…</p>}
        <div className="acct-foot">
          <Button variant="ghost" size="sm" onClick={() => void recheck()} disabled={checking}>
            <RotateCw className={checking ? "status-spin" : undefined} />
            Check again
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
