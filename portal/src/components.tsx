import DOMPurify from "dompurify";
import { marked } from "marked";
import { ReactNode, useEffect, useRef, useState } from "react";
import { get } from "./api";

// ---- notifications ---------------------------------------------------------------

type Toast = { id: number; text: string; kind: "ok" | "error" };
let pushToast: (t: Omit<Toast, "id">) => void = () => {};

export function notify(text: string, kind: "ok" | "error" = "ok") {
  pushToast({ text, kind });
}

export function Toasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  useEffect(() => {
    let n = 0;
    pushToast = (t) => {
      const id = ++n;
      setToasts((ts) => [...ts, { ...t, id }]);
      setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), t.kind === "error" ? 8000 : 3500);
    };
  }, []);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          {t.text}
        </div>
      ))}
    </div>
  );
}

/** Run an async action, show its error as a toast, and report success. */
export async function attempt<T>(fn: () => Promise<T>, success?: string): Promise<T | undefined> {
  try {
    const r = await fn();
    if (success) notify(success);
    return r;
  } catch (e) {
    notify(e instanceof Error ? e.message : String(e), "error");
    return undefined;
  }
}

// ---- small building blocks -------------------------------------------------------

export function Help({ children }: { children: ReactNode }) {
  return <div className="help">{children}</div>;
}

export function Badge({ kind, children }: { kind: string; children: ReactNode }) {
  return <span className={`badge ${kind}`}>{children}</span>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children && <div>{children}</div>}
    </div>
  );
}

export function Modal({ title, onClose, children, wide }: {
  title: string; onClose: () => void; children: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Markdown({ text }: { text: string }) {
  const html = DOMPurify.sanitize(marked.parse(text, { async: false }) as string);
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />;
}

// ---- Markdown editor with live preview --------------------------------------------

export function MarkdownEditor({ value, onChange, onSave, readOnly }: {
  value: string; onChange: (v: string) => void; onSave?: () => void; readOnly?: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [view, setView] = useState<"split" | "edit" | "preview">("split");

  function wrapLine(prefix: string) {
    const ta = ref.current;
    if (!ta) return;
    const start = value.lastIndexOf("\n", ta.selectionStart - 1) + 1;
    onChange(value.slice(0, start) + prefix + value.slice(start));
    requestAnimationFrame(() => ta.focus());
  }
  function wrapSelection(mark: string) {
    const ta = ref.current;
    if (!ta) return;
    const { selectionStart: a, selectionEnd: b } = ta;
    onChange(value.slice(0, a) + mark + (value.slice(a, b) || "text") + mark + value.slice(b));
    requestAnimationFrame(() => ta.focus());
  }

  return (
    <div className="md-editor">
      <div className="md-toolbar">
        {!readOnly && (
          <>
            <button type="button" onClick={() => wrapLine("## ")} title="Section heading">Heading</button>
            <button type="button" onClick={() => wrapSelection("**")} title="Bold"><b>B</b></button>
            <button type="button" onClick={() => wrapLine("- ")} title="Bullet point">• List</button>
            <button type="button" onClick={() => wrapSelection("`")} title="Code, file names, commands">Code</button>
          </>
        )}
        <span className="spacer" />
        <div className="segmented">
          {(["edit", "split", "preview"] as const).map((v) => (
            <button key={v} type="button" className={view === v ? "on" : ""} onClick={() => setView(v)}>
              {v === "edit" ? "Write" : v === "split" ? "Side by side" : "Preview"}
            </button>
          ))}
        </div>
      </div>
      <div className={`md-panes ${view}`}>
        {view !== "preview" && (
          <textarea
            ref={ref}
            value={value}
            readOnly={readOnly}
            spellCheck
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "s") {
                e.preventDefault();
                onSave?.();
              }
            }}
          />
        )}
        {view !== "edit" && (
          <div className="md-preview">
            <Markdown text={value} />
          </div>
        )}
      </div>
    </div>
  );
}

// ---- folder picker -----------------------------------------------------------------

