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

GREETING = (f"Hello! This is {NAME}. I'm here to help you set up and run the Bioinformatics Shared Resource's "
            "RNA-seq pipeline on your data.\n\nBefore we start, I'm going to ask a few questions to clarify the "
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
    "how": ("End with this as a single-choice selection if you can (otherwise as two plain choices). 'Start "
            "pipeline' is the person's confirmation: then call start_analysis with user_confirmed=true. Anything "
            "else: answer or re-plan, and ask again."),
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
    "contacts": [{"name": "James Rouse", "email": "rouse@cshl.edu"},
                 {"name": "Aarushi Gajri", "email": "gajri@cshl.edu"},
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

def _geneset_words(name: str) -> str:
    return name.replace("MSigDB_", "").replace("_", " ")


def _steps_words(steps: list[dict], genesets: list[str]) -> str:
    words = []
    for s in steps:
        w = STEP_WORDS.get(s.get("step"), s.get("step", ""))
        if s.get("step") == "GSEA" and genesets:
            w = f"pathways ({', '.join(_geneset_words(g) for g in genesets)})"
        words.append(w)
    text = ", ".join(words)
    return text[:1].upper() + text[1:]


def _samples_words(plan: dict, species: str, compare: dict | None) -> str:
    n = len(plan.get("samples") or [])
    groups = (plan.get("groups") or {}).get((compare or {}).get("column", ""), {}) or \
        next(iter((plan.get("groups") or {}).values()), {})
    split = ", ".join(f"{count} {level}" for level, count in groups.items())
    return f"{n} {species} samples" + (f" ({split})" if split else "")


def _storage_words(storage: dict | None, quantifiers: list[str]) -> str:
    if not storage:
        return "Not checked"
    est = storage.get("rnaseq_estimate") or {}
    text = f"{storage.get('free', '?')} free. This run needs about {est.get('typical', '?')} ({est.get('range', '?')})"
    if quantifiers:
        text += ", plus a little for " + " and ".join({"rsem": "RSEM", "kallisto": "Kallisto"}[q] for q in quantifiers)
    verdict = storage.get("verdict", "")
    if verdict.startswith("not enough"):
        text = "Not enough space. " + text
    elif verdict.startswith("tight"):
        text = "Tight. " + text
    return text


def rnaseq_preview(plan: dict, dataset_id: str, species: str, compare: dict | None, genesets: list[str],
                   quantifiers: list[str], run_mode: str, email: dict | None = None) -> str:
    rows = [("Data", f"{_samples_words(plan, species, compare)}, `{dataset_id}`")]
    if compare:
        rows.append(("Comparison", f"{compare.get('comparison')} vs {compare.get('reference')}"))
    rows += [
        ("Reference", plan.get("reference", "")),
        ("Steps", _steps_words(plan.get("steps") or [], genesets)),
        ("Run mode", ("One step at a time" if run_mode == "step" else "All at once") + ", on the cluster under your account"),
        ("Storage", _storage_words(plan.get("storage"), quantifiers)),
        ("Output", f"`{plan.get('results_dir', '')}`"),
    ]
    if isinstance(email, dict):
        every = email.get("every_hours") or 0
        rows.insert(-1, ("Email updates", f"To {email.get('email_to', 'your CSHL address')}, when each step finishes"
                                          + (f", plus a summary every {every} h" if every else "")))
    table = "\n".join(["### Your RNA-seq plan", "", "| | |", "|---|---|"] + [f"| **{k}** | {v} |" for k, v in rows])
    return f"{table}\n\n{support()['block']}"


# ---- while it runs --------------------------------------------------------------------------------

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
