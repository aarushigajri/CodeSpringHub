"""The Hub's read operations (and Claude's two "write" actions: suggest and request review),
shared by the MCP server and the web portal.

Every function takes the acting `user` explicitly and does its own permission checks,
so the same code is safe whether it is called from stdio, the portal, or (later) a
multi-user HTTPS service. Personal items are visible only to their owner and the lab's PIs;
private items only to their owner. A core facility's shared section is visible to everyone using
the Hub; its internal section only to the core's staff.
"""

import os
import secrets
import subprocess
from pathlib import Path

import yaml

from . import acl, config, inspect_data, knowledge, store
from .errors import Forbidden, HubError, NotFound
from .permissions import (can_access_lab, can_edit_lab, can_see, can_view_lab, is_bsr, is_member, is_pi,
                          require_lab, require_visible_lab, resolve_asset)

USAGE_NOTICE = ("CodeSpringHub shares usage logs with BSR: which Hub tools are used, when, and their inputs "
                "(not your conversation with Claude). Requests about personal items are logged without details.")


def accessible_labs(user):
    """Labs (and cores) the user has full access to: their own labs, BSR's pilot labs, their core."""
    return {lid: lab for lid, lab in knowledge.load_labs().items() if can_access_lab(user, lab)}


def visible_labs(user):
    """Everything the user may read something in: accessible labs plus every core's shared section."""
    return {lid: lab for lid, lab in knowledge.load_labs().items() if can_view_lab(user, lab)}


def visible_documents(user, lab) -> list[knowledge.Document]:
    return [d for d in knowledge.load_documents(lab).values() if can_see(user, lab, d.owner, d.private, d.shared)]


def visible_datasets(user, lab) -> list[knowledge.Dataset]:
    return [d for d in knowledge.load_datasets(lab).values() if can_see(user, lab, d.owner, d.private, d.shared)]


def _role(user, lab) -> str:
    if lab.is_core:
        if is_member(user, lab):
            return "lead" if is_pi(user, lab) else "staff"
        return "bsr" if can_access_lab(user, lab) else "user"
    return "pi" if is_pi(user, lab) else "member" if is_member(user, lab) else "bsr"


def list_labs(user) -> list[dict]:
    """Labs the user works in, then core facilities (BSR first). For a core, staff see its shared
    and internal sections; everyone else sees the shared section."""
    out = []
    labs = sorted(visible_labs(user).values(), key=lambda lab: (lab.is_core, lab.id != "bsr", lab.name.lower()))
    for lab in labs:
        row = {
            "lab_id": lab.id, "kind": lab.kind, "name": lab.name, "description": lab.description,
            "projects": [vars(p) for p in lab.projects],
            "datasets": len(visible_datasets(user, lab)),
            "documents": len(visible_documents(user, lab)),
            "can_edit": can_edit_lab(user, lab),
            "role": _role(user, lab),
        }
        if lab.is_core:
            row["sees"] = "shared and internal" if can_access_lab(user, lab) else "shared"
            row["on_hub"] = lab.onboarded
            row["website"] = lab.website
            row["contacts"] = lab.contacts
        out.append(row)
    return out


def document_index(user, lab) -> list[dict]:
    return [
        {"id": d.id, "title": d.title, "category": d.category, "rel_path": d.rel_path,
         "scope": knowledge.scope_of(d), "owner": d.owner}
        for d in visible_documents(user, lab)
    ]


def _read(path) -> str:
    try:
        return path.read_text() if path.exists() else ""
    except PermissionError:
        return ""


def lab_context(user, lab_id) -> dict:
    lab = require_visible_lab(user, knowledge.load_labs(), lab_id)
    out = {"documents": [d for d in document_index(user, lab) if d["category"] != "context"]}
    if not lab.is_core:
        return {"context": _read(lab.dir / "context.md"), **out}
    out = {"kind": "core", "context": _read(lab.dir / knowledge.SHARED_PART / "context.md"), **out,
           "website": lab.website, "contacts": lab.contacts, "on_hub": lab.onboarded}
    if can_access_lab(user, lab):
        out["internal_context"] = _read(lab.dir / knowledge.INTERNAL_PART / "context.md")
        out["note"] = ("You are on this core's staff: `context` and documents with scope 'shared' are seen by "
                       "everyone using the Hub; `internal_context` and scope 'lab' are for the core's staff only. "
                       "Never repeat internal content to people outside the core.")
    return out


def search(user, query, lab_id=None) -> list[dict]:
    labs = visible_labs(user)
    if lab_id:
        labs = {lab_id: require_visible_lab(user, labs, lab_id)}
    docs = [d for lab in labs.values() for d in visible_documents(user, lab)]
    return store.search(docs, query)


def _split_id(hub_id: str, prefix: str):
    if not hub_id.startswith(prefix) or "/" not in hub_id:
        raise HubError(f"IDs look like '{prefix}<lab>/<name>'.")
    return hub_id[len(prefix):].split("/", 1)[0]


def get_document(user, doc_id) -> knowledge.Document:
    lab = require_visible_lab(user, knowledge.load_labs(), _split_id(doc_id, "doc:"))
    doc = knowledge.load_documents(lab).get(doc_id)
    if doc is None or not can_see(user, lab, doc.owner, doc.private, doc.shared):
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
    lab = require_visible_lab(user, knowledge.load_labs(), _split_id(dataset_id, "ds:"))
    ds = knowledge.load_datasets(lab).get(dataset_id)
    if ds is None or not can_see(user, lab, ds.owner, ds.private, ds.shared):
        raise NotFound(f"No registered dataset '{dataset_id}'. Use list_datasets to find them.")
    return ds


def list_datasets(user, lab_id, project=None) -> list[dict]:
    lab = require_visible_lab(user, knowledge.load_labs(), lab_id)
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
    owner's own _suggestions/ for a personal or private document. People apply it; Claude can't.
    A core's suggestions go to its staff, in its internal section."""
    labs = knowledge.load_labs()
    core = labs.get(lab_id)
    if core is not None and core.is_core and not can_access_lab(user, core) and can_view_lab(user, core):
        who = ", ".join(f"{c['name']} ({c['email']})" for c in core.contacts if c.get("email")) or "the core's staff"
        raise Forbidden(f"Only {core.name} staff can change its pages. To suggest a change, contact {who}.")
    lab = require_lab(user, labs, lab_id)
    owner, private = None, False
    if doc_id:
        doc = get_document(user, doc_id)
        if doc.lab_id != lab_id:
            raise HubError("That document belongs to another lab.")
        owner, private = doc.owner, doc.private
        if owner and owner != user:
            raise Forbidden("Only the owner can add suggestions to a personal document.")
    area = (knowledge.private_dir(lab, user) if private else knowledge.personal_dir(lab, user) if owner
            else lab.dir / knowledge.INTERNAL_PART if lab.is_core else lab.dir)  # a core's are for its staff
    folder = area / "_suggestions"
    if not folder.exists():
        folder.mkdir(parents=True)
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
