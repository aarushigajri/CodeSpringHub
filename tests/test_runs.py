"""Starting BSR pipelines from Claude (runs.py), against a stand-in for CodeSpringApp's runner.

The real runner (CodeSpringApp bin/codespring-run) has its own tests in CodeSpringApp; here we
check the Hub's guardrails: who may plan and start, which inputs are allowed, that nothing runs
without a confirmed, unchanged, unexpired, single-use plan, and how results are opened.
"""

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest
import yaml

from codespringhub import config, runs
from codespringhub.errors import Forbidden, HubError, NotFound
from conftest import USER

FAKE_RUNNER = r'''#!{python}
import json, os, sys
from pathlib import Path
here = Path(__file__).parent
cmd, args = sys.argv[1], sys.argv[2:]
spec = json.loads(Path(args[0]).read_text()) if cmd in ("check", "start") else None
with open(here / "calls.jsonl", "a") as fh:
    fh.write(json.dumps({{"cmd": cmd, "args": args, "spec": spec,
                         "csl_env": sorted(k for k in os.environ if k.startswith("CSL_"))}}) + "\n")
state = (here / "state").read_text().strip() if (here / "state").exists() else "running"
def out(x, code=0):
    print(json.dumps(x)); sys.exit(code)
if cmd == "catalog":
    out({{"ok": True, "pipelines": [{{"id": "rnaseq", "runnable_from_hub": True}}, {{"id": "atac", "runnable_from_hub": False}}],
         "rnaseq": {{"references": [], "default_reference": {{}}, "adapters_r1": {{"Illumina Universal TruSeq RNA": "AGATC"}},
                    "features": ["gene_name"], "gsea_genesets": ["MSigDB_Hallmark_2020"], "limits": {{}}}}}})
if cmd in ("check", "start"):
    if spec["project_name"] == "taken":
        out({{"ok": False, "errors": ["A results folder for 'taken' already exists."]}}, 2)
    plan = {{"pipeline": "rnaseq", "project_id": "rna/" + spec["project_name"], "results_dir": "/r/" + spec["project_name"],
            "steps": [{{"step": "STAR"}}, {{"step": "DESeq2"}}], "codespringapp": "/x", "codespringlab": "/y"}}
    if cmd == "check":
        out({{"ok": True, "plan": plan}})
    out({{"ok": True, "project_id": plan["project_id"], "results_dir": plan["results_dir"]}})
if cmd == "status":
    extra = {{"paused_after": "FastQC", "next_step": "STAR"}} if state == "paused" else {{}}
    out({{"ok": True, "state": state, "mode": "step", "results_dir": "/r", "next_actions": ["continue: run STAR", "cancel"],
         "steps": [{{"step": "FastQC", "state": "complete", "jobs": {{"done": 6, "running": 0, "queued": 0, "failed": 0}}}},
                   {{"step": "STAR", "state": "waiting"}}], **extra}})
if cmd == "qc":
    out({{"ok": True, "samples": [{{"sample": "c1", "star": {{"uniquely_mapped_pct": 91.5}}}}],
         "summary": {{"star": {{"uniquely_mapped": "91.5% to 91.5%"}}}}, "note": "tool numbers"}})
if cmd == "continue":
    out({{"ok": True, "steps": [{{"step": "STAR", "state": "waiting"}}, {{"step": "Kallisto", "state": "waiting"}}]}})
if cmd == "cancel":
    out({{"ok": True, "state": "cancelled", "controller": "stopped", "jobs": ["Requested cancellation of 2 active STAR jobs"],
         "note": "Finished steps are kept."}})
if cmd == "report":
    rep = here / "results" / "report"
    rep.mkdir(parents=True, exist_ok=True)
    png = here / "results" / "volcano_t_vs_c.png"
    png.write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"fake")
    (rep / "exp1_codespring_report.html").write_text("<html>report</html>")
    out({{"ok": True, "state": state, "report": str(rep / "exp1_codespring_report.html"), "size_mb": 0.01,
         "plots": [{{"label": "volcano_t_vs_c.png", "path": str(png), "section": "deseq2"}}],
         "files": ["DEG_fdr0.05_t_vs_c.txt"], "left_out": [{{"label": "count_matrix.txt", "path": "/r/x", "size_mb": 60, "why": "big"}}],
         "deg_summary": {{"t_vs_c": {{"significant": 55, "up": 27, "down": 28}}}}, "gsea_top": {{}}, "qc_summary": {{}}, "note": "n"}})
if cmd == "resume":
    out({{"ok": True, "resumed": True}})
out({{"ok": False, "errors": "unknown"}}, 2)
'''


