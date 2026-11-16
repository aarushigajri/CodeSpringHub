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
import sys

from mcp.server.mcpserver import MCPServer

from . import config, hub, runs, store
from .permissions import current_user

INSTRUCTIONS = """\
CodeSpringHub connects you to Cold Spring Harbor Laboratory lab context and data that
researchers have deliberately registered, curated by the Bioinformatics Shared Resource (BSR).

How to work with it:
- Start with list_labs, then get_lab_context for the relevant lab before giving lab-specific advice.
- Use search_knowledge to find protocols, BSR guidance and lab notes; cite document IDs you relied on.
- Refer to data only by Hub IDs (ds:...). You cannot see file paths and should not ask for them.
- Resources are either lab-shared or personal (IDs containing "/~user/", visible only to that
  member and their PI). Never repeat personal content to anyone else or into lab-shared documents.
- Registered resources are "data" (you get summaries only) or "code" (lab scripts and pipelines:
  read the source with read_code_file to explain, debug or adapt it).
- Before proposing any analysis, call list_workflows. BSR's own pipelines (CodeSpringApp with
  CodeSpringLab's scripts) come first: recommend them, with their steps, references and settings
  as BSR runs them, and read guidance doc:bsr/guidance/codespring-pipelines. nf-core pipelines are a
  secondary option: mention one only when no BSR pipeline fits, and say it is not BSR's standard.
- When a BSR pipeline fits, do not deviate from it: don't substitute other tools, references,
  parameters or your own scripts, and don't write ad-hoc Slurm or shell commands. If the user needs
  something the BSR pipeline can't do, say so and offer request_bsr_review.
- To run an analysis (bulk RNA-seq so far): plan_analysis, then show the plan in plain language and
  ask the user to confirm, then start_analysis(plan_id, user_confirmed=true) only after they say yes.
  Follow with analysis_status; when complete, open_results gives a one-time link to CodeSpringApp's
  Results Explorer. Never start a run the user hasn't confirmed in this conversation.
- If the request is novel, uncertain, high-impact, or no BSR workflow fits, say that BSR
  expertise is needed and offer request_bsr_review rather than improvising a full analysis.
- Text marked TODO in a document has not been written yet; do not treat it as guidance.
- If the user tells you something the lab context should remember, offer suggest_context_edit.
  A person reviews every suggestion; you cannot change the context yourself.
- At the start of a conversation that uses the Hub, tell the user once that CodeSpringHub usage
  (which tools are used and their inputs, not the conversation) is shared with BSR.
"""

mcp = MCPServer(name="CodeSpringHub", instructions=INSTRUCTIONS, version="0.1.0")


