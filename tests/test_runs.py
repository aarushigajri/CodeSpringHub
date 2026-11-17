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
    extra = {{"paused_after": "FastQC", "next_step": "STAR"}} if state == "paused" else {{}}
    out({{"ok": True, "state": state, "results_dir": "/r", "next_actions": ["continue: run STAR", "cancel"],
         "steps": [{{"step": "FastQC", "state": "complete", "jobs": {{"done": 6, "running": 0, "queued": 0, "failed": 0}}}},
                   {{"step": "STAR", "state": "waiting"}}], **extra}})
if cmd == "qc":
    out({{"ok": True, "samples": [{{"sample": "c1", "star": {{"uniquely_mapped_pct": 91.5}}}}],
         "summary": {{"star": {{"uniquely_mapped": "91.5% to 91.5%"}}}}, "note": "tool numbers"}})
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
    assert runs.open_app(USER)["link"] is None


def test_open_app_gives_one_link_to_codespringapp_itself(suite, monkeypatch, tmp_path):
    from codespringhub import editing
    monkeypatch.setattr(runs, "_app_home", lambda: tmp_path)
    monkeypatch.setattr(runs, "_running_app", lambda: (31804, True))
    monkeypatch.setattr(runs, "ports_forwarded", lambda: True)
    monkeypatch.setattr(editing, "codespringapp_port", lambda u: 31804)
    out = runs.open_app(USER)
    assert out["link"].startswith("http://localhost:31804/?ticket=") and "project=" not in out["link"]
    assert out["say"].startswith(f"**[Open CodeSpringApp]({out['link']})**") and not out["needs_tunnel"]


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


def test_claude_introduces_the_agent_then_asks_then_checks_storage_then_plans():
    from codespringhub import server
    rules = server.INSTRUCTIONS
    order = ["1. analysis_options", "greeting is your first words", "3. check_storage", "4. plan_analysis",
             "5. start_analysis"]
    assert [rules.index(r) for r in order] == sorted(rules.index(r) for r in order)
    from test_server import call
    out = call("check_storage")
    assert "free" in out["say"] and "Open" not in out["say"]  # no dataset: just the space, no links


def test_the_links_come_in_the_preview_not_before_a_tool_call(suite, monkeypatch):
    # Claude Desktop folds text written before a tool call into a summary, so links shown with
    # check_storage (right before plan_analysis) never reached the person. They live in the preview.
    from codespringhub import portal_launch
    from test_server import call
    hub_link = {"link": "http://localhost:41000/#ticket=a", "port": 41000, "host": "bamdev1.cshl.edu",
                "needs_tunnel": True}
    app_link = {"link": "http://localhost:21000/?ticket=b", "port": 21000, "host": "bamdev1.cshl.edu",
                "needs_tunnel": True}
    monkeypatch.setattr(portal_launch, "open_portal", lambda u: hub_link)
    monkeypatch.setattr(runs, "open_app", lambda u: app_link)
    out = call("check_storage", dataset_id="ds:lab-a/reads", fastq_folder="fastq")
    assert out["say"].startswith("Your raw data is") and "so there's enough space." in out["say"]
    assert "Open" not in out["say"] and "plan_analysis" in out["next"] and "without writing" in out["next"]

    p = call("plan_analysis", dataset_id="ds:lab-a/reads", project_name="exp1", species="mouse",
             fastq_folder="fastq", compare={"column": "group", "reference": "x", "comparison": "y"})
    preview = p["preview"]
    # One tunnel command for both ports, before the links; both links; BSR's contacts still last.
    assert preview.count("ssh -N") == 1 and "-L 41000:localhost:41000 -L 21000:localhost:21000" in preview
    assert preview.index("**Steps**") < preview.index("**Your links**") < preview.index("ssh -N")
    assert preview.index("ssh -N") < preview.index("[Open CodeSpringHub](http://localhost:41000/#ticket=a)")
    assert "[Open CodeSpringApp](http://localhost:21000/?ticket=b)" in preview
    assert preview.rstrip().endswith("Rad Utama (utama@cshl.edu).") and "links" in p["next"]
    assert _no_symbols(preview)

    # Forwarded ports need no tunnel; an app that can't start leaves just the Hub link.
    monkeypatch.setattr(portal_launch, "open_portal", lambda u: {**hub_link, "needs_tunnel": False})
    def broken(u):
        raise HubError("CodeSpringApp isn't installed for the Hub yet.")
    monkeypatch.setattr(runs, "open_app", broken)
    preview = call("plan_analysis", dataset_id="ds:lab-a/reads", project_name="exp2", species="mouse",
                   fastq_folder="fastq")["preview"]
    assert "ssh" not in preview and "Open CodeSpringHub" in preview and "Open CodeSpringApp" not in preview