@pytest.fixture
def suite(hub, monkeypatch):
    s = hub / "suite"
    exe = s / "CodeSpringApp" / "bin" / "codespring-run"
    exe.parent.mkdir(parents=True)
    exe.write_text(FAKE_RUNNER.format(python=sys.executable))
    exe.chmod(0o755)
    monkeypatch.setattr(config, "CODESPRING_SUITE", s)

    # FASTQ datasets: one lab-shared in lab-a, one personal to ann, one in lab-b, plus a code folder.
    for name in ("reads", "ann-reads", "b-reads"):
        d = hub / "data" / name / "fastq"
        d.mkdir(parents=True)
        (d / "c1_R1.fastq.gz").write_text("@r\nA\n+\nI\n")
        (hub / "data" / name / "design_matrix.txt").write_text("sample\tgroup\tfilename\nc1\tx\tc1_R1.fastq.gz\n")
    kdir = config.KNOWLEDGE_DIR

    def register(path: Path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"datasets": entries}))

    register(kdir / "lab-a" / "datasets.yaml", [
        {"id": "reads", "title": "Reads", "path": str(hub / "data" / "reads"), "assay": "rna-seq"},
        {"id": "scripts", "title": "Scripts", "path": str(hub / "data" / "scripts"), "kind": "code"},
    ])
    register(kdir / "lab-a" / "members" / "ann" / "datasets.yaml",
             [{"id": "mine", "title": "Ann's reads", "path": str(hub / "data" / "ann-reads"), "assay": "rna-seq"}])
    register(kdir / "lab-b" / "datasets.yaml",
             [{"id": "reads", "title": "B reads", "path": str(hub / "data" / "b-reads"), "assay": "rna-seq"}])
    return s


def calls(suite):
    f = suite / "CodeSpringApp" / "bin" / "calls.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def plan(name="exp1", **kw):
    args = dict(dataset_id="ds:lab-a/reads", project_name=name, species="mouse", fastq_folder="fastq",
                compare={"column": "group", "reference": "x", "comparison": "y"})
    args.update(kw)
    return runs.plan_analysis(USER, **args)


def test_planning_checks_with_codespringapp_and_submits_nothing(suite):
    p = plan()
    assert p["plan_id"].startswith("plan-") and p["plan"]["input"]["dataset"] == "ds:lab-a/reads"
    assert "codespringapp" not in p["plan"] and "confirm" in p["next"]
    assert [c["cmd"] for c in calls(suite)] == ["check"]
    spec = calls(suite)[0]["spec"]
    assert spec["fastq_dirs"] == [str(suite.parent / "data" / "reads" / "fastq")]
    assert spec["design_matrix_path"].endswith("reads/design_matrix.txt")  # found inside the dataset
    assert spec["deseq2"] == {"column": "group", "reference": "x", "comparison": "y"}
    assert "results_root" not in spec  # the Hub can't choose where results go


def test_nothing_starts_without_the_users_confirmation(suite):
    p = plan()
    with pytest.raises(HubError, match="confirm"):
        runs.start_analysis(USER, p["plan_id"])
    with pytest.raises(HubError, match="confirm"):
        runs.start_analysis(USER, p["plan_id"], user_confirmed="yes")
    assert "start" not in [c["cmd"] for c in calls(suite)]
    started = runs.start_analysis(USER, p["plan_id"], user_confirmed=True)
    assert started["run_id"] == "rna/exp1"
    # the run uses exactly the checked request
    starts = [c for c in calls(suite) if c["cmd"] == "start"]
    assert starts[0]["spec"] == calls(suite)[0]["spec"]


def test_a_plan_is_single_use_unchangeable_and_expires(suite, monkeypatch):
    p = plan()
    runs.start_analysis(USER, p["plan_id"], user_confirmed=True)
    with pytest.raises(HubError, match="already been started"):
        runs.start_analysis(USER, p["plan_id"], user_confirmed=True)

    p2 = plan("exp2")
    path = config.USER_DIR / "runs" / f"{p2['plan_id']}.json"
    record = json.loads(path.read_text())
    record["spec"]["fastq_dirs"] = ["/somewhere/else"]  # tampering after the user saw the plan
    path.write_text(json.dumps(record))
    with pytest.raises(HubError, match="changed"):
        runs.start_analysis(USER, p2["plan_id"], user_confirmed=True)

    p3 = plan("exp3")
    monkeypatch.setattr(config, "RUN_PLAN_TTL_SECONDS", 0)
    time.sleep(0.01)
    with pytest.raises(HubError, match="expired"):
        runs.start_analysis(USER, p3["plan_id"], user_confirmed=True)
    with pytest.raises(HubError, match="isn't a plan id"):
        runs.start_analysis(USER, "../../etc/passwd", user_confirmed=True)


