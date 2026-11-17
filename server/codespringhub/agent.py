"""How BSR's Agent talks: persona, tone, the questions before a run, the plan preview and progress lines.

Claude follows wording that arrives in tool results more reliably than general instructions, so the
Hub builds the standard pieces here (greeting, questions, preview, progress, support contacts) and
the tools hand them to Claude ready to show. Everyone gets the same questions in the same order and
the same preview layout, whichever Claude client they use.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import config

NAME = "BSR's Agent"

# The voice: a bench colleague. Warm, plain, first-person plural ("we", "let's"), like a friendly
# bioinformatician at the next desk. Every reply that used the Hub ends with SIGNATURE, so people
# can always tell when they're talking to BSR's configured tools rather than plain Claude.
SIGNATURE = "— BSR's Agent · Bioinformatics Shared Resource, CSHL · CodeSpring"

STYLE = (
    f"Speak as {NAME}, the Bioinformatics Shared Resource's assistant, in the voice of a bench colleague: warm, "
    "plain and brief, first-person plural where it fits ('let's look', 'we'd hope to see'), like a friendly "
    "bioinformatician at the next desk. Never use emojis, check marks, ticks, crosses or other decorative symbols; "
    "say progress in words. No filler such as 'Great question', 'Certainly', 'I'd be happy to help' or summaries "
    "of what you just said. Short paragraphs, plain numbers, one question at a time. End the reply with the "
    f"sign-off line on its own line: {SIGNATURE}"
)

HELLO = (f"Hi, this is {NAME}! Let's take a look at your data. I'll help you run the Bioinformatics Shared "
         "Resource's RNA-seq pipeline on it.")

ABOUT = (
    "Two pieces work together here. CodeSpringHub is how I see your lab's registered data and BSR's guidance. "
    "CodeSpringApp is BSR's pipeline app on the cluster: it runs the analysis under your own account, and you can "
    "watch it live and browse the results there. Once we've checked your data and storage, I'll give you a "
    "link to each."
)

GREETING = (HELLO + "\n\n" + ABOUT + "\n\nFirst, a few quick questions. Nothing runs until you've seen the full "
            "plan and said go.")

def link_say(what: str, link: str, minutes: int, tunnel_command: str, needs_tunnel: bool) -> str:
    """The one standard way a Hub link is shown: a clickable link first. The tunnel command leads only
    when this Hub's SSH login doesn't forward the port, because then the link can't load without it."""
    text = f"**[Open {what}]({link})** (works once, within {minutes} minutes; ask me for a new one any time)."
    if needs_tunnel:
        return ("First run this in a terminal on your laptop (CSHL Wi-Fi or VPN) and leave it open:\n\n"
                f"`{tunnel_command}`\n\nThen: " + text)
    return text + f"\n\nIf the page doesn't load, run `{tunnel_command}` on your laptop and ask me for a new link."


def links_say(items: list[dict], user: str, host: str, minutes: int) -> str:
    """Several Hub links at once (CodeSpringHub and CodeSpringApp), the same way link_say shows one: clickable
    links, led by a single tunnel command for every port that needs one. Items are open_portal / open_app
    results; one without a link (e.g. the person's own CodeSpringApp is open) adds its `say` instead."""
    linked = [i for i in items if i.get("link")]
    lines = [f"- **[Open {i['what']}]({i['link']})**" for i in linked]
    text = "\n".join(lines)
    if linked:
        text += f"\n\nEach link works once, within {minutes} minutes; ask me for new ones any time."
    ports = [i["port"] for i in linked if i.get("needs_tunnel")]
    if ports:
        forwards = " ".join(f"-L {p}:localhost:{p}" for p in ports)
        text = ("First run this in a terminal on your laptop (CSHL Wi-Fi or VPN) and leave it open:\n\n"
                f"`ssh -N {forwards} {user}@{host}`\n\nThen:\n\n" + text)
    extra = [i["say"] for i in items if not i.get("link") and i.get("say")]
    return "\n\n".join([t for t in [text] + extra if t])


