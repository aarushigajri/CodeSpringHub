import { useEffect, useState } from "react";
import { Ctx, go } from "../App";
import { DocInfo, Lab, Project, get, post, put } from "../api";
import { Empty, Help, MarkdownEditor, Modal, attempt, useUnsavedWarning } from "../components";
import Datasets from "./Datasets";

const TABS = [
  ["about", "Lab page"],
  ["documents", "Documents"],
  ["datasets", "Datasets"],
  ["history", "History"],
] as const;

export default function LabPage({ ctx, labId, tab, query }: { ctx: Ctx; labId: string; tab: string; query: URLSearchParams }) {
  const [lab, setLab] = useState<Lab | null>(null);
  const [error, setError] = useState("");

  async function load() {
    try {
      setLab(await get<Lab>(`/api/labs/${labId}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  useEffect(() => {
    setLab(null);
    load();
  }, [labId]);

  if (error) return <div className="page"><div className="error-box">{error}</div></div>;
  if (!lab) return <div className="page loading">Loading…</div>;

  const reload = async () => {
    await load();
    await ctx.reloadLabs();
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>{lab.name}</h1>
          <p className="muted">
            Visible to members of the <code>{lab.unix_group}</code> group.
            {!lab.can_edit && " You can view but not edit this lab."}
          </p>
        </div>
        <a className="button" href="#/try">Try it as Claude</a>
      </div>
      <div className="tabs">
        {TABS.map(([id, label]) => (
          <a key={id} href={`#/lab/${labId}/${id}`} className={tab === id ? "on" : ""}>{label}</a>
        ))}
      </div>
      {tab === "about" && <About lab={lab} onSaved={reload} />}
      {tab === "documents" && <Documents ctx={ctx} lab={lab} openNew={query.get("new") === "1"} onChanged={reload} />}
      {tab === "datasets" && <Datasets ctx={ctx} lab={lab} openAdd={query.get("add") === "1"} onChanged={reload} />}
      {tab === "history" && <History labId={labId} />}
    </div>
  );
}

// ---- "Lab page": details + context.md --------------------------------------------

function About({ lab, onSaved }: { lab: Lab; onSaved: () => Promise<void> }) {
  const [name, setName] = useState(lab.name);
  const [description, setDescription] = useState(lab.description);
  const [projects, setProjects] = useState<Project[]>(lab.projects);
  const [context, setContext] = useState<string | null>(null);
  const [savedContext, setSavedContext] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    get<{ text: string }>(`/api/labs/${lab.lab_id}/docs/context.md`)
      .then((d) => {
        setContext(d.text);
        setSavedContext(d.text);
      })
      .catch(() => {
        setContext("");
        setSavedContext("");
      });
  }, [lab.lab_id]);

  const detailsDirty =
    name !== lab.name || description !== lab.description || JSON.stringify(projects) !== JSON.stringify(lab.projects);
  const contextDirty = context !== null && context !== savedContext;
  useUnsavedWarning(detailsDirty || contextDirty);

  async function save() {
    setSaving(true);
    if (detailsDirty) {
      await attempt(() => put(`/api/labs/${lab.lab_id}`, { name, description, projects }));
    }
    if (contextDirty) {
      const ok = await attempt(() => put(`/api/labs/${lab.lab_id}/docs/context.md`, { text: context }));
      if (ok) setSavedContext(context!);
    }
    await attempt(onSaved, "Saved. Claude will see the change on its next question.");
    setSaving(false);
  }

  return (
    <div className="stack">
      <section className="panel">
        <h2>Lab details</h2>
        <label>
          Lab name
          <input value={name} onChange={(e) => setName(e.target.value)} disabled={!lab.can_edit} />
        </label>
        <label>
          One-line description
          <input value={description} onChange={(e) => setDescription(e.target.value)} disabled={!lab.can_edit}
            placeholder="e.g. Pancreatic cancer biology using mouse models and organoids" />
        </label>
        <div className="label">Projects</div>
        <Help>Projects group your datasets, e.g. "Mouse WGS" or "Organoid scRNA-seq".</Help>
        {projects.map((p, i) => (
          <div key={i} className="project-row">
            <input value={p.name} placeholder="Project name" disabled={!lab.can_edit}
              onChange={(e) => setProjects(projects.map((q, j) => (j === i ? { ...q, name: e.target.value } : q)))} />
            <input value={p.description} placeholder="Short description (optional)" disabled={!lab.can_edit}
              onChange={(e) => setProjects(projects.map((q, j) => (j === i ? { ...q, description: e.target.value } : q)))} />
            {lab.can_edit && (
              <button className="ghost" title="Remove project" onClick={() => setProjects(projects.filter((_, j) => j !== i))}>✕</button>
            )}
          </div>
        ))}
        {lab.can_edit && (
          <button onClick={() => setProjects([...projects, { id: "", name: "", description: "" }])}>+ Add project</button>
        )}
      </section>

      <section className="panel">
        <h2>Lab context</h2>
        <Help>
          This is the first page Claude reads about your lab. Good things to include: research areas, organisms and
          models, terms and abbreviations you use, reference genomes, and how you like analyses done. Write{" "}
          <code>TODO</code> for anything not filled in yet; Claude will ignore it.
        </Help>
        {context === null ? (
          <div className="loading">Loading…</div>
        ) : (
          <MarkdownEditor value={context} onChange={setContext} onSave={save} readOnly={!lab.can_edit} />
        )}
      </section>

      {lab.can_edit && (
        <div className="savebar">
          <span className="muted">{detailsDirty || contextDirty ? "You have unsaved changes." : "All changes saved."}</span>
          <button className="primary" disabled={saving || !(detailsDirty || contextDirty)} onClick={save}>
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      )}
    </div>
  );
}

