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
  * continuing and resuming (which submit more work) and cancelling also need the person's go-ahead;
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

from . import agent, config, identity, pipelines, portal_launch, qc, storage, store
from .errors import Forbidden, HubError, NotFound
from .hub import get_dataset
from .permissions import resolve_asset

PLAN_ID = re.compile(r"^plan-[a-f0-9]{16}$")
RUN_ID = re.compile(r"^rna/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
FINISHED = {"complete", "failed", "stopped", "cancelled"}
ACTIVE = {"queued", "running", "cancelling"}  # "paused" waits for the person and holds no cluster resources
QC_STEPS = ("Cutadapt", "FastQC", "STAR", "featureCounts", "Kallisto")
SKIPPABLE = ("RSEM", "Kallisto", "GSEA")
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
        usage = next((e for e in errors if str(e).startswith("usage: codespring-run")), None)
        if usage and f" {command} " not in f" {usage.replace('|', ' ')} ":
            raise HubError(f"The shared CodeSpringApp is older than the Hub and has no '{command}' command yet, so "
                           "this can't be done from the chat. Your run and its results aren't affected. BSR: update "
                           f"the shared install ({config.CODESPRING_SUITE / 'CodeSpringApp'}) to the latest main.")
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


def _needs_new_runner(sets, quants, mode, email=False) -> bool:
    return len(sets) > 1 or bool(quants) or mode != "all" or email


def plan_analysis(user, dataset_id, project_name, species, fastq_folder="", design=None, design_file="",
                  paired_end=True, genome_version="", trim_adapters=True, adapter="Illumina Universal TruSeq RNA",
                  min_length=20, count_by="gene_name", compare=None, pathway_geneset=None, pathway_genesets=None,
                  extra_quantification=None, run_mode="all", email_updates=False, email_every_hours=0) -> dict:
    _require_self(user)
    pipelines.require(user, "rnaseq")  # BSR decides which pipelines labs may start themselves
    sets, quants, mode = _choices(pathway_geneset, pathway_genesets, extra_quantification, run_mode)
    if _needs_new_runner(sets, quants, mode, bool(email_updates)) and _cli("catalog").get("spec_version", 1) < 2:
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
    if email_updates:
        # Only to the person's own CSHL address (CodeSpringApp decides it; no address can be given).
        spec["notify"] = {"email": True, "every_hours": email_every_hours or 0}
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
        "preview": agent.rnaseq_preview(plan, dataset_id, species, compare, sets, quants, mode,
                                        email=plan.get("notify") if email_updates else None),
        "final_prompt": agent.FINAL_PROMPT,
        "plan": plan,
        "next": ("Make the preview your final message this turn: any other tool calls (including a closing "
                 "or wrap-up tool) come first, and none after it, or the app may fold the preview into a "
                 "short summary. Your message is `preview` exactly as given, word for word, with nothing added "
                 "before it, nothing left out and no extra summary (it already has the settings, storage, steps and "
                 "BSR's contacts), then final_prompt's question in plain words (no selection tool). "
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


def _check_run_limit(exclude=None) -> list[dict]:
    """Refresh states, then refuse if starting/continuing/resuming would exceed the active-run limit.
    Paused runs don't count: they wait for the person and hold no cluster resources."""
    runs = _run_records()
    for r in [r for r in runs if r.get("state") not in FINISHED]:
        try:
            r["state"] = _cli("status", r["run_id"]).get("state", r.get("state"))
        except HubError:
            pass
    _save_runs(runs)
    active = [r for r in runs if r.get("state") in ACTIVE and r["run_id"] != exclude]
    if len(active) >= config.MAX_ACTIVE_RUNS:
        raise HubError(f"You already have {len(active)} analyses running from the Hub (the limit is "
                       f"{config.MAX_ACTIVE_RUNS}). Wait for one to finish, or ask BSR.")
    return runs


def _remember_state(run_id, state):
    runs = _run_records()
    for r in runs:
        if r["run_id"] == run_id:
            r["state"] = state
    _save_runs(runs)


def start_analysis(user, plan_id, user_confirmed=False) -> dict:
    _require_self(user)
    if user_confirmed is not True:
        raise HubError("Not started. Show the plan to the user and ask them to confirm; then call again with user_confirmed=true.")
    path, record = _load_plan(user, plan_id)
    pipelines.require(user, record["spec"].get("pipeline", "rnaseq"))  # BSR may have changed it since planning
    runs = _check_run_limit()
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
    say = (f"Your pipeline has started (run `{result['project_id']}`). "
           + ("I'll stop after each step, tell you how it went and wait for you before going on."
              if step_mode else "I'll note each step as it finishes and give you the full quality report at the end."))
    out = {"run_id": result["project_id"], "results_folder": result["results_dir"],
           "steps": [s["step"] for s in record["plan"]["steps"]]}
    try:
        live = open_results(user, result["project_id"], view="progress")
    except HubError:
        live = None
    if live and live.get("link"):
        out["live_progress"] = live
        say += "\n\nYou can watch it live in CodeSpringApp. " + live["say"]
    out["say"] = say
    out["next"] = "Show `say` as is. " + _follow_rule() + " " + agent.STYLE
    return out


def _follow_rule() -> str:
    return ("To follow the run, call analysis_status with wait_seconds=45 again and again: it returns as soon as a "
            "step changes, and its `say` tells you what to show (when `say` is empty, show nothing and call again). "
            "Short runs (the example dataset takes 15-20 minutes): follow to the end. Longer runs (real datasets "
            "take hours): stop after the first step finishes, point to the live CodeSpringApp link and say they can "
            "ask 'how's my run?' any time. You can't message the person between turns: if they chose email "
            "updates, those come by email. cancel_analysis stops the run at any point.")


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
    _remember_state(run_id, st.get("state"))
    steps = []
    for s in st.get("steps", []):
        row = {k: s.get(k) for k in ("step", "state", "what", "started", "finished", "message", "samples_done",
                                     "samples_total") if s.get(k) is not None}
        jobs = s.get("jobs") or {}
        if any(jobs.get(k) for k in ("queued", "running", "done", "failed")):
            row["jobs"] = {k: jobs.get(k, 0) for k in ("done", "running", "queued", "failed")}
            if jobs.get("failed_samples"):
                row["jobs"]["failed_samples"] = jobs["failed_samples"]
        steps.append(row)
    state = st.get("state")
    quality, just_finished = _quality(run_id, st.get("results_dir"), steps)
    shown = [s for s in steps if s.get("state") != "skipped"]
    position = {s.get("step"): i for i, s in enumerate(shown, 1)}
    lines = [agent.step_line(step, quality[step], position.get(step, 0), len(shown)) for step in just_finished]
    table = agent.status_table(steps, quality)
    report = agent.qc_report(steps, quality)
    out = {
        "run_id": run_id, "state": state, "summary": agent.progress_line(steps), "error": st.get("error"),
        "steps": steps, "just_finished": just_finished,
        "flags": [f for step in just_finished for f in (quality[step].get("flags") or [])],
        "next_actions": st.get("next_actions", []), "results_folder": st.get("results_dir"),
        "updated": st.get("updated"),
    }
    following = bool(wait)
    final = ("This is your final message of the turn, so it stays visible: make any other tool calls first, then "
             "show `say` exactly as given, adding only what the next sentence asks for. ")
    if state == "paused":
        finished = agent.STEP_WORDS.get(st.get("paused_after"), st.get("paused_after") or "That step")
        out["paused_after"], out["next_step"] = st.get("paused_after"), st.get("next_step")
        out["say"] = f"{finished[:1].upper() + finished[1:]} is finished. {out['summary']}\n\n{table}" + \
            (f"\n\n**Quality so far**\n\n{report}" if report else "")
        out["next"] = (final + "Then ask in plain words: continue to the next step (then continue_analysis with "
                       "user_confirmed=true; RSEM or Kallisto can be added, or RSEM/Kallisto/GSEA skipped, if they "
                       "ask), stop here for now, or cancel the run (cancel_analysis). " + agent.STYLE)
    elif state in ("cancelled", "cancelling"):
        out["say"] = ("The run is stopping." if state == "cancelling" else "The run is cancelled.") + \
            f" Finished steps and their results are kept; it can be resumed later.\n\n{table}"
        out["next"] = "Show `say`. Offer resume_analysis only if they want to carry on. " + agent.STYLE
    elif state in ("failed", "stopped"):
        out["say"] = agent.with_support(f"The run stopped with a problem. {out['summary']}\n\n{table}"
                                        + (f"\n\n**Quality so far**\n\n{report}" if report else ""))
        out["next"] = (final + "Before it, explain the error in one or two plain sentences and offer "
                       "resume_analysis if it looks temporary. " + agent.STYLE)
        out["bsr_check_in"] = agent.check_in("failed")
    elif state == "complete":
        out["say"] = agent.with_support(
            f"**All {len(shown)} steps are done.** Your results are in `{st.get('results_dir')}`.\n\n"
            f"**Quality report**\n\n{report or 'No quality numbers were found in the results folder.'}")
        out["next"] = (final + "First call analysis_report and open_results (view 'results'); then, after `say`, "
                       "add the report's download link and the Results Explorer link with its how_to_open (and the "
                       "tunnel command if needs_tunnel). If either tool fails, say so in one sentence. "
                       + agent.STYLE)
        out["bsr_check_in"] = agent.check_in("complete")
    elif lines:
        out["say"] = "\n\n".join(lines)
        out["next"] = "Show `say` as is, adding nothing. " + _follow_rule()
    elif following:
        out["say"] = ""
        out["next"] = "Nothing new: show nothing and call analysis_status again with wait_seconds=45."
    else:  # the person asked how it's going
        out["say"] = f"{out['summary']}\n\n{table}" + (f"\n\n**Quality so far**\n\n{report}" if report else "")
        out["next"] = "Show `say` as is. " + agent.STYLE
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


def list_analyses(user) -> list[dict]:
    _require_self(user)
    return [{k: r.get(k) for k in ("run_id", "dataset_id", "state", "started")} for r in _run_records()]


def analysis_qc(user, run_id, step=None) -> dict:
    """Per-sample QC so far, straight from each tool's own report."""
    _require_self(user)
    _own_run(run_id)
    if step and step not in QC_STEPS:
        raise HubError(f"step must be one of: {', '.join(QC_STEPS)}.")
    qc = _cli("qc", run_id, *([step] if step else []))
    return {"run_id": run_id, "samples": qc.get("samples", []), "summary": qc.get("summary"), "note": qc.get("note")}


def continue_analysis(user, run_id, add_quantification=None, skip_steps=None, run_mode=None,
                      user_confirmed=False) -> dict:
    """Start the next step of a paused run (one step at a time), or add RSEM/Kallisto to a finished one."""
    _require_self(user)
    _own_run(run_id)
    if user_confirmed is not True:
        raise HubError("Not continued. Tell the user what will run next (and anything added or skipped), ask them "
                       "to confirm, then call again with user_confirmed=true.")
    _, add, mode = _choices(None, None, add_quantification, run_mode or "all")
    skip = [str(x).strip() for x in (skip_steps or []) if str(x).strip()]
    bad = [x for x in skip if x not in SKIPPABLE]
    if bad:
        raise HubError(f"Only {', '.join(SKIPPABLE)} can be skipped; not {', '.join(bad)}.")
    _check_run_limit(exclude=run_id)
    args = [run_id]
    if add:
        args += ["--add", ",".join(add)]
    if skip:
        args += ["--skip", ",".join(skip)]
    if run_mode:
        args += ["--mode", mode]
    res = _cli("continue", *args)
    _remember_state(run_id, "queued")
    nxt = agent.STEP_WORDS.get(res.get("next_step"), res.get("next_step") or "the next step")
    return {"run_id": run_id, "continued": True, "next_step": res.get("next_step"), "steps": res.get("steps"),
            "say": f"Going on with {nxt}. I'll let you know when it's finished.", "next": _follow_rule()}


def cancel_analysis(user, run_id, user_confirmed=False) -> dict:
    """Stop the run's controller and its queued/running Slurm jobs. Finished steps are kept."""
    _require_self(user)
    _own_run(run_id)
    if user_confirmed is not True:
        raise HubError("Not cancelled. Ask the user to confirm they want to stop this analysis (finished steps are "
                       "kept), then call again with user_confirmed=true.")
    res = _cli("cancel", run_id, timeout=300)
    _remember_state(run_id, res.get("state", "cancelled"))
    return {"run_id": run_id, "state": res.get("state", "cancelled"), "controller": res.get("controller"),
            "jobs": res.get("jobs"), "note": res.get("note"),
            "say": "Stopped. Finished steps and their results are kept, and the run can be resumed later."}


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
        how = (f"Your own CodeSpringApp is already running (port {running[0]}). In it, choose RNA-seq, "
               f"then project '{run_id.split('/', 1)[1]}', then the Results Explorer tab. Or close it "
               "and ask again for a direct link.")
        return {"link": None, "how_to_open": how, "say": how}
    what = {"results": "this run in CodeSpringApp", "progress": "the live progress page in CodeSpringApp",
            "logs": "this run's logs in CodeSpringApp"}[view]
    return _app_link(user, running[0] if running else _start_app(), f"&project={run_id}&view={view}", what)


def open_app(user) -> dict:
    """A one-time link to CodeSpringApp itself (no particular run), starting it if needed."""
    _require_self(user)
    running = _running_app()
    if running and not running[1]:
        how = (f"Your own CodeSpringApp is already running on the cluster (port {running[0]}); open it the way you "
               "usually do, or close it and ask again for a direct link.")
        return {"link": None, "how_to_open": how, "say": how}
    return _app_link(user, running[0] if running else _start_app(), "", "CodeSpringApp")


def _app_link(user, port, query, what) -> dict:
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
    forwarded = port == codespringapp_port(user) and ports_forwarded()
    if forwarded:
        # The Hub was started by the Connect page's Claude Desktop setup, whose SSH login also forwards
        # this port to the laptop, so the link opens directly.
        how = (f"Click the link within {minutes} minutes (it works once). If the page doesn't load, run the tunnel "
               "command in a terminal on your laptop (CSHL Wi-Fi or VPN), leave it open, and ask for a new link.")
    else:
        how = ("First run the tunnel command in a terminal on your laptop (CSHL Wi-Fi or VPN) and leave it open; "
               f"then click the link within {minutes} minutes. The link works once; ask again for a new one.")
    link = f"http://localhost:{port}/?ticket={ticket}{query}"
    tunnel = f"ssh -N -L {port}:localhost:{port} {user}@{host}"
    return {
        "say": agent.link_say(what, link, minutes, tunnel, not forwarded),
        "link": link,
        "needs_tunnel": not forwarded,
        "tunnel_command": tunnel,
        "how_to_open": how,
    }


SHELLS = {"bash", "sh", "dash", "zsh", "ksh", "tcsh", "csh", "env"}


def ports_forwarded() -> bool:
    """True when this Hub was started straight from an SSH login (the Connect page's Claude Desktop
    setup, which forwards the portal and CodeSpringApp ports to the laptop). When something else
    started it, such as Claude Code on the cluster, nothing is forwarded and a link needs a tunnel."""
    pid = os.getppid()
    for _ in range(4):
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            if comm.startswith("sshd"):
                return True
            if comm not in SHELLS:
                return False
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def _laptop_host() -> str:
    from .editing import _laptop_hostname  # one definition of the laptop-facing host name
    return _laptop_hostname()