def storage_say(out: dict) -> str:
    """check_storage in plain words: free space, raw data, what the pipeline will write, the verdict."""
    where = out.get("results_location") or "your results folder"
    est, raw = out.get("rnaseq_estimate"), out.get("raw_input")
    if not est or not raw:
        return f"You have {out.get('free', 'an unknown amount of')} free in {where}."
    verdict = out.get("verdict", "")
    text = (f"Your raw data is {raw['size']} ({raw['fastq_files']} FASTQ files). The pipeline should write between "
            f"{est['range'].replace(' to ', ' and ')}, typically {est['typical']}, mostly STAR's BAM files. You have "
            f"{out.get('free')} free in {where}, ")
    if verdict == "enough space":
        return text + "so there's enough space."
    return (f"Storage is {verdict}. " + text + f"and BSR recommends at least {est['recommended_free']} free. "
            "Free up some space or ask BSR before starting.")


RNASEQ_SUMMARY = (
    "BSR's RNA-seq pipeline trims adapter sequences from your reads, checks read quality, aligns the reads to the "
    "genome, counts reads per gene, then compares your groups to find differentially expressed genes and looks for "
    "pathways that shift between them. You can browse everything afterwards in CodeSpringApp's Results Explorer."
)

HOW_TO_ASK = (
    "Show the greeting, then ask the project_name question first, on its own and in plain words (not a "
    "selection tool), and wait for the name the person types. Never suggest, invent or change a project name "
    "yourself, including when one is taken: ask them for another. Once they've named it, ask the remaining "
    "questions together as a selection if you have a multiple-choice question tool (in Claude Code, "
    "AskUserQuestion with every remaining question in one call; use multiSelect where multi_select is true and "
    "put each option's description in the option). Otherwise ask them as one short numbered list with the "
    "default marked. Don't add questions of your own; the reference genome and comparison come from the dataset "
    "and appear in the preview, where the person can ask to change them."
)

PROJECT_NAME_RULE = (
    "Every CodeSpringHub pipeline run needs a project name the person types in themselves (it names the results "
    "folder and the project in CodeSpringApp): ask for it before any other choice such as pathway gene sets, and "
    "never make one up."
)


def project_name_question(pipeline_words: str = "this analysis") -> dict:
    """The typed question every Hub pipeline asks first: the project name, in the person's own words."""
    return {"id": "project_name", "header": "Project", "type": "text", "multi_select": False,
            "question": (f"What would you like to call {pipeline_words}? Type a project name: letters, numbers, "
                         "'.', '_' or '-', up to 64 characters, starting with a letter or number."),
            "options": [], "default": None,
            "note": ("Ask in plain words and wait for their typed answer; pass it unchanged as project_name. "
                     "If it's taken or not allowed, plan_analysis says so: ask them for another.")}


FINAL_PROMPT = {
    "question": "Ready to go?",
    "header": "Next",
    "options": [
        {"label": "Start pipeline", "description": "Submit this plan to the cluster now."},
        {"label": "Ask for clarification", "description": "Ask a question or change something before it starts."},
    ],
    "how": ("Ask this in words at the very end of the preview message, as two plain choices: \"Ready to go? Reply "
            "'Start pipeline', or ask for clarification if you'd like to change anything.\" Do NOT use a "
            "question/selection tool here, and make no tool call after the preview: some apps (Claude Desktop) "
            "fold text written before a tool call into a short summary, and the person never sees the plan. "
            "If your app needs a closing or wrap-up tool call, make it before the preview, so the preview is "
            "your final message. 'Start pipeline' (or a clear yes) is the person's confirmation: then call "
            "start_analysis with user_confirmed=true. Anything else: answer or re-plan, and ask again."),
}

