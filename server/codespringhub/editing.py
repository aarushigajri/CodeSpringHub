"""The Hub's write operations, used by the web portal (never exposed to Claude).

Every change:
  - checks the user may edit that lab (or is BSR, for global things),
  - validates input and explains problems in plain language,
  - writes the file atomically (a crash never leaves half a file),
  - records a local git commit with the user as author, so there is history and undo,
  - writes an audit row.
"""

import os
import re
import subprocess
import tempfile
import threading
from datetime import date
from pathlib import Path

import yaml

from . import config, knowledge, store
from .errors import Forbidden, HubError, NotFound
from .permissions import can_access_lab, display_name, is_bsr, require_lab, require_lab_editor

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
CATEGORIES = {
    "guidance": "Guidance: how BSR/the lab wants a kind of analysis done",
    "protocols": "Protocol / SOP: step-by-step lab or analysis procedure",
    "notes": "Note: background, decisions, things worth remembering",
    "grants": "Grant / project description: aims and scientific background",
}
ASSAYS = ["rna-seq", "scrna-seq", "atac-seq", "chip-seq", "wgs", "wes", "dna-seq", "methylation",
          "proteomics", "imaging", "other"]
YAML_HEADER = "# Managed by the CodeSpringHub portal. Hand edits are fine; keep the structure.\n"

_git_lock = threading.Lock()


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:63] or "untitled"


# ---- low-level file + history helpers -------------------------------------------

def _write_atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o660
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _make_private_dir(path: Path, like: Path):
    if not path.exists():
        path.mkdir()
        os.chmod(path, like.stat().st_mode & 0o7777)


def _git(*args, env=None, check=True):
    """Run git in the Hub repo as whichever BSR member is using the portal.

    safe.directory: git refuses repos owned by another user unless told otherwise.
    core.sharedRepository=group: new git objects stay writable for the whole group.
    """
    repo = str(config.HUB_HOME)
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-c", "core.sharedRepository=group", "-C", repo, *args],
        check=check, capture_output=True, text=True, env=env,
    )


