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
    version = int((here / "spec_version").read_text()) if (here / "spec_version").exists() else 1
    out({{"ok": True, "spec_version": version, "pipelines": [{{"id": "rnaseq", "runnable_from_hub": True}}, {{"id": "atac", "runnable_from_hub": False}}],
         "rnaseq": {{"references": [], "default_reference": {{}}, "adapters_r1": {{"Illumina Universal TruSeq RNA": "AGATC"}},
                    "features": ["gene_name"], "gsea_genesets": ["MSigDB_Hallmark_2020"], "limits": {{}}}}}})
if cmd in ("check", "start"):
    if spec["project_name"] == "taken":
        out({{"ok": False, "errors": ["A results folder for 'taken' already exists."]}}, 2)
    plan = {{"pipeline": "rnaseq", "project_id": "rna/" + spec["project_name"], "results_dir": "/r/" + spec["project_name"],
            "steps": [{{"step": "STAR"}}, {{"step": "DESeq2"}}] + ([{{"step": "GSEA"}}] if spec.get("gsea") else []),
            "samples": ["c1", "c2"], "groups": {{"group": {{"x": 1, "y": 1}}}}, "reference": "Mouse GRCm39",
            "codespringapp": "/x", "codespringlab": "/y"}}
    if cmd == "check":
        out({{"ok": True, "plan": plan}})
    out({{"ok": True, "project_id": plan["project_id"], "results_dir": plan["results_dir"]}})
if cmd == "status":
    if (here / "status.json").exists():
        out({{"ok": True, **json.loads((here / "status.json").read_text())}})
    out({{"ok": True, "state": state, "steps": [{{"step": "STAR", "state": "complete"}}], "results_dir": "/r"}})
if cmd == "resume":
    out({{"ok": True, "resumed": True}})
if cmd == "continue":
    out({{"ok": True, "continued": True, "next_step": "STAR"}})
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


# ---- storage --------------------------------------------------------------------------------------

def test_storage_estimate_is_a_range_around_the_measured_run():
    from codespringhub import storage
    gb = 1024**3
    est = storage.estimate_rnaseq(50 * gb)
    assert est["low_bytes"] < est["typical_bytes"] < est["high_bytes"]
    assert 6.9 * 50 * gb < est["typical_bytes"] < 7.2 * 50 * gb  # ~7x the gzipped FASTQs
    assert est["recommended_free"] == storage.human(est["high_bytes"]) and " to " in est["range"]
    assert storage.estimate_rnaseq(gb, trim=False)["typical_bytes"] < storage.estimate_rnaseq(gb)["typical_bytes"]


def test_only_the_fastqs_the_design_names_are_sized(tmp_path):
    from codespringhub import storage
    tmp_path = tmp_path / "fq"  # (tmp_path itself also holds the test Hub)
    tmp_path.mkdir()
    (tmp_path / "a_R1.fastq.gz").write_bytes(b"x" * 1000)
    (tmp_path / "a_R2.fastq.gz").write_bytes(b"x" * 1000)
    (tmp_path / "other.fastq.gz").write_bytes(b"x" * 5000)
    (tmp_path / "plain.fastq").write_bytes(b"x" * 4000)  # counted as ~1/4 (its gzipped size)
    (tmp_path / "notes.txt").write_bytes(b"x" * 9999)
    assert storage.raw_fastq_bytes(tmp_path, ["a_R1.fastq.gz,a_R2.fastq.gz"]) == (2000, 2)
    assert storage.raw_fastq_bytes(tmp_path) == (8000, 4)


def test_check_storage_reports_free_space_and_the_range(suite):
    out = runs.check_storage(USER, "ds:lab-a/reads", "fastq")
    assert out["free"] and out["results_location"]
    assert out["raw_input"]["fastq_files"] == 1 and " to " in out["rnaseq_estimate"]["range"]
    assert out["verdict"] == "enough space" and "free" in out["next"]
    assert "rnaseq_estimate" not in runs.check_storage(USER)  # no dataset: free space only
    with pytest.raises(HubError, match="code"):
        runs.check_storage(USER, "ds:lab-a/scripts")
    with pytest.raises(Forbidden):
        runs.check_storage("ann")