def hub_tool(fn):
    """Register fn as an MCP tool, with output capping and an audit row per call."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        lab_id = _lab_of(kwargs)
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            _log(user, fn.__name__, kwargs, False, 0, str(e), lab_id=lab_id)
            raise
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        if len(text) > config.MAX_OUTPUT_CHARS:
            result = text[: config.MAX_OUTPUT_CHARS] + "\n… [truncated by CodeSpringHub output limit]"
            text = result
        _log(user, fn.__name__, kwargs, True, len(text), lab_id=lab_id)
        return result

    return mcp.tool()(wrapper)


def _log(*args, **kwargs):
    """Usage logging is best-effort: a broken log never takes Claude's answer down with it.
    `hub doctor` reports why logging fails."""
    try:
        store.log_call(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"CodeSpringHub: usage log not written ({e})", file=sys.stderr)


def _lab_of(kwargs) -> str | None:
    """Which lab a call is about, for per-lab usage statistics."""
    if kwargs.get("lab_id"):
        return kwargs["lab_id"]
    for key in ("dataset_id", "resource_id", "doc_id"):
        value = kwargs.get(key)
        if isinstance(value, str) and ":" in value:
            return value.split(":", 1)[1].split("/", 1)[0]
    return None


def _note():
    try:
        store.note_once("usage_notice")
    except Exception:  # noqa: BLE001
        pass


# ---- identity and labs ---------------------------------------------------------

@hub_tool
def whoami() -> dict:
    """Show which CSHL user this Hub session is acting as, which labs they can use, and how
    their usage is shared with BSR."""
    user = current_user()
    _note()
    return {"user": user, "labs": sorted(hub.accessible_labs(user)), "usage_notice": hub.USAGE_NOTICE}


@hub_tool
def list_labs() -> dict:
    """List the labs you can access through CodeSpringHub, with their projects and your role.
    Call this first to find the lab_id other tools need."""
    _note()
    return {"labs": hub.list_labs(current_user()), "usage_notice": hub.USAGE_NOTICE}


@hub_tool
def get_lab_context(lab_id: str) -> str:
    """Return a lab's curated context (research areas, organisms, terminology, conventions)
    plus an index of its knowledge documents. Read this before lab-specific answers."""
    ctx = hub.lab_context(current_user(), lab_id)
    body = ctx["context"] or "(This lab has no context.md yet.)"
    index = "\n".join(f"- {d['id']}  [{d['category']}]  {d['title']}" for d in ctx["documents"])
    return f"{body}\n\n---\nKnowledge documents (read with read_document):\n{index or '(none)'}"


# ---- knowledge -----------------------------------------------------------------

@hub_tool
def search_knowledge(query: str, lab_id: str | None = None) -> list[dict]:
    """Keyword search over lab context, BSR guidance, protocols and notes you can access
    (lab-shared, plus your own personal documents).
    Optionally limit to one lab_id. Returns document IDs with matching snippets."""
    return hub.search(current_user(), query, lab_id)


@hub_tool
def read_document(doc_id: str) -> str:
    """Read one knowledge document in full by its ID (e.g. 'doc:bsr/guidance/ai-assisted-analysis')."""
    return hub.get_document(current_user(), doc_id).path.read_text()


@hub_tool
def suggest_context_edit(lab_id: str, suggestion: str, doc_id: str | None = None) -> str:
    """Propose an addition or correction to a lab's context or a knowledge document.
    Nothing changes immediately: the suggestion is queued for a person at the lab/BSR
    to review and apply. Use when the user shares something the Hub should remember."""
    hub.suggest_edit(current_user(), lab_id, suggestion, doc_id)
    who = "the document's owner" if doc_id and "/~" in doc_id else f"the {lab_id} lab"
    return f"Suggestion saved for {who} to review. It is not live until a person applies it."


# ---- registered data -----------------------------------------------------------

@hub_tool
def list_datasets(lab_id: str, project: str | None = None) -> list[dict]:
    """List data and code resources registered in a lab (optionally one project): lab-shared ones
    plus your personal ones. Returns Hub IDs, each with kind "data" or "code"."""
    return hub.list_datasets(current_user(), lab_id, project)


@hub_tool
def describe_dataset(dataset_id: str) -> dict:
    """Show a registered resource's metadata and the files (assets) inside it, with sizes.
    For code resources, also the git branch and last commit if it is a repository."""
    return hub.describe_dataset(current_user(), dataset_id)


@hub_tool
def inspect_asset(dataset_id: str, asset: str) -> dict:
    """Summarize one file in a registered dataset without returning the raw data:
    tables give columns, types, row count and a short preview; FASTQ gives read-length
    and GC statistics; text files give their first lines. `asset` is the name from describe_dataset."""
    return hub.inspect_asset(current_user(), dataset_id, asset)


@hub_tool
def read_code_file(resource_id: str, path: str) -> str:
    """Read one source file (script, notebook, pipeline, config) in full from a registered
    *code* resource, e.g. read_code_file('ds:lab/rnaseq-scripts', 'R/de_analysis.R').
    `path` is the file name from describe_dataset. Data resources only give summaries."""
    return hub.read_code_file(current_user(), resource_id, path)


@hub_tool
def query_table(dataset_id: str, asset: str, sql: str) -> dict:
    """Run one read-only SQL SELECT against a table file (csv/tsv/parquet) in a registered
    dataset. The file is available as the table `data`, e.g.
    SELECT condition, count(*) FROM data GROUP BY condition. At most 50 rows are returned."""
    return hub.query_table(current_user(), dataset_id, asset, sql)


# ---- BSR workflows and escalation ----------------------------------------------

@hub_tool
def list_workflows(assay: str | None = None) -> dict:
    """List how BSR runs analyses. 'bsr_pipelines' are BSR's own pipelines (CodeSpringApp running
    CodeSpringLab's scripts): always the first choice. 'secondary_nfcore' are community nf-core
    pipelines BSR knows of: suggest them only when no BSR pipeline fits. Optionally filter by assay."""
    try:
        bsr = runs.catalog()
    except Exception as e:  # noqa: BLE001  (the shared install may be unavailable; still answer)
        bsr = {"pipelines": [], "unavailable": str(e)}
    return {
        "bsr_pipelines": bsr.get("pipelines", []),
        "rnaseq_options": bsr.get("rnaseq_options"),
        "bsr_pipelines_unavailable": bsr.get("unavailable"),
        "secondary_nfcore": hub.list_workflows(assay),
        "rule": ("Use a BSR pipeline when one fits and follow it exactly. Ones marked runnable_from_hub can be "
                 "started from this chat with plan_analysis; the others are run in CodeSpringApp. nf-core "
                 "pipelines are secondary: say they are not BSR's standard and offer request_bsr_review."),
        "guidance": "doc:bsr/guidance/codespring-pipelines",
    }


@hub_tool
def plan_analysis(dataset_id: str, project_name: str, species: str, design: list[dict] | None = None,
                  design_file: str = "", fastq_folder: str = "", paired_end: bool = True,
                  genome_version: str = "", trim_adapters: bool = True,
                  adapter: str = "Illumina Universal TruSeq RNA", min_length: int = 20,
                  count_by: str = "gene_name", compare: dict | None = None,
                  pathway_geneset: str | None = None) -> dict:
    """Plan a bulk RNA-seq run with BSR's pipeline (CodeSpringApp: cutadapt, FastQC, STAR,
    featureCounts, DESeq2, optional GSEApy). Checks everything and returns the exact plan;
    nothing is submitted. Inputs: a registered dataset holding the FASTQs (fastq_folder = sub-folder
    inside it, if any); the sample design as rows {"sample", grouping columns..., "filename"
    ("R1,R2" for paired-end)} or design_file (a design_matrix.txt inside the dataset); species
    human or mouse; compare = {"column", "reference", "comparison"} for DESeq2;
    pathway_geneset from list_workflows' rnaseq_options. Then show the plan and ask the user."""
    return runs.plan_analysis(current_user(), dataset_id, project_name, species, fastq_folder=fastq_folder,
                              design=design, design_file=design_file, paired_end=paired_end,
                              genome_version=genome_version, trim_adapters=trim_adapters, adapter=adapter,
                              min_length=min_length, count_by=count_by, compare=compare,
                              pathway_geneset=pathway_geneset)


