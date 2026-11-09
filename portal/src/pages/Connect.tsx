import { useEffect, useState } from "react";
import { Ctx } from "../App";
import { ApiError, get } from "../api";
import { Help, notify } from "../components";

type Info = { user: string; host: string; command: string; claude_desktop: object; notice: string; portal_port: number; portal_command: string };

function Copy({ text, label = "Copy" }: { text: string; label?: string }) {
  return (
    <button
      className="copy"
      onClick={() => navigator.clipboard?.writeText(text).then(() => notify("Copied."), () => notify("Select the text and copy it.", "error"))}
    >
      {label}
    </button>
  );
}

function Code({ text }: { text: string }) {
  return (
    <div className="codeblock">
      <pre>{text}</pre>
      <Copy text={text} />
    </div>
  );
}

export default function Connect({ ctx }: { ctx: Ctx }) {
  const [info, setInfo] = useState<Info | null>(null);
  const [who, setWho] = useState(ctx.me.user);
  const [whoError, setWhoError] = useState<string | null>(null);
  const [os, setOs] = useState<"mac" | "windows">(navigator.userAgent.includes("Windows") ? "windows" : "mac");

  // Instructions are built for whoever is typed here, so BSR can prepare them for a researcher.
  useEffect(() => {
    const name = who.trim();
    if (!name) return;
    const t = setTimeout(() => {
      get<Info>(`/api/connect?for_user=${encodeURIComponent(name)}`)
        .then((i) => { setInfo(i); setWhoError(null); })
        .catch((e) => setWhoError(e instanceof ApiError ? e.message : "Couldn't check that username."));
    }, 400);
    return () => clearTimeout(t);
  }, [who]);
  if (!info) return <div className="page loading">Loading…</div>;
  const forSomeoneElse = info.user !== ctx.me.user;

  const target = `${info.user}@${info.host}`;
  const json = JSON.stringify(info.claude_desktop, null, 2);
  const keyPath = os === "mac" ? "~/.ssh/id_ed25519.pub" : "$env:USERPROFILE\\.ssh\\id_ed25519.pub";
  const copyKey = os === "mac"
    ? `ssh-copy-id ${target}`
    : `type ${keyPath} | ssh ${target} "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"`;
  const configPath = os === "mac"
    ? "~/Library/Application Support/Claude/claude_desktop_config.json"
    : "%APPDATA%\\Claude\\claude_desktop_config.json";

  return (
    <div className="page narrow">
      <h1>Connect Claude to CodeSpringHub</h1>
      <p className="lead">
        About 10 minutes, once. Afterwards, Claude Desktop (signed in with your CSHL account) can use your lab's
        context, data and code. A BSR team member is happy to do this with you.
      </p>

      <div className="callout">
        <strong>Before you start:</strong> {info.notice}
      </div>

      <div className="who-field">
        <label>
          Cluster username of the person connecting
          <input value={who} onChange={(e) => setWho(e.target.value)} spellCheck={false} autoComplete="off" />
        </label>
        {whoError ? <div className="error-box small">{whoError}</div>
          : forSomeoneElse ? <div className="muted small">Showing the setup for <strong>{info.user}</strong>. Send them this page's steps, or sit with them.</div>
          : <div className="muted small">That's you. Helping a researcher? Type their username and every step below updates.</div>}
      </div>

      <div className="segmented">
        <button className={os === "mac" ? "on" : ""} onClick={() => setOs("mac")}>Mac</button>
        <button className={os === "windows" ? "on" : ""} onClick={() => setOs("windows")}>Windows</button>
      </div>

      <ol className="connect-steps">
        <li>
          <h3>Be on the CSHL network</h3>
          <p>Use CSHL Wi-Fi, or connect to the CSHL VPN if you're elsewhere.</p>
        </li>

        <li>
          <h3>Let your computer log in to the cluster without a password</h3>
          <p>
            Claude can't type your password, so your computer needs a key. Open{" "}
            {os === "mac" ? <strong>Terminal</strong> : <strong>PowerShell</strong>} and run these one at a time. Press
            Enter at every question in the first one.
          </p>
          <Code text={`ssh-keygen -t ed25519`} />
          <p className="muted small">Then send the key to the cluster (it asks for your cluster password one last time):</p>
          <Code text={copyKey} />
          <p className="muted small">Check it worked: this should print your username without asking for a password.</p>
          <Code text={`ssh ${target} whoami`} />
        </li>

        <li>
          <h3>Tell Claude Desktop about CodeSpringHub</h3>
          <p>
            In Claude Desktop open <strong>Settings → Developer → Edit Config</strong>. That opens{" "}
            <code>{configPath}</code>. Replace its contents with this (or, if it already has an{" "}
            <code>mcpServers</code> section, add the <code>codespringhub</code> part to it), then save:
          </p>
          <Code text={json} />
        </li>

        <li>
          <h3>Restart Claude Desktop and try it</h3>
          <p>Quit Claude completely and reopen it. Then ask:</p>
          <Code text={`Using CodeSpringHub, which labs can I use, and what does Claude know about my lab?`} />
        </li>

        <li>
          <h3>Optional: open the CodeSpringHub portal</h3>
          <p>
            To add data, code or notes yourself, run this in {os === "mac" ? "Terminal" : "PowerShell"}. It logs in,
            starts your own portal, and prints a private link: open that link in your browser. Keep the window open
            while you use the portal; close it when you're done.
          </p>
          <Code text={info.portal_command} />
          <p className="muted small">
            If it doesn't work, run <code>hub doctor</code> on the cluster, or ask BSR. You can also preview exactly what
            Claude receives on the <a href="#/try">Try it as Claude</a> page.
          </p>
        </li>
      </ol>

      <Help>
        <strong>How it works:</strong> Claude Desktop starts the Hub on the cluster through your own SSH login, so the
        Hub runs as <em>you</em> and can only reach what you can already reach. Nothing is installed on your computer
        and no data is copied to it.
      </Help>
    </div>
  );
}