def test_the_plan_leads_with_storage_and_a_run_that_cannot_fit_is_refused(suite, monkeypatch):
    p = plan()
    st = p["plan"]["storage"]
    assert st["raw_input"]["fastq_files"] == 1 and "rnaseq_estimate" in st
    assert "free" in p["preview"] and "/r/exp1" in p["preview"]
    from codespringhub import storage
    monkeypatch.setattr(storage, "free_space", lambda where: {"free_bytes": 10, "free": "10 B"})
    with pytest.raises(HubError, match="Not started"):
        runs.start_analysis(USER, p["plan_id"], user_confirmed=True)
    assert "start" not in [c["cmd"] for c in calls(suite)]


def test_claude_is_told_to_talk_about_storage_first():
    from codespringhub import server
    assert "check_storage before anything else" in server.INSTRUCTIONS
    assert server.INSTRUCTIONS.index("1. check_storage") < server.INSTRUCTIONS.index("2. analysis_options")
    from test_server import call
    assert "free" in call("check_storage")


# ---- BSR's Agent: questions, preview, progress ------------------------------------------------------

EMOJI_OR_TICKS = set("\u2705\u2714\u2713\u274c\u2716\u23f3\u2b1c\u23f8\U0001f389\U0001f44d")


def _no_symbols(text):
    return not any(ch in EMOJI_OR_TICKS or ord(ch) >= 0x1F000 for ch in text)


def test_analysis_options_asks_the_standard_questions(suite):
    from test_server import call
    opts = call("analysis_options")
    assert opts["greeting"].startswith("Hello! This is BSR's Agent.")
    q = {x["id"]: x for x in opts["questions"]}
    assert list(q) == ["pipeline", "pathway_genesets", "extra_quantification", "run_mode"]
    assert q["pathway_genesets"]["multi_select"] and not q["extra_quantification"]["multi_select"]
    assert q["extra_quantification"]["options"][0]["label"] == "None, skip this"
    assert q["extra_quantification"]["options"][0]["value"] == []
    assert "selection" in opts["how_to_ask"]
    assert _no_symbols(json.dumps(opts, ensure_ascii=False))


def test_preview_is_friendly_with_steps_output_path_and_bsr_support(suite):
    p = plan()
    preview = p["preview"]
    assert preview.startswith("Hello! This is BSR's Agent. I'm here to help you set up and run the Bioinformatics "
                              "Shared Resource's RNA-seq pipeline on your data.\n\n**Your answers:** CodeSpringApp "
                              "pipeline, ")
    assert "\n\n**Storage:** Results go to `" in preview and "% used)." in preview
    assert "\n\nI'm BSR's Agent, and your RNA-seq plan is ready! We'll compare y vs x for 2 mouse samples" in preview
    assert "running all at once on the cluster" in preview and "`/r/exp1`" in preview
    assert "Here are the steps I'll run:" in preview and "| | Step | Tool | What it does |" in preview
    for step in p["plan"]["steps"]:
        assert f"| {step['step']} |" in preview
    assert "If you need any help, reach out to us: " in preview
    assert preview.endswith("reach out to us: Aarushi Gajri (gajri@cshl.edu), James Rouse (rouse@cshl.edu), "
                            "or Rad Utama (utama@cshl.edu).")
    assert [o["label"] for o in p["final_prompt"]["options"]] == ["Start pipeline", "Ask for clarification"]
    # The preview must be the turn's last message: a tool call after it lets Claude Desktop fold it away.
    assert "no tool call after" in p["final_prompt"]["how"] and "final message" in p["next"]
    assert _no_symbols(preview) and _no_symbols(p["next"])


def test_reference_words_are_short():
    from codespringhub import agent
    assert agent._reference_words("Mouse GRCm39 / GENCODE M39", "mouse") == "GRCm39"
    assert agent._reference_words("Human GRCh38 / GENCODE 47", "human") == "GRCh38"


def test_new_options_need_the_updated_codespringapp(suite):
    with pytest.raises(HubError, match="CodeSpringApp update"):
        plan(pathway_genesets=["MSigDB_Hallmark_2020", "Reactome_Pathways_2024"])
    with pytest.raises(HubError, match="CodeSpringApp update"):
        plan(extra_quantification=["rsem"])
    with pytest.raises(HubError, match="CodeSpringApp update"):
        plan(run_mode="step")
    p = plan(pathway_genesets=["MSigDB_Hallmark_2020"], extra_quantification=["none"])  # works on any version
    assert calls(suite)[-1]["spec"]["gsea"] == {"geneset": "MSigDB_Hallmark_2020"}
    assert "quantifiers" not in calls(suite)[-1]["spec"] and "run_mode" not in calls(suite)[-1]["spec"]


