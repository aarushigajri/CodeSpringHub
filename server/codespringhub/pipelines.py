"""Which CodeSpringApp pipelines each person may use through the Hub.

BSR decides per pipeline, in knowledge/bsr/shared/pipelines.yaml:

  public    every lab using the Hub (bulk RNA-seq, ATAC, ChIP: running standard tools)
  internal  BSR staff only, plus any labs listed for it (single-cell, variant calling: analyses
            that need BSR's judgement); everyone else is told BSR runs it for them

Unlisted pipelines get `default` (internal), so a new pipeline is never public by accident. This
controls what Claude offers and what it may start from a chat; the CodeSpringApp install itself is
a separate matter (its files are readable on the cluster).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import config, knowledge
from .errors import Forbidden, HubError
from .permissions import is_bsr, is_member

ACCESS = ("public", "internal")


def settings_path() -> Path:
    return config.KNOWLEDGE_DIR / "bsr" / knowledge.SHARED_PART / "pipelines.yaml"


def load() -> dict:
    """{'default': 'internal', 'pipelines': {id: {'access': 'public'|'internal', 'labs': [...]}}}"""
    try:
        raw = yaml.safe_load(settings_path().read_text()) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    default = raw.get("default") if raw.get("default") in ACCESS else "internal"
    out = {}
    for pid, val in (raw.get("pipelines") or {}).items():
        if isinstance(val, str):
            val = {"access": val}
        if not isinstance(val, dict):
            continue
        access = val.get("access") if val.get("access") in ACCESS else "internal"  # a typo fails closed
        labs = [str(x) for x in (val.get("labs") or []) if str(x).strip()]
        out[str(pid)] = {"access": access, "labs": labs}
    return {"default": default, "pipelines": out}


def rule_for(pipeline_id: str, settings: dict | None = None) -> dict:
    s = settings or load()
    return s["pipelines"].get(pipeline_id, {"access": s["default"], "labs": []})


def can_use(user: str, pipeline_id: str, settings: dict | None = None) -> bool:
    if is_bsr(user):
        return True
    rule = rule_for(pipeline_id, settings)
    if rule["access"] == "public":
        return True
    labs = knowledge.load_labs()
    return any(lab_id in labs and is_member(user, labs[lab_id]) for lab_id in rule["labs"])


def annotate(user: str, pipelines: list[dict]) -> list[dict]:
    """CodeSpringApp's catalog entries, plus access, whether this person may use them, and what
    Claude should do when they may not."""
    s = load()
    out = []
    for p in pipelines:
        rule = rule_for(p.get("id", ""), s)
        ok = can_use(user, p.get("id", ""), s)
        row = dict(p) | {"access": rule["access"], "you_can_use": ok}
        if not ok:
            row["runnable_from_hub"] = False
            row["how"] = ("BSR runs this analysis for your lab: offer request_bsr_review with the dataset and the "
                          "question. Don't walk them through running it themselves.")
        out.append(row)
    return out


def require(user: str, pipeline_id: str):
    if not can_use(user, pipeline_id):
        raise Forbidden(f"BSR runs the '{pipeline_id}' pipeline for labs rather than it being started from a chat. "
                        "Use request_bsr_review to ask BSR, with the dataset and your question.")


def for_portal(user: str, catalog: list[dict] | None) -> dict:
    s = load()
    ids = [p["id"] for p in catalog or []] or sorted(s["pipelines"])
    names = {p["id"]: p.get("name", p["id"]) for p in catalog or []}
    rows = [{"id": pid, "name": names.get(pid, pid), **rule_for(pid, s), "listed": pid in s["pipelines"],
             "you_can_use": can_use(user, pid, s)} for pid in ids]
    return {"default": s["default"], "pipelines": rows, "can_edit": is_bsr(user)}


def save(user: str, pipeline_id: str, access: str, labs: list[str] | None = None):
    """BSR staff only. Keeps the file's comments and other entries."""
    from . import editing  # editing imports a lot; only needed when saving
    if not is_bsr(user):
        raise Forbidden("Only BSR staff can change who may use a pipeline.")
    if access not in ACCESS:
        raise HubError("Access must be 'public' (every lab) or 'internal' (BSR, plus the labs you list).")
    if not editing.SLUG.match(pipeline_id or ""):
        raise HubError("Unknown pipeline id.")
    known = knowledge.load_labs()
    labs = [x for x in (labs or []) if x]
    bad = [x for x in labs if x not in known or known[x].is_core]
    if bad:
        raise HubError(f"Not labs on the Hub: {', '.join(bad)}.")
    path = settings_path()
    text = path.read_text() if path.exists() else "default: internal\npipelines:\n"
    head = text.split("pipelines:", 1)[0]
    s = load()
    s["pipelines"][pipeline_id] = {"access": access, "labs": labs if access == "internal" else []}
    body = {pid: (r["access"] if not r["labs"] else {"access": r["access"], "labs": r["labs"]})
            for pid, r in sorted(s["pipelines"].items())}
    editing._write_atomic(path, head + yaml.safe_dump({"pipelines": body}, sort_keys=False))
    editing._commit(user, [path], f"pipeline {pipeline_id}: {access}" + (f" (+ {', '.join(labs)})" if labs else ""))
    editing._audit(user, "pipeline_access", {"pipeline": pipeline_id, "access": access, "labs": labs}, lab_id="bsr")
