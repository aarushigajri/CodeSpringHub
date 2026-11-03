// Readable views of what the Hub returns to Claude. Each view recognizes one result shape;
// anything unrecognized falls back to formatted JSON. The exact text is always one click away.

import { Badge, Markdown } from "./components";

type Any = any;

export function FriendlyResult({ tool, data, text }: { tool: string; data: Any; text: string }) {
  if (data === null) return <div className="md-preview"><Markdown text={text} /></div>;

  if (tool === "search_knowledge" && Array.isArray(data)) {
    if (!data.length) return <p className="muted">Nothing found. Claude would be told there are no matching documents.</p>;
    return (
      <div className="hits">
        {data.map((h: Any) => (
          <div key={h.doc_id} className="hit">
            <strong>{h.title}</strong> <Badge kind="muted">{h.category}</Badge>
            <div className="muted small"><code>{h.doc_id}</code></div>
            <div className="snippet"><Markdown text={h.snippet.replace(/\n+/g, " ")} /></div>
          </div>
        ))}
      </div>
    );
  }

  if (tool === "list_workflows" && Array.isArray(data)) {
    if (!data.length) return <p className="muted">No BSR workflow matches. Claude would suggest asking BSR.</p>;
    return (
      <table>
        <thead><tr><th>Workflow</th><th>For</th><th>Status</th></tr></thead>
        <tbody>
          {data.map((w: Any) => (
            <tr key={w.id}>
              <td><strong>{w.name}</strong><div className="muted small">{w.summary}</div></td>
              <td>{w.assays.join(", ")}</td>
              <td>{w.status === "validated" ? <Badge kind="ok">Validated</Badge> : <Badge kind="warn">Candidate</Badge>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }

  if (data && Array.isArray(data.assets)) return <DatasetView d={data} />;
  if (data && data.kind) return <AssetView a={data} />;

  return <pre className="result">{JSON.stringify(data, null, 2)}</pre>;
}

function DatasetView({ d }: { d: Any }) {
  return (
    <div className="kv">
      <div><span>Title</span>{d.title}</div>
      <div><span>Claude's ID</span><code>{d.id}</code></div>
      <div><span>Type</span>{d.assay || "–"}</div>
      <div><span>Description</span>{d.description || "–"}</div>
      <div><span>Access</span>{d.sensitivity === "sensitive" ? "Summaries only" : "Summaries + short previews"}</div>
      <div><span>Files</span>{d.assets.length}{d.assets_truncated ? "+ (list shortened)" : ""}</div>
      <ul className="file-list">
        {d.assets.map((a: Any) => <li key={a.asset}>📄 {a.asset} <span className="muted">{a.size}</span></li>)}
      </ul>
    </div>
  );
}

export function AssetView({ a }: { a: Any }) {
  if (a.kind === "table") {
    return (
      <div className="kv">
        <div><span>File</span>{a.asset} <span className="muted">({a.size})</span></div>
        <div><span>Size</span>{a.rows.toLocaleString()} rows × {a.n_columns} columns</div>
        <div><span>Columns</span>
          <span className="chips">{a.columns.map((c: Any) => <code key={c.name} title={c.type}>{c.name}</code>)}</span>
        </div>
        {a.preview_first_5_rows ? (
          <>
            <div className="muted small">First rows, as Claude sees them:</div>
            <div className="scroll-x">
              <table className="compact">
                <thead><tr>{a.columns.slice(0, 20).map((c: Any) => <th key={c.name}>{c.name}</th>)}</tr></thead>
                <tbody>{a.preview_first_5_rows.map((r: string[], i: number) => <tr key={i}>{r.map((v, j) => <td key={j}>{v}</td>)}</tr>)}</tbody>
              </table>
            </div>
          </>
        ) : <div className="muted small">No rows shown: this dataset is marked sensitive.</div>}
      </div>
    );
  }
  if (a.kind === "fastq") {
    return (
      <div className="kv">
        <div><span>File</span>{a.asset} <span className="muted">({a.size})</span></div>
        <div><span>Reads checked</span>{a.reads_sampled.toLocaleString()} <span className="muted">({a.note})</span></div>
        <div><span>Read length</span>{a.read_length_min}–{a.read_length_max} (average {a.read_length_mean})</div>
        <div><span>GC content</span>{a.gc_percent}%</div>
        <div className="muted small">No sequences are sent to Claude, only these statistics.</div>
      </div>
    );
  }
  if (a.kind === "text") {
    return (
      <div className="kv">
        <div><span>File</span>{a.asset} <span className="muted">({a.size})</span></div>
        <div className="muted small">First lines{a.truncated ? " (file continues)" : ""}:</div>
        <pre className="result">{a.first_lines}</pre>
      </div>
    );
  }
  return (
    <div className="kv">
      <div><span>File</span>{a.asset} <span className="muted">({a.size})</span></div>
      <div className="muted">{a.note}</div>
    </div>
  );
}
