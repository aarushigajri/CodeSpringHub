"""Running BSR pipelines from a Claude chat, through CodeSpringApp.

The Hub never builds Slurm commands itself. It hands a request to CodeSpringApp's command-line
runner (`bin/codespring-run` in the BSR-managed shared install), which uses the app's own code:
the same CodeSpringLab scripts, references, resource requests, folders and job records as
clicking through CodeSpringApp. The runner refuses anything outside the BSR pipelines' options.

The Hub adds the guardrails that depend on who is asking:
  * inputs must be Hub-registered datasets the person can see (lab-shared or their own personal);
  * nothing runs without a plan that the person has seen and confirmed (plan -> confirm -> start);
  * a plan is single-use, expires, belongs to one person and can't be changed after it is shown;
  * a run can only be started by the person whose account it will run under;
  * a limited number of Hub-started runs per person at once (paused runs don't count: they use nothing);
  * continuing (which submits more work) and cancelling also need the person's go-ahead in the chat;
  * QC shown in the chat is each tool's own numbers; judging them is the researcher's call;
  * results open through a one-time link, so the app's long-lived access token never enters a chat.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, identity, portal_launch, store
from .errors import Forbidden, HubError, NotFound
from .hub import get_dataset
from .permissions import resolve_asset

PLAN_ID = re.compile(r"^plan-[a-f0-9]{16}$")
RUN_ID = re.compile(r"^rna/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
FINISHED = {"complete", "failed", "stopped", "cancelled"}
ACTIVE = {"queued", "running"}  # "paused" waits for the person and holds no cluster resources
OPTIONAL_STEPS = ("kallisto", "rsem")


# ---- talking to CodeSpringApp ------------------------------------------------------------------

def runner() -> Path:
    return config.CODESPRING_SUITE / "CodeSpringApp" / "bin" / "codespring-run"


def _runs_dir() -> Path:
    d = config.USER_DIR / "runs"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(config.USER_DIR, 0o700)
    os.chmod(d, 0o700)
    return d


def _cli(command: str, *args: str, spec: dict | None = None, timeout: int = 180) -> dict:
    """Run codespring-run and return its JSON answer. Refusals become HubErrors with its reasons."""
    exe = runner()
    if not exe.exists():
        raise HubError("CodeSpringApp isn't installed for the Hub yet (BSR: see docs/DESIGN.md, 'Running pipelines').")
    work = _runs_dir()  # also makes sure the private folder exists before we run anything in it
    spec_file = None
    if spec is not None:
        spec_file = work / f"request-{secrets.token_hex(6)}.json"
        spec_file.write_text(json.dumps(spec))
        args = (str(spec_file), *args)
    # The shared install decides where CodeSpringLab and results live; ignore personal overrides.
    env = {k: v for k, v in os.environ.items() if not k.startswith("CSL_")}
    try:
        proc = subprocess.run([str(exe), command, *args], capture_output=True, text=True, timeout=timeout,
                              env=env, cwd=str(work))
    except subprocess.TimeoutExpired:
        raise HubError("CodeSpringApp took too long to answer; try again in a minute.")
    finally:
        if spec_file:
            spec_file.unlink(missing_ok=True)
    try:
        answer = json.loads(proc.stdout)
    except ValueError:
        raise HubError(f"CodeSpringApp gave an unexpected answer: {(proc.stderr or proc.stdout)[-400:]}")
    if not answer.get("ok"):
        errors = answer.get("errors") or ["unknown problem"]
        errors = [errors] if isinstance(errors, str) else errors
        raise HubError("CodeSpringApp refused this request:\n- " + "\n- ".join(errors))
    return answer


def _require_self(user: str):
    """Runs execute under the account running this Hub process; never on someone else's behalf."""
    if user != getpass.getuser():
        raise Forbidden("Analyses can only be started by the person whose account runs them.")


# ---- what BSR runs -----------------------------------------------------------------------------