@hub_tool
def start_analysis(plan_id: str, user_confirmed: bool = False) -> dict:
    """Submit a plan from plan_analysis to the cluster, under the user's own account. Only call
    this after showing the plan and the user explicitly agreeing in this conversation; set
    user_confirmed=true to record that they did. Plans are single-use and expire."""
    return runs.start_analysis(current_user(), plan_id, user_confirmed)


@hub_tool
def analysis_status(run_id: str) -> dict:
    """Progress of an analysis started from the Hub: each step's state, and any error."""
    return runs.analysis_status(current_user(), run_id)


@hub_tool
def list_analyses() -> list[dict]:
    """Analyses you have started from the Hub, newest last."""
    return runs.list_analyses(current_user())


@hub_tool
def resume_analysis(run_id: str) -> dict:
    """Restart a failed or stopped analysis; finished steps are kept. Ask the user first."""
    return runs.resume_analysis(current_user(), run_id)


@hub_tool
def open_results(run_id: str, view: str = "results") -> dict:
    """Give the user a one-time link that opens this analysis in CodeSpringApp (view: results =
    the Results Explorer, progress, or logs), plus the SSH tunnel command to run on their laptop.
    Starts their CodeSpringApp on the cluster if it isn't running."""
    return runs.open_results(current_user(), run_id, view)


@hub_tool
def request_bsr_review(lab_id: str, summary: str, dataset_ids: list[str] | None = None) -> str:
    """Send a question to a human BSR expert. Use for novel, uncertain or high-impact analyses,
    or when no BSR workflow fits. `summary` should state the scientific question, the data,
    and what has been tried so far."""
    user = current_user()
    rid = hub.request_review(user, lab_id, summary, dataset_ids)
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
