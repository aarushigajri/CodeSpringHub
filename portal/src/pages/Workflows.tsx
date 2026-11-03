import { useEffect, useState } from "react";
import { Ctx } from "../App";
import { Workflow, del, get, post, put } from "../api";
import { Badge, Empty, Help, Modal, attempt } from "../components";

export default function Workflows({ ctx }: { ctx: Ctx }) {
  const [rows, setRows] = useState<Workflow[] | null>(null);
  const [editing, setEditing] = useState<Workflow | "new" | null>(null);
  const canEdit = ctx.me.is_bsr;

  async function load() {
    setRows(await get<Workflow[]>("/api/workflows"));
  }
  useEffect(() => {
    load();
  }, []);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>BSR workflows</h1>
          <p className="muted">Shared with every lab. Claude recommends these before writing its own pipelines.</p>
        </div>
        {canEdit && <button className="primary" onClick={() => setEditing("new")}>+ Add workflow</button>}
      </div>
      <Help>
        <Badge kind="ok">Validated</Badge> means BSR has tested and signed off this workflow and version.{" "}
        <Badge kind="warn">Candidate</Badge> means recommended but not yet signed off; Claude tells users so.
        {!canEdit && " Only BSR staff can change this list."}
      </Help>
      {rows?.length === 0 && <Empty title="No workflows registered yet." />}
      {rows && rows.length > 0 && (
        <table>
          <thead><tr><th>Workflow</th><th>For</th><th>Version</th><th>Status</th>{canEdit && <th />}</tr></thead>
          <tbody>
            {rows.map((w) => (
              <tr key={w.id}>
                <td>
                  <strong>{w.url ? <a href={w.url} target="_blank" rel="noreferrer">{w.name}</a> : w.name}</strong>
                  <div className="muted small">{w.summary}</div>
                </td>
                <td>{w.assays.join(", ")}</td>
                <td>{w.version === "TODO" ? <span className="muted">not set</span> : w.version}</td>
                <td>{w.status === "validated" ? <Badge kind="ok">Validated</Badge> : <Badge kind="warn">Candidate</Badge>}</td>
                {canEdit && <td className="actions-cell"><button onClick={() => setEditing(w)}>Edit</button></td>}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {editing && <WorkflowForm ctx={ctx} existing={editing === "new" ? null : editing}
        onClose={() => setEditing(null)} onSaved={load} />}
    </div>
  );
}

function WorkflowForm({ ctx, existing, onClose, onSaved }: {
  ctx: Ctx; existing: Workflow | null; onClose: () => void; onSaved: () => Promise<void>;
}) {
  const [w, setW] = useState<Omit<Workflow, "id">>(
    existing ?? { name: "", assays: [], status: "candidate", version: "", url: "", summary: "" },
  );
  const [confirmDelete, setConfirmDelete] = useState(false);
  const set = (patch: Partial<Workflow>) => setW({ ...w, ...patch });

  async function save() {
    const ok = await attempt(
      () => (existing ? put(`/api/workflows/${existing.id}`, w) : post("/api/workflows", w)),
      "Workflow saved.",
    );
    if (ok) {
      await onSaved();
      onClose();
    }
  }
  async function remove() {
    const ok = await attempt(() => del(`/api/workflows/${existing!.id}`), "Workflow removed.");
    if (ok) {
      await onSaved();
      onClose();
    }
  }

  return (
    <Modal title={existing ? `Edit ${existing.name}` : "Add a workflow"} onClose={onClose}>
      <label>Name<input value={w.name} onChange={(e) => set({ name: e.target.value })} placeholder="nf-core/rnaseq" /></label>
      <div className="label">Data types it handles</div>
      <div className="checks">
        {ctx.me.assays.map((a) => (
          <label key={a} className="check">
            <input type="checkbox" checked={w.assays.includes(a)}
              onChange={(e) => set({ assays: e.target.checked ? [...w.assays, a] : w.assays.filter((x) => x !== a) })} />
            {a}
          </label>
        ))}
      </div>
      <div className="grid2">
        <label>Version BSR uses<input value={w.version === "TODO" ? "" : w.version} onChange={(e) => set({ version: e.target.value })} placeholder="3.14.0" /></label>
        <label>
          Status
          <select value={w.status} onChange={(e) => set({ status: e.target.value as Workflow["status"] })}>
            <option value="candidate">Candidate (recommended, not signed off)</option>
            <option value="validated">Validated (BSR signed off)</option>
          </select>
        </label>
      </div>
      <label>Link<input value={w.url} onChange={(e) => set({ url: e.target.value })} placeholder="https://nf-co.re/rnaseq" /></label>
      <label>One-line summary<input value={w.summary} onChange={(e) => set({ summary: e.target.value })} /></label>
      <div className="modal-foot">
        {existing && (confirmDelete
          ? <button className="danger" onClick={remove}>Really remove</button>
          : <button className="ghost danger-text" onClick={() => setConfirmDelete(true)}>Remove</button>)}
        <span className="spacer" />
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={save} disabled={!w.name.trim() || !w.assays.length}>Save</button>
      </div>
    </Modal>
  );
}