def test_plans_are_private_files(suite):
    p = plan()
    path = config.USER_DIR / "runs" / f"{p['plan_id']}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((config.USER_DIR / "runs").stat().st_mode) == 0o700


def test_only_the_account_owner_can_plan_or_start(suite):
    with pytest.raises(Forbidden):
        runs.plan_analysis("ann", "ds:lab-a/reads", "x", "mouse")
    p = plan()
    with pytest.raises(Forbidden):
        runs.start_analysis("ann", p["plan_id"], user_confirmed=True)


def test_inputs_must_be_registered_data_the_person_can_see(suite):
    with pytest.raises(NotFound):  # another member's personal data (USER isn't lab-a's PI)
        plan(dataset_id="ds:lab-a/~ann/mine")
    # (USER is also BSR staff, so lab-b's shared data is visible to them: BSR supports every lab.)
    with pytest.raises(HubError, match="code"):
        plan(dataset_id="ds:lab-a/scripts")
    with pytest.raises(Forbidden):
        plan(fastq_folder="../b-reads/fastq")  # escaping the dataset
    assert "check" not in [c["cmd"] for c in calls(suite)]


def test_design_rows_keep_sample_first_and_filename_last(suite):
    plan(design=[{"filename": "c1_R1.fastq.gz", "group": "x", "sample": "c1"},
                 {"sample": "c2", "group": "y", "filename": "c2_R1.fastq.gz"}])
    spec = calls(suite)[0]["spec"]
    assert spec["design"]["columns"] == ["sample", "group", "filename"]
    assert spec["design"]["rows"][0] == ["c1", "x", "c1_R1.fastq.gz"]
    with pytest.raises(HubError, match="same columns"):
        plan("e2", design=[{"sample": "a", "filename": "a.fq"}, {"sample": "b", "group": "y", "filename": "b.fq"}])


def test_codespringapp_refusals_reach_claude_and_personal_overrides_are_ignored(suite, monkeypatch):
    monkeypatch.setenv("CSL_CODESPRINGLAB_ROOT", "/tmp/my-own-lab")
    with pytest.raises(HubError, match="already exists"):
        plan("taken")
    assert calls(suite)[0]["csl_env"] == []  # the shared install decides, not the caller's environment


