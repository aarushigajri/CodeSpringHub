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
  * a limited number of Hub-started runs per person at once;
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

from . import agent, config, qc, storage, store
from .errors import Forbidden, HubError, NotFound
from .hub import get_dataset
from .permissions import resolve_asset

PLAN_ID = re.compile(r"^plan-[a-f0-9]{16}$")
RUN_ID = re.compile(r"^rna/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
FINISHED = {"complete", "failed", "stopped"}
QUANTIFIERS = ("rsem", "kallisto")
RUN_MODES = ("all", "step")
MAX_GENESETS = 6
MAX_WAIT_SECONDS = 45


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
        "spec_version": cat.get("spec_version", 1),
        "pipelines": cat["pipelines"],
        "rnaseq_options": {
            "references": cat["rnaseq"]["references"],
            "default_reference": cat["rnaseq"]["default_reference"],
            "adapters": list(cat["rnaseq"]["adapters_r1"]),
            "features": cat["rnaseq"]["features"],
            "pathway_genesets": cat["rnaseq"]["gsea_genesets"],
            "extra_quantification": cat["rnaseq"].get("quantifiers") and list(cat["rnaseq"]["quantifiers"]) or [],
            "run_modes": cat["rnaseq"].get("run_modes") or ["all"],
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


def _design_filenames(design, design_path) -> list[str] | None:
    """The read files a design names (its 'filename' column), or None if it can't be read."""
    if design:
        return [str(r.get("filename", "")) for r in design]
    try:
        lines = [l for l in Path(design_path).read_text().splitlines() if l.strip()]
        header = lines[0].split("\t")
        col = header.index("filename") if "filename" in header else len(header) - 1
        return [l.split("\t")[col] for l in lines[1:] if len(l.split("\t")) > col]
    except (OSError, ValueError, IndexError):
        return None


def _input_storage(fastq_dir: Path, filenames, trim: bool, where: Path | None = None) -> dict:
    raw, n = storage.raw_fastq_bytes(fastq_dir, filenames)
    if filenames and n == 0:  # names didn't match; size the whole folder rather than report nothing
        raw, n = storage.raw_fastq_bytes(fastq_dir)
    return storage.report(raw, n, trim, where)


def check_storage(user, dataset_id=None, fastq_folder="", trim_adapters=True) -> dict:
    """Free space where this person's results go, plus the RNA-seq estimate for a dataset if given."""
    _require_self(user)
    if not dataset_id:
        out = storage.report()
    else:
        ds = get_dataset(user, dataset_id)
        if ds.kind != "data":
            raise HubError(f"{dataset_id} is registered as code, not data.")
        fastq_dir = resolve_asset(ds, fastq_folder)
        if not fastq_dir.is_dir():
            raise HubError("fastq_folder must be a folder inside the dataset.")
        out = _input_storage(fastq_dir, None, bool(trim_adapters))
        out["input"] = {"dataset": dataset_id, "folder": fastq_folder or "(whole dataset)"}
    out["next"] = storage.tool_text_rule()
    return out


def _choices(pathway_geneset, pathway_genesets, extra_quantification, run_mode):
    """The person's answers to analysis_options, normalised and checked."""
    sets = [pathway_geneset] if pathway_geneset else []
    sets += list(pathway_genesets or [])
    sets = list(dict.fromkeys(str(g).strip() for g in sets if str(g).strip()))
    if len(sets) > MAX_GENESETS:
        raise HubError(f"Pick at most {MAX_GENESETS} pathway gene sets for one run.")
    extra = extra_quantification or []
    extra = [extra] if isinstance(extra, str) else list(extra)
    quants = list(dict.fromkeys(str(q).strip().lower() for q in extra if str(q).strip()))
    quants = [q for q in quants if q not in ("none", "skip")]
    bad = [q for q in quants if q not in QUANTIFIERS]
    if bad:
        raise HubError(f"extra_quantification can be rsem and/or kallisto (or none), not: {', '.join(bad)}.")
    mode = (run_mode or "all").strip().lower()
    if mode not in RUN_MODES:
        raise HubError("run_mode must be 'all' (every step in order) or 'step' (one step at a time).")
    return sets, quants, mode


def _needs_new_runner(sets, quants, mode) -> bool:
    return len(sets) > 1 or bool(quants) or mode != "all"


def plan_analysis(user, dataset_id, project_name, species, fastq_folder="", design=None, design_file="",
                  paired_end=True, genome_version="", trim_adapters=True, adapter="Illumina Universal TruSeq RNA",
                  min_length=20, count_by="gene_name", compare=None, pathway_geneset=None, pathway_genesets=None,
                  extra_quantification=None, run_mode="all") -> dict:
    _require_self(user)
    sets, quants, mode = _choices(pathway_geneset, pathway_genesets, extra_quantification, run_mode)
    if _needs_new_runner(sets, quants, mode) and _cli("catalog").get("spec_version", 1) < 2:
        raise HubError("Several gene sets, RSEM/Kallisto and one-step-at-a-time runs need a CodeSpringApp update that "
                       "isn't installed yet. Plan with one gene set, no extra quantification and run_mode 'all', "
                       "or ask BSR to update the shared install.")
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
    if len(sets) == 1:
        spec["gsea"] = {"geneset": sets[0]}  # understood by every CodeSpringApp version
    elif sets:
        spec["gsea"] = {"genesets": sets}
    if quants:
        spec["quantifiers"] = quants
    if mode != "all":
        spec["run_mode"] = mode
    plan = _cli("check", spec=spec)["plan"]
    plan.pop("codespringapp", None)
    plan.pop("codespringlab", None)
    plan["input"] = {"dataset": dataset_id, "folder": fastq_folder or "(whole dataset)"}
    results_dir = Path(plan.get("results_dir") or storage.results_root())
    plan["storage"] = _input_storage(fastq_dir, _design_filenames(design, design_path), bool(trim_adapters),
                                     results_dir)

    plan_id = f"plan-{secrets.token_hex(8)}"
    record = {"plan_id": plan_id, "user": user, "created": time.time(), "dataset_id": dataset_id, "lab_id": ds.lab_id,
              "personal": bool(ds.owner), "spec": spec, "plan": plan}
    record["digest"] = hashlib.sha256(json.dumps([spec, plan], sort_keys=True).encode()).hexdigest()
    path = _runs_dir() / f"{plan_id}.json"
    path.write_text(json.dumps(record, indent=2))
    os.chmod(path, 0o600)
    verdict = plan["storage"].get("verdict", "")
    return {
        "plan_id": plan_id,
        "expires_in_minutes": config.RUN_PLAN_TTL_SECONDS // 60,
        "preview": agent.rnaseq_preview(plan, dataset_id, species, compare, sets, quants, mode),
        "final_prompt": agent.FINAL_PROMPT,
        "plan": plan,
        "next": ("Show `preview` exactly as given (BSR's Agent's intro, the steps table and how to reach BSR), "
                 "with no extra summary before or after it, then the final_prompt. "
                 + ("Space is short: before the preview, say so in one sentence and suggest freeing space or "
                    "asking BSR. " if not verdict.startswith("enough") else "")
                 + "Only after they pick 'Start pipeline' (or clearly say yes), call start_analysis with this "
                 "plan_id and user_confirmed=true. " + agent.STYLE),
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


def start_analysis(user, plan_id, user_confirmed=False) -> dict:
    _require_self(user)
    if user_confirmed is not True:
        raise HubError("Not started. Show the plan to the user and ask them to confirm; then call again with user_confirmed=true.")
    path, record = _load_plan(user, plan_id)
    runs = _run_records()
    active = [r for r in runs if r.get("state") not in FINISHED]
    for r in active:  # refresh before counting
        try:
            r["state"] = _cli("status", r["run_id"]).get("state", r.get("state"))
        except HubError:
            pass
    active = [r for r in runs if r.get("state") not in FINISHED]
    if len(active) >= config.MAX_ACTIVE_RUNS:
        raise HubError(f"You already have {len(active)} analyses running from the Hub (the limit is "
                       f"{config.MAX_ACTIVE_RUNS}). Wait for one to finish, or ask BSR.")
    est = (record["plan"].get("storage") or {}).get("rnaseq_estimate") or {}
    storage.require_room(est.get("typical_bytes", 0),
                         Path(record["plan"].get("results_dir") or storage.results_root()))
    result = _cli("start", spec=record["spec"])
    record["used"] = time.time()
    path.write_text(json.dumps(record, indent=2))
    runs.append({"run_id": result["project_id"], "plan_id": plan_id, "dataset_id": record["dataset_id"],
                 "lab_id": record["lab_id"], "personal": record["personal"], "state": "queued",
                 "started": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    _save_runs(runs)
    step_mode = record["spec"].get("run_mode") == "step"
    return {
        "run_id": result["project_id"],
        "results_folder": result["results_dir"],
        "steps": [s["step"] for s in record["plan"]["steps"]],
        "say": (f"Your pipeline has started. Run ID `{result['project_id']}`. "
                + ("I'll stop after each step, tell you how it went and wait for you before going on."
                   if step_mode else "I'll update you as each step finishes.")),
        "next": ("Show `say` as is. " + _follow_rule() + " " + agent.STYLE),
    }


def _follow_rule() -> str:
    return ("To follow the run, call analysis_status with wait_seconds=45: it returns as soon as a step changes. "
            "Each time a step finishes (not on every call), report its quality and show `say`. The small example "
            "dataset takes 15-20 minutes. For real datasets (hours), after the first step finishes stop following "
            "it: give the open_results progress link and say they can ask 'how's my run?' any time. When it is "
            "complete, give the open_results link to the Results Explorer.")


def _own_run(run_id) -> dict:
    if not RUN_ID.match(run_id or ""):
        raise HubError("That isn't a run id from start_analysis.")
    for r in _run_records():
        if r["run_id"] == run_id:
            return r
    raise NotFound("No analysis with that id was started by you from the Hub. list_analyses shows yours.")


def _status_file(results_dir) -> Path | None:
    if not results_dir:
        return None
    path = Path(results_dir) / "log" / "codespring_hub_run.json"
    return path if path.is_file() else None


def _step_states(path: Path) -> tuple | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data.get("state"), tuple((s.get("step"), s.get("state")) for s in data.get("steps", []))


def analysis_status(user, run_id, wait_seconds=0) -> dict:
    _require_self(user)
    _own_run(run_id)
    st = _cli("status", run_id)
    wait = max(0, min(int(wait_seconds or 0), MAX_WAIT_SECONDS))
    path = _status_file(st.get("results_dir")) if wait and st.get("state") not in FINISHED | {"paused"} else None
    if path:
        # Wait here, reading the run's own status file, until a step or the run changes (or time is up), so
        # following a run costs one short answer per step instead of many polls.
        before, deadline = _step_states(path), time.time() + wait
        while time.time() < deadline:
            time.sleep(min(5, max(0.0, deadline - time.time())))
            if _step_states(path) != before:
                break
        st = _cli("status", run_id)
    runs = _run_records()
    for r in runs:
        if r["run_id"] == run_id:
            r["state"] = st.get("state")
    _save_runs(runs)
    steps = [{k: s.get(k) for k in ("step", "state", "what", "started", "finished", "message", "samples_done",
                                    "samples_total") if s.get(k) is not None} for s in st.get("steps", [])]
    state = st.get("state")
    quality, just_finished = _quality(run_id, st.get("results_dir"), steps)
    table = agent.status_table(steps, quality)
    out = {
        "run_id": run_id, "state": state, "summary": agent.progress_line(steps), "error": st.get("error"),
        "steps": steps, "table": table, "quality": quality, "just_finished": just_finished,
        "results_folder": st.get("results_dir"), "updated": st.get("updated"),
    }
    report = _report_rule(just_finished)
    if state == "paused":
        finished = agent.STEP_WORDS.get(st.get("paused_after"), st.get("paused_after") or "That step")
        out["say"] = f"{finished[:1].upper() + finished[1:]} is finished. {out['summary']}\n\n{table}"
        out["next"] = (report + "Show `say`, then ask with a single-choice selection: 'Continue to the next step' "
                       "(then call continue_analysis), 'Stop here for now' or 'Ask for clarification'. " + agent.STYLE)
    elif state in ("failed", "stopped"):
        out["say"] = agent.with_support(f"The run stopped with a problem. {out['summary']}\n\n{table}")
        out["next"] = ("Explain the error in plain words, offer resume_analysis if it looks temporary, and show "
                       "`say` (it has the BSR support block). " + report + agent.STYLE)
    elif state == "complete":
        out["say"] = agent.with_support(f"All done. Your results are ready.\n\n{table}")
        out["next"] = (report + "Then show `say`, call open_results and give its link. " + agent.STYLE)
    else:
        out["say"] = f"{out['summary']}\n\n{table}"
        out["next"] = (report + "Show `say` each time a step finishes (not on every call). " + _follow_rule())
    return out


def _quality(run_id: str, results_dir, steps: list[dict]) -> tuple[dict, list[str]]:
    """QC for every finished step, and which of them haven't been reported to the person yet."""
    quality = {}
    for s in steps:
        if s.get("state") == "complete" and (q := qc.step_qc(results_dir, s.get("step"))):
            quality[s["step"]] = q
    records = _run_records()
    just_finished = []
    for r in records:
        if r["run_id"] == run_id:
            seen = set(r.get("qc_reported") or [])
            just_finished = [step for step in quality if step not in seen]
            if just_finished:
                r["qc_reported"] = sorted(seen | set(just_finished))
                _save_runs(records)
            break
    return quality, just_finished


def _report_rule(just_finished: list[str]) -> str:
    if not just_finished:
        return ""
    names = ", ".join(agent.STEP_WORDS.get(s, s) for s in just_finished)
    return (f"Before anything else, report on what just finished ({names}) from `quality`: in two to four plain "
            "sentences per step, give the key numbers (range across samples), say whether they look good for bulk "
            "RNA-seq, and name any sample that stands out from the others or appears in `flags`. FastQC modules in "
            "usual_for_rnaseq are normal for RNA-seq; say so briefly rather than as a problem. For DESeq2 and GSEA "
            "name a few top genes or gene sets. These are rules of thumb, not a verdict: if something looks off, "
            "suggest checking with BSR. ")


def list_analyses(user) -> list[dict]:
    _require_self(user)
    return [{k: r.get(k) for k in ("run_id", "dataset_id", "state", "started")} for r in _run_records()]


def continue_analysis(user, run_id) -> dict:
    """Start the next step of a run paused in one-step-at-a-time mode."""
    _require_self(user)
    _own_run(run_id)
    res = _cli("continue", run_id)
    nxt = agent.STEP_WORDS.get(res.get("next_step"), res.get("next_step") or "the next step")
    runs = _run_records()
    for r in runs:
        if r["run_id"] == run_id:
            r["state"] = "queued"
    _save_runs(runs)
    return {"run_id": run_id, "continued": True, "next_step": res.get("next_step"),
            "say": f"Going on with {nxt}. I'll let you know when it's finished.", "next": _follow_rule()}


def resume_analysis(user, run_id) -> dict:
    _require_self(user)
    _own_run(run_id)
    _cli("resume", run_id)
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
