"""The Hub's read operations (and Claude's two "write" actions: suggest and request review),
shared by the MCP server and the web portal.

Every function takes the acting `user` explicitly and does its own permission checks,
so the same code is safe whether it is called from stdio, the portal, or (later) a
multi-user HTTPS service. Personal items are visible only to their owner and the lab's PIs;
private items only to their owner.
"""

import os
import secrets
import subprocess
from pathlib import Path

import yaml

from . import acl, config, inspect_data, knowledge, store
from .errors import Forbidden, HubError, NotFound
from .permissions import (can_access_lab, can_edit_lab, can_see, is_bsr, is_member, is_pi, require_lab,
                          resolve_asset)

USAGE_NOTICE = ("CodeSpringHub shares usage logs with BSR: which Hub tools are used, when, and their inputs "
                "(not your conversation with Claude). Requests about personal items are logged without details.")


def accessible_labs(user):
    return {lid: lab for lid, lab in knowledge.load_labs().items() if can_access_lab(user, lab)}


def visible_documents(user, lab) -> list[knowledge.Document]:
    return [d for d in knowledge.load_documents(lab).values() if can_see(user, lab, d.owner, d.private)]


def visible_datasets(user, lab) -> list[knowledge.Dataset]:
    return [d for d in knowledge.load_datasets(lab).values() if can_see(user, lab, d.owner, d.private)]


def list_labs(user) -> list[dict]:
    out = []
    for lab in accessible_labs(user).values():
        out.append({
            "lab_id": lab.id, "name": lab.name, "description": lab.description,
            "projects": [vars(p) for p in lab.projects],
            "datasets": len(visible_datasets(user, lab)),
            "documents": len(visible_documents(user, lab)),
            "can_edit": can_edit_lab(user, lab),
            "role": "pi" if is_pi(user, lab) else "member" if is_member(user, lab) else "bsr",
        })
    return out


def document_index(user, lab) -> list[dict]:
    return [
        {"id": d.id, "title": d.title, "category": d.category, "rel_path": d.rel_path,
         "scope": knowledge.scope_of(d), "owner": d.owner}
        for d in visible_documents(user, lab)
    ]


def lab_context(user, lab_id) -> dict:
    lab = require_lab(user, knowledge.load_labs(), lab_id)
    ctx = lab.dir / "context.md"
    return {
        "context": ctx.read_text() if ctx.exists() else "",
        "documents": [d for d in document_index(user, lab) if d["category"] != "context"],
    }


def search(user, query, lab_id=None) -> list[dict]:
    labs = accessible_labs(user)
    if lab_id:
        labs = {lab_id: require_lab(user, labs, lab_id)}
    docs = [d for lab in labs.values() for d in visible_documents(user, lab)]
    return store.search(docs, query)


def _split_id(hub_id: str, prefix: str):
    if not hub_id.startswith(prefix) or "/" not in hub_id:
        raise HubError(f"IDs look like '{prefix}<lab>/<name>'.")
    return hub_id[len(prefix):].split("/", 1)[0]


def get_document(user, doc_id) -> knowledge.Document:
    lab = require_lab(user, knowledge.load_labs(), _split_id(doc_id, "doc:"))
    doc = knowledge.load_documents(lab).get(doc_id)
    if doc is None or not can_see(user, lab, doc.owner, doc.private):
        raise NotFound(f"No document '{doc_id}'. Use get_lab_context or search_knowledge.")
    return doc


def public_dataset(ds: knowledge.Dataset) -> dict:
    """Dataset metadata safe to show the model (everything except the path)."""
    return {
        "id": ds.id, "title": ds.title, "kind": ds.kind, "project": ds.project, "assay": ds.assay,
        "description": ds.description, "sensitivity": ds.sensitivity,
        "scope": knowledge.scope_of(ds), "owner": ds.owner,
        "registered_by": ds.registered_by, "registered_on": ds.registered_on,
    }


def get_dataset(user, dataset_id) -> knowledge.Dataset:
    lab = require_lab(user, knowledge.load_labs(), _split_id(dataset_id, "ds:"))
    ds = knowledge.load_datasets(lab).get(dataset_id)
    if ds is None or not can_see(user, lab, ds.owner, ds.private):
        raise NotFound(f"No registered dataset '{dataset_id}'. Use list_datasets to find them.")
    return ds