def catalog() -> dict:
    cat = _cli("catalog")
    return {
        "pipelines": cat["pipelines"],
        "rnaseq_options": {
            "references": cat["rnaseq"]["references"],
            "default_reference": cat["rnaseq"]["default_reference"],
            "adapters": list(cat["rnaseq"]["adapters_r1"]),
            "features": cat["rnaseq"]["features"],
            "pathway_genesets": cat["rnaseq"]["gsea_genesets"],
            "optional_steps": cat["rnaseq"].get("optional_steps", []),
            "run_modes": cat["rnaseq"].get("modes", {}),
            "email_updates": cat["rnaseq"].get("notify", {}),
            "batching": cat["rnaseq"].get("batching", {}),
            "limits": cat["rnaseq"]["limits"],
        },
    }


# ---- plan -> confirm -> start -------------------------------------------------------------------

def _design_spec(dataset_path: Path, design, design_file):
    if design:
        if not isinstance(design, list) or not all(isinstance(r, dict) for r in design):
            raise HubError("design must be a list of rows like {'sample': 'ctrl_1', 'condition': 'control', 'filename': 'ctrl1_R1.fastq.gz,ctrl1_R2.fastq.gz'}.")
        keys = list(design[0])
        if "sample" not in keys or "filename" not in keys:
            raise HubError("Every design row needs 'sample' and 'filename' (R1,R2 for paired-end), plus grouping columns.")
        columns = ["sample"] + [k for k in keys if k not in ("sample", "filename")] + ["filename"]
        if any(set(r) != set(columns) for r in design):
            raise HubError("Every design row needs the same columns.")
        return {"design": {"columns": columns, "rows": [[str(r[c]) for c in columns] for r in design]}}
    return {"design_matrix_path": str(design_file)}


def plan_analysis(user, dataset_id, project_name, species, fastq_folder="", design=None, design_file="",
                  paired_end=True, genome_version="", trim_adapters=True, adapter="Illumina Universal TruSeq RNA",
                  min_length=20, count_by="gene_name", compare=None, pathway_geneset=None,
                  run_mode="auto", optional_steps=None, email_updates=False, email_every_hours=0) -> dict:
    _require_self(user)
    ds = get_dataset(user, dataset_id)
    if ds.kind != "data":
        raise HubError(f"{dataset_id} is registered as code, not data.")
    fastq_dir = resolve_asset(ds, fastq_folder)
    if not fastq_dir.is_dir():
        raise HubError("fastq_folder must be a folder inside the dataset.")
    if not design and not design_file:
        for candidate in ("design_matrix.txt", "manifest/design_matrix.txt"):
            if (Path(ds.path) / candidate).exists():
                design_file = candidate
                break
        else:
            raise HubError("No design given. Pass design rows, or design_file (a design_matrix.txt inside the dataset).")
    design_path = resolve_asset(ds, design_file) if not design else None
    spec = {
        "pipeline": "rnaseq", "project_name": project_name, "fastq_dirs": [str(fastq_dir)],
        "paired_end": bool(paired_end), "species": species, "genome_version": genome_version or "",
        "trim": {"enabled": bool(trim_adapters), "adapter_r1": adapter, "adapter_r2": adapter, "min_length": int(min_length)},
        "feature": count_by,
        **_design_spec(Path(ds.path), design, design_path),
    }
    if compare:
        spec["deseq2"] = {"column": compare.get("column", ""), "reference": compare.get("reference", ""),
                          "comparison": compare.get("comparison", "")}
    if pathway_geneset:
        spec["gsea"] = {"geneset": pathway_geneset}
    spec["mode"] = run_mode or "auto"
    if optional_steps:
        spec["optional"] = [str(x).lower() for x in optional_steps]
    if email_updates:
        spec["notify"] = {"email": True, "every_hours": email_every_hours or 0}
    plan = _cli("check", spec=spec)["plan"]
    plan.pop("codespringapp", None)
    plan.pop("codespringlab", None)
    plan["input"] = {"dataset": dataset_id, "folder": fastq_folder or "(whole dataset)"}

    plan_id = f"plan-{secrets.token_hex(8)}"
    record = {"plan_id": plan_id, "user": user, "created": time.time(), "dataset_id": dataset_id, "lab_id": ds.lab_id,
              "personal": bool(ds.owner), "spec": spec, "plan": plan}
    record["digest"] = hashlib.sha256(json.dumps([spec, plan], sort_keys=True).encode()).hexdigest()
    path = _runs_dir() / f"{plan_id}.json"
    path.write_text(json.dumps(record, indent=2))
    os.chmod(path, 0o600)
    return {
        "plan_id": plan_id,
        "expires_in_minutes": config.RUN_PLAN_TTL_SECONDS // 60,
        "plan": plan,
        "next": ("Show this plan to the user in plain language (samples and groups, reference, each step, the "
                 "comparison, where results go, and that it runs on Slurm under their account). Also say whether "
                 "it runs straight through or pauses after each step, which optional steps are included or can be "
                 "added later, and whether email updates are on (ask if they want them; they go only to the "
                 "user's own CSHL address). Ask them to confirm. Only after they say yes, call start_analysis "
                 "with this plan_id and user_confirmed=true."),
    }


