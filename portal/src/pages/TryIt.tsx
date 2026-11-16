import { useEffect, useState } from "react";
import { Ctx } from "../App";
import { Dataset, callTool, get } from "../api";
import { Help } from "../components";
import { FriendlyResult } from "../Friendly";

type Tool = { name: string; description: string; schema: { properties: Record<string, any>; required?: string[] } };
type Result = { tool: string; args: Record<string, unknown>; is_error: boolean; content: string[]; characters: number };

export default function TryIt({ ctx }: { ctx: Ctx }) {
  const [labId, setLabId] = useState(ctx.labs[0]?.lab_id ?? "");
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [datasetId, setDatasetId] = useState("");
  const [query, setQuery] = useState("");
  const [assay, setAssay] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!labId) return;
    get<Dataset[]>(`/api/labs/${labId}/datasets`).then((d) => {
      setDatasets(d);
      setDatasetId(d[0]?.id ?? "");
    });
  }, [labId]);

  async function run(tool: string, args: Record<string, unknown>) {
    setBusy(true);
    try {
      const r = await callTool(tool, args);
      setResult({ tool, args, ...r });
    } catch (e) {
      setResult({ tool, args, is_error: true, content: [e instanceof Error ? e.message : String(e)], characters: 0 });
    }
    setBusy(false);
  }

  return (
    <div className="page">
      <h1>Try it as Claude</h1>
      <p className="lead">
        Ask the Hub the same questions Claude asks, and see exactly what Claude would receive, under your own
        permissions. Use this to check your lab page and datasets before researchers rely on them.
      </p>

      <div className="grid2 top">
        <label>
          Lab
          <select value={labId} onChange={(e) => setLabId(e.target.value)}>
            {ctx.labs.map((l) => <option key={l.lab_id} value={l.lab_id}>{l.name}</option>)}
          </select>
        </label>
      </div>

      <div className="try-cards">
        <div className="try-card">
          <strong>What does Claude know about this lab?</strong>
          <p className="muted">The lab context page plus the list of documents.</p>
          <button className="primary" disabled={busy || !labId} onClick={() => run("get_lab_context", { lab_id: labId })}>Show me</button>
        </div>
        <div className="try-card">
          <strong>Search the knowledge</strong>
          <p className="muted">What Claude finds when it looks something up.</p>
          <form onSubmit={(e) => { e.preventDefault(); query.trim() && run("search_knowledge", { query, lab_id: labId }); }}>
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="e.g. reference genome" />
            <button className="primary" disabled={busy || !query.trim()}>Search</button>
          </form>
        </div>
        <div className="try-card">
          <strong>Look inside a dataset</strong>
          <p className="muted">The file list Claude gets for a registered dataset.</p>
          {datasets.length ? (
            <>
              <select value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
                {datasets.map((d) => <option key={d.id} value={d.id}>{d.title}</option>)}
              </select>
              <button className="primary" disabled={busy} onClick={() => run("describe_dataset", { dataset_id: datasetId })}>Show me</button>
            </>
          ) : <p className="muted small">No datasets in this lab yet.</p>}
        </div>
        <div className="try-card">
          <strong>Which BSR workflow fits?</strong>
          <p className="muted">What Claude recommends for a type of data.</p>
          <select value={assay} onChange={(e) => setAssay(e.target.value)}>
            <option value="">All workflows</option>
            {ctx.me.assays.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          <button className="primary" disabled={busy} onClick={() => run("list_workflows", assay ? { assay } : {})}>Show me</button>
        </div>
      </div>

      {result && <ResultView result={result} />}

      <details className="panel advanced">
        <summary>Advanced: call any tool directly</summary>
        <Help>These are all the tools Claude has. Fill in the inputs and run one.</Help>
        <AnyTool onRun={run} busy={busy} />
      </details>
    </div>
  );
}

const LIST_TOOLS = new Set(["list_labs", "search_knowledge", "list_datasets"]);

function ResultView({ result }: { result: Result }) {
  const [raw, setRaw] = useState(false);
  const text = result.content.join("\n");
  // A tool that returns a list sends one block per item; rebuild the list.
  let data: any = null;
  try {
    const parsed = result.content.map((c) => JSON.parse(c));
    data = LIST_TOOLS.has(result.tool) ? parsed : parsed.length === 1 ? parsed[0] : parsed;
  } catch {
    data = null;
  }
  return (
    <section className={`panel result-panel ${result.is_error ? "error" : ""}`}>
      <div className="row-between">
        <strong>{result.is_error ? "Claude would get this error:" : "Claude would receive:"}</strong>
        {!result.is_error && (
          <span className="muted small">
            {result.characters.toLocaleString()} characters ·{" "}
            <button className="link" onClick={() => setRaw(!raw)}>{raw ? "show readable view" : "show exactly what Claude receives"}</button>
          </span>
        )}
      </div>
      {result.is_error ? (
        <div className="error-box">{text.replace(/^Error executing tool \w+: /, "")}</div>
      ) : raw ? (
        <>
          <code className="muted small">{result.tool}({JSON.stringify(result.args)})</code>
          <pre className="result">{text || "(empty)"}</pre>
        </>
      ) : (
        <FriendlyResult tool={result.tool} data={data} text={text} />
      )}
    </section>
  );
}

function AnyTool({ onRun, busy }: { onRun: (t: string, a: Record<string, unknown>) => void; busy: boolean }) {
  const [tools, setTools] = useState<Tool[]>([]);
  const [name, setName] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  useEffect(() => {
    get<Tool[]>("/api/tools").then((t) => {
      setTools(t);
      setName(t[0]?.name ?? "");
    });
  }, []);
  const tool = tools.find((t) => t.name === name);

  function submit() {
    const args: Record<string, unknown> = {};
    for (const [k, spec] of Object.entries(tool?.schema.properties ?? {})) {
      const v = values[k]?.trim();
      if (!v) continue;
      const isArray = spec.type === "array" || spec.anyOf?.some((s: any) => s.type === "array");
      args[k] = isArray ? v.split(",").map((s) => s.trim()).filter(Boolean) : v;
    }
    onRun(name, args);
  }

  return (
    <div className="stack">
      <label>
        Tool
        <select value={name} onChange={(e) => { setName(e.target.value); setValues({}); }}>
          {tools.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
        </select>
      </label>
      {tool && <p className="muted small pre">{tool.description}</p>}
      {tool && Object.entries(tool.schema.properties).map(([k]) => (
        <label key={k}>
          {k}{tool.schema.required?.includes(k) ? " (required)" : " (optional)"}
          <input value={values[k] ?? ""} onChange={(e) => setValues({ ...values, [k]: e.target.value })} />
        </label>
      ))}
      <div><button className="primary" disabled={busy || !tool} onClick={submit}>Run</button></div>
    </div>
  );
}