# Shown as options; the rest of the catalog's gene sets can be typed under "Other".
FEATURED_GENESETS = {
    "MSigDB_Hallmark_2020": "50 well-defined biological states and processes. A good place to start.",
    "GO_Biological_Process_2025": "Broad and detailed. Expect a long list of results.",
    "Reactome_Pathways_2024": "Curated signaling and metabolic pathways.",
    "KEGG_2021_Human": "Classic metabolic and signaling maps. Mouse genes are mapped to human.",
}

STEP_WORDS = {
    "Cutadapt": "trimming", "FastQC": "quality check", "STAR": "alignment", "featureCounts": "gene counts",
    "DESeq2": "differential expression", "GSEA": "pathways", "RSEM": "RSEM", "Kallisto": "Kallisto",
}

_DEFAULT_SUPPORT = {
    "team": "Bioinformatics Shared Resource (BSR)",
    "contacts": [{"name": "Aarushi Gajri", "email": "gajri@cshl.edu"},
                 {"name": "James Rouse", "email": "rouse@cshl.edu"},
                 {"name": "Rad Utama", "email": "utama@cshl.edu"}],
}


# ---- support contacts ----------------------------------------------------------------------------

def support() -> dict:
    """BSR's support contacts (knowledge/support_contacts.yaml), with the block to show."""
    path = Path(config.KNOWLEDGE_DIR) / "support_contacts.yaml"
    data = _DEFAULT_SUPPORT
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
        if loaded.get("contacts"):
            data = {"team": loaded.get("team") or _DEFAULT_SUPPORT["team"], "contacts": loaded["contacts"]}
    except (OSError, yaml.YAMLError):
        pass
    contacts = [c for c in data["contacts"] if isinstance(c, dict) and c.get("name") and c.get("email")]
    lines = ["> **Need help? Contact BSR support**"] + [f"> **{c['name']}** {c['email']}" for c in contacts]
    return {"team": data["team"], "contacts": contacts, "block": "  \n".join(lines)}


def with_support(message: str) -> str:
    return f"{message}\n\n{support()['block']}"


# ---- before a run: the questions ------------------------------------------------------------------

def rnaseq_questions(genesets: list[str] | None = None) -> list[dict]:
    available = list(genesets or FEATURED_GENESETS)
    featured = [g for g in FEATURED_GENESETS if g in available] or available[:4]
    return [
        project_name_question("this RNA-seq project"),
        {"id": "pipeline", "header": "Pipeline", "multi_select": False,
         "question": "Which pipeline would you like to use?",
         "options": [
             {"label": "CodeSpringApp (Recommended)", "value": "codespringapp",
              "description": "BSR's standard RNA-seq pipeline. I can start it for you from here."},
             {"label": "nf-core/rnaseq", "value": "nf-core",
              "description": "Community pipeline, not BSR's standard yet. I'll pass your request to the BSR team."}],
         "default": "codespringapp",
         "if_nf_core": ("Say plainly that nf-core/rnaseq isn't BSR's standard and can't be started from here yet, "
                        "and offer request_bsr_review with their dataset and question.")},
        {"id": "pathway_genesets", "header": "Pathways", "multi_select": True,
         "question": "Which pathway gene sets should the pathway analysis use? Pick as many as you like.",
         "options": [{"label": gs.replace("_", " "), "value": gs, "description": FEATURED_GENESETS.get(gs, "")}
                     for gs in featured],
         "default": [featured[0]] if featured else [],
         "other_allowed": [g for g in available if g not in featured],
         "note": "Up to 6. Others from other_allowed can be typed under 'Other'; pass exact names to plan_analysis."},
        {"id": "extra_quantification", "header": "Extras", "multi_select": False,
         "question": "Would you like any extra quantification after the gene counts?",
         "options": [
             {"label": "None, skip this", "value": [],
              "description": "Gene counts from featureCounts only. This is what most projects need."},
             {"label": "RSEM", "value": ["rsem"],
              "description": "Gene and transcript estimates from the alignments. Adds some time."},
             {"label": "Kallisto", "value": ["kallisto"],
              "description": "Fast transcript-level estimates straight from the reads."},
             {"label": "Both RSEM and Kallisto", "value": ["rsem", "kallisto"],
              "description": "Runs both, one after the other, at the end."}],
         "default": []},
        {"id": "run_mode", "header": "Run mode", "multi_select": False,
         "question": "How would you like it to run?",
         "options": [
             {"label": "All at once (Recommended)", "value": "all",
              "description": "Every step runs in order. I'll let you know when it's finished."},
             {"label": "One step at a time", "value": "step",
              "description": "I'll stop after each step, tell you how it went, and wait for you before going on."}],
         "default": "all"},
        {"id": "email_updates", "header": "Emails", "multi_select": False,
         "question": "Would you like email updates while it runs?",
         "options": [
             {"label": "No emails (Recommended)", "value": False,
              "description": "Ask me how it's going any time."},
             {"label": "Email me as each step finishes", "value": True,
              "description": "Sent only to your own CSHL address, also if a step fails or the run stops."}],
         "default": False,
         "note": "Pass as email_updates to plan_analysis; email_every_hours (1-24) adds a regular summary if they ask."},
    ]