def _load_plan(user, plan_id) -> tuple[Path, dict]:
    if not PLAN_ID.match(plan_id or ""):
        raise HubError("That isn't a plan id from plan_analysis.")
    path = _runs_dir() / f"{plan_id}.json"
    if not path.exists():
        raise NotFound("No such plan (it may have been used already). Make a new one with plan_analysis.")
    record = json.loads(path.read_text())
    if record.get("user") != user:
        raise Forbidden("That plan belongs to someone else.")
    if record.get("used"):
        raise HubError("That plan has already been started. Make a new plan to run again.")
    if time.time() - record["created"] > config.RUN_PLAN_TTL_SECONDS:
        raise HubError("That plan has expired. Make a new one with plan_analysis and confirm it with the user again.")
    digest = hashlib.sha256(json.dumps([record["spec"], record["plan"]], sort_keys=True).encode()).hexdigest()
    if digest != record.get("digest"):
        raise HubError("That plan was changed after it was made. Make a new one with plan_analysis.")
    return path, record


def _run_records() -> list[dict]:
    path = _runs_dir() / "runs.json"
    return json.loads(path.read_text()) if path.exists() else []


def _save_runs(runs: list[dict]):
    path = _runs_dir() / "runs.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(runs, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _check_run_limit(exclude=None) -> list[dict]:
    """Refresh states, then refuse if starting/continuing/resuming would exceed the active-run limit."""
    runs = _run_records()
    for r in [r for r in runs if r.get("state") not in FINISHED]:
        try:
            r["state"] = _cli("status", r["run_id"]).get("state", r.get("state"))
        except HubError:
            pass
    _save_runs(runs)
    active = [r for r in runs if r.get("state") in ACTIVE | {"cancelling"} and r["run_id"] != exclude]
    if len(active) >= config.MAX_ACTIVE_RUNS:
        raise HubError(f"You already have {len(active)} analyses running from the Hub (the limit is "
                       f"{config.MAX_ACTIVE_RUNS}). Wait for one to finish, or ask BSR.")
    return runs


