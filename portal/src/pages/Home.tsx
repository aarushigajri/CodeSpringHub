import { Ctx } from "../App";
import { Empty } from "../components";

export default function Home({ ctx }: { ctx: Ctx }) {
  const first = ctx.me.name.split(" ")[0];
  return (
    <div className="page">
      <h1>Welcome, {first}</h1>
      <p className="lead">
        CodeSpringHub decides what Claude knows about your lab and which data it may look at.
        Nothing reaches Claude unless someone adds it here.
      </p>

      <div className="steps">
        <div className="step">
          <span className="num">1</span>
          <strong>Describe your lab</strong>
          <p>Write what your lab works on, the organisms and terms you use, and how you like analyses done.</p>
        </div>
        <div className="step">
          <span className="num">2</span>
          <strong>Add your data</strong>
          <p>Pick folders on the cluster. Data stays where it is; Claude only gets short summaries.</p>
        </div>
        <div className="step">
          <span className="num">3</span>
          <strong>Check what Claude sees</strong>
          <p>Use <a href="#/try">Try it as Claude</a> to see exactly what Claude would receive.</p>
        </div>
      </div>

      <h2>Your labs</h2>
      {ctx.labs.filter((l) => l.kind !== "core" || l.yours).length === 0 && (
        <Empty title="You're not in any CodeSpringHub lab yet.">Ask BSR to add your lab to the pilot.</Empty>
      )}
      <div className="cards">
        {ctx.labs.filter((l) => l.kind !== "core" || l.yours).map((l) => (
          <div key={l.lab_id} className="card">
            <h3>{l.name}</h3>
            <p className="muted">{l.description || "No description yet."}</p>
            <div className="stats">
              <span><strong>{l.documents}</strong> documents</span>
              <span><strong>{l.datasets}</strong> datasets</span>
              <span><strong>{l.projects.length}</strong> projects</span>
            </div>
            <div className="actions">
              <a className="button primary" href={`#/lab/${l.lab_id}/${l.kind === "core" ? "shared" : "about"}`}>{l.kind === "core" ? "Open core" : "Open lab"}</a>
              <a className="button" href={`#/lab/${l.lab_id}/datasets?add=1`}>Add a dataset</a>
              <a className="button" href={`#/lab/${l.lab_id}/${l.kind === "core" ? "shared" : "documents"}?new=1`}>Add a document</a>
            </div>
            {!l.can_edit && <p className="muted small">You can view this lab but not edit it.</p>}
          </div>
        ))}
      </div>

      <p className="muted">
        Every CSHL core facility shares its protocols and guidelines here: open <a href="#/core/bsr/shared">BSR</a> or
        the <strong>Cores</strong> list in the sidebar.
      </p>

      {ctx.inboxCount > 0 && (
        <div className="callout">
          You have <strong>{ctx.inboxCount}</strong> item{ctx.inboxCount === 1 ? "" : "s"} waiting in the{" "}
          <a href="#/inbox">Inbox</a>: suggestions from Claude or requests for BSR.
        </div>
      )}
    </div>
  );
}