# ---- the plan preview -----------------------------------------------------------------------------

def _reference_words(reference: str, species: str) -> str:
    """'Mouse GRCm39 / GENCODE M39' -> 'GRCm39'."""
    words = (reference or "").split(" / ")[0].split()
    if len(words) > 1 and words[0].lower() == (species or "").lower():
        words = words[1:]
    return " ".join(words)


def _join_words(items: list[str], last: str = "and") -> str:
    if len(items) <= 2:
        return f" {last} ".join(items)
    return ", ".join(items[:-1]) + f", {last} {items[-1]}"


def _steps_table(steps: list[dict], genesets: list[str]) -> str:
    rows = ["| | Step | Tool | What it does |", "|---|---|---|---|"]
    for i, s in enumerate(steps, 1):
        tool = s.get("step", "")
        name = STEP_WORDS.get(tool, tool)
        what = s.get("what") or ""
        if tool == "GSEA" and genesets:
            what = "Pathway enrichment with " + ", ".join(g.replace("MSigDB_", "").replace("_", " ") for g in genesets)
        rows.append(f"| {i} | {name[:1].upper() + name[1:]} | {tool} | {what} |")
    return "\n".join(rows)


def _genesets_words(genesets: list[str]) -> str:
    return ", ".join(g.replace("_", " ") for g in genesets) if genesets else "None (no pathway analysis)"


def _storage_cell(plan: dict) -> str:
    """One line: what the run needs, what is free, and the verdict."""
    st = plan.get("storage") or {}
    if not st:
        return "Couldn't be checked for this run"
    est, verdict = st.get("rnaseq_estimate") or {}, st.get("verdict", "")
    text = f"Needs about {est.get('typical', '?')} ({est.get('range', '?')}); {st.get('free', '?')} free."
    if verdict.startswith("not enough"):
        return text + f" Not enough: free at least {est.get('recommended_free', '?')} or ask BSR first."
    if verdict.startswith("tight"):
        return text + f" Tight: have at least {est.get('recommended_free', '?')} free before starting."
    return text + " Enough space."