type Listing = {
  path: string | null;
  parent: string | null;
  folders: { name: string; path: string }[];
  files: { name: string; size: number }[];
};

export function humanSize(n: number) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function FolderPicker({ start, onPick }: { start?: string; onPick: (path: string) => void }) {
  const [listing, setListing] = useState<Listing | null>(null);
  const [error, setError] = useState("");
  const [typed, setTyped] = useState("");

  async function open(path: string | null) {
    setError("");
    try {
      const l = await get<Listing>(`/api/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`);
      setListing(l);
      setTyped(l.path ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  useEffect(() => {
    open(start ?? null);
  }, []);

  const crumbs = listing?.path ? listing.path.split("/").filter(Boolean) : [];
  return (
    <div className="picker">
      <form
        className="picker-path"
        onSubmit={(e) => {
          e.preventDefault();
          open(typed);
        }}
      >
        <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="/grid/yourlab/…" aria-label="Folder path" />
        <button type="submit">Go</button>
      </form>
      <div className="crumbs">
        <button className="link" onClick={() => open(null)}>Start</button>
        {crumbs.map((c, i) => {
          const p = "/" + crumbs.slice(0, i + 1).join("/");
          return (
            <span key={p}>
              {" / "}
              <button className="link" onClick={() => open(p)}>{c}</button>
            </span>
          );
        })}
      </div>
      {error && <div className="error-box">{error}</div>}
      <div className="picker-list">
        {listing?.parent && (
          <button className="row" onClick={() => open(listing.parent)}>⬑ Up one level</button>
        )}
        {listing?.folders.map((f) => (
          <button key={f.path} className="row" onClick={() => open(f.path)}>📁 {f.name}</button>
        ))}
        {listing?.files.map((f) => (
          <div key={f.name} className="row file">📄 {f.name} <span className="muted">{humanSize(f.size)}</span></div>
        ))}
        {listing && !listing.folders.length && !listing.files.length && <div className="muted pad">This folder is empty.</div>}
      </div>
      {listing?.path && (
        <div className="picker-foot">
          <span className="muted">Selected: <code>{listing.path}</code></span>
          <button className="primary" onClick={() => onPick(listing.path!)}>Use this folder</button>
        </div>
      )}
    </div>
  );
}

/** Warn before leaving a page with unsaved edits. */
export function useUnsavedWarning(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);
}

// ---- "Who can see it?" -------------------------------------------------------------

export function ScopeChoices({ lab, scope, setScope }: {
  lab: import("./api").Lab; scope: string; setScope: (s: import("./api").Scope) => void;
}) {
  const insider = lab.kind === "core" ? lab.role === "staff" || lab.role === "lead" : lab.role !== "bsr";
  const opts: [import("./api").Scope, string, string, boolean][] = lab.kind === "core"
    ? [
        ["shared", "Everyone", "Shared: every lab using CodeSpringHub can read it.", lab.can_edit],
        ["lab", "Core staff", "Internal: only this core's staff.", lab.can_edit],
        ["personal", "Me and the core lead", "Other staff can't see it.", insider],
        ["private", "Only me", "Not the lead, not other staff.", insider],
      ]
    : [
        ["lab", "The whole lab", "All lab members, and BSR when helping your lab.", lab.can_edit],
        ["personal", "Me and my PI", "Work in progress. Other members and BSR can't see it.", insider],
        ["private", "Only me", "Not your PI, not BSR, not other members.", insider],
      ];
  return (
    <>
      <div className="label">Who can see it?</div>
      <div className={`choices ${opts.length === 4 ? "four" : "three"}`}>
        {opts.map(([id, label, explain, ok]) => (
          <label key={id} className={`choice ${scope === id ? "on" : ""} ${ok ? "" : "disabled"}`}>
            <input type="radio" disabled={!ok} checked={scope === id} onChange={() => setScope(id)} />
            <strong>{label}</strong>
            <span className="muted">{explain}</span>
          </label>
        ))}
      </div>
    </>
  );
}