def test_several_gene_sets_quantifiers_and_step_mode_reach_codespringapp(suite):
    (suite / "CodeSpringApp" / "bin" / "spec_version").write_text("2")
    p = plan(pathway_genesets=["MSigDB_Hallmark_2020", "Reactome_Pathways_2024"], extra_quantification=["rsem", "kallisto"],
             run_mode="step")
    spec = [c for c in calls(suite) if c["cmd"] == "check"][-1]["spec"]
    assert spec["gsea"] == {"genesets": ["MSigDB_Hallmark_2020", "Reactome_Pathways_2024"]}
    assert spec["quantifiers"] == ["rsem", "kallisto"] and spec["run_mode"] == "step"
    assert "running one step at a time" in p["preview"]
    assert "Hallmark 2020, Reactome Pathways 2024" in p["preview"]
    started = runs.start_analysis(USER, p["plan_id"], user_confirmed=True)
    assert "stop after each step" in started["say"]
    with pytest.raises(HubError, match="at most 6"):
        plan(name="many", pathway_genesets=[f"set{i}" for i in range(7)])
    with pytest.raises(HubError, match="rsem and/or kallisto"):
        plan(name="salmon", extra_quantification=["salmon"])
    with pytest.raises(HubError, match="run_mode"):
        plan(name="fast", run_mode="fast")


def _start(suite, name="exp1"):
    p = plan(name)
    return runs.start_analysis(USER, p["plan_id"], user_confirmed=True)["run_id"]


def test_progress_is_one_line_in_words_and_paused_runs_continue(suite):
    run_id = _start(suite)
    status = suite / "CodeSpringApp" / "bin" / "status.json"
    status.write_text(json.dumps({"state": "running", "results_dir": "/r/exp1", "steps": [
        {"step": "Cutadapt", "state": "complete"}, {"step": "FastQC", "state": "running", "samples_done": 4, "samples_total": 6},
        {"step": "STAR", "state": "waiting"}]}))
    st = runs.analysis_status(USER, run_id)
    assert st["summary"] == "Done: trimming. Running: quality check (4 of 6 samples). Still to come: alignment."
    assert _no_symbols(json.dumps(st, ensure_ascii=False))
    status.write_text(json.dumps({"state": "paused", "paused_after": "FastQC", "results_dir": "/r/exp1", "steps": [
        {"step": "Cutadapt", "state": "complete"}, {"step": "FastQC", "state": "complete"}, {"step": "STAR", "state": "waiting"}]}))
    st = runs.analysis_status(USER, run_id)
    assert st["say"].startswith("Quality check is finished.") and "continue_analysis" in st["next"]
    cont = runs.continue_analysis(USER, run_id)
    assert cont["continued"] and cont["say"] == "Going on with alignment. I'll let you know when it's finished."
    status.write_text(json.dumps({"state": "failed", "error": "STAR ran out of memory", "results_dir": "/r/exp1",
                                  "steps": [{"step": "STAR", "state": "failed"}]}))
    assert "Need help? Contact BSR support" in runs.analysis_status(USER, run_id)["say"]


def test_waiting_for_progress_returns_when_a_step_changes(suite, tmp_path):
    run_id = _start(suite)
    results = tmp_path / "res"
    (results / "log").mkdir(parents=True)
    run_file = results / "log" / "codespring_hub_run.json"
    run_file.write_text(json.dumps({"state": "running", "steps": [{"step": "STAR", "state": "running"}]}))
    (suite / "CodeSpringApp" / "bin" / "status.json").write_text(json.dumps(
        {"state": "running", "results_dir": str(results), "steps": [{"step": "STAR", "state": "running"}]}))
    import threading
    threading.Timer(1.0, lambda: run_file.write_text(json.dumps(
        {"state": "running", "steps": [{"step": "STAR", "state": "complete"}]}))).start()
    t0 = time.time()
    runs.analysis_status(USER, run_id, wait_seconds=45)
    assert time.time() - t0 < 15  # returned on the change, not after the full wait


def test_run_tool_errors_and_review_requests_point_to_bsr_support(suite):
    from test_server import call
    out = call("start_analysis", plan_id="plan-0000000000000000", user_confirmed=True)["error"]
    assert "No such plan" in out and "Need help? Contact BSR support" in out
    assert "rouse@cshl.edu" in str(call("request_bsr_review", lab_id="lab-a", summary="question"))