// ---- Documents -------------------------------------------------------------------

function Documents({ ctx, lab, openNew, onChanged }: { ctx: Ctx; lab: Lab; openNew: boolean; onChanged: () => Promise<void> }) {
  const [creating, setCreating] = useState(openNew && lab.can_edit);
  const docs = (lab.documents_list ?? []).filter((d) => d.category !== "context");
  const groups = Object.keys(ctx.me.categories)
    .map((c) => [c, docs.filter((d) => d.category === c)] as [string, DocInfo[]])
    .filter(([, list]) => list.length);

  return (
    <div className="stack">
      <div className="row-between">
        <Help>
          Documents are longer pieces Claude can search and read: BSR guidance, protocols, notes and grant aims.
        </Help>
        {lab.can_edit && <button className="primary" onClick={() => setCreating(true)}>+ New document</button>}
      </div>
      {groups.length === 0 && <Empty title="No documents yet.">Start with a protocol or a short note about how your lab works.</Empty>}
      {groups.map(([cat, list]) => (
        <section key={cat} className="panel">
          <h3>{ctx.me.categories[cat].split(":")[0]}</h3>
          <ul className="doc-list">
            {list.map((d) => (
              <li key={d.id}>
                <a href={`#/lab/${lab.lab_id}/doc/${d.rel_path}`}>{d.title}</a>
                <code className="muted small">{d.id}</code>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {creating && <NewDocument ctx={ctx} lab={lab} onClose={() => setCreating(false)} onCreated={onChanged} />}
    </div>
  );
}

function NewDocument({ ctx, lab, onClose, onCreated }: { ctx: Ctx; lab: Lab; onClose: () => void; onCreated: () => Promise<void> }) {
  const [category, setCategory] = useState("guidance");
  const [title, setTitle] = useState("");
  async function create() {
    const r = await attempt(() => post<{ rel_path: string }>(`/api/labs/${lab.lab_id}/docs`, { category, title }));
    if (r) {
      await onCreated();
      go(`/lab/${lab.lab_id}/doc/${r.rel_path}`);
    }
  }
  return (
    <Modal title="New document" onClose={onClose}>
      <div className="label">What kind of document?</div>
      <div className="choices">
        {Object.entries(ctx.me.categories).map(([id, text]) => {
          const [label, explain] = text.split(": ");
          return (
            <label key={id} className={`choice ${category === id ? "on" : ""}`}>
              <input type="radio" name="cat" checked={category === id} onChange={() => setCategory(id)} />
              <strong>{label}</strong>
              <span className="muted">{explain}</span>
            </label>
          );
        })}
      </div>
      <label>
        Title
        <input autoFocus value={title} onChange={(e) => setTitle(e.target.value)}
          placeholder="e.g. Bulk RNA-seq differential expression at BSR"
          onKeyDown={(e) => e.key === "Enter" && title.trim() && create()} />
      </label>
      <div className="modal-foot">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!title.trim()} onClick={create}>Create and start writing</button>
      </div>
    </Modal>
  );
}

// ---- History ---------------------------------------------------------------------

function History({ labId }: { labId: string }) {
  const [rows, setRows] = useState<{ commit: string; author: string; date: string; message: string }[] | null>(null);
  useEffect(() => {
    get(`/api/labs/${labId}/history`).then(setRows).catch(() => setRows([]));
  }, [labId]);
  if (!rows) return <div className="loading">Loading…</div>;
  return (
    <section className="panel">
      <Help>Every save is recorded with who made it and when. BSR can restore any earlier version.</Help>
      {rows.length === 0 ? (
        <Empty title="No history yet." />
      ) : (
        <table>
          <thead><tr><th>When</th><th>Who</th><th>What changed</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.commit}><td>{r.date}</td><td>{r.author}</td><td>{r.message.replace(/^portal: /, "")}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