def test_limit_on_runs_at_once(suite, monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_RUNS", 2)
    for i in range(2):
        runs.start_analysis(USER, plan(f"e{i}")["plan_id"], user_confirmed=True)
    p = plan("e9")
    with pytest.raises(HubError, match="limit"):
        runs.start_analysis(USER, p["plan_id"], user_confirmed=True)
    (suite / "CodeSpringApp" / "bin" / "state").write_text("complete")
    assert runs.start_analysis(USER, p["plan_id"], user_confirmed=True)["run_id"] == "rna/e9"


def test_status_and_results_only_for_your_own_runs(suite, monkeypatch):
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    assert runs.analysis_status(USER, "rna/exp1")["state"] == "running"
    assert [r["run_id"] for r in runs.list_analyses(USER)] == ["rna/exp1"]
    with pytest.raises(NotFound):
        runs.analysis_status(USER, "rna/someone_elses")
    with pytest.raises(HubError):
        runs.open_results(USER, "rna/../../x")


def test_results_open_through_a_one_time_link_not_the_app_token(suite, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runs, "_running_app", lambda: (24567, True))
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    link = runs.open_results(USER, "rna/exp1")
    assert link["link"].startswith("http://localhost:24567/?ticket=") and "token=" not in link["link"]
    assert "project=rna/exp1&view=results" in link["link"]
    assert link["tunnel_command"].startswith(f"ssh -N -L 24567:localhost:24567 {USER}@")
    ticket = link["link"].split("ticket=")[1].split("&")[0]
    f = tmp_path / "home" / ".codespringweb" / "open_tickets" / ticket
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert 0 < float(f.read_text()) - time.time() <= config.OPEN_LINK_TTL_SECONDS


def test_list_workflows_puts_bsr_pipelines_first_and_nfcore_second(suite):
    from conftest import REPO  # noqa: F401
    from test_server import call
    out = call("list_workflows")
    assert [p["id"] for p in out["bsr_pipelines"]] == ["rnaseq", "atac"]
    assert out["secondary_nfcore"] and all(w["id"].startswith("nf-core") for w in out["secondary_nfcore"])
    assert "secondary" in out["rule"]


def test_list_workflows_still_answers_without_the_shared_install(hub, monkeypatch):
    monkeypatch.setattr(config, "CODESPRING_SUITE", hub / "missing")
    from test_server import call
    out = call("list_workflows")
    assert out["bsr_pipelines"] == [] and "isn't installed" in out["bsr_pipelines_unavailable"]
    assert out["secondary_nfcore"]


def test_a_personal_codespringapp_is_not_closed_to_open_results(suite, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runs, "_running_app", lambda: (31000, False))
    monkeypatch.setattr(runs, "_start_app", lambda: pytest.fail("must not restart the person's own app"))
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    out = runs.open_results(USER, "rna/exp1")
    assert out["link"] is None and "exp1" in out["how_to_open"] and "Results Explorer" in out["how_to_open"]


# ---- step by step, optional steps, email, QC, cancel ------------------------------------------------

def test_plan_passes_mode_optional_steps_and_email_choice(suite):
    plan(run_mode="step", optional_steps=["Kallisto", "rsem"], email_updates=True, email_every_hours=6)
    spec = calls(suite)[0]["spec"]
    assert spec["mode"] == "step" and spec["optional"] == ["kallisto", "rsem"]
    assert spec["notify"] == {"email": True, "every_hours": 6}  # no address: the runner only mails the person
    plan("e2")
    spec = calls(suite)[1]["spec"]
    assert spec["mode"] == "auto" and "notify" not in spec and "optional" not in spec


def test_status_shows_pauses_job_counts_and_qc(suite):
    (suite / "CodeSpringApp" / "bin" / "state").write_text("paused")
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    st = runs.analysis_status(USER, "rna/exp1")
    assert st["state"] == "paused" and st["next_step"] == "STAR" and "cancel" in st["next_actions"]
    assert st["steps"][0]["jobs"] == {"done": 6, "running": 0, "queued": 0, "failed": 0}
    assert st["qc_summary"]["star"]["uniquely_mapped"].startswith("91.5")
    qc = runs.analysis_qc(USER, "rna/exp1", "STAR")
    assert qc["samples"][0]["star"]["uniquely_mapped_pct"] == 91.5
    assert [c["args"] for c in calls(suite) if c["cmd"] == "qc"][-1] == ["rna/exp1", "STAR"]
    with pytest.raises(HubError, match="step must be"):
        runs.analysis_qc(USER, "rna/exp1", "rm -rf")


def test_continuing_and_cancelling_need_the_users_go_ahead(suite):
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    with pytest.raises(HubError, match="confirm"):
        runs.continue_analysis(USER, "rna/exp1", add_optional_steps=["kallisto"])
    with pytest.raises(HubError, match="confirm"):
        runs.cancel_analysis(USER, "rna/exp1")
    assert not [c for c in calls(suite) if c["cmd"] in ("continue", "cancel")]
    with pytest.raises(HubError, match="Optional steps are"):
        runs.continue_analysis(USER, "rna/exp1", add_optional_steps=["salmon"], user_confirmed=True)
    with pytest.raises(HubError, match="mode must be"):
        runs.continue_analysis(USER, "rna/exp1", mode="turbo", user_confirmed=True)
    out = runs.continue_analysis(USER, "rna/exp1", add_optional_steps=["Kallisto"], skip_steps=["GSEA"], mode="auto",
                                 user_confirmed=True)
    assert out["continued"] and [c["args"] for c in calls(suite) if c["cmd"] == "continue"][-1] == \
        ["rna/exp1", "--add", "kallisto", "--skip", "GSEA", "--mode", "auto"]
    cx = runs.cancel_analysis(USER, "rna/exp1", user_confirmed=True)
    assert cx["state"] == "cancelled" and "STAR" in cx["jobs"][0]
    assert [r["state"] for r in runs.list_analyses(USER)] == ["cancelled"]


def test_others_cant_follow_continue_or_cancel_your_runs(suite):
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    for fn in (lambda u: runs.analysis_qc(u, "rna/exp1"), lambda u: runs.cancel_analysis(u, "rna/exp1", user_confirmed=True),
               lambda u: runs.continue_analysis(u, "rna/exp1", user_confirmed=True)):
        with pytest.raises(Forbidden):
            fn("ann")
    with pytest.raises(NotFound):
        runs.cancel_analysis(USER, "rna/not_mine", user_confirmed=True)


def test_paused_runs_dont_count_against_the_limit(suite, monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_RUNS", 1)
    runs.start_analysis(USER, plan("e1")["plan_id"], user_confirmed=True)
    (suite / "CodeSpringApp" / "bin" / "state").write_text("paused")
    assert runs.start_analysis(USER, plan("e2")["plan_id"], user_confirmed=True)["run_id"] == "rna/e2"


def test_resuming_needs_go_ahead_and_continuing_respects_the_run_limit(suite, monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_RUNS", 1)
    (suite / "CodeSpringApp" / "bin" / "state").write_text("paused")
    runs.start_analysis(USER, plan("e1")["plan_id"], user_confirmed=True)
    runs.start_analysis(USER, plan("e2")["plan_id"], user_confirmed=True)  # e1 is paused: allowed
    with pytest.raises(HubError, match="confirm"):
        runs.resume_analysis(USER, "rna/e1")
    (suite / "CodeSpringApp" / "bin" / "state").write_text("running")  # now both count as running
    with pytest.raises(HubError, match="limit"):
        runs.continue_analysis(USER, "rna/e1", user_confirmed=True)
    with pytest.raises(HubError, match="limit"):
        runs.resume_analysis(USER, "rna/e1", user_confirmed=True)


# ---- the post-run report packet ---------------------------------------------------------------

def test_report_packet_summary_plots_and_one_time_download(suite, monkeypatch, tmp_path):
    import asyncio
    from fastapi.testclient import TestClient
    from mcp import Client
    from codespringhub import identity, portal_launch
    from codespringhub.identity import LocalLinkAuth
    from codespringhub.server import mcp
    from codespringhub.web import create_app
    monkeypatch.setattr(portal_launch, "running_portal", lambda: 23456)
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    (suite / "CodeSpringApp" / "bin" / "state").write_text("complete")
    rep = runs.analysis_report(USER, "rna/exp1")
    assert rep["differential_expression"]["t_vs_c"]["up"] == 27 and "warning" not in rep
    assert rep["kept_on_cluster"][0]["label"] == "count_matrix.txt"
    assert rep["download_link"].startswith("http://localhost:23456/api/download/")
    assert rep["_plot_files"][0].endswith("volcano_t_vs_c.png")
    # The chat tool attaches the plots as images; the paths list itself isn't shown.
    async def call():
        async with Client(mcp) as c:
            return await c.call_tool("analysis_report", {"run_id": "rna/exp1", "download_link": False})
    r = asyncio.run(call())
    assert not r.is_error and [type(x).__name__ for x in r.content] == ["TextContent", "ImageContent"]
    assert "_plot_files" not in r.content[0].text and "download_link" not in r.content[0].text
    # The link downloads the HTML once, with no portal secret.
    api = TestClient(create_app(LocalLinkAuth(new_link=True)))
    ticket = rep["download_link"].rsplit("/", 1)[1]
    got = api.get(f"/api/download/{ticket}")
    assert got.status_code == 200 and got.text == "<html>report</html>"
    assert "exp1_codespring_report.html" in got.headers["content-disposition"]
    assert api.get(f"/api/download/{ticket}").status_code == 404
    # Only report packets are ever served this way, and guessed tickets get nothing.
    other = identity.issue_download(suite / "CodeSpringApp" / "bin" / "codespring-run")
    assert api.get(f"/api/download/{other}").status_code == 404
    assert api.get("/api/download/" + "x" * 43).status_code == 404


def test_reports_only_for_your_own_runs_and_mid_run_ones_say_so(suite, monkeypatch):
    from codespringhub import portal_launch
    monkeypatch.setattr(portal_launch, "running_portal", lambda: 23456)
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    with pytest.raises(Forbidden):
        runs.analysis_report("ann", "rna/exp1")
    with pytest.raises(NotFound):
        runs.analysis_report(USER, "rna/not_mine")
    assert "hasn't finished" in runs.analysis_report(USER, "rna/exp1", download_link=False)["warning"]
