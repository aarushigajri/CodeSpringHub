import { useEffect, useState } from "react";
import { Ctx } from "../App";
import { Dataset, Lab, callTool, del, get, post, put } from "../api";
import { Badge, Empty, FolderPicker, Help, Modal, attempt } from "../components";
import { AssetView } from "../Friendly";

const idQuery = (id: string) => `?id=${encodeURIComponent(id)}`;

export default function Datasets({ ctx, lab, openAdd, onChanged }: {
  ctx: Ctx; lab: Lab; openAdd: boolean; onChanged: () => Promise<void>;
}) {
  const [rows, setRows] = useState<Dataset[] | null>(null);
  const [editing, setEditing] = useState<Dataset | "new" | null>(openAdd ? "new" : null);
  const [removing, setRemoving] = useState<Dataset | null>(null);
  const [peek, setPeek] = useState<Dataset | null>(null);
  const isMember = lab.role !== "bsr";

  async function load() {
    setRows(await get<Dataset[]>(`/api/labs/${lab.lab_id}/datasets`));
  }
  useEffect(() => {
    load();
  }, [lab.lab_id]);

  async function remove(ds: Dataset) {
    const ok = await attempt(() => del(`/api/labs/${lab.lab_id}/dataset${idQuery(ds.id)}`),
      "Removed from the Hub. The files themselves were not touched.");
    if (ok) {
      setRemoving(null);
      await load();
      await onChanged();
    }
  }

  const shared = rows?.filter((d) => d.scope === "lab") ?? [];
  const personal = rows?.filter((d) => d.scope === "personal") ?? [];

  return (
    <div className="stack">
      <div className="row-between">
        <Help>
          Register <strong>data</strong> (Claude gets summaries: file lists, table columns, read statistics) or{" "}
          <strong>code</strong> (your lab's scripts and pipelines, which Claude can read in full to explain, debug or
          adapt). Nothing moves, and Claude never sees folder locations.
        </Help>
        {(lab.can_edit || isMember) && <button className="primary" onClick={() => setEditing("new")}>+ Add data or code</button>}
      </div>
      {rows === null && <div className="loading">Loading…</div>}

      {rows && (
        <>
          <h3>Shared with the lab</h3>
          {shared.length === 0
            ? <Empty title="Nothing shared yet.">Everything here is visible to all lab members and to BSR.</Empty>
            : <Table rows={shared} lab={lab} onPeek={setPeek} onEdit={setEditing} onRemove={setRemoving} />}

          {(isMember || personal.length > 0) && (
            <>
              <h3>{lab.role === "pi" ? "Personal (yours, and your members' as PI)" : "Personal: only you and your PI"}</h3>
              {personal.length === 0
                ? <Empty title="No personal items.">Use these for work in progress you don't want to share with the whole lab yet. BSR can't see them.</Empty>
                : <Table rows={personal} lab={lab} onPeek={setPeek} onEdit={setEditing} onRemove={setRemoving} />}
            </>
          )}
        </>
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
        <Modal title="Remove this from the Hub?" onClose={() => setRemoving(null)}>
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

function Table({ rows, lab, onPeek, onEdit, onRemove }: {
  rows: Dataset[]; lab: Lab; onPeek: (d: Dataset) => void; onEdit: (d: Dataset) => void; onRemove: (d: Dataset) => void;
}) {
  return (
    <table>
      <thead>
        <tr><th>Name</th><th>Type</th><th>Project</th><th>Claude sees</th><th>Added</th><th /></tr>
      </thead>
      <tbody>
        {rows.map((d) => (
          <tr key={d.id}>
            <td>
              <strong>{d.title}</strong>
              <div className="muted small"><code>{d.id}</code></div>
              <div className="muted small path" title={d.path}>{d.path}</div>
            </td>
            <td>
              {d.kind === "code" ? <Badge kind="info">Code</Badge> : <Badge kind="muted">Data</Badge>}
              <div className="small">{d.assay || ""}</div>
            </td>
            <td>{lab.projects.find((p) => p.id === d.project)?.name ?? "–"}</td>
            <td>
              {d.kind === "code"
                ? <Badge kind="info">Full source files</Badge>
                : d.sensitivity === "sensitive"
                  ? <Badge kind="warn">Summaries only</Badge>
                  : <Badge kind="ok">Summaries + previews</Badge>}
            </td>
            <td className="small">{d.registered_on}<div className="muted">{d.owner ?? d.registered_by}</div></td>
            <td className="actions-cell">
              <button onClick={() => onPeek(d)}>What Claude sees</button>
              {d.can_edit && <button onClick={() => onEdit(d)}>Edit</button>}
              {d.can_edit && <button className="ghost danger-text" onClick={() => onRemove(d)}>Remove</button>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---- Add / edit wizard -----------------------------------------------------------

function DatasetWizard({ ctx, lab, existing, onClose, onSaved }: {
  ctx: Ctx; lab: Lab; existing: Dataset | null; onClose: () => void; onSaved: () => Promise<void>;
}) {
  const canShare = lab.can_edit;
  const canPersonal = lab.role !== "bsr";
  const [step, setStep] = useState(existing ? 2 : 1);
  const [path, setPath] = useState(existing?.path ?? "");
  const [kind, setKind] = useState<"data" | "code">(existing?.kind ?? "data");
  const [scope, setScope] = useState(existing?.scope ?? (canShare ? "lab" : "personal"));
  const [title, setTitle] = useState(existing?.title ?? "");
  const [project, setProject] = useState(existing?.project ?? "");
  const [assay, setAssay] = useState(existing?.assay ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [sensitivity, setSensitivity] = useState(existing?.sensitivity ?? "standard");
  const [saved, setSaved] = useState<{ id: string; kind: "data" | "code" } | null>(null);

  async function save() {
    const body = { title, path, kind, scope, project: project || null, assay, description, sensitivity };
    const r = await attempt(() =>
      existing
        ? put<{ id: string }>(`/api/labs/${lab.lab_id}/dataset${idQuery(existing.id)}`, body)
        : post<{ id: string }>(`/api/labs/${lab.lab_id}/datasets`, body),
    );
    if (r) {
      setSaved({ id: r.id, kind });
      setStep(3);
      await onSaved();
    }
  }

  return (
    <Modal title={existing ? `Edit ${existing.title}` : "Add data or code"} onClose={onClose} wide>
      <ol className="wizard-steps">
        <li className={step === 1 ? "on" : step > 1 ? "done" : ""}>Choose folder</li>
        <li className={step === 2 ? "on" : step > 2 ? "done" : ""}>Describe it</li>
        <li className={step === 3 ? "on" : ""}>Check what Claude sees</li>
      </ol>

      {step === 1 && (
        <>
          <Help>
            Click through the folders to find your data or code, then press <strong>Use this folder</strong>. You'll
            only see folders you already have permission to read.
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

          <div className="label">What is it?</div>
          <div className="choices two">
            <label className={`choice ${kind === "data" ? "on" : ""}`}>
              <input type="radio" checked={kind === "data"} onChange={() => setKind("data")} />
              <strong>Data</strong>
              <span className="muted">Sequencing files, tables, results. Claude gets summaries, never whole files.</span>
            </label>
            <label className={`choice ${kind === "code" ? "on" : ""}`}>
              <input type="radio" checked={kind === "code"} onChange={() => setKind("code")} />
              <strong>Code</strong>
              <span className="muted">Scripts, notebooks, pipelines. Claude can read the source to explain or adapt it.</span>
            </label>
          </div>

          {!existing && (
            <>
              <div className="label">Who can see it?</div>
              <div className="choices two">
                <label className={`choice ${scope === "lab" ? "on" : ""} ${canShare ? "" : "disabled"}`}>
                  <input type="radio" disabled={!canShare} checked={scope === "lab"} onChange={() => setScope("lab")} />
                  <strong>The whole lab</strong>
                  <span className="muted">All lab members, and BSR when helping your lab.</span>
                </label>
                <label className={`choice ${scope === "personal" ? "on" : ""} ${canPersonal ? "" : "disabled"}`}>
                  <input type="radio" disabled={!canPersonal} checked={scope === "personal"} onChange={() => setScope("personal")} />
                  <strong>Just me (and my PI)</strong>
                  <span className="muted">Private work in progress. Other members and BSR can't see it.</span>
                </label>
              </div>
            </>
          )}

          <label>
            Short title
            <input autoFocus value={title} onChange={(e) => setTitle(e.target.value)}
              placeholder={kind === "code" ? "e.g. RNA-seq analysis scripts" : "e.g. Mouse WGS, 12 samples"} />
          </label>
          <div className="grid2">
            <label>
              {kind === "code" ? "Analysis type (optional)" : "Type of data"}
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
              placeholder={kind === "code"
                ? "What do these scripts do? Language, how they're run, anything Claude should know."
                : "What is this data? Samples, conditions, sequencing, anything Claude should know."} />
          </label>
          {kind === "data" && (
            <>
              <div className="label">How much may Claude see?</div>
              <div className="choices two">
                <label className={`choice ${sensitivity === "standard" ? "on" : ""}`}>
                  <input type="radio" checked={sensitivity === "standard"} onChange={() => setSensitivity("standard")} />
                  <strong>Summaries and short previews</strong>
                  <span className="muted">File lists, table columns and the first few rows, read statistics.</span>
                </label>
                <label className={`choice ${sensitivity === "sensitive" ? "on" : ""}`}>
                  <input type="radio" checked={sensitivity === "sensitive"} onChange={() => setSensitivity("sensitive")} />
                  <strong>Summaries only (sensitive)</strong>
                  <span className="muted">File lists, sizes and statistics, but never actual rows or text from files.</span>
                </label>
              </div>
            </>
          )}
          <div className="modal-foot">
            <button onClick={onClose}>Cancel</button>
            <button className="primary" disabled={!title.trim() || !path} onClick={save}>
              {existing ? "Save changes" : "Add to the Hub"}
            </button>
          </div>
        </>
      )}

      {step === 3 && saved && (
        <>
          <div className="callout ok">
            Saved as <code>{saved.id}</code>. This is exactly what Claude gets when it asks about it:
          </div>
          <DescribePreview datasetId={saved.id} kind={saved.kind} />
          <div className="modal-foot">
            <button className="primary" onClick={onClose}>Done</button>
          </div>
        </>
      )}
    </Modal>
  );
}

// ---- What Claude sees -------------------------------------------------------------

type Described = { assets: { asset: string; size: string }[]; assets_truncated: boolean; git?: { branch: string; last_commit: string } | null };

function DescribePreview({ datasetId, kind }: { datasetId: string; kind: "data" | "code" }) {
  const [data, setData] = useState<Described | null>(null);
  const [error, setError] = useState("");
  const [inspected, setInspected] = useState<{ asset: string; error: boolean; text: string; source: boolean } | null>(null);

  useEffect(() => {
    callTool("describe_dataset", { dataset_id: datasetId }).then((r) =>
      r.is_error ? setError(r.content[0]) : setData(JSON.parse(r.content[0])),
    );
  }, [datasetId]);

  async function inspect(asset: string) {
    const r = kind === "code"
      ? await callTool("read_code_file", { resource_id: datasetId, path: asset })
      : await callTool("inspect_asset", { dataset_id: datasetId, asset });
    setInspected({ asset, error: r.is_error, text: r.content[0], source: kind === "code" });
  }

  if (error) return <div className="error-box">{error}</div>;
  if (!data) return <div className="loading">Asking the Hub…</div>;
  return (
    <div className="describe">
      {data.git?.last_commit && (
        <div className="muted small">Git: branch <code>{data.git.branch}</code>, last change {data.git.last_commit}</div>
      )}
      <div className="muted small">
        {data.assets.length} file{data.assets.length === 1 ? "" : "s"}{data.assets_truncated ? " (list shortened)" : ""}.
        Click a file to see {kind === "code" ? "the source Claude can read" : "Claude's summary of it"}.
      </div>
      <div className="asset-list">
        {data.assets.map((a) => (
          <button key={a.asset} className={`row ${inspected?.asset === a.asset ? "on" : ""}`} onClick={() => inspect(a.asset)}>
            📄 {a.asset} <span className="muted">{a.size}</span>
          </button>
        ))}
      </div>
      {inspected && (inspected.error
        ? <div className="error-box">{inspected.text.replace(/^Error executing tool \w+: /, "")}</div>
        : inspected.source
          ? <pre className="result">{inspected.text}</pre>
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
        Claude knows this as <code>{dataset.id}</code> and never sees the folder path.
        {dataset.scope === "personal" && " It's personal: only its owner and the lab's PI can use it."}
      </Help>
      <DescribePreview datasetId={dataset.id} kind={dataset.kind} />
    </Modal>
  );
}