def _commit(user: str, paths: list[Path], message: str):
    """Record a local git commit for just these paths. Never pushes. A git problem never
    blocks a save: the file is already written, so we log it and carry on."""
    repo = config.HUB_HOME
    if not (repo / ".git").exists():
        return
    rel = [str(p.relative_to(repo)) for p in paths]
    name, email = display_name(user), f"{user}@cshl.edu"
    # Set the committer too: a portal user may never have configured git.
    env = os.environ | {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    with _git_lock:
        try:
            _git("add", "-A", "--", *rel, env=env)
            if _git("diff", "--cached", "--quiet", "--", *rel, check=False).returncode == 0:
                return  # nothing changed
            _git("commit", "-q", "-m", f"portal: {message}", "--", *rel, env=env)
        except subprocess.CalledProcessError as e:
            conn = store.connect()
            try:
                store.log_call(conn, user, "portal:history_not_recorded", {"paths": rel}, False, 0,
                               (e.stderr or str(e)).strip()[:500])
            finally:
                conn.close()


def _audit(user, action, details):
    conn = store.connect()
    try:
        store.log_call(conn, user, f"portal:{action}", details, True, 0)
    finally:
        conn.close()


def history(user, lab_id, rel_path=None, limit=20) -> list[dict]:
    lab = require_lab(user, knowledge.load_labs(), lab_id)
    target = lab.dir / rel_path if rel_path else lab.dir
    if not (config.HUB_HOME / ".git").exists():
        return []
    out = _git("log", f"-{limit}", "--format=%h%x09%an%x09%ad%x09%s", "--date=format:%Y-%m-%d %H:%M",
               "--", str(target.relative_to(config.HUB_HOME)), check=False).stdout
    rows = [line.split("\t", 3) for line in out.splitlines() if line]
    return [{"commit": c, "author": a, "date": d, "message": m} for c, a, d, m in rows]


# ---- lab details ---------------------------------------------------------------

def update_lab(user, lab_id, name: str, description: str, projects: list[dict]):
    lab = require_lab_editor(user, knowledge.load_labs(), lab_id)
    if not name.strip():
        raise HubError("The lab needs a name.")
    seen = set()
    clean = []
    for p in projects:
        pid = p.get("id") or slugify(p.get("name", ""))
        if not SLUG.match(pid):
            raise HubError(f"Project ID '{pid}' should use lowercase letters, numbers and dashes.")
        if pid in seen:
            raise HubError(f"Two projects share the ID '{pid}'.")
        seen.add(pid)
        clean.append({"id": pid, "name": p.get("name", "").strip() or pid, "description": p.get("description", "").strip()})
    path = lab.dir / "lab.yaml"
    data = {"name": name.strip(), "unix_group": lab.unix_group, "description": description.strip(), "projects": clean}
    _write_atomic(path, YAML_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    _commit(user, [path], f"update {lab_id} lab details")
    _audit(user, "update_lab", {"lab_id": lab_id})


# ---- documents -----------------------------------------------------------------

def _doc_path(lab, rel_path: str) -> Path:
    parts = Path(rel_path).parts
    ok = (
        rel_path == "context.md"
        or (len(parts) == 2 and parts[0] in CATEGORIES and parts[1].endswith(".md")
            and SLUG.match(parts[1][:-3]))
    )
    if not ok:
        raise HubError("Documents live at context.md or <category>/<name>.md (lowercase, dashes).")
    return lab.dir / rel_path


def read_document_file(user, lab_id, rel_path) -> dict:
    lab = require_lab(user, knowledge.load_labs(), lab_id)
    path = _doc_path(lab, rel_path)
    if not path.exists():
        raise NotFound("That document doesn't exist (it may have been deleted).")
    return {"rel_path": rel_path, "text": path.read_text(), "can_edit": os.access(path, os.W_OK)}


def save_document(user, lab_id, rel_path, text: str):
    lab = require_lab_editor(user, knowledge.load_labs(), lab_id)
    path = _doc_path(lab, rel_path)
    if len(text) > 200_000:
        raise HubError("That document is very long (over 200,000 characters). Split it into several documents.")
    _make_private_dir(path.parent, lab.dir)
    _write_atomic(path, text if text.endswith("\n") else text + "\n")
    _commit(user, [path], f"edit {lab_id}/{rel_path}")
    _audit(user, "save_document", {"lab_id": lab_id, "rel_path": rel_path})


def create_document(user, lab_id, category: str, title: str) -> str:
    lab = require_lab_editor(user, knowledge.load_labs(), lab_id)
    if category not in CATEGORIES:
        raise HubError(f"Pick one of: {', '.join(CATEGORIES)}.")
    if not title.strip():
        raise HubError("Give the document a title.")
    rel_path = f"{category}/{slugify(title)}.md"
    if (lab.dir / rel_path).exists():
        raise HubError("A document with a very similar title already exists. Open that one, or choose another title.")
    template = (
        f"# {title.strip()}\n\n## Summary\nTODO: one or two sentences.\n\n"
        "## Details\nTODO\n\n## When to involve BSR\nTODO\n"
    )
    save_document(user, lab_id, rel_path, template)
    return rel_path


def delete_document(user, lab_id, rel_path):
    lab = require_lab_editor(user, knowledge.load_labs(), lab_id)
    if rel_path == "context.md":
        raise HubError("The lab context page can't be deleted; clear its text instead.")
    path = _doc_path(lab, rel_path)
    if not path.exists():
        raise NotFound("That document doesn't exist.")
    path.unlink()
    _commit(user, [path], f"delete {lab_id}/{rel_path}")
    _audit(user, "delete_document", {"lab_id": lab_id, "rel_path": rel_path})


# ---- datasets ------------------------------------------------------------------

def _load_dataset_entries(lab) -> list[dict]:
    path = lab.dir / "datasets.yaml"
    if not path.exists():
        return []
    with open(path) as fh:
        return (yaml.safe_load(fh) or {}).get("datasets", [])


def _save_dataset_entries(user, lab, entries, message):
    path = lab.dir / "datasets.yaml"
    _write_atomic(path, YAML_HEADER + yaml.safe_dump({"datasets": entries}, sort_keys=False, allow_unicode=True))
    _commit(user, [path], message)


def check_dataset_path(user, path_str: str) -> dict:
    """Plain-language checks on a folder/file before it is registered."""
    if not path_str or not os.path.isabs(path_str):
        raise HubError("Choose a folder from the browser (it should start with /grid/).")
    real = os.path.realpath(path_str)
    if not any(real == r or real.startswith(r + os.sep) for r in config.BROWSE_ROOTS):
        raise HubError(f"Only data under {', '.join(config.BROWSE_ROOTS)} can be registered.")
    hub_home = str(config.HUB_HOME)
    inside_hub = real == hub_home or real.startswith(hub_home + os.sep)
    if inside_hub and not real.startswith(str(config.HUB_HOME / "demo_data")):
        raise HubError("That folder is part of the Hub itself and can't be registered as data.")
    if not os.path.exists(real):
        raise HubError("That folder doesn't exist (it may have been moved or deleted).")
    if not os.access(real, os.R_OK):
        raise Forbidden("You don't have permission to read that folder on the cluster.")
    return {"path": real, "is_dir": os.path.isdir(real)}


def save_dataset(user, lab_id, entry: dict, original_id: str | None = None) -> str:
    lab = require_lab_editor(user, knowledge.load_labs(), lab_id)
    title = (entry.get("title") or "").strip()
    if not title:
        raise HubError("Give the dataset a short title, e.g. 'Mouse WGS, 12 samples'.")
    ds_id = (entry.get("id") or original_id or slugify(title)).strip()
    if not SLUG.match(ds_id):
        raise HubError("The short ID should use lowercase letters, numbers and dashes.")
    checked = check_dataset_path(user, entry.get("path", ""))
    project = entry.get("project") or None
    if project and project not in {p.id for p in lab.projects}:
        raise HubError(f"'{project}' isn't one of this lab's projects. Add it on the lab page first.")
    sensitivity = entry.get("sensitivity", "standard")
    if sensitivity not in ("standard", "sensitive"):
        raise HubError("Sensitivity must be 'standard' or 'sensitive'.")

    entries = _load_dataset_entries(lab)
    existing = next((e for e in entries if e["id"] == (original_id or ds_id)), None)
    if existing is None and any(e["id"] == ds_id for e in entries):
        raise HubError(f"A dataset with the ID '{ds_id}' already exists in this lab.")
    if existing and ds_id != original_id and any(e["id"] == ds_id for e in entries):
        raise HubError(f"A dataset with the ID '{ds_id}' already exists in this lab.")

    new = {
        "id": ds_id, "title": title, "project": project, "path": checked["path"],
        "assay": entry.get("assay", ""), "description": (entry.get("description") or "").strip(),
        "sensitivity": sensitivity,
        "registered_by": existing["registered_by"] if existing else user,
        "registered_on": existing["registered_on"] if existing else date.today().isoformat(),
    }
    if existing:
        entries[entries.index(existing)] = new
    else:
        entries.append(new)
    _save_dataset_entries(user, lab, entries, f"{'update' if existing else 'register'} dataset {lab_id}/{ds_id}")
    _audit(user, "save_dataset", {"lab_id": lab_id, "dataset": ds_id})
    return f"ds:{lab_id}/{ds_id}"


def delete_dataset(user, lab_id, ds_id):
    lab = require_lab_editor(user, knowledge.load_labs(), lab_id)
    entries = _load_dataset_entries(lab)
    remaining = [e for e in entries if e["id"] != ds_id]
    if len(remaining) == len(entries):
        raise NotFound("That dataset isn't registered.")
    _save_dataset_entries(user, lab, remaining, f"unregister dataset {lab_id}/{ds_id}")
    _audit(user, "delete_dataset", {"lab_id": lab_id, "dataset": ds_id})


def browse(user, path_str: str | None) -> dict:
    """List sub-folders and files the user can read, for the folder picker."""
    roots = config.BROWSE_ROOTS
    if not path_str:
        return {"path": None, "parent": None,
                "folders": [{"name": r, "path": r} for r in roots if os.access(r, os.R_OK | os.X_OK)], "files": []}
    real = os.path.realpath(path_str)
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise Forbidden(f"Browsing is limited to {', '.join(roots)}.")
    if not os.path.isdir(real) or not os.access(real, os.R_OK | os.X_OK):
        raise Forbidden("You don't have permission to open that folder.")
    folders, files = [], []
    try:
        with os.scandir(real) as it:
            for e in sorted(it, key=lambda e: e.name.lower()):
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        if os.access(e.path, os.R_OK | os.X_OK):
                            folders.append({"name": e.name, "path": e.path})
                    elif len(files) < 50:
                        files.append({"name": e.name, "size": e.stat().st_size})
                except OSError:
                    continue
                if len(folders) >= 500:
                    break
    except PermissionError:
        raise Forbidden("You don't have permission to open that folder.") from None
    parent = os.path.dirname(real) if real not in roots else None
    return {"path": real, "parent": parent, "folders": folders, "files": files}


# ---- workflows (BSR only) ------------------------------------------------------

def _require_bsr(user):
    if not is_bsr(user):
        raise Forbidden("Only BSR staff can change the workflow registry.")


def _workflows_path() -> Path:
    return config.KNOWLEDGE_DIR / "workflows.yaml"


def save_workflow(user, wf: dict, original_id: str | None = None) -> str:
    _require_bsr(user)
    name = (wf.get("name") or "").strip()
    if not name:
        raise HubError("Give the workflow a name, e.g. nf-core/rnaseq.")
    wf_id = (wf.get("id") or original_id or slugify(name)).strip()
    if not SLUG.match(wf_id):
        raise HubError("The workflow ID should use lowercase letters, numbers and dashes.")
    if wf.get("status") not in ("validated", "candidate"):
        raise HubError("Status must be 'validated' (BSR signed off) or 'candidate'.")
    assays = [a.strip().lower() for a in wf.get("assays", []) if a.strip()]
    if not assays:
        raise HubError("Pick at least one assay this workflow is for.")
    entries = knowledge.load_workflows()
    new = {"id": wf_id, "name": name, "assays": assays, "status": wf["status"],
           "version": (wf.get("version") or "TODO").strip(), "url": (wf.get("url") or "").strip(),
           "summary": (wf.get("summary") or "").strip()}
    idx = next((i for i, e in enumerate(entries) if e["id"] == (original_id or wf_id)), None)
    if idx is None and any(e["id"] == wf_id for e in entries):
        raise HubError(f"A workflow with the ID '{wf_id}' already exists.")
    if idx is None:
        entries.append(new)
    else:
        entries[idx] = new
    path = _workflows_path()
    _write_atomic(path, YAML_HEADER + yaml.safe_dump({"workflows": entries}, sort_keys=False, allow_unicode=True))
    _commit(user, [path], f"{'update' if idx is not None else 'add'} workflow {wf_id}")
    _audit(user, "save_workflow", {"workflow": wf_id})
    return wf_id


def delete_workflow(user, wf_id):
    _require_bsr(user)
    entries = knowledge.load_workflows()
    remaining = [e for e in entries if e["id"] != wf_id]
    if len(remaining) == len(entries):
        raise NotFound("That workflow isn't registered.")
    path = _workflows_path()
    _write_atomic(path, YAML_HEADER + yaml.safe_dump({"workflows": remaining}, sort_keys=False, allow_unicode=True))
    _commit(user, [path], f"remove workflow {wf_id}")
    _audit(user, "delete_workflow", {"workflow": wf_id})


# ---- inbox: Claude's suggestions and BSR review tickets -------------------------

def inbox(user) -> dict:
    labs = {lid for lid, lab in knowledge.load_labs().items() if can_access_lab(user, lab)}
    conn = store.connect()
    try:
        marks = ",".join("?" * len(labs)) or "''"
        sugg = conn.execute(f"SELECT * FROM suggestions WHERE lab_id IN ({marks}) ORDER BY id DESC", tuple(labs)).fetchall()
        # Review tickets: BSR sees all of them; others see the ones they raised.
        if is_bsr(user):
            rev = conn.execute("SELECT * FROM reviews ORDER BY id DESC").fetchall()
        else:
            rev = conn.execute("SELECT * FROM reviews WHERE user=? ORDER BY id DESC", (user,)).fetchall()
        return {"suggestions": [dict(r) for r in sugg], "reviews": [dict(r) for r in rev]}
    finally:
        conn.close()


def set_suggestion_status(user, sid: int, status: str):
    if status not in ("pending", "applied", "dismissed"):
        raise HubError("Unknown status.")
    conn = store.connect()
    try:
        row = conn.execute("SELECT lab_id FROM suggestions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise NotFound("No such suggestion.")
        require_lab_editor(user, knowledge.load_labs(), row["lab_id"])
        with conn:
            conn.execute("UPDATE suggestions SET status=? WHERE id=?", (status, sid))
    finally:
        conn.close()
    _audit(user, "suggestion_status", {"id": sid, "status": status})


def set_review_status(user, rid: int, status: str):
    if status not in ("open", "in progress", "closed"):
        raise HubError("Unknown status.")
    _require_bsr(user)
    conn = store.connect()
    try:
        with conn:
            if conn.execute("UPDATE reviews SET status=? WHERE id=?", (status, rid)).rowcount == 0:
                raise NotFound("No such review ticket.")
    finally:
        conn.close()
    _audit(user, "review_status", {"id": rid, "status": status})


def activity(user, limit=100) -> list[dict]:
    """Audit log: BSR sees everyone's activity, other users see their own."""
    conn = store.connect()
    try:
        if is_bsr(user):
            rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit WHERE user=? ORDER BY id DESC LIMIT ?", (user, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