def test_short_storage_stops_before_planning(suite, monkeypatch):
    from codespringhub import storage
    from test_server import call
    monkeypatch.setattr(storage, "free_space", lambda where: {"free_bytes": 10, "free": "10 B"})
    out = call("check_storage", dataset_id="ds:lab-a/reads", fastq_folder="fastq")
    assert "Don't call plan_analysis" in out["next"]


def test_storage_that_is_short_leads_with_the_warning():
    from codespringhub import agent
    out = {"results_location": "~/csl_results", "free": "1 GB", "verdict": "not enough space",
           "raw_input": {"size": "2 GB", "fastq_files": 4},
           "rnaseq_estimate": {"range": "11 GB to 18 GB", "typical": "14 GB", "recommended_free": "18 GB"}}
    say = agent.storage_say(out)
    assert say.startswith("Storage is not enough space.") and "at least 18 GB" in say and "ask BSR" in say


# ---- BSR's Agent: questions, preview, progress ------------------------------------------------------

EMOJI_OR_TICKS = set("\u2705\u2714\u2713\u274c\u2716\u23f3\u2b1c\u23f8\U0001f389\U0001f44d")


def _no_symbols(text):
    return not any(ch in EMOJI_OR_TICKS or ord(ch) >= 0x1F000 for ch in text)


def test_analysis_options_asks_the_standard_questions(suite):
    from test_server import call
    opts = call("analysis_options")
    assert opts["greeting"].startswith("Hi, this is BSR's Agent! I'll help you run")
    assert "CodeSpringHub is how I see" in opts["greeting"] and "CodeSpringApp is BSR's pipeline app" in opts["greeting"]
    assert "link to each" in opts["greeting"] and "check_storage" in opts["next"]
    q = {x["id"]: x for x in opts["questions"]}
    assert list(q) == ["pipeline", "pathway_genesets", "extra_quantification", "run_mode", "email_updates"]
    assert q["email_updates"]["default"] is False and "own CSHL address" in json.dumps(q["email_updates"])
    assert q["pathway_genesets"]["multi_select"] and not q["extra_quantification"]["multi_select"]
    assert q["extra_quantification"]["options"][0]["label"] == "None, skip this"
    assert q["extra_quantification"]["options"][0]["value"] == []
    assert "selection" in opts["how_to_ask"]
    assert _no_symbols(json.dumps(opts, ensure_ascii=False))


def test_preview_is_friendly_with_steps_output_path_and_bsr_support(suite):
    p = plan()
    preview = p["preview"]
    assert preview.startswith("**BSR's Agent: your RNA-seq plan is ready.** We'll compare y vs x across 2 mouse "
                              "samples on GRCm39, running all steps at once under your account.")
    # Every setting once, in one table; storage appears once, not twice.
    assert "| Setting | Choice |" in preview and "| Results folder | `/r/exp1` |" in preview
    assert "| Pathway gene sets | None (no pathway analysis) |" in preview
    assert "| Email updates | None |" in preview
    assert preview.count("free") == 1 and preview.count("Storage") == 1
    assert "**Steps**" in preview and "| | Step | Tool | What it does |" in preview
    for step in p["plan"]["steps"]:
        assert f"| {step['step']} |" in preview
    assert preview.endswith("Reach BSR: Aarushi Gajri (gajri@cshl.edu), James Rouse (rouse@cshl.edu), "
                            "or Rad Utama (utama@cshl.edu).")
    assert [o["label"] for o in p["final_prompt"]["options"]] == ["Start pipeline", "Ask for clarification"]
    # The preview must be the turn's last message: a tool call after it lets Claude Desktop fold it away.
    assert "no tool call after" in p["final_prompt"]["how"] and "final message" in p["next"]
    assert _no_symbols(preview) and _no_symbols(p["next"])


def test_start_gives_a_live_link_and_says_when_a_tunnel_is_needed(suite, monkeypatch, tmp_path):
    monkeypatch.setattr(runs, "_app_home", lambda: tmp_path)
    monkeypatch.setattr(runs, "_running_app", lambda: (31804, True))
    monkeypatch.setattr(runs, "ports_forwarded", lambda: False)
    started = runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    assert "[Open the live progress page in CodeSpringApp](http://localhost:31804/?ticket=" in started["say"]
    assert "view=progress" in started["say"] and "ssh -N -L 31804:localhost:31804" in started["say"]
    assert started["live_progress"]["needs_tunnel"] is True
    # Started over the Connect page's SSH login, on the person's own port: no tunnel step.
    from codespringhub import editing
    monkeypatch.setattr(runs, "ports_forwarded", lambda: True)
    monkeypatch.setattr(editing, "codespringapp_port", lambda u: 31804)
    link = runs.open_results(USER, "rna/exp1")
    assert link["needs_tunnel"] is False and link["how_to_open"].startswith("Click the link")


