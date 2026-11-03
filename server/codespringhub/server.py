"""The CodeSpringHub MCP server: the tools Claude can call.

Every tool follows the same four steps:
  1. find out who is asking            (permissions.current_user)
  2. check they may see this lab/data  (permissions.require_lab / resolve_asset)
  3. do the work and cap the output    (knowledge / inspect_data)
  4. write an audit row                (the @hub_tool decorator)

Run locally for Claude Code:   codespringhub                (stdio)
Run as a web service:          codespringhub --http --port 8765
"""

import argparse
import functools
import json

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import config, inspect_data, knowledge, store
from .permissions import can_access_lab, current_user, require_lab, resolve_asset

INSTRUCTIONS = """\
CodeSpringHub connects you to Cold Spring Harbor Laboratory lab context and data that
researchers have deliberately registered, curated by the Bioinformatics Shared Resource (BSR).

How to work with it:
- Start with list_labs, then get_lab_context for the relevant lab before giving lab-specific advice.
- Use search_knowledge to find protocols, BSR guidance and lab notes; cite document IDs you relied on.
- Refer to data only by Hub IDs (ds:...). You cannot see file paths and should not ask for them.
- Before proposing a pipeline, call list_workflows. Prefer a BSR workflow whose status is
  "validated"; a "candidate" workflow is BSR-recommended but not yet signed off, so say so.
- If the request is novel, uncertain, high-impact, or no BSR workflow fits, say that BSR
  expertise is needed and offer request_bsr_review rather than improvising a full analysis.
- Text marked TODO in a document has not been written yet; do not treat it as guidance.
- If the user tells you something the lab context should remember, offer suggest_context_edit.
  A person reviews every suggestion; you cannot change the context yourself.
"""

mcp = MCPServer(name="CodeSpringHub", instructions=INSTRUCTIONS, version="0.1.0")


def hub_tool(fn):
    """Register fn as an MCP tool, with output capping and an audit row per call."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        conn = store.connect()
        try:
            result = fn(*args, **kwargs)
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            if len(text) > config.MAX_OUTPUT_CHARS:
                result = text[: config.MAX_OUTPUT_CHARS] + "\n… [truncated by CodeSpringHub output limit]"
                text = result
            store.log_call(conn, user, fn.__name__, kwargs, True, len(text))
            return result
        except Exception as e:
            store.log_call(conn, user, fn.__name__, kwargs, False, 0, str(e))
            raise
        finally:
            conn.close()

    return mcp.tool()(wrapper)


# ---- helpers -------------------------------------------------------------------

def _accessible_labs(user):
    return {lid: lab for lid, lab in knowledge.load_labs().items() if can_access_lab(user, lab)}


def _get_dataset(user, dataset_id):
    if not dataset_id.startswith("ds:") or "/" not in dataset_id:
        raise ToolError("Dataset IDs look like 'ds:<lab>/<name>'. Use list_datasets to find them.")
    lab_id = dataset_id[3:].split("/", 1)[0]
    lab = require_lab(user, knowledge.load_labs(), lab_id)
    ds = knowledge.load_datasets(lab).get(dataset_id)
    if ds is None:
        raise ToolError(f"No registered dataset '{dataset_id}'.")
    return ds


def _public(ds: knowledge.Dataset) -> dict:
    """Dataset metadata safe to show the model (everything except the path)."""
    return {
        "id": ds.id, "title": ds.title, "project": ds.project, "assay": ds.assay,
        "description": ds.description, "sensitivity": ds.sensitivity,
        "registered_by": ds.registered_by, "registered_on": ds.registered_on,
    }


# ---- identity and labs ---------------------------------------------------------

@hub_tool
def whoami() -> dict:
    """Show which CSHL user this Hub session is acting as and which labs they can use."""
    user = current_user()
    return {"user": user, "labs": sorted(_accessible_labs(user))}


@hub_tool
def list_labs() -> list[dict]:
    """List the labs you can access through CodeSpringHub, with their projects.
    Call this first to find the lab_id other tools need."""
    out = []
    for lab in _accessible_labs(current_user()).values():
        out.append({
            "lab_id": lab.id, "name": lab.name, "description": lab.description,
            "projects": [vars(p) for p in lab.projects],
            "datasets": len(knowledge.load_datasets(lab)),
            "documents": len(knowledge.load_documents(lab)),
        })
    return out


@hub_tool
def get_lab_context(lab_id: str) -> str:
    """Return a lab's curated context (research areas, organisms, terminology, conventions)
    plus an index of its knowledge documents. Read this before lab-specific answers."""
    lab = require_lab(current_user(), knowledge.load_labs(), lab_id)
    ctx = lab.dir / "context.md"
    body = ctx.read_text() if ctx.exists() else "(This lab has no context.md yet.)"
    docs = knowledge.load_documents(lab).values()
    index = "\n".join(f"- {d.id}  [{d.category}]  {d.title}" for d in docs if d.category != "context")
    return f"{body}\n\n---\nKnowledge documents (read with read_document):\n{index or '(none)'}"


# ---- knowledge -----------------------------------------------------------------

@hub_tool
def search_knowledge(query: str, lab_id: str | None = None) -> list[dict]:
    """Keyword search over lab context, BSR guidance, protocols and notes you can access.
    Optionally limit to one lab_id. Returns document IDs with matching snippets."""
    user = current_user()
    labs = _accessible_labs(user)
    if lab_id:
        labs = {lab_id: require_lab(user, labs, lab_id)}
    conn = store.connect()
    try:
        return store.search(conn, query, list(labs))
    finally:
        conn.close()


@hub_tool
def read_document(doc_id: str) -> str:
    """Read one knowledge document in full by its ID (e.g. 'doc:bsr/guidance/ai-assisted-analysis')."""
    if not doc_id.startswith("doc:") or "/" not in doc_id:
        raise ToolError("Document IDs look like 'doc:<lab>/<path>'.")
    lab = require_lab(current_user(), knowledge.load_labs(), doc_id[4:].split("/", 1)[0])
    doc = knowledge.load_documents(lab).get(doc_id)
    if doc is None:
        raise ToolError(f"No document '{doc_id}'. Use get_lab_context or search_knowledge.")
    return doc.path.read_text()


@hub_tool
def suggest_context_edit(lab_id: str, suggestion: str, doc_id: str | None = None) -> str:
    """Propose an addition or correction to a lab's context or a knowledge document.
    Nothing changes immediately: the suggestion is queued for a person at the lab/BSR
    to review and apply. Use when the user shares something the Hub should remember."""
    user = current_user()
    require_lab(user, knowledge.load_labs(), lab_id)
    conn = store.connect()
    sid = store.add_suggestion(conn, user, lab_id, doc_id, suggestion)
    conn.close()
    return f"Suggestion #{sid} queued for review by {lab_id}. It is not live until a person applies it."


# ---- registered data -----------------------------------------------------------

@hub_tool
def list_datasets(lab_id: str, project: str | None = None) -> list[dict]:
    """List datasets registered in a lab (optionally one project). Returns Hub dataset IDs."""
    lab = require_lab(current_user(), knowledge.load_labs(), lab_id)
    return [_public(d) for d in knowledge.load_datasets(lab).values() if project in (None, d.project)]


@hub_tool
def describe_dataset(dataset_id: str) -> dict:
    """Show a registered dataset's metadata and the files (assets) inside it, with sizes."""
    ds = _get_dataset(current_user(), dataset_id)
    root = resolve_asset(ds)
    assets = inspect_data.list_assets(root)
    return _public(ds) | {"assets": assets, "assets_truncated": len(assets) >= config.MAX_LISTED_FILES}