def start_analysis(user, plan_id, user_confirmed=False) -> dict:
    _require_self(user)
    if user_confirmed is not True:
        raise HubError("Not started. Show the plan to the user and ask them to confirm; then call again with user_confirmed=true.")
    path, record = _load_plan(user, plan_id)
    runs = _check_run_limit()
    result = _cli("start", spec=record["spec"])
    record["used"] = time.time()
    path.write_text(json.dumps(record, indent=2))
    runs.append({"run_id": result["project_id"], "plan_id": plan_id, "dataset_id": record["dataset_id"],
                 "lab_id": record["lab_id"], "personal": record["personal"], "state": "queued",
                 "started": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    _save_runs(runs)
    return {
        "run_id": result["project_id"],
        "results_folder": result["results_dir"],
        "steps": [s["step"] for s in record["plan"]["steps"]],
        "mode": record["spec"].get("mode", "auto"),
        "next": ("Use analysis_status to follow it (and analysis_qc for per-sample QC as steps finish). In 'step' "
                 "mode it pauses after each step: show that step's QC, then ask whether to continue, add an "
                 "optional step, skip one, or cancel. You can't message the user on your own: tell them to ask "
                 "you for an update any time, or rely on the email updates if they turned them on. When it is "
                 "complete, open_results gives a link to CodeSpringApp's Results Explorer."),
    }


def _own_run(run_id) -> dict:
    if not RUN_ID.match(run_id or ""):
        raise HubError("That isn't a run id from start_analysis.")
    for r in _run_records():
        if r["run_id"] == run_id:
            return r
    raise NotFound("No analysis with that id was started by you from the Hub. list_analyses shows yours.")


def _remember_state(run_id, state):
    runs = _run_records()
    for r in runs:
        if r["run_id"] == run_id:
            r["state"] = state
    _save_runs(runs)


def analysis_status(user, run_id, include_qc=True) -> dict:
    _require_self(user)
    _own_run(run_id)
    st = _cli("status", run_id)
    _remember_state(run_id, st.get("state"))
    steps = []
    for s in st.get("steps", []):
        row = {k: s.get(k) for k in ("step", "state", "what", "started", "finished", "message") if s.get(k)}
        jobs = s.get("jobs") or {}
        if any(jobs.get(k) for k in ("queued", "running", "done", "failed")):
            row["jobs"] = {k: jobs.get(k, 0) for k in ("done", "running", "queued", "failed")}
            if jobs.get("failed_samples"):
                row["jobs"]["failed_samples"] = jobs["failed_samples"]
        steps.append(row)
    out = {
        "run_id": run_id, "state": st.get("state"), "mode": st.get("mode", "auto"), "error": st.get("error"),
        "steps": steps, "next_actions": st.get("next_actions", []),
        "results_folder": st.get("results_dir"), "updated": st.get("updated"),
    }
    if st.get("state") == "paused":
        out["paused_after"], out["next_step"] = st.get("paused_after"), st.get("next_step")
    if include_qc and any(s.get("state") in ("complete", "running") for s in steps):
        try:
            out["qc_summary"] = _cli("qc", run_id).get("summary")
        except HubError:
            pass
    return out


def analysis_qc(user, run_id, step=None) -> dict:
    _require_self(user)
    _own_run(run_id)
    steps = ("Cutadapt", "FastQC", "STAR", "featureCounts", "Kallisto")
    if step and step not in steps:
        raise HubError(f"step must be one of: {', '.join(steps)}.")
    qc = _cli("qc", run_id, *([step] if step else []))
    return {"run_id": run_id, "samples": qc.get("samples", []), "summary": qc.get("summary"), "note": qc.get("note")}


def continue_analysis(user, run_id, add_optional_steps=None, skip_steps=None, mode=None, user_confirmed=False) -> dict:
    _require_self(user)
    _own_run(run_id)
    if user_confirmed is not True:
        raise HubError("Not continued. Tell the user what will run next (and anything added or skipped), ask them "
                       "to confirm, then call again with user_confirmed=true.")
    add = [str(x).lower() for x in (add_optional_steps or [])]
    bad = [a for a in add if a not in OPTIONAL_STEPS]
    if bad:
        raise HubError(f"Optional steps are {', '.join(OPTIONAL_STEPS)} (CodeSpringApp's own); not {', '.join(bad)}.")
    if mode not in (None, "auto", "step"):
        raise HubError("mode must be 'auto' (run straight through) or 'step' (pause after each step).")
    _check_run_limit(exclude=run_id)
    args = [run_id]
    if add:
        args += ["--add", ",".join(add)]
    if skip_steps:
        args += ["--skip", ",".join(str(x) for x in skip_steps)]
    if mode:
        args += ["--mode", mode]
    res = _cli("continue", *args)
    _remember_state(run_id, "queued")
    return {"run_id": run_id, "continued": True, "steps": res.get("steps"), "note": res.get("note")}


def cancel_analysis(user, run_id, user_confirmed=False) -> dict:
    _require_self(user)
    _own_run(run_id)
    if user_confirmed is not True:
        raise HubError("Not cancelled. Ask the user to confirm they want to stop this analysis (finished steps are "
                       "kept), then call again with user_confirmed=true.")
    res = _cli("cancel", run_id, timeout=300)
    _remember_state(run_id, res.get("state", "cancelled"))
    return {"run_id": run_id, "state": res.get("state", "cancelled"), "controller": res.get("controller"),
            "jobs": res.get("jobs"), "note": res.get("note")}


REPORT_PLOTS_IN_CHAT = 6
REPORT_PLOT_MAX_BYTES = 1_500_000


def analysis_report(user, run_id, download_link=True) -> dict:
    """Build (or rebuild) the run's report packet and say where it is. The images to show in the
    chat are returned separately under "_plot_files" (the tool attaches them; they never reach the
    log)."""
    _require_self(user)
    _own_run(run_id)
    ans = _cli("report", run_id, timeout=600)
    report = Path(ans["report"])
    out = {
        "run_id": run_id,
        "state": ans.get("state"),
        "report_file": str(report),
        "size_mb": ans.get("size_mb"),
        "differential_expression": ans.get("deg_summary") or {},
        "top_pathways": ans.get("gsea_top") or {},
        "qc_summary": ans.get("qc_summary") or {},
        "plots_in_report": [p["label"] for p in ans.get("plots", [])],
        "files_in_report": ans.get("files", []),
        "kept_on_cluster": ans.get("left_out", []),
        "note": ans.get("note"),
    }
    if ans.get("state") != "complete":
        out["warning"] = "The run hasn't finished; the report shows only the steps done so far."
    if download_link:
        port = portal_launch.running_portal() or portal_launch.start_portal()
        ticket = identity.issue_download(report)
        host = config.CONNECT_HOST or _laptop_host()
        minutes = config.OPEN_LINK_TTL_SECONDS // 60
        out["download_link"] = f"http://localhost:{port}/api/download/{ticket}"
        out["how_to_download"] = (f"Click within {minutes} minutes; it works once and saves one HTML file that opens in any "
                                  "browser, with every plot and small file downloadable from it. If it doesn't load, run "
                                  "the tunnel command on your laptop (CSHL Wi-Fi or VPN) and ask for a new link.")
        out["tunnel_command"] = f"ssh -N -L {port}:localhost:{port} {user}@{host}"
    out["open_in_app"] = "For interactive plots and every file, use open_results to open this run in CodeSpringApp."
    out["_plot_files"] = [p["path"] for p in ans.get("plots", [])
                          if p["path"].lower().endswith(".png") and Path(p["path"]).is_file()
                          and Path(p["path"]).stat().st_size <= REPORT_PLOT_MAX_BYTES][:REPORT_PLOTS_IN_CHAT]
    return out


def list_analyses(user) -> list[dict]:
    _require_self(user)
    return [{k: r.get(k) for k in ("run_id", "dataset_id", "state", "started")} for r in _run_records()]


def resume_analysis(user, run_id, user_confirmed=False) -> dict:
    _require_self(user)
    _own_run(run_id)
    if user_confirmed is not True:
        raise HubError("Not resumed. Ask the user to confirm (it submits the remaining steps to the cluster), then "
                       "call again with user_confirmed=true.")
    _check_run_limit(exclude=run_id)
    _cli("resume", run_id)
    _remember_state(run_id, "queued")
    return {"run_id": run_id, "resumed": True, "note": "Finished steps are kept; the rest start again."}


# ---- opening the Results Explorer ---------------------------------------------------------------

def _app_home() -> Path:
    return Path.home() / ".codespringweb"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _running_app() -> tuple[int, bool] | None:
    """This person's running CodeSpringApp on this login node, if any: (port, is the shared install)."""
    for url_file in sorted(_app_home().glob("codespringweb_*.url"), key=lambda p: p.stat().st_mtime, reverse=True):
        port = url_file.stem.split("_")[-1]
        pid_file = url_file.with_suffix(".pid")
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            continue
        if port.isdigit() and _alive(pid):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", int(port))) == 0:
                    try:
                        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
                    except OSError:
                        cmdline = ""
                    return int(port), str(config.CODESPRING_SUITE) in cmdline
    return None


def _start_app(wait_seconds: int = 120) -> int:
    launcher = config.CODESPRING_SUITE / "CodeSpringApp" / "run_codespringweb.sh"
    if not launcher.exists():
        raise HubError("CodeSpringApp isn't installed for the Hub yet.")
    log = _runs_dir() / "codespringapp-launch.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CSL_")}
    with open(log, "w") as fh:
        subprocess.Popen(["setsid", "bash", str(launcher)], stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                         env=env, cwd=str(Path.home()), start_new_session=True)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(2)
        running = _running_app()
        if running:
            return running[0]
    raise HubError(f"CodeSpringApp didn't start within {wait_seconds} s. Its log: {log}")


