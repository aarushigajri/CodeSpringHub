import { useEffect, useState } from "react";
import { ApiError, Lab, Me, get } from "./api";
import { Toasts } from "./components";
import Activity from "./pages/Activity";
import Connect from "./pages/Connect";
import DocumentEditor from "./pages/DocumentEditor";
import Home from "./pages/Home";
import Inbox from "./pages/Inbox";
import LabPage from "./pages/LabPage";
import TryIt from "./pages/TryIt";
import Workflows from "./pages/Workflows";

// Hash routes: #/  #/lab/<id>/<tab>  #/lab/<id>/doc/<path>  #/workflows  #/inbox  #/try  #/activity
export function go(path: string) {
  location.hash = "#" + path;
}

function useRoute() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const h = () => {
      setHash(location.hash);
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", h);
    return () => window.removeEventListener("hashchange", h);
  }, []);
  const [path, query = ""] = hash.replace(/^#/, "").split("?");
  return { parts: path.split("/").filter(Boolean).map(decodeURIComponent), query: new URLSearchParams(query) };
}

export type Ctx = { me: Me; labs: Lab[]; reloadLabs: () => Promise<void>; inboxCount: number; refreshInbox: () => void };

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [labs, setLabs] = useState<Lab[]>([]);
  const [inboxCount, setInboxCount] = useState(0);
  const [fatal, setFatal] = useState<string | null>(null);
  const { parts, query } = useRoute();

  async function reloadLabs() {
    setLabs(await get<Lab[]>("/api/labs"));
  }
  function refreshInbox() {
    get<{ suggestions: { status: string; can_change: boolean }[]; reviews: { status: string }[] }>("/api/inbox")
      .then((i) => setInboxCount(
        i.suggestions.filter((s: any) => s.status === "pending" && s.can_change).length + i.reviews.filter((r) => r.status === "open").length,
      ))
      .catch(() => {});
  }

  useEffect(() => {
    get<Me>("/api/me")
      .then(async (m) => {
        setMe(m);
        await reloadLabs();
        refreshInbox();
      })
      .catch((e) => setFatal(e instanceof ApiError ? e.message : "Can't reach the portal. Is it still running?"));
  }, []);

  if (fatal) {
    return (
      <div className="fatal">
        <h1>CodeSpringHub</h1>
        <p>{fatal}</p>
        <p className="muted">
          Start the portal on the cluster with <code>./portal</code> and open the private link it prints.
        </p>
      </div>
    );
  }
  if (!me) return <div className="loading">Loading…</div>;

  const ctx: Ctx = { me, labs, reloadLabs, inboxCount, refreshInbox };
  const [section, a, b, ...rest] = parts;

  let page;
  if (section === "lab" && a && b === "doc") page = <DocumentEditor ctx={ctx} labId={a} relPath={rest.join("/")} suggestion={query.get("suggestion")} />;
  else if (section === "lab" && a) page = <LabPage ctx={ctx} labId={a} tab={b ?? "about"} query={query} />;
  else if (section === "workflows") page = <Workflows ctx={ctx} />;
  else if (section === "inbox") page = <Inbox ctx={ctx} />;
  else if (section === "try") page = <TryIt ctx={ctx} />;
  else if (section === "activity") page = <Activity ctx={ctx} />;
  else if (section === "connect") page = <Connect ctx={ctx} />;
  else page = <Home ctx={ctx} />;

  const navItem = (href: string, label: string, active: boolean, extra?: React.ReactNode) => (
    <a href={"#" + href} className={active ? "active" : ""}>
      {label}
      {extra}
    </a>
  );

  return (
    <div className="shell">
      <aside className="sidebar">
        <a className="brand" href="#/">
          <span className="logo">CS</span>
          <span>
            CodeSpringHub
            <small>Bioinformatics Shared Resource</small>
          </span>
        </a>
        <nav>
          {navItem("/", "Home", !section)}
          <div className="nav-label">Your labs</div>
          {labs.map((l) => navItem(`/lab/${l.lab_id}/about`, l.name, section === "lab" && a === l.lab_id))}
          <div className="nav-label">Shared</div>
          {navItem("/workflows", "BSR workflows", section === "workflows")}
          {navItem("/inbox", "Inbox", section === "inbox", inboxCount > 0 && <span className="count">{inboxCount}</span>)}
          {navItem("/try", "Try it as Claude", section === "try")}
          {navItem("/connect", "Connect Claude", section === "connect")}
          {navItem("/activity", "Activity", section === "activity")}
        </nav>
        <div className="who">
          Signed in as <strong>{me.name}</strong>
          <div className="muted">{me.user}{me.is_bsr ? " · BSR" : ""}</div>
        </div>
      </aside>
      <main className="content">{page}</main>
      <Toasts />
    </div>
  );
}