@hub_tool
def inspect_asset(dataset_id: str, asset: str) -> dict:
    """Summarize one file in a registered dataset without returning the raw data:
    tables give columns, types, row count and a short preview; FASTQ gives read-length
    and GC statistics; text files give their first lines. `asset` is the name from describe_dataset."""
    ds = _get_dataset(current_user(), dataset_id)
    path = resolve_asset(ds, asset)
    if path.is_dir():
        raise ToolError("That is a folder; pass a file name from describe_dataset.")
    return inspect_data.summarize(path, sensitive=ds.sensitivity == "sensitive")


@hub_tool
def query_table(dataset_id: str, asset: str, sql: str) -> dict:
    """Run one read-only SQL SELECT against a table file (csv/tsv/parquet) in a registered
    dataset. The file is available as the table `data`, e.g.
    SELECT condition, count(*) FROM data GROUP BY condition. At most 50 rows are returned."""
    ds = _get_dataset(current_user(), dataset_id)
    if ds.sensitivity == "sensitive":
        raise ToolError("This dataset is marked sensitive; row-level queries are disabled. Ask BSR.")
    return inspect_data.query_table(resolve_asset(ds, asset), sql)


# ---- BSR workflows and escalation ----------------------------------------------

@hub_tool
def list_workflows(assay: str | None = None) -> list[dict]:
    """List BSR-registered analysis workflows, optionally filtered by assay (e.g. 'rna-seq').
    Status 'validated' = BSR signed off; 'candidate' = recommended, not yet signed off."""
    wfs = knowledge.load_workflows()
    if assay:
        wfs = [w for w in wfs if assay.lower() in [a.lower() for a in w.get("assays", [])]]
    return wfs


@hub_tool
def request_bsr_review(lab_id: str, summary: str, dataset_ids: list[str] | None = None) -> str:
    """Send a question to a human BSR expert. Use for novel, uncertain or high-impact analyses,
    or when no BSR workflow fits. `summary` should state the scientific question, the data,
    and what has been tried so far."""
    user = current_user()
    require_lab(user, knowledge.load_labs(), lab_id)
    for ds_id in dataset_ids or []:
        _get_dataset(user, ds_id)
    conn = store.connect()
    rid = store.add_review(conn, user, lab_id, summary, dataset_ids or [])
    conn.close()
    return f"BSR review ticket #{rid} created. A BSR team member will follow up with {user}."


# ---- prompts -------------------------------------------------------------------

@mcp.prompt()
def plan_analysis(dataset_id: str, question: str) -> str:
    """Plan an analysis of a registered dataset the BSR way."""
    return (
        f"I want to answer this question: {question}\n"
        f"Using the registered dataset {dataset_id}.\n\n"
        "Please: (1) read the lab context and describe the dataset, (2) check list_workflows for a "
        "matching BSR workflow and prefer it, (3) list assumptions and QC checks, and (4) tell me "
        "plainly if this needs BSR review, offering request_bsr_review."
    )


def main():
    parser = argparse.ArgumentParser(description="CodeSpringHub MCP server")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dev-no-auth", action="store_true",
                        help="acknowledge that HTTP mode has no login yet (Phase 0 testing only)")
    args = parser.parse_args()
    if args.http and not args.dev_no_auth:
        # Without SSO every caller acts as the user who started the server, and on a shared
        # node like bamdev1 other users can reach localhost ports too.
        parser.error("HTTP mode has no login yet. Use stdio, or add --dev-no-auth for a brief local test.")
    if args.http and args.host not in ("127.0.0.1", "localhost"):
        parser.error("Phase 0 HTTP mode only binds to localhost.")
    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