def open_results(user, run_id, view="results") -> dict:
    _require_self(user)
    _own_run(run_id)
    if view not in ("results", "progress", "logs"):
        raise HubError("view must be results, progress or logs.")
    running = _running_app()
    if running and not running[1]:
        # Their own CodeSpringApp checkout is open. Starting the shared one would close it (the
        # launcher stops a person's other instances), and older checkouts can't open project links.
        return {
            "link": None,
            "how_to_open": (f"Your own CodeSpringApp is already running (port {running[0]}). In it, choose RNA-seq, "
                            f"then project '{run_id.split('/', 1)[1]}', then the Results Explorer tab. Or close it "
                            "and ask again for a direct link."),
        }
    port = running[0] if running else _start_app()
    tickets = _app_home() / "open_tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    os.chmod(tickets, 0o700)
    ticket = secrets.token_urlsafe(32)
    path = tickets / ticket
    path.write_text(str(time.time() + config.OPEN_LINK_TTL_SECONDS))
    os.chmod(path, 0o600)
    host = config.CONNECT_HOST or _laptop_host()
    minutes = config.OPEN_LINK_TTL_SECONDS // 60
    from .editing import codespringapp_port
    if port == codespringapp_port(user):
        # The Connect page's Claude Desktop setup forwards this port, so the link usually just works.
        how = (f"Click the link within {minutes} minutes (it works once). If Claude Desktop was set up from the "
               "Connect page, it opens straight away. If the page doesn't load, run the tunnel command in a "
               "terminal on your laptop (CSHL Wi-Fi or VPN), leave it open, and ask for a new link.")
    else:
        how = ("On your laptop (CSHL Wi-Fi or VPN), run the tunnel command in a terminal and leave it open, "
               f"then open the link within {minutes} minutes. The link works once; ask again for a new one.")
    return {
        "link": f"http://localhost:{port}/?ticket={ticket}&project={run_id}&view={view}",
        "tunnel_command": f"ssh -N -L {port}:localhost:{port} {user}@{host}",
        "how_to_open": how,
    }


def _laptop_host() -> str:
    from .editing import _laptop_hostname  # one definition of the laptop-facing host name
    return _laptop_hostname()
