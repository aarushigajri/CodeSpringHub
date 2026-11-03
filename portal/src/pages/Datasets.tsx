import { useEffect, useState } from "react";
import { Ctx } from "../App";
import { Dataset, Lab, callTool, del, get, post, put } from "../api";
import { Badge, Empty, FolderPicker, Help, Modal, attempt } from "../components";
import { AssetView } from "../Friendly";

export default function Datasets({ ctx, lab, openAdd, onChanged }: {
  ctx: Ctx; lab: Lab; openAdd: boolean; onChanged: () => Promise<void>;
}) {
  const [rows, setRows] = useState<Dataset[] | null>(null);
  const [editing, setEditing] = useState<Dataset | "new" | null>(openAdd && lab.can_edit ? "new" : null);
  const [removing, setRemoving] = useState<Dataset | null>(null);
  const [peek, setPeek] = useState<Dataset | null>(null);

  async function load() {
    setRows(await get<Dataset[]>(`/api/labs/${lab.lab_id}/datasets`));
  }
  useEffect(() => {
    load();
  }, [lab.lab_id]);

  async function remove(ds: Dataset) {
    const ok = await attempt(() => del(`/api/labs/${lab.lab_id}/datasets/${ds.short_id}`),
      "Dataset removed from the Hub. The files themselves were not touched.");
    if (ok) {
      setRemoving(null);
      await load();
      await onChanged();
    }
  }

  return (
    <div className="stack">
      <div className="row-between">
        <Help>
          Registering a folder lets Claude look at <em>summaries</em> of it (file list, table columns, read statistics).
          The data never moves, and Claude never sees the folder's location.
        </Help>
        {lab.can_edit && <button className="primary" onClick={() => setEditing("new")}>+ Add a dataset</button>}
      </div>
      {rows === null && <div className="loading">Loading…</div>}
      {rows?.length === 0 && <Empty title="No datasets yet.">Click "Add a dataset" and pick a folder on the cluster.</Empty>}
      {rows && rows.length > 0 && (
        <table>
          <thead>
            <tr><th>Dataset</th><th>Type</th><th>Project</th><th>Access</th><th>Added</th><th /></tr>
          </thead>
          <tbody>
            {rows.map((d) => (
              <tr key={d.id}>
                <td>
                  <strong>{d.title}</strong>
                  <div className="muted small"><code>{d.id}</code></div>
                  <div className="muted small path" title={d.path}>{d.path}</div>
                </td>
                <td>{d.assay || "–"}</td>
                <td>{lab.projects.find((p) => p.id === d.project)?.name ?? "–"}</td>
                <td>
                  {d.sensitivity === "sensitive"
                    ? <Badge kind="warn">Summaries only</Badge>
                    : <Badge kind="ok">Summaries + previews</Badge>}
                </td>
                <td className="small">{d.registered_on}<div className="muted">{d.registered_by}</div></td>
                <td className="actions-cell">
                  <button onClick={() => setPeek(d)}>What Claude sees</button>
                  {lab.can_edit && <button onClick={() => setEditing(d)}>Edit</button>}
                  {lab.can_edit && <button className="ghost danger-text" onClick={() => setRemoving(d)}>Remove</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {editing && (
        <DatasetWizard ctx={ctx} lab={lab} existing={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            await load();
            await onChanged();
          }} />
      )}
      {peek && <WhatClaudeSees dataset={peek} onClose={() => setPeek(null)} />}
      {removing && (
        <Modal title="Remove this dataset from the Hub?" onClose={() => setRemoving(null)}>
          <p>
            Claude will no longer be able to see <strong>{removing.title}</strong>. The files on the cluster are{" "}
            <strong>not</strong> deleted or changed.
          </p>
          <div className="modal-foot">
            <button onClick={() => setRemoving(null)}>Cancel</button>
            <button className="danger" onClick={() => remove(removing)}>Remove from Hub</button>
          </div>
        </Modal>
      )}
    </div>
  );
}

// ---- Add / edit wizard -----------------------------------------------------------

function DatasetWizard({ ctx, lab, existing, onClose, onSaved }: {
  ctx: Ctx; lab: Lab; existing: Dataset | null; onClose: () => void; onSaved: () => Promise<void>;
}) {
  const [step, setStep] = useState(existing ? 2 : 1);
  const [path, setPath] = useState(existing?.path ?? "");
  const [title, setTitle] = useState(existing?.title ?? "");
  const [project, setProject] = useState(existing?.project ?? "");
  const [assay, setAssay] = useState(existing?.assay ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [sensitivity, setSensitivity] = useState(existing?.sensitivity ?? "standard");
  const [savedId, setSavedId] = useState<string | null>(null);

  async function save() {
    const body = { title, path, project: project || null, assay, description, sensitivity };
    const r = await attempt(() =>
      existing
        ? put<{ id: string }>(`/api/labs/${lab.lab_id}/datasets/${existing.short_id}`, body)
        : post<{ id: string }>(`/api/labs/${lab.lab_id}/datasets`, body),
    );
    if (r) {
      setSavedId(r.id);
      setStep(3);
      await onSaved();
    }
  }

  return (
    <Modal title={existing ? `Edit ${existing.title}` : "Add a dataset"} onClose={onClose} wide>
      <ol className="wizard-steps">
        <li className={step === 1 ? "on" : step > 1 ? "done" : ""}>Choose folder</li>
        <li className={step === 2 ? "on" : step > 2 ? "done" : ""}>Describe it</li>
        <li className={step === 3 ? "on" : ""}>Check what Claude sees</li>
      </ol>

      {step === 1 && (
        <>
          <Help>
            Click through the folders to find your data, then press <strong>Use this folder</strong>. You'll only see
            folders you already have permission to read.
          </Help>
          <FolderPicker start={path || undefined} onPick={(p) => {
            setPath(p);
            if (!title) setTitle(p.split("/").pop() ?? "");
            setStep(2);
          }} />
        </>
      )}

      {step === 2 && (
        <>
          <div className="picked">
            Folder: <code>{path}</code> <button className="link" onClick={() => setStep(1)}>change</button>
          </div>
          <label>
            Short title
            <input autoFocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Mouse WGS, 12 samples" />
          </label>
          <div className="grid2">
            <label>
              Type of data
              <select value={assay} onChange={(e) => setAssay(e.target.value)}>
                <option value="">Choose…</option>
                {ctx.me.assays.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </label>
            <label>
              Project
              <select value={project} onChange={(e) => setProject(e.target.value)}>
                <option value="">No project</option>
                {lab.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
          </div>
          <label>
            Description
            <textarea rows={3} value={description} onChange={(e) => setDescription(e.target.value)}
              placeholder="What is this data? Samples, conditions, sequencing, anything Claude should know." />
          </label>
          <div className="label">How much may Claude see?</div>
          <div className="choices two">
            <label className={`choice ${sensitivity === "standard" ? "on" : ""}`}>
              <input type="radio" checked={sensitivity === "standard"} onChange={() => setSensitivity("standard")} />
              <strong>Summaries and short previews</strong>
              <span className="muted">File lists, table columns and the first few rows, read statistics. Good for most data.</span>
            </label>
            <label className={`choice ${sensitivity === "sensitive" ? "on" : ""}`}>
              <input type="radio" checked={sensitivity === "sensitive"} onChange={() => setSensitivity("sensitive")} />
              <strong>Summaries only (sensitive)</strong>
              <span className="muted">File lists, sizes and statistics, but never actual rows or text from files.</span>
            </label>
          </div>
          <div className="modal-foot">
            <button onClick={onClose}>Cancel</button>
            <button className="primary" disabled={!title.trim() || !path} onClick={save}>
              {existing ? "Save changes" : "Add dataset"}
            </button>
          </div>
        </>
      )}

      {step === 3 && savedId && (
        <>
          <div className="callout ok">
            Saved as <code>{savedId}</code>. This is exactly what Claude gets when it asks about this dataset:
          </div>
          <DescribePreview datasetId={savedId} />
          <div className="modal-foot">
            <button className="primary" onClick={onClose}>Done</button>
          </div>
        </>
      )}
    </Modal>
  );
}

// ---- What Claude sees -------------------------------------------------------------

type Described = { assets: { asset: string; size: string }[]; assets_truncated: boolean };

function DescribePreview({ datasetId }: { datasetId: string }) {
  const [data, setData] = useState<Described | null>(null);
  const [error, setError] = useState("");
  const [inspected, setInspected] = useState<{ asset: string; error: boolean; text: string } | null>(null);

  useEffect(() => {
    callTool("describe_dataset", { dataset_id: datasetId }).then((r) =>
      r.is_error ? setError(r.content[0]) : setData(JSON.parse(r.content[0])),
    );
  }, [datasetId]);

  async function inspect(asset: string) {
    const r = await callTool("inspect_asset", { dataset_id: datasetId, asset });
    setInspected({ asset, error: r.is_error, text: r.content[0] });
  }

  if (error) return <div className="error-box">{error}</div>;
  if (!data) return <div className="loading">Asking the Hub…</div>;
  return (
    <div className="describe">
      <div className="muted small">{data.assets.length} file{data.assets.length === 1 ? "" : "s"}{data.assets_truncated ? " (list shortened)" : ""}. Click a file to see Claude's summary of it.</div>
      <div className="asset-list">
        {data.assets.map((a) => (
          <button key={a.asset} className={`row ${inspected?.asset === a.asset ? "on" : ""}`} onClick={() => inspect(a.asset)}>
            📄 {a.asset} <span className="muted">{a.size}</span>
          </button>
        ))}
      </div>
      {inspected && (inspected.error
        ? <div className="error-box">{inspected.text.replace(/^Error executing tool \w+: /, "")}</div>
        : (
          <div className="panel">
            <AssetView a={JSON.parse(inspected.text)} />
            <details>
              <summary className="muted small">Show exactly what Claude receives</summary>
              <pre className="result">{inspected.text}</pre>
            </details>
          </div>
        ))}
    </div>
  );
}

function WhatClaudeSees({ dataset, onClose }: { dataset: Dataset; onClose: () => void }) {
  return (
    <Modal title={`What Claude sees: ${dataset.title}`} onClose={onClose} wide>
      <Help>
        Claude knows this dataset as <code>{dataset.id}</code>. It never sees the folder path. Below is the file list
        it receives; click a file for the summary Claude would get.
      </Help>
      <DescribePreview datasetId={dataset.id} />
    </Modal>
  );
}

