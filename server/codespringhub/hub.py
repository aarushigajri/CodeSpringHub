"""The Hub's read operations, shared by the MCP server and the web portal.

Every function takes the acting `user` explicitly and does its own permission checks,
so the same code is safe whether it is called from stdio, the portal, or (later) a
multi-user HTTPS service.
"""

from . import config, inspect_data, knowledge, store
from .errors import Forbidden, HubError, NotFound
from .permissions import can_access_lab, can_edit_lab, require_lab, resolve_asset


def accessible_labs(user):
    return {lid: lab for lid, lab in knowledge.load_labs().items() if can_access_lab(user, lab)}


def list_labs(user) -> list[dict]:
    out = []
    for lab in accessible_labs(user).values():
        out.append({
            "lab_id": lab.id, "name": lab.name, "description": lab.description,
            "projects": [vars(p) for p in lab.projects],
            "datasets": len(knowledge.load_datasets(lab)),
            "documents": len(knowledge.load_documents(lab)),
            "can_edit": can_edit_lab(user, lab),
        })
    return out


def document_index(lab) -> list[dict]:
    return [
        {"id": d.id, "title": d.title, "category": d.category, "rel_path": d.rel_path}
        for d in knowledge.load_documents(lab).values()
    ]


def lab_context(user, lab_id) -> dict:
    lab = require_lab(user, knowledge.load_labs(), lab_id)
    ctx = lab.dir / "context.md"
    return {
        "context": ctx.read_text() if ctx.exists() else "",
        "documents": [d for d in document_index(lab) if d["category"] != "context"],
    }


def search(user, query, lab_id=None) -> list[dict]:
    labs = accessible_labs(user)
    if lab_id:
        labs = {lab_id: require_lab(user, labs, lab_id)}
    conn = store.connect()
    try:
        return store.search(conn, query, list(labs))
    finally:
        conn.close()


def get_document(user, doc_id) -> knowledge.Document:
    if not doc_id.startswith("doc:") or "/" not in doc_id:
        raise HubError("Document IDs look like 'doc:<lab>/<path>'.")
    lab = require_lab(user, knowledge.load_labs(), doc_id[4:].split("/", 1)[0])
    doc = knowledge.load_documents(lab).get(doc_id)
    if doc is None:
        raise NotFound(f"No document '{doc_id}'. Use get_lab_context or search_knowledge.")
    return doc


def public_dataset(ds: knowledge.Dataset) -> dict:
    """Dataset metadata safe to show the model (everything except the path)."""
    return {
        "id": ds.id, "title": ds.title, "project": ds.project, "assay": ds.assay,
        "description": ds.description, "sensitivity": ds.sensitivity,
        "registered_by": ds.registered_by, "registered_on": ds.registered_on,
    }


def get_dataset(user, dataset_id) -> knowledge.Dataset:
    if not dataset_id.startswith("ds:") or "/" not in dataset_id:
        raise HubError("Dataset IDs look like 'ds:<lab>/<name>'. Use list_datasets to find them.")
    lab = require_lab(user, knowledge.load_labs(), dataset_id[3:].split("/", 1)[0])
    ds = knowledge.load_datasets(lab).get(dataset_id)
    if ds is None:
        raise NotFound(f"No registered dataset '{dataset_id}'.")
    return ds


def list_datasets(user, lab_id, project=None) -> list[dict]:
    lab = require_lab(user, knowledge.load_labs(), lab_id)
    return [public_dataset(d) for d in knowledge.load_datasets(lab).values() if project in (None, d.project)]


def describe_dataset(user, dataset_id) -> dict:
    ds = get_dataset(user, dataset_id)
    assets = inspect_data.list_assets(resolve_asset(ds))
    return public_dataset(ds) | {"assets": assets, "assets_truncated": len(assets) >= config.MAX_LISTED_FILES}


def inspect_asset(user, dataset_id, asset) -> dict:
    ds = get_dataset(user, dataset_id)
    path = resolve_asset(ds, asset)
    if path.is_dir():
        raise HubError("That is a folder; pass a file name from describe_dataset.")
    return inspect_data.summarize(path, sensitive=ds.sensitivity == "sensitive")


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


def suggest_edit(user, lab_id, suggestion, doc_id=None) -> int:
    require_lab(user, knowledge.load_labs(), lab_id)
    conn = store.connect()
    try:
        return store.add_suggestion(conn, user, lab_id, doc_id, suggestion)
    finally:
        conn.close()


def request_review(user, lab_id, summary, dataset_ids=None) -> int:
    require_lab(user, knowledge.load_labs(), lab_id)
    for ds_id in dataset_ids or []:
        get_dataset(user, ds_id)
    conn = store.connect()
    try:
        return store.add_review(conn, user, lab_id, summary, dataset_ids or [])
    finally:
        conn.close()
