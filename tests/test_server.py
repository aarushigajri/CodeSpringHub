"""End-to-end tests: a real MCP client talks to the server in-process (setup in conftest.py)."""

import asyncio
import json

import pytest
from mcp import Client

from codespringhub import config
from codespringhub.permissions import acting_as
from codespringhub.server import mcp

def call(tool, **args):
    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(tool, args)

    r = asyncio.run(go())
    if r.is_error:
        return {"error": r.content[0].text}

    def parse(text):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # A tool returning a list sends one content block per item (none for an empty list).
    blocks = [parse(c.text) for c in r.content]
    return blocks if tool in LIST_TOOLS else blocks[0]


LIST_TOOLS = {"list_labs", "search_knowledge", "list_datasets", "list_workflows"}


def test_only_member_labs_are_visible():
    assert [lab["lab_id"] for lab in call("list_labs")] == ["bsr"]
    assert "error" in call("get_lab_context", lab_id="otherlab")


def test_context_edit_is_live_immediately(hub):
    ctx = hub / "hub" / "knowledge" / "bsr" / "context.md"
    assert "Slurm" in call("get_lab_context", lab_id="bsr")
    assert call("search_knowledge", query="axolotl") == []

    ctx.write_text(ctx.read_text() + "\n## New\nBSR now supports axolotl genome projects.\n")

    assert "axolotl" in call("get_lab_context", lab_id="bsr")
    hits = call("search_knowledge", query="axolotl")
    assert hits and hits[0]["doc_id"] == "doc:bsr/context"


def test_new_guidance_file_is_found_and_templates_are_hidden(hub):
    (hub / "hub" / "knowledge" / "bsr" / "guidance" / "scrna-qc.md").write_text(
        "# scRNA-seq QC at BSR\n\nFilter cells with mitochondrial fraction above the agreed threshold.\n"
    )
    hits = call("search_knowledge", query="mitochondrial")
    assert hits[0]["doc_id"] == "doc:bsr/guidance/scrna-qc"
    assert "doc:bsr/guidance/_TEMPLATE" not in call("get_lab_context", lab_id="bsr")


def test_search_never_returns_other_labs():
    assert call("search_knowledge", query="zebrafish") == []


def test_dataset_inspection_hides_paths():
    ds = call("describe_dataset", dataset_id="ds:bsr/demo-rnaseq")
    assert "path" not in ds and "/grid/" not in json.dumps(ds)
    assert {"counts.tsv", "samplesheet.csv"} <= {a["asset"] for a in ds["assets"]}

    table = call("inspect_asset", dataset_id="ds:bsr/demo-rnaseq", asset="counts.tsv")
    assert table["rows"] == 500 and table["n_columns"] == 7

    fq = call("inspect_asset", dataset_id="ds:bsr/demo-rnaseq", asset="reads/S1_R1.fastq.gz")
    assert fq["kind"] == "fastq" and fq["read_length_mean"] == 100


def test_query_table():
    r = call("query_table", dataset_id="ds:bsr/demo-rnaseq", asset="samplesheet.csv",
             sql="SELECT condition, count(*) AS n FROM data GROUP BY condition ORDER BY condition")
    assert r["rows"] == [["control", "3"], ["treated", "3"]]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "COPY data TO '/tmp/x.csv'",
    "SELECT 1; SELECT 2",
])
def test_query_table_cannot_escape(sql):
    assert "error" in call("query_table", dataset_id="ds:bsr/demo-rnaseq", asset="counts.tsv", sql=sql)


@pytest.mark.parametrize("asset", ["../../secret.csv", "escape.csv", "/etc/passwd"])
def test_path_escapes_are_refused(asset):
    r = call("inspect_asset", dataset_id="ds:bsr/demo-rnaseq", asset=asset)
    assert "outside the registered dataset" in r["error"]


def test_suggestions_and_reviews_are_queued_and_everything_is_audited():
    msg = call("suggest_context_edit", lab_id="bsr", suggestion="Default mouse reference is GRCm39.")
    assert "not live until a person applies it" in msg
    assert "#1" in call("request_bsr_review", lab_id="bsr", summary="Novel design", dataset_ids=["ds:bsr/demo-rnaseq"])

    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    tools = [r[0] for r in conn.execute("SELECT tool FROM audit")]
    assert tools == ["suggest_context_edit", "request_bsr_review"]
    # The context file itself is untouched by a suggestion.
    assert "GRCm39" not in call("get_lab_context", lab_id="bsr")


def test_new_state_files_are_private():
    call("whoami")
    assert config.VAR_DIR.stat().st_mode & 0o777 == 0o770
    assert config.DB_PATH.stat().st_mode & 0o777 == 0o660


def test_workflows_filter():
    assert [w["id"] for w in call("list_workflows", assay="RNA-seq")] == ["nf-core-rnaseq"]


def test_acting_user_reaches_the_tools():
    # The portal and the future HTTPS endpoint set the user per request; the tools must see it.
    async def go():
        with acting_as("root"):
            async with Client(mcp) as c:
                return await c.call_tool("whoami", {})

    assert json.loads(asyncio.run(go()).content[0].text)["user"] == "root"
