import { useEffect, useState } from "react";
import { Ctx, go } from "../App";
import { del, get, put } from "../api";
import { Help, MarkdownEditor, Modal, attempt, useUnsavedWarning } from "../components";

export default function DocumentEditor({ ctx, labId, relPath, suggestion }: {
  ctx: Ctx; labId: string; relPath: string; suggestion: string | null;
}) {
  const [text, setText] = useState<string | null>(null);
  const [saved, setSaved] = useState("");
  const [canEdit, setCanEdit] = useState(false);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [history, setHistory] = useState<{ commit: string; author: string; date: string; message: string }[]>([]);
  const lab = ctx.labs.find((l) => l.lab_id === labId);

  async function load() {
    try {
      const d = await get<{ text: string; can_edit: boolean }>(`/api/labs/${labId}/docs/${relPath}`);
      setText(d.text);
      setSaved(d.text);
      setCanEdit(d.can_edit);
      setHistory(await get(`/api/labs/${labId}/history?path=${encodeURIComponent(relPath)}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  useEffect(() => {
    load();
  }, [labId, relPath]);

  const dirty = text !== null && text !== saved;
  useUnsavedWarning(dirty);

  async function save() {
    if (!dirty) return;
    const ok = await attempt(() => put(`/api/labs/${labId}/docs/${relPath}`, { text }),
      "Saved. Claude will see the change on its next question.");
    if (ok) {
      setSaved(text!);
      setHistory(await get(`/api/labs/${labId}/history?path=${encodeURIComponent(relPath)}`));
      ctx.reloadLabs();
    }
  }

  async function remove() {
    const ok = await attempt(() => del(`/api/labs/${labId}/docs/${relPath}`), "Document deleted.");
    if (ok) {
      await ctx.reloadLabs();
      go(`/lab/${labId}/documents`);
    }
  }

  const isCore = lab?.kind === "core";
  const coreArea = isCore ? (relPath.startsWith("internal/") ? "internal" : relPath.startsWith("shared/") ? "shared" : null) : null;
  const backHref = isCore ? `#/${lab?.yours ? "lab" : "core"}/${labId}/${coreArea === "internal" || relPath.match(/^(members|private)\//) ? "internal" : "shared"}`
    : relPath === "context.md" ? `#/lab/${labId}/about` : `#/lab/${labId}/documents`;
  const personalMatch = relPath.match(/^members\/([^/]+)\/(.+)\.md$/);
  const privateMatch = relPath.match(/^private\/([^/]+)\/(.+)\.md$/);
  const docId = personalMatch
    ? `doc:${labId}/~${personalMatch[1]}/${personalMatch[2]}`
    : privateMatch
    ? `doc:${labId}/~${privateMatch[1]}/private/${privateMatch[2]}`
    : `doc:${labId}/${relPath.replace(/\.md$/, "")}`;

  if (error) return <div className="page"><a href={backHref}>← Back</a><div className="error-box">{error}</div></div>;
  if (text === null) return <div className="page loading">Loading…</div>;

  return (
    <div className="page">
      <a href={backHref} className="back">← {lab?.name ?? labId}</a>
      <div className="page-head">
        <div>
          <h1>{text.match(/^# (.+)$/m)?.[1] ?? relPath}</h1>
          <p className="muted">
            Claude refers to this as <code>{docId}</code>
            {personalMatch && <> · <span className="badge warn">Personal</span> only {personalMatch[1] === ctx.me.user ? "you" : personalMatch[1]} and the lab's PI can see it</>}
            {privateMatch && <> · <span className="badge warn">Only you</span> nobody else can see it, not even your PI</>}
            {coreArea === "shared" && <> · <span className="badge info">Shared</span> every lab using CodeSpringHub can read it</>}
            {coreArea === "internal" && <> · <span className="badge warn">Internal</span> only {lab?.name}'s staff can see it</>}
          </p>
        </div>
      </div>

      {suggestion && (
        <div className="callout">
          <strong>Suggested by Claude:</strong> {suggestion}
          <div className="muted small">Edit the document below to include it, save, then mark it applied in the Inbox.</div>
        </div>
      )}

      <Help>
        The first line starting with <code># </code> is the title. Use <code>## </code> for sections.
        Write <code>TODO</code> for unfinished parts. <kbd>Ctrl</kbd>+<kbd>S</kbd> saves.
      </Help>
      <MarkdownEditor value={text} onChange={setText} onSave={save} readOnly={!canEdit} />

      {canEdit && (
        <div className="savebar">
          {relPath !== "context.md" && <button className="ghost danger-text" onClick={() => setConfirmDelete(true)}>Delete this document…</button>}
          <span className="spacer" />
          <span className="muted">{dirty ? "Unsaved changes" : "All changes saved"}</span>
          <button onClick={() => setText(saved)} disabled={!dirty}>Undo changes</button>
          <button className="primary" onClick={save} disabled={!dirty}>Save</button>
        </div>
      )}

      {history.length > 0 && (
        <section className="panel">
          <h3>Recent changes to this document</h3>
          <ul className="plain">
            {history.slice(0, 8).map((h) => (
              <li key={h.commit}><span className="muted">{h.date}</span> · {h.author}</li>
            ))}
          </ul>
        </section>
      )}

      {confirmDelete && (
        <Modal title="Delete this document?" onClose={() => setConfirmDelete(false)}>
          <p>Claude will no longer see it. BSR can still restore it from the history if needed.</p>
          <div className="modal-foot">
            <button onClick={() => setConfirmDelete(false)}>Keep it</button>
            <button className="danger" onClick={remove}>Delete</button>
          </div>
        </Modal>
      )}
    </div>
  );
}
