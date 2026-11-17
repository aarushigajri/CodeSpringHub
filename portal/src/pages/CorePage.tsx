import { useEffect, useState } from "react";
import { Ctx } from "../App";
import { DocInfo, Lab, Scope, get, isInsider, put } from "../api";
import { Badge, Empty, Help, MarkdownEditor, attempt, useUnsavedWarning } from "../components";
import Datasets from "./Datasets";
import { History, NewDocument } from "./LabPage";

// A core facility (BSR, Sequencing, Microscopy, ...). Everyone sees its shared section; the core's
// own staff also see its internal section and their personal items.

export default function CorePage({ ctx, lab, tab, query, onChanged }: {
  ctx: Ctx; lab: Lab; tab: string; query: URLSearchParams; onChanged: () => Promise<void>;
}) {
  const staff = isInsider(lab);
  const tabs: [string, string][] = [["shared", "Shared with everyone"]];
  if (staff || lab.sees?.includes("internal")) tabs.push(["internal", "Internal"]);
  tabs.push(["datasets", "Data and code"]);
  if (staff || lab.sees?.includes("internal")) tabs.push(["history", "History"]);
  const current = tabs.some(([id]) => id === tab) ? tab : "shared";

  return (
    <div className="page">
      <div className="page-head core-head">
        <div>
          <h1>
            {lab.name} <Badge kind="info">Core facility</Badge>
            {lab.on_hub === false && <Badge kind="warn">Not on CodeSpringHub yet</Badge>}
          </h1>
          <p className="muted">
            {lab.description}
            {lab.website && <> · <a href={lab.website} target="_blank" rel="noreferrer">Website</a></>}
          </p>
          {lab.contacts && lab.contacts.length > 0 && (
            <p className="muted contacts">
              Contacts: {lab.contacts.map((c) => (c.email ? `${c.name} (${c.email})` : c.name)).join(", ")}
            </p>
          )}
          <p className="muted">
            {lab.role === "lead" && "You lead this core: you see its shared and internal sections."}
            {lab.role === "staff" && "You're on this core's staff: you see its shared and internal sections."}
            {lab.role === "bsr" && "BSR keeps this starter page until the core joins CodeSpringHub."}
            {lab.role === "user" && "You see what this core shares with every lab. Only its staff can change these pages."}
          </p>
        </div>
        <a className="button" href="#/try">Try it as Claude</a>
      </div>
      <div className="tabs">
        {tabs.map(([id, label]) => (
          <a key={id} href={`#/lab/${lab.lab_id}/${id}`} className={current === id ? "on" : ""}>{label}</a>
        ))}
      </div>
      {current === "shared" && <Section ctx={ctx} lab={lab} area="shared" openNew={query.get("new") === "1"} onChanged={onChanged} />}
      {current === "internal" && <Section ctx={ctx} lab={lab} area="internal" openNew={false} onChanged={onChanged} />}
      {current === "datasets" && <Datasets ctx={ctx} lab={lab} openAdd={query.get("add") === "1"} onChanged={onChanged} />}
      {current === "history" && <History labId={lab.lab_id} />}
    </div>
  );
}

function Section({ ctx, lab, area, openNew, onChanged }: {
  ctx: Ctx; lab: Lab; area: "shared" | "internal"; openNew: boolean; onChanged: () => Promise<void>;
}) {
  const [creating, setCreating] = useState(openNew);
  const [text, setText] = useState<string | null>(null);
  const [saved, setSaved] = useState("");
  const [saving, setSaving] = useState(false);
  const rel = `${area}/context.md`;
  const docs = (lab.documents_list ?? []).filter((d) => d.category !== "context");
  const mine: DocInfo[] = area === "shared" ? docs.filter((d) => d.scope === "shared") : docs.filter((d) => d.scope === "lab");
  const personal = area === "internal" ? docs.filter((d) => d.scope === "personal" || d.scope === "private") : [];

  useEffect(() => {
    setText(null);
    get<{ text: string }>(`/api/labs/${lab.lab_id}/docs/${rel}`)
      .then((d) => { setText(d.text); setSaved(d.text); })
      .catch(() => { setText(""); setSaved(""); });
  }, [lab.lab_id, area]);
  const dirty = text !== null && text !== saved;
  useUnsavedWarning(dirty);

  async function save() {
    setSaving(true);
    const ok = await attempt(() => put(`/api/labs/${lab.lab_id}/docs/${rel}`, { text }),
      "Saved. Claude will see the change on its next question.");
    if (ok) setSaved(text!);
    setSaving(false);
  }

  const list = (items: DocInfo[]) => (
    <section className="panel">
      <ul className="doc-list">
        {items.map((d) => (
          <li key={d.id}>
            <a href={`#/lab/${lab.lab_id}/doc/${d.rel_path}`}>{d.title}</a>
            <span className="muted small">{d.category}{d.owner ? ` · ${d.owner === ctx.me.user ? "you" : d.owner}` : ""}</span>
            {d.scope === "private" && <span className="badge muted">only you</span>}
            <code className="muted small">{d.id}</code>
          </li>
        ))}
      </ul>
    </section>
  );

  return (
    <div className="stack">
      <section className="panel">
        <h2>{area === "shared" ? "What every lab sees" : "Internal page (core staff only)"}</h2>
        <Help>
          {area === "shared"
            ? "Claude reads this first when someone asks about this core: services, how to submit samples, turnaround, and where data goes. Every lab using CodeSpringHub can read it."
            : "Instrument notes, internal procedures and anything not ready to share. Only this core's staff (and Claude, when one of you uses it) can read it."}{" "}
          Write <code>TODO</code> for anything not filled in yet; Claude ignores it.
        </Help>
        {text === null ? <div className="loading">Loading…</div>
          : <MarkdownEditor value={text} onChange={setText} onSave={save} readOnly={!lab.can_edit} />}
        {lab.can_edit && (
          <div className="savebar">
            <span className="muted">{dirty ? "You have unsaved changes." : "All changes saved."}</span>
            <button className="primary" disabled={saving || !dirty} onClick={save}>{saving ? "Saving…" : "Save changes"}</button>
          </div>
        )}
      </section>

      <div className="row-between">
        <h3>{area === "shared" ? "Shared documents (protocols, guidelines, guidance)" : "Internal documents"}</h3>
        {(lab.can_edit || (area === "internal" && isInsider(lab))) && (
          <button className="primary" onClick={() => setCreating(true)}>+ New document</button>
        )}
      </div>
      {mine.length === 0
        ? <Empty title="No documents yet.">{area === "shared"
            ? "Protocols and submission guidelines the core shares will appear here."
            : "Internal notes appear here."}</Empty>
        : list(mine)}
      {area === "internal" && isInsider(lab) && (
        <>
          <h3>Personal: you and the core lead, or only you</h3>
          {personal.length === 0 ? <Empty title="No personal documents." /> : list(personal)}
        </>
      )}
      {creating && (
        <NewDocument ctx={ctx} lab={lab} initialScope={(area === "shared" ? "shared" : lab.can_edit ? "lab" : "personal") as Scope}
          onClose={() => setCreating(false)} onCreated={onChanged} />
      )}
    </div>
  );
}
