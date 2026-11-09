import { useEffect, useState } from "react";
import { Ctx } from "../App";
import { get, post } from "../api";
import { Badge, Empty, Help, attempt } from "../components";

type Suggestion = {
  id: string; ts: string; user: string; lab_id: string; doc_id: string | null; suggestion: string; status: string;
  scope: "lab" | "personal"; can_change: boolean;
};
type Review = { id: number; key: string; ts: string; user: string; lab_id: string; summary: string; resource_ids: string; status: string };

export default function Inbox({ ctx }: { ctx: Ctx }) {
  const [data, setData] = useState<{ suggestions: Suggestion[]; reviews: Review[] } | null>(null);
  const [showDone, setShowDone] = useState(false);

  async function load() {
    setData(await get("/api/inbox"));
    ctx.refreshInbox();
  }
  useEffect(() => {
    load();
  }, []);

  if (!data) return <div className="page loading">Loading…</div>;

  const sugg = data.suggestions.filter((s) => showDone || s.status === "pending");
  const reviews = data.reviews.filter((r) => showDone || r.status !== "closed");
  const labName = (id: string) => ctx.labs.find((l) => l.lab_id === id)?.name ?? id;

  function docLink(s: Suggestion) {
    // doc:lab/guidance/x -> guidance/x.md ; doc:lab/~ann/notes/x -> members/ann/notes/x.md
    const inner = s.doc_id ? s.doc_id.split("/").slice(1) : [];
    const rel = !inner.length ? "context.md"
      : inner[0].startsWith("~") ? `members/${inner[0].slice(1)}/${inner.slice(1).join("/")}.md`
      : `${inner.join("/")}.md`;
    return `#/lab/${s.lab_id}/doc/${rel}?suggestion=${encodeURIComponent(s.suggestion)}`;
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Inbox</h1>
        <label className="check"><input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> Show finished items</label>
      </div>

      <h2>Suggestions from Claude</h2>
      <Help>
        When a researcher tells Claude something worth remembering, Claude can suggest adding it. Nothing changes until
        a person applies it: open the document, add the text, save, then mark it applied.
      </Help>
      {sugg.length === 0 && <Empty title="No suggestions waiting." />}
      {sugg.map((s) => (
        <div key={s.id} className="item">
          <div className="item-head">
            <strong>{labName(s.lab_id)}</strong>
            <span className="muted"> · {s.doc_id ?? "lab context"} · from {s.user} · {s.ts.slice(0, 16).replace("T", " ")}</span>
            {s.status !== "pending" && <Badge kind="muted">{s.status}</Badge>}
          </div>
          <blockquote>{s.suggestion}</blockquote>
          {s.status === "pending" && s.can_change && (
            <div className="actions">
              <a className="button primary" href={docLink(s)}>Open document to apply</a>
              <button onClick={async () => (await attempt(() => post(`/api/suggestions/${encodeURIComponent(s.id)}`, { status: "applied" }), "Marked as applied.")) && load()}>Mark applied</button>
              <button className="ghost" onClick={async () => (await attempt(() => post(`/api/suggestions/${encodeURIComponent(s.id)}`, { status: "dismissed" }), "Dismissed.")) && load()}>Dismiss</button>
            </div>
          )}
        </div>
      ))}

      <h2>Requests for BSR</h2>
      <Help>
        {ctx.me.is_bsr
          ? "Questions Claude escalated to BSR because they need an expert. Update the status as you work on them."
          : "Questions you sent to BSR through Claude. BSR will follow up with you directly."}
      </Help>
      {reviews.length === 0 && <Empty title="No open requests." />}
      {reviews.map((r) => (
        <div key={r.key} className="item">
          <div className="item-head">
            <strong>#{r.id} · {labName(r.lab_id)}</strong>
            <span className="muted"> · from {r.user} · {r.ts.slice(0, 16).replace("T", " ")}</span>
            <Badge kind={r.status === "closed" ? "muted" : r.status === "open" ? "warn" : "ok"}>{r.status}</Badge>
          </div>
          <blockquote>{r.summary}</blockquote>
          {JSON.parse(r.resource_ids).length > 0 && <div className="muted small">Datasets: {JSON.parse(r.resource_ids).join(", ")}</div>}
          {ctx.me.is_bsr && (
            <div className="actions">
              {["open", "in progress", "closed"].filter((st) => st !== r.status).map((st) => (
                <button key={st} onClick={async () => (await attempt(() => post(`/api/reviews/${encodeURIComponent(r.key)}`, { status: st }))) && load()}>
                  Mark {st}
                </button>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