def rnaseq_preview(plan: dict, dataset_id: str, species: str, compare: dict | None, genesets: list[str],
                   quantifiers: list[str], run_mode: str, email: dict | None = None) -> str:
    """The plan, once: a one-line summary, every setting in one table, the steps, then who to ask.

    It names BSR's Agent at the top because some apps fold the greeting (written before a question
    tool) away, and this preview is the message the person is sure to see."""
    n = len(plan.get("samples") or [])
    what = f"{n} {species} samples"
    if compare:
        what = f"{compare.get('comparison')} vs {compare.get('reference')} across {what}"
    ref = _reference_words(plan.get("reference", ""), species)
    mode = "one step at a time, pausing for you after each" if run_mode == "step" else "all steps at once"
    summary = (f"We'll compare {what}" + (f" on {ref}" if ref else "") + f", running {mode} under your account.")
    names = {"rsem": "RSEM", "kallisto": "Kallisto"}
    if isinstance(email, dict):
        every = email.get("every_hours") or 0
        emails = "As each step finishes" + (f", plus a summary every {every} h" if every else "")
        emails += f" (to {email['email_to']})" if email.get("email_to") else " (to your CSHL address)"
    else:
        emails = "None"
    rows = [
        ("Pipeline", "CodeSpringApp, BSR's standard RNA-seq pipeline"),
        ("Data", f"`{dataset_id}`, {n} samples" + (f", {plan['input']['folder']}" if (plan.get('input') or {}).get('folder') else "")),
    ]
    if compare:
        rows.append(("Comparison", f"{compare.get('comparison')} vs {compare.get('reference')} (reference), "
                                   f"column '{compare.get('column')}'"))
    rows += [
        ("Reference genome", plan.get("reference") or "?"),
        ("Pathway gene sets", _genesets_words(genesets)),
        ("Extra quantification", _join_words([names.get(q, q) for q in quantifiers]) if quantifiers else "None"),
        ("Run mode", "One step at a time" if run_mode == "step" else "All steps at once"),
        ("Email updates", emails),
        ("Results folder", f"`{plan.get('results_dir', '')}`"),
        ("Storage", _storage_cell(plan)),
    ]
    settings = "| Setting | Choice |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)
    help_line = ("Questions at any point? Reach BSR: "
                 + _join_words([f"{c['name']} ({c['email']})" for c in support()["contacts"]], "or") + ".")
    return (f"**{NAME}: your RNA-seq plan is ready.** {summary}\n\n{settings}\n\n**Steps**\n\n"
            f"{_steps_table(plan.get('steps') or [], genesets)}\n\n{help_line}")


# ---- while it runs --------------------------------------------------------------------------------

STATUS_WORDS = {"complete": "Done", "running": "Running", "failed": "Stopped with a problem"}


def status_table(steps: list[dict], qc: dict[str, dict]) -> str:
    """The plan's steps with where each one is and, once done, its key quality numbers."""
    rows = ["| | Step | Tool | Status | Quality |", "|---|---|---|---|---|"]
    for i, s in enumerate(steps, 1):
        tool = s.get("step", "")
        name = STEP_WORDS.get(tool, tool)
        state = STATUS_WORDS.get(s.get("state"), "Waiting")
        if s.get("state") == "running" and s.get("samples_total"):
            state += f" ({s.get('samples_done', 0)} of {s['samples_total']} samples)"
        headline = (qc.get(tool) or {}).get("headline", "") if s.get("state") == "complete" else ""
        rows.append(f"| {i} | {name[:1].upper() + name[1:]} | {tool} | {state} | {headline} |")
    return "\n".join(rows)


def progress_line(steps: list[dict]) -> str:
    """One short line in words, e.g. 'Done: trimming. Running: alignment (2 of 6 samples). Still to come: ...'"""
    done, running, failed, waiting = [], [], [], []
    for s in steps:
        word = STEP_WORDS.get(s.get("step"), s.get("step", ""))
        state = s.get("state")
        if state == "complete":
            done.append(word)
        elif state == "running":
            if s.get("samples_total"):
                word += f" ({s.get('samples_done', 0)} of {s['samples_total']} samples)"
            running.append(word)
        elif state == "failed":
            failed.append(word)
        elif state == "skipped":
            continue
        elif state == "cancelled":
            failed.append(word + " (cancelled)")
        else:
            waiting.append(word)
    parts = []
    for label, items in (("Done", done), ("Running", running), ("Stopped with a problem", failed),
                         ("Still to come", waiting)):
        if items:
            parts.append(f"{label}: {', '.join(items)}.")
    return " ".join(parts)


