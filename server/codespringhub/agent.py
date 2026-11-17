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

STYLE = (
    f"Speak as {NAME}, the Bioinformatics Shared Resource's assistant. Be warm, plain and brief, like a helpful "
    "person at the core facility. Never use emojis, check marks, ticks, crosses or other decorative symbols; say "
    "progress in words. No filler such as 'Great question', 'Certainly', 'I'd be happy to help' or summaries of "
    "what you just said. Short paragraphs, plain numbers, one question at a time."
)

HELLO = (f"Hello! This is {NAME}. I'm here to help you set up and run the Bioinformatics Shared Resource's "
         "RNA-seq pipeline on your data.")
GREETING = (HELLO + "\n\nBefore we start, I'm going to ask a few questions to clarify the "
            "analysis. It only takes a moment, and I'll show you the full plan before anything runs.")

RNASEQ_SUMMARY = (
    "BSR's RNA-seq pipeline trims adapter sequences from your reads, checks read quality, aligns the reads to the "
    "genome, counts reads per gene, then compares your groups to find differentially expressed genes and looks for "
    "pathways that shift between them. You can browse everything afterwards in CodeSpringApp's Results Explorer."
)

HOW_TO_ASK = (
    "Show the greeting, then ask all of these questions together as a selection if you have a multiple-choice "
    "question tool (in Claude Code, AskUserQuestion with every question in one call; use multiSelect where "
    "multi_select is true and put each option's description in the option). Otherwise ask them as one short "
    "numbered list with the default marked. Don't add questions of your own; the reference genome, comparison and "
    "project name come from the dataset and appear in the preview, where the person can ask to change them."
)

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


def _answers_line(genesets: list[str], quantifiers: list[str], run_mode: str) -> str:
    """'Your answers: CodeSpringApp pipeline, MSigDB Hallmark 2020 gene sets, no extra quantification, ...'"""
    sets = _join_words([g.replace("_", " ") for g in genesets]) + (" gene sets" if genesets else "")
    names = {"rsem": "RSEM", "kallisto": "Kallisto"}
    extra = (_join_words([names.get(q, q) for q in quantifiers]) + " quantification") if quantifiers \
        else "no extra quantification"
    mode = "one step at a time" if run_mode == "step" else "all steps at once"
    parts = ["CodeSpringApp pipeline"] + ([sets] if genesets else ["no pathway analysis"]) + [extra, mode]
    return "**Your answers:** " + ", ".join(parts) + "."


def _storage_paragraph(plan: dict) -> str:
    """Where results go, free space, raw data size, the expected range and the verdict, in words."""
    st = plan.get("storage") or {}
    if not st:
        return "**Storage:** I couldn't check storage for this run."
    where = st.get("results_location", "")
    if plan.get("project_name") and where.rstrip("/").endswith("/" + plan["project_name"]):
        where = where.rstrip("/").rsplit("/", 1)[0]
    raw, est, verdict = st.get("raw_input") or {}, st.get("rnaseq_estimate") or {}, st.get("verdict", "")
    text = (f"**Storage:** Results go to `{where}`, which has {st.get('free', '?')} free "
            f"({st.get('total', '?')} filesystem, {st.get('used_percent', '?')}% used).")
    if raw:
        text += f" The raw data is {raw.get('fastq_files', '?')} FASTQ files, {raw.get('size', '?')} in total."
    if est:
        text += (f" This pipeline usually writes {est.get('range', '?')}, about {est.get('typical', '?')} in a "
                 "typical run. Most of that is STAR's BAM files.")
    if verdict.startswith("not enough"):
        text += (f" There isn't enough space: please free some up (at least {est.get('recommended_free', '?')}) "
                 "or ask BSR before starting.")
    elif verdict.startswith("tight"):
        text += f" Space is tight, so have at least {est.get('recommended_free', '?')} free before starting."
    else:
        text += " There is enough space."
    return text


def rnaseq_preview(plan: dict, dataset_id: str, species: str, compare: dict | None, genesets: list[str],
                   quantifiers: list[str], run_mode: str) -> str:
    """BSR's Agent introduces the plan, shows the steps it will run, then who to reach for help."""
    what = f"{len(plan.get('samples') or [])} {species} samples"
    if compare:
        what = f"{compare.get('comparison')} vs {compare.get('reference')} for {what}"
    ref = _reference_words(plan.get("reference", ""), species)
    if ref:
        what += f" on {ref}"
    mode = "one step at a time, pausing for you after each" if run_mode == "step" else "all at once"
    storage = plan.get("storage") or {}
    est = storage.get("rnaseq_estimate") or {}
    verdict = storage.get("verdict", "")
    if not storage:
        space = "I couldn't check storage for this run."
    elif verdict.startswith("not enough"):
        space = (f"Heads up: it needs about {est.get('typical', '?')} and only {storage.get('free', '?')} is free, "
                 "so we'll need to free some space first.")
    else:
        space = f"It needs about {est.get('typical', '?')} and you have {storage.get('free', '?')} free"
        space += ", which is a little tight." if verdict.startswith("tight") else ", so you're all set."
    head = f"{HELLO}\n\n{_answers_line(genesets, quantifiers, run_mode)}\n\n{_storage_paragraph(plan)}"
    intro = (f"I'm BSR's Agent, and your RNA-seq plan is ready! We'll compare {what}, running {mode} "
             f"on the cluster under your account. {space} Your results will go to "
             f"`{plan.get('results_dir', '')}`.")
    help_line = ("If you need any help, reach out to us: "
                 + _join_words([f"{c['name']} ({c['email']})" for c in support()["contacts"]], "or") + ".")
    return f"{head}\n\n{intro}\n\nHere are the steps I'll run:\n\n{_steps_table(plan.get('steps') or [], genesets)}\n\n{help_line}"


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
        else:
            waiting.append(word)
    parts = []
    for label, items in (("Done", done), ("Running", running), ("Stopped with a problem", failed),
                         ("Still to come", waiting)):
        if items:
            parts.append(f"{label}: {', '.join(items)}.")
    return " ".join(parts)
