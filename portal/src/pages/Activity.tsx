import { useEffect, useState } from "react";
import { Ctx } from "../App";
import { get } from "../api";
import { Badge, Empty, Help } from "../components";

type Row = { id: number; ts: string; user: string; lab_id: string | null; tool: string; args: string; ok: number; chars_out: number; error: string | null };

export default function Activity({ ctx }: { ctx: Ctx }) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [filter, setFilter] = useState<"all" | "claude" | "portal">("all");
  useEffect(() => {
    get<Row[]>("/api/activity").then(setRows);
  }, []);

  const shown = (rows ?? []).filter((r) =>
    filter === "all" ? true : filter === "portal" ? r.tool.startsWith("portal:") : !r.tool.startsWith("portal:"));

  return (
    <div className="page">
      <h1>Activity</h1>
      <Help>
        Every request Claude made to the Hub, and every change made in this portal.{" "}
        {ctx.me.is_bsr
          ? "As BSR you see every lab's usage (shared by connecting). Requests about personal items show no details."
          : "You see your own activity. BSR can see it too; requests about your personal items show them no details."}
      </Help>
      <div className="segmented">
        {(["all", "claude", "portal"] as const).map((f) => (
          <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
            {f === "all" ? "Everything" : f === "claude" ? "Claude's requests" : "Portal edits"}
          </button>
        ))}
      </div>
      {rows === null && <div className="loading">Loading…</div>}
      {rows && shown.length === 0 && <Empty title="No activity yet." />}
      {shown.length > 0 && (
        <table>
          <thead><tr><th>When (UTC)</th><th>Who</th><th>Lab</th><th>What</th><th>Details</th><th>Result</th></tr></thead>
          <tbody>
            {shown.map((r) => (
              <tr key={`${r.user}-${r.id}`}>
                <td className="small">{r.ts.slice(0, 19).replace("T", " ")}</td>
                <td>{r.user}</td>
                <td>{r.lab_id ?? "–"}</td>
                <td><code>{r.tool.replace(/^portal:/, "")}</code>{r.tool.startsWith("portal:") && <Badge kind="muted">portal</Badge>}</td>
                <td className="small muted args">
                  {r.args.includes("withheld") ? <span className="badge muted">personal: details withheld</span> : r.args === "{}" ? "" : r.args}
                </td>
                <td>
                  {r.ok ? <Badge kind="ok">ok</Badge> : <Badge kind="err">failed</Badge>}
                  {!r.ok && r.error && <div className="small muted">{r.error.replace(/^Error executing tool \w+: /, "")}</div>}
                  {r.ok && r.chars_out > 0 && <div className="small muted">{r.chars_out.toLocaleString()} chars sent</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