# ---- the standard QC report ------------------------------------------------------------------------

def _title(step: str) -> str:
    word = STEP_WORDS.get(step, step)
    return word[:1].upper() + word[1:]


def step_line(step: str, q: dict, index: int, total: int) -> str:
    """One line when a step finishes, e.g. '**Step 3 of 6 done: Alignment (STAR).** Uniquely mapped
    81.4-82.2% (BSR guide 70% or more). Looks good.' The full table comes with the final report."""
    from . import qc as qcmod
    rows = qcmod.checks(step, q, q.get("species", ""))
    key = next((r for r in rows if r["guide"]), rows[0] if rows else None)
    line = f"**Step {index} of {total} done: {_title(step)} ({step}).**"
    if key:
        guide = key["guide"] if key["guide"] and key["guide"] != key["result"] else ""
        line += f" {key['check']}: {key['result']}" + (f" (BSR guide {guide})" if guide else "") + "."
    line += f" {qcmod.verdict(rows, q.get('flags'))}."
    if q.get("flags"):
        line += " " + "; ".join(q["flags"]) + "."
    return line


def qc_report(steps: list[dict], quality: dict[str, dict]) -> str:
    """Every finished step's standard checks in one table, the same rows and guides for every run,
    then anything worth a look. Empty if no step has finished yet."""
    from . import qc as qcmod
    lines = ["| Step | Check | Result | BSR guide | Status |", "|---|---|---|---|---|"]
    flags, notes = [], []
    for s in steps:
        tool = s.get("step", "")
        q = quality.get(tool)
        if s.get("state") != "complete" or not q:
            continue
        for i, r in enumerate(qcmod.checks(tool, q, q.get("species", ""))):
            lines.append(f"| {_title(tool) if i == 0 else ''} | {r['check']} | {r['result']} | {r['guide']} | "
                         f"{r['status']} |")
        flags += q.get("flags") or []
        if q.get("usual_for_rnaseq"):
            notes.append("FastQC warnings on " + _join_words(q["usual_for_rnaseq"]).lower()
                         + " are normal for RNA-seq libraries and aren't counted.")
    if len(lines) == 2:
        return ""
    out = "\n".join(lines)
    if flags:
        out += "\n\n**Worth a look:** " + "; ".join(flags) + ". These are rules of thumb; BSR can help you judge."
    if notes:
        out += "\n\n" + " ".join(notes)
    return out


# ---- human connection: offers to bring in a person at BSR ----------------------------------------

CHECK_INS = {
    "periodic": "Want a second pair of human eyes? I can email the BSR team with a short summary of what we've "
                "done, or ask them for a time to talk.",
    "plan": "If you'd like someone at BSR to look over this plan with you first, I can email them a short summary.",
    "complete": "If you'd like help reading these results, the BSR team is happy to meet: I can email them a "
                "summary of this run and ask for a time.",
    "failed": "If you'd rather have a person look at what went wrong, I can email the BSR team a summary of this run.",
    "lab_setup": "Your lab isn't on CodeSpringHub yet. I can email the BSR team to set it up for you, so I can "
                 "work with your lab's own data and context.",
}


def check_in(reason: str) -> dict:
    """An offer to bring in a person at BSR, for Claude to add to its reply in one line."""
    return {
        "reason": reason,
        "say": CHECK_INS.get(reason, CHECK_INS["periodic"]),
        "how": ("Add `say` as one short line at the end of your reply, just above the sign-off; don't make it a "
                "question tool or a separate message. If they say yes, call email_bsr (purpose 'question', "
                "'lab_setup' or 'meeting'), with a short summary of this conversation if they want one."),
    }