def list_datasets(user, lab_id, project=None) -> list[dict]:
    lab = require_lab(user, knowledge.load_labs(), lab_id)
    return [public_dataset(d) for d in visible_datasets(user, lab) if project in (None, d.project)]


def _git_info(root: Path) -> dict | None:
    if not (root / ".git").exists():
        return None

    def git(*args):
        r = subprocess.run(["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "last_commit": git("log", "-1", "--format=%h %ad %s", "--date=short"),
            "remote": git("remote", "get-url", "origin")}


def describe_dataset(user, dataset_id) -> dict:
    ds = get_dataset(user, dataset_id)
    root = resolve_asset(ds)
    assets = inspect_data.list_assets(root)
    out = public_dataset(ds) | {"assets": assets, "assets_truncated": len(assets) >= config.MAX_LISTED_FILES}
    if ds.kind == "code" and root.is_dir():
        out["git"] = _git_info(root)
    return out


def inspect_asset(user, dataset_id, asset) -> dict:
    ds = get_dataset(user, dataset_id)
    path = resolve_asset(ds, asset)
    if path.is_dir():
        raise HubError("That is a folder; pass a file name from describe_dataset.")
    return inspect_data.summarize(path, sensitive=ds.sensitivity == "sensitive")


def read_code_file(user, resource_id, path) -> str:
    ds = get_dataset(user, resource_id)
    if ds.kind != "code":
        raise Forbidden("This is a data resource; use inspect_asset for a summary. Only code resources can be read in full.")
    target = resolve_asset(ds, path)
    if target.is_dir():
        raise HubError("That is a folder; pass a file name from describe_dataset.")
    return inspect_data.read_source(target)


def query_table(user, dataset_id, asset, sql) -> dict:
    ds = get_dataset(user, dataset_id)
    if ds.sensitivity == "sensitive":
        raise Forbidden("This dataset is marked sensitive; row-level queries are disabled. Ask BSR.")
    return inspect_data.query_table(resolve_asset(ds, asset), sql)


def list_workflows(assay=None) -> list[dict]:
    wfs = knowledge.load_workflows()
    if assay:
        wfs = [w for w in wfs if assay.lower() in [a.lower() for a in w.get("assays", [])]]
    return wfs


def suggest_edit(user, lab_id, suggestion, doc_id=None) -> str:
    """Write a suggestion file next to what it is about: in the lab's _suggestions/, or in the
    owner's own _suggestions/ for a personal or private document. People apply it; Claude can't."""
    lab = require_lab(user, knowledge.load_labs(), lab_id)
    owner, private = None, False
    if doc_id:
        doc = get_document(user, doc_id)
        if doc.lab_id != lab_id:
            raise HubError("That document belongs to another lab.")
        owner, private = doc.owner, doc.private
        if owner and owner != user:
            raise Forbidden("Only the owner can add suggestions to a personal document.")
    area = knowledge.private_dir(lab, user) if private else knowledge.personal_dir(lab, user) if owner else lab.dir
    folder = area / "_suggestions"
    if not folder.exists():
        folder.mkdir()
        if owner and not acl.inherits(area):  # on GPFS the inherited ACL already fits; chmod would replace it
            os.chmod(folder, 0o2700)
    sid = f"{store.now()[:19].replace(':', '').replace('-', '')}-{user}-{secrets.token_hex(2)}"
    record = {"id": sid, "ts": store.now(), "user": user, "lab_id": lab_id, "doc_id": doc_id,
              "suggestion": suggestion, "status": "pending"}
    path = folder / f"{sid}.yaml"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if owner else 0o660)
    with os.fdopen(fd, "w") as fh:
        yaml.safe_dump(record, fh, sort_keys=False, allow_unicode=True)
    return sid


def request_review(user, lab_id, summary, dataset_ids=None) -> int:
    require_lab(user, knowledge.load_labs(), lab_id)
    for ds_id in dataset_ids or []:
        get_dataset(user, ds_id)
    return store.add_review(user, lab_id, summary, dataset_ids or [])