def test_an_older_codespringapp_gets_a_plain_message_not_a_usage_line(suite, monkeypatch):
    exe = suite / "CodeSpringApp" / "bin" / "codespring-run"
    exe.write_text(exe.read_text().replace('if cmd == "report":', 'if cmd == "report_unsupported":').replace(
        'out({"ok": False, "errors": "unknown"}, 2)',
        'out({"ok": False, "errors": "usage: codespring-run catalog | check SPEC.json | status PROJECT_ID"}, 2)'))
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    with pytest.raises(HubError, match="older than the Hub and has no 'report' command"):
        runs.analysis_report(USER, "rna/exp1", download_link=False)


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
    with pytest.raises(HubError, match="confirm"):
        runs.continue_analysis(USER, run_id)
    cont = runs.continue_analysis(USER, run_id, user_confirmed=True)
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


# ---- emails, QC, cancel, limits ------------------------------------------------------------------

def test_email_updates_are_asked_and_go_only_to_the_person(suite):
    with pytest.raises(HubError, match="CodeSpringApp update"):
        plan(email_updates=True)
    (suite / "CodeSpringApp" / "bin" / "spec_version").write_text("2")
    plan(email_updates=True, email_every_hours=6)
    spec = [c for c in calls(suite) if c["cmd"] == "check"][-1]["spec"]
    assert spec["notify"] == {"email": True, "every_hours": 6}  # no address: the runner only mails the person
    plan("e2")
    assert "notify" not in [c for c in calls(suite) if c["cmd"] == "check"][-1]["spec"]


def test_status_shows_pauses_job_counts_and_qc(suite):
    (suite / "CodeSpringApp" / "bin" / "state").write_text("paused")
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    st = runs.analysis_status(USER, "rna/exp1")
    assert st["state"] == "paused" and st["next_step"] == "STAR" and "cancel" in st["next_actions"]
    assert st["steps"][0]["jobs"] == {"done": 6, "running": 0, "queued": 0, "failed": 0}
    assert "user_confirmed=true" in st["next"] and "cancel_analysis" in st["next"]
    qc = runs.analysis_qc(USER, "rna/exp1", "STAR")
    assert qc["samples"][0]["star"]["uniquely_mapped_pct"] == 91.5
    assert [c["args"] for c in calls(suite) if c["cmd"] == "qc"][-1] == ["rna/exp1", "STAR"]
    with pytest.raises(HubError, match="step must be"):
        runs.analysis_qc(USER, "rna/exp1", "rm -rf")


def test_continuing_and_cancelling_need_the_users_go_ahead(suite):
    runs.start_analysis(USER, plan()["plan_id"], user_confirmed=True)
    with pytest.raises(HubError, match="confirm"):
        runs.continue_analysis(USER, "rna/exp1", add_quantification=["kallisto"])
    with pytest.raises(HubError, match="confirm"):
        runs.cancel_analysis(USER, "rna/exp1")
    assert not [c for c in calls(suite) if c["cmd"] in ("continue", "cancel")]
    with pytest.raises(HubError, match="rsem and/or kallisto"):
        runs.continue_analysis(USER, "rna/exp1", add_quantification=["salmon"], user_confirmed=True)
    with pytest.raises(HubError, match="run_mode"):
        runs.continue_analysis(USER, "rna/exp1", run_mode="turbo", user_confirmed=True)
    with pytest.raises(HubError, match="can be skipped"):
        runs.continue_analysis(USER, "rna/exp1", skip_steps=["STAR"], user_confirmed=True)
    out = runs.continue_analysis(USER, "rna/exp1", add_quantification=["Kallisto"], skip_steps=["GSEA"], run_mode="all",
                                 user_confirmed=True)
    assert out["continued"] and [c["args"] for c in calls(suite) if c["cmd"] == "continue"][-1] == \
        ["rna/exp1", "--add", "kallisto", "--skip", "GSEA", "--mode", "all"]
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


def test_cancelled_runs_say_so_in_words(suite):
    run_id = _start(suite)
    (suite / "CodeSpringApp" / "bin" / "status.json").write_text(json.dumps({"state": "cancelled", "results_dir": "/r/exp1",
        "steps": [{"step": "Cutadapt", "state": "complete"}, {"step": "STAR", "state": "cancelled"}, {"step": "GSEA", "state": "skipped"}]}))
    st = runs.analysis_status(USER, run_id)
    assert st["say"].startswith("The run is cancelled.") and "STAR" not in st["summary"]
    assert st["summary"] == "Done: trimming. Stopped with a problem: alignment (cancelled)."


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
